"""Стадия 6: отправка готовых черновиков на финальное согласование.

Показывает в той же группе сам текст поста (как он будет опубликован) с
кнопками [Опубликовать] [Отклонить]. В отличие от стадии 3 (согласование
тем) — здесь утверждают готовый текст, не идею.
"""
import sys

from common import DATA_DIR, require_env, today, read_json, write_json
from notify_telegram import tg_call  # переиспользуем HTTP-обвязку

RUBRIC_LABEL = {
    "дайджест": "📰 дайджест",
    "кейс-с-цифрами": "📊 кейс с цифрами",
    "лайфхак-инструкция": "🛠 лайфхак",
    "ai-инструмент": "🤖 AI-инструмент",
}

CAPTION_LIMIT = 1024  # лимит Telegram для подписи к фото


def already_sent_ids() -> set[str]:
    final_dir = DATA_DIR / "final_pending"
    if not final_dir.exists():
        return set()
    ids = set()
    for path in sorted(final_dir.glob("*.json")):
        ids.update(read_json(path, {}).keys())
    return ids


def load_drafts() -> list[dict]:
    drafts_dir = DATA_DIR / "drafts"
    if not drafts_dir.exists():
        return []
    items = []
    for path in sorted(drafts_dir.glob("*.json")):
        items.extend(read_json(path, []))
    return items


def format_preview(item: dict) -> str:
    rubric = RUBRIC_LABEL.get(item.get("rubric", ""), item.get("rubric", ""))
    return f"{rubric} · на публикацию:\n\n{item['draft_text']}"


def main():
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    drafts = load_drafts()
    sent = already_sent_ids()
    pending_send = [d for d in drafts if d["id"] not in sent]

    if not pending_send:
        print("Нет новых черновиков для финального согласования.", file=sys.stderr)
        return

    final_pending = {}
    for item in pending_send:
        text = format_preview(item)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📤 Опубликовать", "callback_data": f"publish:{item['id']}"},
                    {"text": "❌ Отклонить", "callback_data": f"reject:{item['id']}"},
                ]
            ]
        }
        image_url = item.get("image_url")
        sent_as = "text"
        if image_url and len(text) <= CAPTION_LIMIT:
            result = tg_call(
                token,
                "sendPhoto",
                chat_id=chat_id,
                photo=image_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            sent_as = "photo"
        else:
            result = tg_call(
                token,
                "sendMessage",
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        final_pending[item["id"]] = {
            **item,
            "message_id": result["message_id"],
            "text": text,
            "sent_as": sent_as,
            "status": "ждёт",
        }

    write_json(DATA_DIR / "final_pending" / f"{today()}.json", final_pending)
    print(f"Отправлено на финальное согласование: {len(final_pending)}.", file=sys.stderr)


if __name__ == "__main__":
    main()
