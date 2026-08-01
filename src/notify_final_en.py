"""Стадия 6b (EN, Фаза 2): финальное согласование англоязычного поста.

По образцу notify_final.py, но без кнопки «Опубликовать» — площадок
(Twitter/X, Threads) пока нет, аккаунты появятся позже (см. память проекта).
Кнопка называется «Утвердить»: фиксирует текст как готовый и складывает его
в data/final_pending_en/ — это терминальное состояние EN-модуля на сегодня.
Когда появятся секреты площадок, публикация подключится отдельным шагом
(коннекторы publish_twitter.py/publish_threads.py — не пишутся вслепую без
реальных аккаунтов для проверки, по тому же принципу, что и разбор Max/VK).

Текст — plain text без HTML/markdown (см. write_draft_en.py), поэтому
сообщения шлются БЕЗ parse_mode: разметка не нужна, а её отсутствие
защищает от случайных "<"/"&" в тексте поста, которые сломали бы HTML-парсинг.
"""
import os
import sys

import drive_banners
from common import DATA_DIR, require_env, today, read_json, visible_length, write_json
from notify_final import render_banner  # общий рендер баннера, не зависит от языка
from notify_telegram import tg_call, tg_send_photo_bytes

CAPTION_LIMIT = 1024  # лимит Telegram для карточки СОГЛАСОВАНИЯ (внутренний чат, не сама площадка)


def already_sent_ids_en() -> set[str]:
    final_dir = DATA_DIR / "final_pending_en"
    if not final_dir.exists():
        return set()
    ids = set()
    for path in sorted(final_dir.glob("*.json")):
        ids.update(read_json(path, {}).keys())
    return ids


def load_drafts_en() -> list[dict]:
    drafts_dir = DATA_DIR / "drafts_en"
    if not drafts_dir.exists():
        return []
    items = []
    for path in sorted(drafts_dir.glob("*.json")):
        items.extend(read_json(path, []))
    return items


def send_final_card_en(token: str, chat_id: str, item: dict, service) -> dict:
    """Отправляет карточку EN-черновика на согласование (фото+подпись, если
    влезает и есть картинка, иначе текст). Переиспользуется и при первой
    отправке, и при пересборке по кнопке «Перегенерировать»."""
    text = f"🇬🇧 {item['draft_text']}"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Утвердить", "callback_data": f"approve_en:{item['id']}"},
                {"text": "🔄 Перегенерировать", "callback_data": f"redo_en:{item['id']}"},
                {"text": "❌ Отклонить", "callback_data": f"reject_en:{item['id']}"},
            ]
        ]
    }
    cover_bytes = render_banner(item, service)
    image_url = item.get("image_url")
    sent_as = "text"
    if cover_bytes and visible_length(text) <= CAPTION_LIMIT:
        result = tg_send_photo_bytes(token, chat_id, cover_bytes, caption=text, reply_markup=keyboard)
        sent_as = "photo"
    elif image_url and visible_length(text) <= CAPTION_LIMIT:
        result = tg_call(token, "sendPhoto", chat_id=chat_id, photo=image_url, caption=text, reply_markup=keyboard)
        sent_as = "photo"
    else:
        if cover_bytes:
            tg_send_photo_bytes(token, chat_id, cover_bytes)
        elif image_url:
            tg_call(token, "sendPhoto", chat_id=chat_id, photo=image_url)
        result = tg_call(
            token, "sendMessage", chat_id=chat_id, text=text,
            disable_web_page_preview=True, reply_markup=keyboard,
        )
    return {**item, "message_id": result["message_id"], "text": text, "sent_as": sent_as, "status": "ждёт"}


def main():
    if os.environ.get("EN_CONTENT_ENABLED", "").strip().lower() != "true":
        print("EN_CONTENT_ENABLED не включён — EN-модуль пропущен.", file=sys.stderr)
        return

    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    drafts = load_drafts_en()
    sent = already_sent_ids_en()
    pending_send = [d for d in drafts if d["id"] not in sent]

    if not pending_send:
        print("Нет новых EN-черновиков для согласования.", file=sys.stderr)
        return

    service = drive_banners.get_service()
    final_pending = {}
    for item in pending_send:
        final_pending[item["id"]] = send_final_card_en(token, chat_id, item, service)

    write_json(DATA_DIR / "final_pending_en" / f"{today()}.json", final_pending)
    print(f"Отправлено EN-постов на согласование: {len(final_pending)}.", file=sys.stderr)


if __name__ == "__main__":
    main()
