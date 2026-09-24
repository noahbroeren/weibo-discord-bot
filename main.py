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

FALLBACK_TITLE = os.environ.get("FALLBACK_TITLE", "718 MADE")

# Kop boven elk Discord-bericht. {title} wordt vervangen door de producttitel.
# Instelbaar via de workflow, zodat meerdere servers dezelfde code kunnen delen.
HEADER_TEMPLATE = os.environ.get(
    "HEADER_TEMPLATE", "**Progress Update | {title}**"
)

WEIBO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 "
        "Safari/604.1"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "MWeibo-Pwa": "1",
    "Referer": "https://m.weibo.cn/",
}

session = requests.Session()
session.headers.update(WEIBO_HEADERS)

# Optioneel: als Weibo vanaf GitHub's IP's een ingelogde sessie eist, kun je
# de cookie-string uit je eigen browser in de WEIBO_COOKIE secret zetten.
_cookie = os.environ.get("WEIBO_COOKIE", "").strip()
if _cookie:
    session.headers["Cookie"] = _cookie


def warm_up(uid: str):
    """
    Weibo geeft de API-endpoints pas bruikbare JSON als de sessie cookies
    heeft. Eerst de gewone profielpagina ophalen levert die cookies op.
    """
    try:
        session.get(
            f"https://m.weibo.cn/u/{uid}",
            headers={"Accept": "text/html,application/xhtml+xml", "Referer": "https://m.weibo.cn/"},
            timeout=20,
        )
        session.headers["Referer"] = f"https://m.weibo.cn/u/{uid}"
        names = ", ".join(session.cookies.keys()) or "(geen)"
        print(f"Warm-up klaar. Cookies ontvangen: {names}")
    except requests.RequestException as e:
        print(f"Warm-up mislukt ({e}); we proberen het alsnog.")


