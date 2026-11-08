#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сбор контента «СМТ Эко»: VK + сайт техникума -> content/*.json.

Запуск:
  VK_TOKEN=... VK_GROUP_ID=163602619 \
  YC_KEY_ID=... YC_SECRET=... YC_BUCKET=smt-schedule-data \
  python content_parser.py

Без VK_TOKEN собираются только новости/сессия/практика с сайта.
Без YC_* файлы только пишутся в content/, без выгрузки в S3.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import socket

# На GitHub Actions (US) у сайта может выбираться IPv6-маршрут,
# которого нет у runner'а: Errno 101. Принудительно идём по IPv4.
_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _ORIGINAL_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _getaddrinfo_ipv4
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SITE = "https://www.satehm.ru"
NEWS_URL = f"{SITE}/news/2026/"
OCHNOE_URL = f"{SITE}/studentam/ochnoe-otdelenie"
DEMO_URL = f"{SITE}/studentam/demonstratsionnyy-ekzamen/"
SCHEDULE_BELL_URL = f"{SITE}/studentam/raspisanie-zvonkov/"
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "163602619")
VK_TOKEN = os.environ.get("VK_TOKEN", "")
TZ = timezone(timedelta(hours=7))  # Asia/Krasnoyarsk
UA = "SMT-Eco-Content/1.0 (+https://smt-eco.ru)"
OUT = Path("content")
ARCHIVE_DAYS = 365

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
}


def log(message: str) -> None:
    print(f"[content] {message}", flush=True)


def fetch_text(url: str, retries: int = 2, timeout: int = 25) -> str:
    last = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as error:  # noqa: BLE001 - логируем и пробуем ещё
            last = error
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def strip_html(value: str) -> str:
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def date_near(html_text: str, position: int) -> str | None:
    window = html_text[max(0, position - 700):position + 700]
    match = re.search(
        r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|"
        r"сентября|октября|ноября|декабря)(?:\s+(\d{4}))?",
        window,
        flags=re.I,
    )
    if not match:
        return None
    day = int(match.group(1))
    month = MONTHS[match.group(2).lower()]
    year = int(match.group(3) or datetime.now(TZ).year)
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def classify(text: str) -> str:
    value = (text or "").lower()
    if re.search(r"сесси|экзамен|зачёт|зачет|консультац|аттестац|демоэкзамен|гиа", value):
        return "session"
    if re.search(r"практик|производственн|демонстрационн|профессионалитет", value):
        return "practice"
    if re.search(r"афиша|мероприят|концерт|встреч|конкурс|фестиваль|форум|олимпиад|акци|спектакл|выставк", value):
        return "events"
    return "news"


def item(
    item_id: str,
    source: str,
    category: str,
    title: str,
    text: str = "",
    date: str | None = None,
    url: str | None = None,
    image_url: str | None = None,
    images: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "source": source,
        "category": category,
        "title": re.sub(r"\s+", " ", title or "").strip()[:240] or "Без заголовка",
        "text": re.sub(r"\s+", " ", text or "").strip()[:4000],
        "date": date,
        "url": url,
        "imageUrl": image_url,
        "images": images or [],
        "archived": False,
    }


def image_near(html_text: str, position: int) -> str | None:
    window_start = max(0, position - 1800)
    window = html_text[window_start:position + 1800]
    best: tuple[int, str] | None = None
    for match in re.finditer(r'<img[^>]+src="([^"]+)"', window, flags=re.I):
        source = urllib.parse.urljoin(SITE, html.unescape(match.group(1)))
        if "/userfiles/news/" not in source and "/userfiles/" not in source:
            continue
        distance = abs((window_start + match.start()) - position)
        if best is None or distance < best[0]:
            best = (distance, source)
    return best[1] if best else None


def parse_site_news() -> list[dict[str, Any]]:
    html_text = fetch_text(NEWS_URL)
    pattern = re.compile(
        r'<a[^>]+href="(https://www\.satehm\.ru/news/2026/[^"#?]+)"[^>]*>(.*?)</a>',
        re.I | re.S,
    )
    result = []
    seen = set()
    for match in pattern.finditer(html_text):
        url = match.group(1)
        raw_title = strip_html(match.group(2))
        if not raw_title or len(raw_title) < 8 or url in seen:
            continue
        seen.add(url)
        title = raw_title
        date = date_near(html_text, match.start())
        date_match = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(.*)$", title)
        if date_match:
            try:
                date = datetime(
                    int(date_match.group(3)),
                    int(date_match.group(2)),
                    int(date_match.group(1)),
                ).date().isoformat()
                title = date_match.group(4).strip()
            except ValueError:
                pass
        category = classify(title)
        result.append(item(
            f"site:{url}",
            "site",
            category,
            title,
            text="Новость сайта техникума.",
            date=date,
            url=url,
            image_url=image_near(html_text, match.start()),
        ))
    log(f"site news: {len(result)}")
    return result


