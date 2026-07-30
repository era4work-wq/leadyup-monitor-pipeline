"""Стадия 6b: сборка 7 PNG-слайдов карусели и отправка на согласование.

PNG не хранятся в репозитории (см. write_carousel.py) — рендерятся здесь,
при отправке, через render_html.renderer() (один Chromium-процесс на все 7
слайдов). У альбома (sendMediaGroup) не может быть кнопок — они живут на
отдельном текстовом сообщении сразу под альбомом, привязка к теме через
callback_data (approve_car/reject_car/redo_car:<id>), как у redo у постов.

Публикация карусели не автоматизирована (Instagram/Pinterest не подключены —
см. план от 30.07.2026, Фаза 4) — кнопка «Одобрить» пока только фиксирует
статус, как раньше «Взято» у тем.
"""
import base64
import sys

import drive_banners
import render_html
from common import DATA_DIR, require_env, today, read_json, write_json
from notify_telegram import tg_call, tg_send_media_group_bytes

COVER_TEMPLATE = "шаблон-карусель-обложка.html"
SLIDE_TEMPLATE = "шаблон-карусель.html"
FINAL_TEMPLATE = "шаблон-карусель-финал.html"
SIZE = (1080, 1350)
SLIDE_COUNT = 7

# Бейдж финального слайда статичный — сам текст (cta_headline/cta_body)
# генерируется моделью в write_carousel.py: без привязки к теме карусели он
# выглядит немотивированной вставкой рекламы (замечание владелицы 30.07.2026).
FINAL_BADGE = "ПОПРОБУЙТЕ"


def already_sent_ids() -> set[str]:
    sent_dir = DATA_DIR / "carousel_pending"
    if not sent_dir.exists():
        return set()
    ids = set()
    for path in sorted(sent_dir.glob("*.json")):
        ids.update(read_json(path, {}).keys())
    return ids


def load_carousel_drafts() -> list[dict]:
    drafts_dir = DATA_DIR / "carousel_drafts"
    if not drafts_dir.exists():
        return []
    items = []
    for path in sorted(drafts_dir.glob("*.json")):
        items.extend(read_json(path, []))
    return items


def render_slides(item: dict, service) -> list[bytes]:
    width, height = SIZE
    banner = item.get("banner")
    background = None
    if banner:
        try:
            raw = service.files().get_media(fileId=banner["id"]).execute()
            background = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        except Exception as exc:
            print(f"  [WARN] не удалось скачать баннер: {exc}", file=sys.stderr)

    badge = (banner or {}).get("badge") or "КАРУСЕЛЬ"
    headline = (banner or {}).get("headline") or item["cover_headline"]

    slides = []
    with render_html.renderer() as r:
        if background:
            slides.append(r(COVER_TEMPLATE, {
                "BACKGROUND": background,
                "BADGE": badge,
                "SLIDE_NUM": f"1/{SLIDE_COUNT}",
                "HEADLINE": headline,
                "BODY": item["cover_body"],
            }, width, height))
        else:
            # Баннер не подобрался (Drive недоступен и т.п.) — обложка всё
            # равно должна быть, показываем как обычный слайд без фото.
            slides.append(r(SLIDE_TEMPLATE, {
                "TAG": badge,
                "SLIDE_NUM": f"1/{SLIDE_COUNT}",
                "HEADLINE": headline,
                "BODY": item["cover_body"],
                "HAND": "",
            }, width, height))

        for i, slide in enumerate(item["slides"], start=2):
            slides.append(r(SLIDE_TEMPLATE, {
                "TAG": slide.get("tag") or "Польза",
                "SLIDE_NUM": f"{i}/{SLIDE_COUNT}",
                "HEADLINE": slide["headline"],
                "BODY": slide["body"],
                "HAND": slide.get("hand") or "",
            }, width, height))

        slides.append(r(FINAL_TEMPLATE, {
            "BADGE": FINAL_BADGE,
            "SLIDE_NUM": f"{SLIDE_COUNT}/{SLIDE_COUNT}",
            "HEADLINE": item["cta_headline"],
            "BODY": item["cta_body"],
        }, width, height))

    return slides


def send_carousel(token: str, chat_id: str, item: dict, service) -> dict:
    slides = render_slides(item, service)
    media_result = tg_send_media_group_bytes(token, chat_id, slides, caption=f"🎠 {item['title']}")
    photo_message_ids = [m["message_id"] for m in media_result]

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"approve_car:{item['id']}"},
                {"text": "🔄 Перегенерировать", "callback_data": f"redo_car:{item['id']}"},
                {"text": "❌ Отклонить", "callback_data": f"reject_car:{item['id']}"},
            ]
        ]
    }
    text = f"Карусель выше — <b>{item['title']}</b>\nИсточник: {item['source']}"
    result = tg_call(
        token, "sendMessage", chat_id=chat_id, text=text,
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard,
    )
    return {
        **item,
        "message_id": result["message_id"],
        "photo_message_ids": photo_message_ids,
        "text": text,
        "sent_as": "text",
        "status": "ждёт",
    }


def main():
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    drafts = load_carousel_drafts()
    sent = already_sent_ids()
    pending_send = [d for d in drafts if d["id"] not in sent]

    if not pending_send:
        print("Нет новых каруселей для согласования.", file=sys.stderr)
        return

    service = drive_banners.get_service()

    carousel_pending = {}
    for item in pending_send:
        print(f"Отправляю карусель: {item['title'][:60]}", file=sys.stderr)
        try:
            carousel_pending[item["id"]] = send_carousel(token, chat_id, item, service)
        except Exception as exc:
            print(f"  [WARN] не удалось отправить карусель: {exc}", file=sys.stderr)

    if not carousel_pending:
        return
    write_json(DATA_DIR / "carousel_pending" / f"{today()}.json", carousel_pending)
    print(f"Отправлено каруселей на согласование: {len(carousel_pending)}.", file=sys.stderr)


if __name__ == "__main__":
    main()