def weibo_api(params: dict, attempts: int = 3):
    """
    Roept de Weibo container-API aan en geeft de JSON terug. Bij een
    niet-JSON antwoord (blokkade, loginpagina, rate limit) print het script
    wat er precies terugkwam, zodat het probleem zichtbaar is in het log.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(
                "https://m.weibo.cn/api/container/getIndex",
                params=params,
                timeout=25,
            )
        except requests.RequestException as e:
            last_error = f"netwerkfout: {e}"
            print(f"  poging {attempt}/{attempts} mislukt ({last_error})")
            time.sleep(attempt * 3)
            continue

        ctype = r.headers.get("Content-Type", "")
        try:
            data = r.json()
        except ValueError:
            snippet = r.text[:300].replace("\n", " ")
            last_error = (
                f"HTTP {r.status_code}, content-type '{ctype}'. "
                f"Antwoord begon met: {snippet!r}"
            )
            print(f"  poging {attempt}/{attempts}: geen JSON terug. {last_error}")
            time.sleep(attempt * 3)
            continue

        # Weibo geeft soms wel JSON, maar met een foutcode erin.
        if data.get("ok") == 0:
            last_error = f"Weibo gaf ok=0 terug: {data.get('msg')!r}"
            print(f"  poging {attempt}/{attempts}: {last_error}")
            time.sleep(attempt * 3)
            continue

        return data

    raise RuntimeError(
        "Kon geen geldige JSON van Weibo krijgen na meerdere pogingen.\n"
        f"Laatste probleem: {last_error}\n"
        "Meest waarschijnlijke oorzaak: Weibo blokkeert het IP van de "
        "GitHub Actions runner, of eist een ingelogde sessie. Zie de README "
        "onder 'Als Weibo blokkeert' voor de cookie-oplossing."
    )


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


def load_products():
    """
    Leest products.json. Ondersteunt twee vormen:
      - oude vorm: ["TITEL A", "TITEL B"]
      - nieuwe vorm: {"products": [{"title": "...", "hints": "..."}]}
    Geeft altijd een lijst met dicts terug.
    """
    raw = load_json(PRODUCTS_FILE, None)
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("products", [])
    entries = []
    for item in raw:
        if isinstance(item, str):
            entries.append({"title": item, "hints": ""})
        elif isinstance(item, dict) and item.get("title"):
            entries.append({
                "title": item["title"],
                "hints": item.get("hints", "") or "",
            })
    return entries


def save_products(entries):
    save_json(PRODUCTS_FILE, {"products": entries})


# ---------------------------------------------------------------------------
# Weibo ophalen
# ---------------------------------------------------------------------------

def get_container_id(uid: str) -> str:
    """Vind de containerid van de 'Weibo' tab (de eigenlijke post-feed)."""
    data = weibo_api({"type": "uid", "value": uid})
    tabs = data.get("data", {}).get("tabsInfo", {}).get("tabs", [])
    for tab in tabs:
        if tab.get("tab_type") == "weibo":
            return tab["containerid"]
    if tabs:
        return tabs[0]["containerid"]
    # Fallback: de containerid voor de post-feed is normaal gesproken
    # voorspelbaar op te bouwen uit het uid.
    print("Geen tabsInfo gevonden; val terug op de standaard containerid-opbouw.")
    return f"107603{uid}"


def get_posts(uid: str, containerid: str, page: int = 1):
    data = weibo_api(
        {"type": "uid", "value": uid, "containerid": containerid, "page": page}
    )
    payload = data.get("data", {}) or {}
    cards = payload.get("cards", [])
    posts = [c["mblog"] for c in cards if c.get("card_type") == 9 and "mblog" in c]

    if not posts:
        # Laat zien wat Weibo wél terugstuurde, zodat duidelijk is of het om
        # een login-muur gaat of om iets anders.
        print("  Diagnose: geen posts in het antwoord.")
        print(f"  - sleutels in data: {sorted(payload.keys())}")
        print(f"  - aantal cards: {len(cards)}")
        if cards:
            types = sorted({c.get("card_type") for c in cards})
            print(f"  - card_types gevonden: {types}")
        info = payload.get("cardlistInfo") or {}
        if info:
            print(f"  - cardlistInfo total: {info.get('total')}")
        if not payload.get("userInfo") and not payload.get("tabsInfo"):
            print("  - Geen userInfo/tabsInfo: Weibo behandelt ons als "
                  "uitgelogde bezoeker. Zet de WEIBO_COOKIE secret (README).")
        else:
            print("  - Profielgegevens zijn er wel; het account heeft mogelijk "
                  "geen publieke posts op deze pagina.")

    return posts


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



EMOJI_PATTERN = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # pictogrammen, emoticons, symbolen
    "\U00002600-\U000027BF"  # diverse symbolen en dingbats
    "\U00002190-\U000021FF"  # pijlen
    "\U00002B00-\U00002BFF"  # extra symbolen
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0001F1E6-\U0001F1FF"  # vlaggen
    "\U00002B50\U00002705\U0000274C\U0000203C\U00002049"
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    """Vangnet: haalt emoji weg, ook als het model er toch een laat staan."""
    cleaned = EMOJI_PATTERN.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +\n", "\n", cleaned)
    return cleaned.strip()


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
    if known_products:
        lines = []
        for p in known_products:
            if p.get("hints"):
                lines.append(f"- {p['title']}  |  recognise by: {p['hints']}")
            else:
                lines.append(f"- {p['title']}")
        catalogue = "\n".join(lines)
    else:
        catalogue = "(none yet)"

    workshop = os.environ.get("WORKSHOP_NAME", "718 Made")
    return f"""You are helping run the Discord announcements channel for {workshop}, a workshop that reproduces garments and footwear from well-known fashion brands. Their Weibo posts are manufacturing progress updates.

You will be given the original Chinese text of a Weibo post, and usually some photos from that post.

Do two things:

1. TRANSLATE the Chinese text into natural, polished English. Fix awkward grammar and phrasing so it reads fluently as a brand social media post. Keep the meaning faithful - do not invent details that are not in the original. Remove ALL emoji - the final text must contain no emoji at all, even if the original post is full of them. Also remove "read more" artifacts, @mentions of other Weibo accounts, and hashtag spam.