def link_items(page_url: str, page_html: str, category: str, prefix: str,
               keywords: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.I | re.S,
    )
    result = []
    seen = set()
    for match in pattern.finditer(page_html):
        href = match.group(1).strip()
        title = strip_html(match.group(2))
        if not title or len(title) < 6:
            continue
        if not re.search(keywords, title, flags=re.I):
            continue
        if href.startswith("/"):
            href = SITE + href
        if not href.startswith("http"):
            continue
        if href in seen or "satehm.ru/news/" in href:
            continue
        seen.add(href)
        result.append(item(
            f"{prefix}:{href}",
            "site",
            category,
            title,
            text="Материал сайта техникума.",
            url=href,
        ))
    return result


def parse_site_sections() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    session: list[dict[str, Any]] = []
    practice: list[dict[str, Any]] = []
    for url, category, prefix, keywords in [
        (OCHNOE_URL, "practice", "site-practice",
         r"расписан|замен|звонк|практик|график|сесси|экзамен"),
        (DEMO_URL, "session", "site-session",
         r"демо|экзамен|гиа|рекомендац|аттестац"),
        (SCHEDULE_BELL_URL, "practice", "site-bells",
         r"звонк|расписан"),
    ]:
        try:
            page = fetch_text(url)
            parsed = link_items(url, page, category, prefix, keywords)
            if category == "session":
                session.extend(parsed)
            else:
                practice.extend(parsed)
        except Exception as error:  # noqa: BLE001
            log(f"site section failed {url}: {error}")
    # Базовые официальные ссылки всегда полезны.
    practice.insert(0, item(
        "site-practice:ochnoe", "site", "practice",
        "Расписание и замены (очное отделение)",
        "Официальное расписание и замены на сайте техникума.",
        url=OCHNOE_URL,
    ))
    session.insert(0, item(
        "site-session:demo", "site", "session",
        "Демонстрационный экзамен",
        "Материалы и рекомендации по демоэкзамену.",
        url=DEMO_URL,
    ))
    log(f"site session: {len(session)}, practice: {len(practice)}")
    return session, practice


def vk_image(post: dict[str, Any]) -> tuple[str | None, list[str]]:
    images: list[str] = []
    for attachment in post.get("attachments") or []:
        photo = None
        if attachment.get("type") == "photo":
            photo = attachment.get("photo")
        elif attachment.get("type") == "link":
            photo = (attachment.get("link") or {}).get("photo")
        if photo:
            sizes = photo.get("sizes") or []
            if sizes:
                best = sorted(
                    sizes,
                    key=lambda size: (size.get("width", 0) * size.get("height", 0)),
                    reverse=True,
                )[0]
                url = best.get("url")
                if url:
                    images.append(url)
        video = attachment.get("video") if attachment.get("type") == "video" else None
        if video and video.get("photo_800"):
            images.append(video["photo_800"])
    for copy in post.get("copy_history") or []:
        _, copy_images = vk_image(copy)
        images.extend(copy_images)
    # Уникальные, максимум 4.
    unique = list(dict.fromkeys(images))[:4]
    return (unique[0] if unique else None), unique


