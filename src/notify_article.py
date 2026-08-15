"""Стадия 6c: доставка готовой статьи в чат — файлом, не текстом сообщения
(решение владелицы 29.07.2026: статья приходит уже оформленной, готовой
вставить в редактор VC.ru/Дзена, публикация — вручную, см.
движок/площадки-и-форматы.md).
"""
import base64
import re
import sys

import drive_banners
import render_html
from common import DATA_DIR, require_env, today, read_json, write_json
from notify_telegram import tg_call, tg_send_document_bytes, tg_send_photo_bytes

BANNER_TEMPLATE = "шаблон-баннер.html"
BANNER_SIZE = (1920, 1080)

SAFE_NAME_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё _-]")


def already_sent_ids() -> set[str]:
    sent_dir = DATA_DIR / "article_sent"
    if not sent_dir.exists():
        return set()
    ids = set()
    for path in sorted(sent_dir.glob("*.json")):
        ids.update(read_json(path, {}).keys())
    return ids


def load_article_drafts() -> list[dict]:
    drafts_dir = DATA_DIR / "article_drafts"
    if not drafts_dir.exists():
        return []
    items = []
    for path in sorted(drafts_dir.glob("*.json")):
        items.extend(read_json(path, []))
    return items


def make_filename(title: str) -> str:
    safe = SAFE_NAME_RE.sub("", title).strip()[:80]
    return f"{safe or 'статья'}.md"


def send_article(token: str, chat_id: str, item: dict, service) -> dict:
    banner = item.get("banner")
    if banner:
        try:
            raw = drive_banners.load_banner_bytes(service, banner)
            headline = banner.get("headline")
            if headline:
                data_uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
                width, height = BANNER_SIZE
                png = render_html.render(BANNER_TEMPLATE, {
                    "BACKGROUND": data_uri,
                    "BADGE": banner.get("badge") or "СТАТЬЯ",
                    "HEADLINE": headline,
                }, width, height)
            else:
                png = raw  # старый кэш без headline (до 30.07.2026) — шлём как есть
            tg_send_photo_bytes(token, chat_id, png, caption=f"Обложка: {item['title']}")
        except Exception as exc:
            print(f"  [WARN] не удалось отправить баннер: {exc}", file=sys.stderr)

    for url in item.get("image_urls", []):
        try:
            tg_call(token, "sendPhoto", chat_id=chat_id, photo=url, caption="Картинка из источника")
        except Exception as exc:
            print(f"  [WARN] не удалось отправить картинку {url}: {exc}", file=sys.stderr)

    filename = make_filename(item["title"])
    caption = f"📄 Статья готова: <b>{item['title']}</b>\nИсточник: {item['source']}\n\nПеред публикацией — вставить в редактор VC.ru/Дзена вручную."
    tg_send_document_bytes(
        token, chat_id, filename,
        item["article_text"].encode("utf-8"),
        caption=caption, parse_mode="HTML",
    )
    return {**item, "status": "отправлено"}


def main():
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    drafts = load_article_drafts()
    sent = already_sent_ids()
    pending_send = [d for d in drafts if d["id"] not in sent]

    if not pending_send:
        print("Нет новых статей для отправки.", file=sys.stderr)
        return

    service = drive_banners.get_service()

    article_sent = {}
    for item in pending_send:
        print(f"Отправляю: {item['title'][:60]}", file=sys.stderr)
        article_sent[item["id"]] = send_article(token, chat_id, item, service)

    # Слияние, не перезапись — см. notify_final.py, тот же баг: несколько
    # запусков write-drafts.yml в день (мгновенный триггер) стирали друг
    # у друга уже отправленные записи.
    path = DATA_DIR / "article_sent" / f"{today()}.json"
    existing = read_json(path, {})
    existing.update(article_sent)
    write_json(path, existing)
    print(f"Отправлено статей файлом: {len(article_sent)}.", file=sys.stderr)


if __name__ == "__main__":
    main()
