"""Публикация готового поста на все настроенные площадки.

Вынесено из collect_approvals.py, чтобы использовалось и оттуда (постановка
в очередь по кнопке «Опубликовать»), и из publish_queue.py (реальная отправка
из очереди по расписанию с разрядкой — см. src/publish_queue.py).
"""
import base64
import os
import sys

import publish_max
import publish_vk
from common import visible_length
from notify_telegram import tg_call, tg_send_photo_bytes

CAPTION_LIMIT = 1024  # лимит Telegram для подписи к фото (у обычных сообщений — 4096)


def tg_call_safe(token: str, method: str, **params):
    """Как tg_call, но не роняет весь прогон (например, если callback_query
    протух — Telegram отвечает 400, а нам всё равно нужно обработать
    остальные апдейты)."""
    try:
        return tg_call(token, method, **params)
    except Exception as exc:
        print(f"[WARN] {method} не удался: {exc}", file=sys.stderr)
        return None


def publish_to_channel(token: str, channel: str, entry: dict) -> bool:
    """Картинка — сначала ИИ-обложка (cover_image_b64, байты), если она
    когда-нибудь появится (сейчас генерация с текстом выключена — см.
    write_draft.py), иначе og:image источника (image_url, по ссылке)."""
    text = entry["draft_text"]
    cover_b64 = entry.get("cover_image_b64")
    image_url = entry.get("image_url")

    if cover_b64 and visible_length(text) <= CAPTION_LIMIT:
        try:
            tg_send_photo_bytes(token, channel, base64.b64decode(cover_b64), caption=text, parse_mode="HTML")
            return True
        except Exception as exc:
            print(f"[WARN] sendPhoto(bytes) не удался: {exc} — пробую URL-картинку", file=sys.stderr)
    elif image_url and visible_length(text) <= CAPTION_LIMIT:
        result = tg_call_safe(token, "sendPhoto", chat_id=channel, photo=image_url, caption=text, parse_mode="HTML")
        if result is not None:
            return True
        print("[WARN] sendPhoto(url) не удался — публикую без картинки в подписи", file=sys.stderr)
    elif cover_b64 or image_url:
        # Не влезает в подпись — картинка отдельным сообщением перед текстом,
        # чтобы она всё равно оказалась вверху поста.
        try:
            if cover_b64:
                tg_send_photo_bytes(token, channel, base64.b64decode(cover_b64))
            else:
                tg_call_safe(token, "sendPhoto", chat_id=channel, photo=image_url)
        except Exception as exc:
            print(f"[WARN] не удалось отправить картинку отдельным сообщением: {exc}", file=sys.stderr)

    result = tg_call_safe(
        token,
        "sendMessage",
        chat_id=channel,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return result is not None


def publish_everywhere(token: str, entry: dict) -> dict:
    """Публикует пост на все настроенные площадки. Площадка без секретов
    (собственник ещё не подключил канал/группу) молча пропускается — это не
    ошибка, а ожидаемое состояние на пути к Фазе 1 (Max, потом VK). Возвращает
    {"Telegram"/"Max"/"VK": True/False} — только для реально подключённых
    площадок, чтобы штамп в чате показывал, куда пост действительно ушёл."""
    results = {}

    tg_channel = os.environ.get("TELEGRAM_CHANNEL")
    if tg_channel:
        results["Telegram"] = publish_to_channel(token, tg_channel, entry)
    else:
        print("[WARN] TELEGRAM_CHANNEL не задан — канал ещё не подключен", file=sys.stderr)

    max_token = os.environ.get("MAX_BOT_TOKEN")
    max_chat = os.environ.get("MAX_CHAT_ID")
    if max_token and max_chat:
        results["Max"] = publish_max.publish(max_token, max_chat, entry["draft_text"], entry.get("image_url"))

    vk_token = os.environ.get("VK_GROUP_TOKEN")
    vk_group = os.environ.get("VK_GROUP_ID")
    if vk_token and vk_group:
        results["VK"] = publish_vk.publish(vk_token, vk_group, entry["draft_text"], entry.get("image_url"))

    return results