def vk_items() -> tuple[list[dict[str, Any]], bool]:
    if not VK_TOKEN:
        log("VK_TOKEN не задан — VK пропущен")
        return [], False
    query = urllib.parse.urlencode({
        "owner_id": f"-{VK_GROUP_ID}",
        "count": "50",
        "filter": "all",
        "v": "5.199",
        "access_token": VK_TOKEN,
    })
    try:
        raw = fetch_text(f"https://api.vk.com/method/wall.get?{query}")
        data = json.loads(raw)
        if data.get("error"):
            raise RuntimeError(data["error"].get("error_msg", "VK error"))
        posts = (data.get("response") or {}).get("items") or []
    except Exception as error:  # noqa: BLE001
        log(f"VK failed: {error}")
        return [], False

    result = []
    for post in posts:
        text = post.get("text") or ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0] if lines else "Пост VK"
        description = "\n".join(lines[1:])
        image, images = vk_image(post)
        date = datetime.fromtimestamp(post.get("date", 0), TZ).date().isoformat()
        result.append(item(
            f"vk:{post.get('owner_id')}_{post.get('id')}",
            "vk",
            classify(text),
            title,
            description,
            date=date,
            url=f"https://vk.com/wall{post.get('owner_id')}_{post.get('id')}",
            image_url=image,
            images=images,
        ))
    log(f"VK posts: {len(result)}")
    return result, True


def load_json(path: Path, fallback: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return fallback


def archive_old(previous: list[dict[str, Any]], current: list[dict[str, Any]],
                vk_ok: bool, site_ok: bool) -> list[dict[str, Any]]:
    archive_data = load_json(OUT / "archive.json", {"items": []})
    archive = list(archive_data.get("items") or [])
    current_ids = {entry.get("id") for entry in current}
    now = datetime.now(TZ)
    for old in previous:
        if old.get("id") in current_ids:
            continue
        if old.get("source") == "vk" and not vk_ok:
            continue
        if old.get("source") == "site" and not site_ok:
            continue
        archived = dict(old)
        archived["archived"] = True
        archived["archivedAt"] = now.isoformat()
        if not any(entry.get("id") == archived.get("id") for entry in archive):
            archive.append(archived)
    cutoff = now - timedelta(days=ARCHIVE_DAYS)
    kept = []
    for entry in archive:
        try:
            archived_at = datetime.fromisoformat(entry.get("archivedAt", ""))
            if archived_at >= cutoff:
                kept.append(entry)
        except Exception:  # noqa: BLE001
            kept.append(entry)
    return kept


def upload_yandex(files: list[Path]) -> None:
    key_id = os.environ.get("YC_KEY_ID")
    secret = os.environ.get("YC_SECRET")
    bucket = os.environ.get("YC_BUCKET")
    if not (key_id and secret and bucket):
        log("YC_* не заданы — S3 пропущен")
        return
    try:
        import boto3  # type: ignore
    except Exception:  # noqa: BLE001
        log("boto3 не установлен — S3 пропущен")
        return
    client = boto3.client(
        "s3",
        endpoint_url="https://storage.yandexcloud.net",
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="ru-central1",
    )
    for path in files:
        key = f"content/{path.name}"
        client.upload_file(
            str(path), bucket, key,
            ExtraArgs={
                "ContentType": "application/json; charset=utf-8",
                "CacheControl": "public, max-age=300",
            },
        )
        log(f"S3: {key}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    vk, vk_ok = vk_items()
    try:
        news = parse_site_news()
        site_ok = True
    except Exception as error:  # noqa: BLE001
        log(f"site news failed: {error}")
        news, site_ok = [], False
    try:
        session, practice = parse_site_sections()
    except Exception as error:  # noqa: BLE001
        log(f"site sections failed: {error}")
        session, practice = [], []

    feed = vk + news
    previous = load_json(OUT / "feed.json", {"items": []}).get("items") or []
    archive = archive_old(previous, feed, vk_ok, site_ok)
    now = datetime.now(TZ).isoformat()

    files = {
        "feed.json": {
            "version": 1,
            "updatedAt": now,
            "items": feed,
        },
        "session.json": {
            "version": 1,
            "updatedAt": now,
            "items": session,
            "note": "Официальные ссылки и материалы сайта техникума.",
        },
        "practice.json": {
            "version": 1,
            "updatedAt": now,
            "items": practice,
        },
        "archive.json": {
            "version": 1,
            "updatedAt": now,
            "items": archive,
        },
        "meta.json": {
            "version": 1,
            "updatedAt": now,
            "vkOk": vk_ok,
            "siteOk": site_ok,
            "counts": {
                "feed": len(feed),
                "vk": len(vk),
                "news": len(news),
                "session": len(session),
                "practice": len(practice),
                "archive": len(archive),
            },
        },
    }
    written = []
    for name, payload in files.items():
        path = OUT / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written.append(path)
        log(f"write {path} ({path.stat().st_size} B)")
    upload_yandex(written)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"FATAL: {exc}")
        raise