2. IDENTIFY the product and give it a short ALL CAPS title, normally "BRAND + PRODUCT TYPE", for example "CANADA GOOSE JACKETS" or "MAISON MARGIELA LOVED TO DEATH SHOES".

How to work out the brand:
- The post often does NOT name the brand. Infer it from distinctive product details in the text (weights, colour names, model names, construction details, fabric specs) and from the photos (silhouette, logos, labels, hardware, stitching, packaging).
- Use the product catalogue below. These are products this workshop has posted about before, with details that identify them. If this post matches one of them, reuse that EXACT title.
- {workshop} is the workshop, NOT the brand. NEVER use the workshop name as the brand name in a product title.
- If you can identify the product type but genuinely cannot determine the brand, use the product type alone, for example "HEAVYWEIGHT SWEATSHIRT". Do not guess a brand you have no evidence for, and do not fall back to the workshop name.
- Only if the post is not about a specific product at all (a general announcement, holiday notice, giveaway with no product) use the title "{FALLBACK_TITLE}".

Product catalogue (previously used titles, with recognition details):
{catalogue}

If the post is about a genuinely new product, create a new title in the same style.

Respond in EXACTLY this format, with no preamble and no markdown fences:

TITLE: PRODUCT TITLE IN CAPS
---TRANSLATION---
the translated English text, which may run over several paragraphs

