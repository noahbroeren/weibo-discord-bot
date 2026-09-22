#!/usr/bin/env python3
"""
718 Made — Weibo naar Discord bot.

Haalt nieuwe Weibo-posts op van een account via de (ongedocumenteerde)
m.weibo.cn JSON-API, laat Claude de tekst vertalen/verbeteren EN het
product bepalen op basis van tekst + foto's, en post het geheel naar
Discord in het vaste formaat:

    **Progress Update | PRODUCTNAAM IN CAPS**

    Vertaalde tekst.

State (laatst verwerkte post-id) staat in state.json; bekende
producttitels in products.json, zodat dezelfde producten steeds
dezelfde naam krijgen.
"""

import os
import re
import io
import json
import time
import html
import base64
import requests
from bs4 import BeautifulSoup
from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
PRODUCTS_FILE = os.path.join(BASE_DIR, "products.json")

# Discord webhook-limiet. Bij Server Boost level 2 mag een upload tot 50MB.
# Kleine marge voor de payload_json overhead.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024 - 200_000

# Hoeveel foto's we meesturen naar Claude om het product te bepalen.
# Meer foto's = iets betere herkenning, maar ook meer kosten.
MAX_IMAGES_FOR_ANALYSIS = 4

# Foto's worden verkleind voordat ze naar de API gaan (scheelt kosten en
# blijft ruim onder de API-limiet per afbeelding).
ANALYSIS_IMAGE_MAX_DIM = 1024

FALLBACK_TITLE = "718 MADE"

WEIBO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 "
        "Safari/604.1"
    ),
    "Referer": "https://m.weibo.cn/",
}

session = requests.Session()
session.headers.update(WEIBO_HEADERS)


# ---------------------------------------------------------------------------
# State & bekende producten
# ---------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Weibo ophalen
# ---------------------------------------------------------------------------

def get_container_id(uid: str) -> str:
    """Vind de containerid van de 'Weibo' tab (de eigenlijke post-feed)."""
    r = session.get(
        "https://m.weibo.cn/api/container/getIndex",
        params={"type": "uid", "value": uid},
        timeout=15,
    )
    r.raise_for_status()
    tabs = r.json().get("data", {}).get("tabsInfo", {}).get("tabs", [])
    for tab in tabs:
        if tab.get("tab_type") == "weibo":
            return tab["containerid"]
    if tabs:
        return tabs[0]["containerid"]
    raise RuntimeError("Kon geen containerid vinden voor dit account")


def get_posts(uid: str, containerid: str, page: int = 1):
    r = session.get(
        "https://m.weibo.cn/api/container/getIndex",
        params={"type": "uid", "value": uid, "containerid": containerid, "page": page},
        timeout=15,
    )
    r.raise_for_status()
    cards = r.json().get("data", {}).get("cards", [])
    return [c["mblog"] for c in cards if c.get("card_type") == 9 and "mblog" in c]


def get_full_text(mblog: dict) -> str:
    """
    Weibo kort lange posts af met een 'volledige tekst' link. In dat geval
    halen we de volledige versie op via de detail-endpoint.
    """
    raw = mblog.get("text", "")
    if mblog.get("isLongText") or "全文" in raw:
        try:
            r = session.get(
                f"https://m.weibo.cn/statuses/extend?id={mblog['id']}", timeout=15
            )
            if r.status_code == 200:
                longtext = r.json().get("data", {}).get("longTextContent")
                if longtext:
                    return longtext
        except (requests.RequestException, ValueError):
            pass
    return raw


def clean_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = BeautifulSoup(raw_html, "html.parser").get_text()
    text = html.unescape(text)
    text = re.sub(r"(全文|查看更多)\s*$", "", text).strip()
    return text


def extract_media(mblog: dict):
    """Geeft (lijst met afbeeldings-urls, video-url of None) terug."""
    images = []
    for pic in mblog.get("pics", []) or []:
        url = pic.get("large", {}).get("url") or pic.get("url")
        if url:
            images.append(url)

    video_url = None
    page_info = mblog.get("page_info")
    if page_info and page_info.get("type") == "video":
        media_info = page_info.get("media_info", {}) or {}
        for key in ("stream_url_hd", "mp4_hd_url", "stream_url", "mp4_sd_url"):
            if media_info.get(key):
                video_url = media_info[key]
                break

    return images, video_url


def download(url: str):
    try:
        r = session.get(url, timeout=60)
        if r.status_code == 200:
            return r.content
    except requests.RequestException:
        pass
    return None


# ---------------------------------------------------------------------------
# Claude: vertalen + product bepalen in één call
# ---------------------------------------------------------------------------

def prepare_image_for_api(image_bytes: bytes):
    """Verklein naar JPEG zodat de API-call klein en goedkoop blijft."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
        img.thumbnail((ANALYSIS_IMAGE_MAX_DIM, ANALYSIS_IMAGE_MAX_DIM))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def build_prompt(chinese_text: str, known_products) -> str:
    known_list = "\n".join(f"- {p}" for p in known_products) if known_products else "(none yet)"
    return f"""You are helping run the Discord announcements channel for a brand called 718 Made, which posts manufacturing progress updates about the garments and footwear it produces.

You will be given the original Chinese text of a Weibo post, and usually some photos from that post.

Do two things:

1. TRANSLATE the Chinese text into natural, polished English. Fix awkward grammar and phrasing so it reads fluently as a brand social media post. Keep the meaning faithful — do not invent details that are not in the original. Keep relevant emoji. Remove any "read more" artifacts, @mentions of other Weibo accounts, and promotional hashtag spam.

2. IDENTIFY the product this post is about, and give it a short title in ALL CAPS. Use the text and the photos together. The title should be the brand plus the product type, for example "CANADA GOOSE JACKETS" or "MAISON MARGIELA LOVED TO DEATH SHOES".

Here are titles already used for previous posts. If this post is about one of these same products, reuse the EXACT same title so the channel stays consistent:
{known_list}

If the post is about a genuinely new product, create a new title in the same style.
If you truly cannot tell what product it is (for example a general announcement, holiday notice, or restock message with no specific product), use the title "{FALLBACK_TITLE}".

Respond with ONLY a JSON object, no markdown fences, no preamble:
{{"title": "PRODUCT TITLE IN CAPS", "translation": "the translated English text"}}

Original Chinese post text:
---
{chinese_text if chinese_text.strip() else "(no text, images only)"}
---"""


def analyse_post(chinese_text: str, image_blobs, known_products):
    """
    Geeft (title, translation) terug. Valt bij problemen terug op de ruwe
    tekst en een generieke titel, zodat de bot nooit een post overslaat.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return FALLBACK_TITLE, chinese_text

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

    content = []
    for blob in image_blobs[:MAX_IMAGES_FOR_ANALYSIS]:
        b64 = prepare_image_for_api(blob)
        if b64:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64,
                },
            })
    content.append({"type": "text", "text": build_prompt(chinese_text, known_products)})

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        blocks = resp.json().get("content", [])
        raw = "".join(b["text"] for b in blocks if b.get("type") == "text").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        title = (parsed.get("title") or FALLBACK_TITLE).strip().upper()
        translation = (parsed.get("translation") or "").strip() or chinese_text
        return title, translation
    except Exception as e:
        print(f"  ! Claude-call mislukt ({e}); val terug op ruwe tekst")
        return FALLBACK_TITLE, chinese_text


# ---------------------------------------------------------------------------
# Naar Discord posten
# ---------------------------------------------------------------------------

def post_to_discord(webhook_url: str, title: str, body: str, image_blobs,
                    video_url, post_link: str):
    files_to_send = []
    total_size = 0

    for i, blob in enumerate(image_blobs):
        if total_size + len(blob) > MAX_UPLOAD_BYTES:
            break
        files_to_send.append((f"image_{i + 1}.jpg", blob))
        total_size += len(blob)

    note = ""
    if video_url:
        video_data = download(video_url)
        if video_data and total_size + len(video_data) <= MAX_UPLOAD_BYTES:
            files_to_send.append(("video.mp4", video_data))
            total_size += len(video_data)
        else:
            note = f"\n\n🎥 Video: {post_link}"

    content = f"**Progress Update | {title}**\n\n{body}{note}"
    # Discord's tekstlimiet is 2000 tekens.
    if len(content) > 2000:
        content = content[:1990].rstrip() + " […]"

    if files_to_send:
        multipart = {
            f"files[{idx}]": (name, data)
            for idx, (name, data) in enumerate(files_to_send)
        }
        resp = requests.post(
            webhook_url,
            data={"payload_json": json.dumps({"content": content})},
            files=multipart,
            timeout=180,
        )
    else:
        resp = requests.post(webhook_url, json={"content": content}, timeout=30)

    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    uid = os.environ["WEIBO_UID"]
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]

    state = load_json(STATE_FILE, {"last_id": None})
    known_products = load_json(PRODUCTS_FILE, [])
    last_id = state.get("last_id")

    containerid = get_container_id(uid)
    posts = get_posts(uid, containerid)
    posts.sort(key=lambda p: int(p["id"]))  # oudste eerst

    # Eerste run: niks terugposten, alleen het startpunt vastleggen, anders
    # dumpt de bot in één keer de hele recente historie in het kanaal.
    if last_id is None:
        if posts:
            state["last_id"] = posts[-1]["id"]
            save_json(STATE_FILE, state)
            print(f"Eerste run: startpunt gezet op post {posts[-1]['id']}. "
                  "Vanaf nu worden alleen nieuwe posts geplaatst.")
        else:
            print("Eerste run: geen posts gevonden.")
        return

    new_last_id = last_id
    posted = 0

    for post in posts:
        pid = post["id"]
        if int(pid) <= int(last_id):
            continue

        print(f"Nieuwe post gevonden: {pid}")
        text = clean_text(get_full_text(post))
        image_urls, video_url = extract_media(post)

        image_blobs = [b for b in (download(u) for u in image_urls) if b]
        title, translation = analyse_post(text, image_blobs, known_products)
        print(f"  -> titel: {title}")

        link = f"https://m.weibo.cn/status/{pid}"
        post_to_discord(webhook_url, title, translation, image_blobs, video_url, link)

        if title != FALLBACK_TITLE and title not in known_products:
            known_products.append(title)
            save_json(PRODUCTS_FILE, known_products)

        new_last_id = pid
        posted += 1
        time.sleep(2)

    if new_last_id != last_id:
        state["last_id"] = new_last_id
        save_json(STATE_FILE, state)

    print(f"Klaar. {posted} nieuwe post(s) geplaatst.")


if __name__ == "__main__":
    main()
