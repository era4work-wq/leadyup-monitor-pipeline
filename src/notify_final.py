"""Стадия 6: отправка готовых черновиков на финальное согласование.

Показывает в той же группе сам текст поста (как он будет опубликован) с
кнопками [Опубликовать] [Отклонить]. В отличие от стадии 3 (согласование
тем) — здесь утверждают готовый текст, не идею.
"""
import base64
import sys

import drive_banners
import render_html
from common import DATA_DIR, require_env, today, read_json, visible_length, write_json
from notify_telegram import tg_call, tg_send_photo_bytes  # переиспользуем HTTP-обвязку

CAPTION_LIMIT = 1024  # лимит Telegram для подписи к фото
BANNER_TEMPLATE = "шаблон-баннер.html"
BANNER_SIZE = (1920, 1080)


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
    # Без служебной метки рубрики (убрано 29.07.2026 по просьбе владелицы —
    # экономит символы подписи, рубрика видна и так по картинке/бейджу).
    return item["draft_text"]


def render_banner(item: dict, service) -> bytes:
    """Рендерит баннер темы (id/headline/badge из общего кэша drive_banners,
    см. get_post_banner в write_draft.py) с наложенным заголовком — тот же
    подход, что у статьи (notify_article.py) и карусели (notify_carousel.py).
    Возвращает None, если баннера нет или рендер не удался — тогда
    send_final_card падает обратно на og:image источника."""
    banner = item.get("banner")
    if not banner:
        return None
    try:
        raw = service.files().get_media(fileId=banner["id"]).execute()
        headline = banner.get("headline")
        if not headline:
            return raw  # старый кэш без headline (до 30.07.2026) — как есть
        data_uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        width, height = BANNER_SIZE
        return render_html.render(BANNER_TEMPLATE, {
            "BACKGROUND": data_uri,
            "BADGE": banner.get("badge") or "AI РАДАР",
            "HEADLINE": headline,
        }, width, height)
    except Exception as exc:
        print(f"  [WARN] не удалось отрендерить баннер: {exc}", file=sys.stderr)
        return None


def send_final_card(token: str, chat_id: str, item: dict, service) -> dict:
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
    cover_bytes = render_banner(item, service)
    image_url = item.get("image_url")
    sent_as = "text"
    if cover_bytes and visible_length(text) <= CAPTION_LIMIT:
        result = tg_send_photo_bytes(
            token, chat_id, cover_bytes,
            caption=text, parse_mode="HTML", reply_markup=keyboard,
        )
        sent_as = "photo"
    elif image_url and visible_length(text) <= CAPTION_LIMIT:
        result = tg_call(
            token, "sendPhoto", chat_id=chat_id, photo=image_url,
            caption=text, parse_mode="HTML", reply_markup=keyboard,
        )
        sent_as = "photo"
    else:
        # Пост не влезает в подпись к фото — картинку всё равно шлём
        # отдельным сообщением ПЕРЕД текстом (заказчик просил картинку
        # именно вверху), кнопки и статус живут на текстовом сообщении.
        if cover_bytes:
            tg_send_photo_bytes(token, chat_id, cover_bytes)
        elif image_url:
            tg_call(token, "sendPhoto", chat_id=chat_id, photo=image_url)
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

    service = drive_banners.get_service()
    final_pending = {}
    for item in pending_send:
        final_pending[item["id"]] = send_final_card(token, chat_id, item, service)

    write_json(DATA_DIR / "final_pending" / f"{today()}.json", final_pending)
    print(f"Отправлено на финальное согласование: {len(final_pending)}.", file=sys.stderr)


if __name__ == "__main__":
    main()
