#!/usr/bin/env python3
"""
Haalt alle foto's op van Weibo-posts over een bepaald product.

Bladert terug door de feed, pakt elke post waarvan de tekst een van de
zoekwoorden bevat en downloadt alle foto's daarvan naar OUT_DIR. Gebruikt
dezelfde Weibo-functies als de bot, maar post niets naar Discord en past
state.json niet aan.

Instellingen via environment:
    WEIBO_UID   Weibo user-id
    KEYWORDS    zoekwoorden, gescheiden door komma's (bv. "老鹰,eagle")
    SINCE       hoe ver terug, als datum JJJJ-MM-DD
    OUT_DIR     map voor de foto's (standaard "photos")
"""

import os
import json
import time
from datetime import datetime

import requests

import main as bot

MAX_PAGES = 100


def created(mblog: dict) -> datetime:
    return datetime.strptime(
        mblog["created_at"], "%a %b %d %H:%M:%S %z %Y"
    ).replace(tzinfo=None)


def all_image_urls(mblog: dict):
    """De feed geeft max 9 foto's mee; bij meer halen we de detailpagina op."""
    pics = mblog.get("pics") or []
    if (mblog.get("pic_num") or 0) > len(pics):
        try:
            r = bot.session.get(
                f"https://m.weibo.cn/statuses/show?id={mblog['id']}", timeout=20
            )
            pics = r.json().get("data", {}).get("pics") or pics
        except (requests.RequestException, ValueError) as e:
            print(f"  detailpagina van {mblog['id']} mislukt ({e})")
    urls = []
    for pic in pics:
        url = (pic.get("large") or {}).get("url") or pic.get("url")
        if url:
            urls.append(url)
    return urls


def find_matching_posts(uid: str, keywords, since: datetime):
    containerid = bot.get_container_id(uid)
    print(f"Containerid: {containerid}")

    matches = []
    seen = set()
    since_id = None

    for page in range(1, MAX_PAGES + 1):
        params = {"type": "uid", "value": uid, "containerid": containerid}
        if since_id:
            params["since_id"] = since_id
        else:
            params["page"] = page
        payload = bot.weibo_api(params).get("data") or {}
        cards = payload.get("cards") or []
        posts = [c["mblog"] for c in cards if c.get("card_type") == 9 and "mblog" in c]
        if not posts:
            print(f"Pagina {page}: geen posts meer.")
            break

        new = [p for p in posts if p["id"] not in seen]
        for post in new:
            seen.add(post["id"])
            text = bot.clean_text(bot.get_full_text(post))
            if any(k.lower() in text.lower() for k in keywords):
                urls = all_image_urls(post)
                matches.append({
                    "id": post["id"],
                    "date": created(post).strftime("%Y-%m-%d"),
                    "link": f"https://m.weibo.cn/status/{post['id']}",
                    "text": text,
                    "images": urls,
                })
                print(f"  match {matches[-1]['date']} {post['id']}: "
                      f"{len(urls)} foto's")

        # Vastgepinde posts staan bovenaan ongeacht hun datum; die tellen
        # niet mee voor de vraag of we ver genoeg terug zijn.
        dated = [created(p) for p in new if not p.get("isTop")]
        oldest = min(dated) if dated else None
        print(f"Pagina {page}: {len(posts)} posts"
              + (f", oudste {oldest:%Y-%m-%d}" if oldest else ""))
        if not new or (oldest and oldest < since):
            break

        since_id = (payload.get("cardlistInfo") or {}).get("since_id")
        time.sleep(1.5)

    return [m for m in matches if m["date"] >= since.strftime("%Y-%m-%d")]


def main():
    uid = os.environ["WEIBO_UID"]
    keywords = [k.strip() for k in os.environ["KEYWORDS"].split(",") if k.strip()]
    since = datetime.strptime(os.environ["SINCE"], "%Y-%m-%d")
    out_dir = os.environ.get("OUT_DIR", "photos")
    os.makedirs(out_dir, exist_ok=True)

    print(f"Zoeken naar {keywords} vanaf {since:%Y-%m-%d}")
    bot.warm_up(uid)
    matches = sorted(find_matching_posts(uid, keywords, since), key=lambda m: m["id"])

    saved = 0
    for m in matches:
        for i, url in enumerate(m["images"], 1):
            ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
            blob = bot.download(url)
            if not blob:
                print(f"  download mislukt: {url}")
                continue
            path = os.path.join(out_dir, f"{m['date']}_{m['id']}_{i:02d}{ext}")
            with open(path, "wb") as f:
                f.write(blob)
            saved += 1
            time.sleep(0.3)

    with open(os.path.join(out_dir, "posts.json"), "w", encoding="utf-8") as f:
        json.dump(matches, f, indent=2, ensure_ascii=False)

    print(f"Klaar. {len(matches)} post(s), {saved} foto('s) opgeslagen in {out_dir}/")


if __name__ == "__main__":
    main()
