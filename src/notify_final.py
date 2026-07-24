"""Стадия 6: отправка готовых черновиков на финальное согласование.

Показывает в той же группе сам текст поста (как он будет опубликован) с
кнопками [Опубликовать] [Отклонить]. В отличие от стадии 3 (согласование
тем) — здесь утверждают готовый текст, не идею.
"""
import base64
import sys

from common import DATA_DIR, require_env, today, read_json, write_json
from notify_telegram import tg_call, tg_send_photo_bytes  # переиспользуем HTTP-обвязку

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


def send_final_card(token: str, chat_id: str, item: dict) -> dict:
    """Отправляет карточку черновика на финальное согласование (фото+подпись,
    если влезает и есть картинка, иначе текст). Переиспользуется и при первой
    отправке, и при пересборке черновика по кнопке «Перегенерировать»."""
    text = format_preview(item)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📤 Опубликовать", "callback_data": f"publish:{item['id']}"},
                {"text": "🔄 Перегенерировать", "callback_data": f"redo:{item['id']}"},
                {"text": "❌ Отклонить", "callback_data": f"reject:{item['id']}"},
            ]
        ]
    }
    cover_b64 = item.get("cover_image_b64")
    sent_as = "text"
    if cover_b64 and len(text) <= CAPTION_LIMIT:
        result = tg_send_photo_bytes(
            token,
            chat_id,
            base64.b64decode(cover_b64),
            caption=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        sent_as = "photo"
    else:
        # Картинки нет (не сгенерировалась) или пост не влезает в подпись —
        # обычным текстом. Источника здесь больше не показываем — заказчик
        # прямо просил не брать обложку с сайта источника.
        result = tg_call(
            token,
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    return {
        **item,
        "message_id": result["message_id"],
        "text": text,
        "sent_as": sent_as,
        "status": "ждёт",
    }


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
        final_pending[item["id"]] = send_final_card(token, chat_id, item)

    write_json(DATA_DIR / "final_pending" / f"{today()}.json", final_pending)
    print(f"Отправлено на финальное согласование: {len(final_pending)}.", file=sys.stderr)


if __name__ == "__main__":
    main()