Original Chinese post text:
---
{chinese_text if chinese_text.strip() else "(no text, images only)"}
---"""



def parse_response(raw: str):
    """
    Haalt titel en vertaling uit het antwoord. Werkt met het
    scheidingsteken-formaat, en valt terug op JSON voor het geval het model
    dat toch teruggeeft. Geeft (titel, vertaling) terug; een lege vertaling
    betekent dat het parsen mislukt is.
    """
    # Voorkeursformaat: TITLE: ... ---TRANSLATION--- ...
    match = re.search(
        r"TITLE:\s*(.+?)\s*\n\s*-{2,}\s*TRANSLATION\s*-{2,}\s*\n(.+)",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if match:
        return match.group(1).strip().upper(), match.group(2).strip()

    # Terugval 1: het model gaf toch JSON
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("translation"):
            title = (parsed.get("title") or FALLBACK_TITLE).strip().upper()
            return title, parsed["translation"].strip()
    except (ValueError, AttributeError):
        pass

    # Terugval 2: alleen een TITLE-regel, rest is de vertaling
    match = re.search(r"TITLE:\s*(.+)", raw, flags=re.IGNORECASE)
    if match:
        title = match.group(1).strip().upper()
        rest = raw[match.end():].strip()
        rest = re.sub(r"^-{2,}\s*TRANSLATION\s*-{2,}\s*", "", rest,
                      flags=re.IGNORECASE).strip()
        if rest:
            return title, rest

    return FALLBACK_TITLE, ""


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
                "max_tokens": 8000,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("stop_reason") == "max_tokens":
            print("  ! Let op: antwoord liep tegen de tokenlimiet aan; "
                  "de vertaling kan onvolledig zijn.")

        blocks = payload.get("content", [])
        raw = "".join(b["text"] for b in blocks if b.get("type") == "text").strip()
        raw = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", raw).strip()

        title, translation = parse_response(raw)
        if not translation:
            print("  ! Kon de vertaling niet uit het antwoord halen. "
                  f"Antwoord begon met: {raw[:200]!r}")
            return FALLBACK_TITLE, chinese_text

        return strip_emoji(title), strip_emoji(translation)
    except Exception as e:
        print(f"  ! Claude-call mislukt ({type(e).__name__}: {e}); "
              "val terug op ruwe tekst")
        return FALLBACK_TITLE, chinese_text



DISCORD_LIMIT = 2000


def split_for_discord(text: str, limit: int = DISCORD_LIMIT):
    """
    Knipt tekst op in stukken van maximaal `limit` tekens. Breekt bij
    voorkeur op een lege regel, anders op een regeleinde, anders op een
    spatie - zodat er nooit midden in een woord wordt afgebroken.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text.strip()

    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


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
            note = f"\n\nVideo: {post_link}"

    content = f"{HEADER_TEMPLATE.format(title=title)}\n\n{body}{note}"

    # Discord staat maximaal 2000 tekens per bericht toe. Lange posts worden
    # over meerdere berichten verdeeld in plaats van afgekapt; de bijlagen
    # gaan mee met het laatste bericht zodat ze onder de tekst verschijnen.
    chunks = split_for_discord(content)

    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        if is_last and files_to_send:
            multipart = {
                f"files[{i}]": (name, data)
                for i, (name, data) in enumerate(files_to_send)
            }
            resp = requests.post(
                webhook_url,
                data={"payload_json": json.dumps({"content": chunk})},
                files=multipart,
                timeout=180,
            )
        else:
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
        resp.raise_for_status()
        if not is_last:
            time.sleep(1)  # webhook rate limit ontzien

    if len(chunks) > 1:
        print(f"  (tekst verdeeld over {len(chunks)} berichten)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    uid = os.environ["WEIBO_UID"]
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]

    state = load_json(STATE_FILE, {"last_id": None})
    known_products = load_products()
    last_id = state.get("last_id")

    # FORCE_LATEST=n plaatst de n nieuwste posts ongeacht de state.
    # Handig om te testen zonder handmatig state.json aan te passen.
    try:
        force_latest = int(os.environ.get("FORCE_LATEST", "0") or "0")
    except ValueError:
        force_latest = 0

    warm_up(uid)
    containerid = get_container_id(uid)
    print(f"Containerid: {containerid}")
    posts = get_posts(uid, containerid)
    print(f"{len(posts)} post(s) opgehaald.")

    if not posts:
        print(
            "Geen posts gevonden. Weibo gaf wel antwoord, maar zonder bruikbare "
            "posts. Mogelijk klopt het uid of de containerid niet, of levert "
            "Weibo pas posts na inloggen (zie README: 'Als Weibo blokkeert')."
        )
        return

    posts.sort(key=lambda p: int(p["id"]))  # oudste eerst

    if force_latest > 0:
        selection = posts[-force_latest:]
        print(f"FORCE_LATEST={force_latest}: {len(selection)} post(s) worden "
              "geplaatst ongeacht de opgeslagen state.")
    elif last_id is None:
        # Eerste run: niks terugposten, alleen het startpunt vastleggen, anders
        # dumpt de bot in één keer de hele recente historie in het kanaal.
        state["last_id"] = posts[-1]["id"]
        save_json(STATE_FILE, state)
        print(f"Eerste run: startpunt gezet op post {posts[-1]['id']}. "
              "Vanaf nu worden alleen nieuwe posts geplaatst.")
        return
    else:
        selection = [p for p in posts if int(p["id"]) > int(last_id)]
        if not selection:
            print("Geen nieuwe posts sinds de vorige run.")

    new_last_id = last_id
    posted = 0

    for post in selection:
        pid = post["id"]

        print(f"Nieuwe post gevonden: {pid}")
        text = clean_text(get_full_text(post))
        image_urls, video_url = extract_media(post)

        image_blobs = [b for b in (download(u) for u in image_urls) if b]
        title, translation = analyse_post(text, image_blobs, known_products)
        print(f"  -> titel: {title}")

        link = f"https://m.weibo.cn/status/{pid}"
        post_to_discord(webhook_url, title, translation, image_blobs, video_url, link)

        if title != FALLBACK_TITLE and title not in {p["title"] for p in known_products}:
            known_products.append({"title": title, "hints": ""})
            save_products(known_products)
            print(f"  -> nieuwe titel toegevoegd aan products.json")

        new_last_id = pid
        posted += 1
        time.sleep(2)

    # In force-modus altijd doorschuiven naar de nieuwste post, zodat de
    # volgende gewone run niet alles nogmaals plaatst.
    if force_latest > 0:
        new_last_id = posts[-1]["id"]

    if new_last_id != last_id:
        state["last_id"] = new_last_id
        save_json(STATE_FILE, state)
        print(f"State bijgewerkt naar post {new_last_id}.")

    print(f"Klaar. {posted} nieuwe post(s) geplaatst.")


if __name__ == "__main__":
    main()
