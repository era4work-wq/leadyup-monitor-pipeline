"""Стадия 6c (EN-карусель) — сборка 7 PNG-слайдов и отправка на согласование.

По образцу notify_carousel.py — рендер тот же (шаблоны design/шаблон-карусель-*.html
одни на RU и EN, текст на них просто на другом языке), но свои дефолты
бейджа/финального слайда (английские, не «КАРУСЕЛЬ»/«ПОПРОБУЙТЕ») и кнопка
«Одобрить», не «Опубликовать» — площадок (Instagram/Pinterest) ещё нет,
как и у RU-карусели (публикация карусели не автоматизирована вообще, это
не EN-специфичное ограничение).
"""
import base64
import os
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
FINAL_BADGE = "TRY IT"


def already_sent_ids_en() -> set:
    sent_dir = DATA_DIR / "carousel_pending_en"
    if not sent_dir.exists():
        return set()
    ids = set()
    for path in sorted(sent_dir.glob("*.json")):
        ids.update(read_json(path, {}).keys())
    return ids


def load_carousel_drafts_en() -> list:
    drafts_dir = DATA_DIR / "carousel_drafts_en"
    if not drafts_dir.exists():
        return []
    items = []
    for path in sorted(drafts_dir.glob("*.json")):
        items.extend(read_json(path, []))
    return items


def render_slides_en(item: dict, service) -> list:
    width, height = SIZE
    banner = item.get("banner")
    background = None
    if banner:
        try:
            raw = drive_banners.load_banner_bytes(service, banner)
            background = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        except Exception as exc:
            print(f"  [WARN] не удалось скачать баннер: {exc}", file=sys.stderr)

    badge = (banner or {}).get("badge") or "CAROUSEL"
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
            slides.append(r(SLIDE_TEMPLATE, {
                "TAG": badge,
                "SLIDE_NUM": f"1/{SLIDE_COUNT}",
                "HEADLINE": headline,
                "BODY": item["cover_body"],
                "HAND": "",
            }, width, height))

        for i, slide in enumerate(item["slides"], start=2):
            slides.append(r(SLIDE_TEMPLATE, {
                "TAG": slide.get("tag") or "Tip",
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


def send_carousel_en(token: str, chat_id: str, item: dict, service) -> dict:
    slides = render_slides_en(item, service)
    media_result = tg_send_media_group_bytes(token, chat_id, slides, caption=f"🇬🇧🎠 {item['title']}")
    photo_message_ids = [m["message_id"] for m in media_result]

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"approve_car_en:{item['id']}"},
                {"text": "🔄 Перегенерировать", "callback_data": f"redo_car_en:{item['id']}"},
                {"text": "❌ Отклонить", "callback_data": f"reject_car_en:{item['id']}"},
            ]
        ]
    }
    text = f"EN-карусель выше — <b>{item['title']}</b>\nИсточник: {item['source']}"
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
    if os.environ.get("EN_CONTENT_ENABLED", "").strip().lower() != "true":
        print("EN_CONTENT_ENABLED не включён — EN-модуль пропущен.", file=sys.stderr)
        return

    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    drafts = load_carousel_drafts_en()
    sent = already_sent_ids_en()
    pending_send = [d for d in drafts if d["id"] not in sent]

    if not pending_send:
        print("Нет новых EN-каруселей для согласования.", file=sys.stderr)
        return

    service = drive_banners.get_service()

    carousel_pending = {}
    for item in pending_send:
        print(f"Отправляю EN-карусель: {item['title'][:60]}", file=sys.stderr)
        try:
            carousel_pending[item["id"]] = send_carousel_en(token, chat_id, item, service)
        except Exception as exc:
            print(f"  [WARN] не удалось отправить EN-карусель: {exc}", file=sys.stderr)

    if not carousel_pending:
        return
    # Слияние, не перезапись — см. notify_final.py, тот же баг: несколько
    # запусков write-drafts.yml в день (мгновенный триггер) стирали друг
    # у друга уже отправленные карточки.
    path = DATA_DIR / "carousel_pending_en" / f"{today()}.json"
    existing = read_json(path, {})
    existing.update(carousel_pending)
    write_json(path, existing)
    print(f"Отправлено EN-каруселей на согласование: {len(carousel_pending)}.", file=sys.stderr)


if __name__ == "__main__":
    main()
