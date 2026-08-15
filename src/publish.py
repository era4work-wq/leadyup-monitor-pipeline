"""Публикация готового поста на все настроенные площадки.

Вынесено из collect_approvals.py, чтобы использовалось и оттуда (постановка
в очередь по кнопке «Опубликовать»), и из publish_queue.py (реальная отправка
из очереди по расписанию с разрядкой — см. src/publish_queue.py).
"""
import base64
import os
import sys

import drive_banners
import publish_max
import publish_vk
import render_html
from common import visible_length
from notify_telegram import tg_call, tg_send_photo_bytes

CAPTION_LIMIT = 1024  # лимит Telegram для подписи к фото (у обычных сообщений — 4096)
BANNER_TEMPLATE = "шаблон-баннер.html"
BANNER_SIZE = (1920, 1080)


def tg_call_safe(token: str, method: str, **params):
    """Как tg_call, но не роняет весь прогон (например, если callback_query
    протух — Telegram отвечает 400, а нам всё равно нужно обработать
    остальные апдейты)."""
    try:
        return tg_call(token, method, **params)
    except Exception as exc:
        print(f"[WARN] {method} не удался: {exc}", file=sys.stderr)
        return None


def render_banner(entry: dict, service) -> bytes:
    """Рендерит баннер темы (id/headline/badge из общего кэша drive_banners,
    см. get_post_banner в write_draft.py) с наложенным заголовком — та же
    логика, что в notify_final.py для карточки согласования, но здесь для
    РЕАЛЬНОЙ публикации в канал. Возвращает None, если баннера нет или
    рендер не удался (Drive недоступен и т.п.) — тогда publish_to_channel
    падает обратно на og:image источника."""
    banner = entry.get("banner")
    if not banner or service is None:
        return None
    try:
        raw = drive_banners.load_banner_bytes(service, banner)
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
        print(f"[WARN] не удалось отрендерить баннер: {exc}", file=sys.stderr)
        return None


def publish_to_channel(token: str, channel: str, entry: dict, cover_bytes: bytes, image_url: str) -> bool:
    """Картинка — сначала баннер темы (готовые байты, отрендеренные один раз
    в publish_everywhere — та же картинка уходит и в Max/VK, пост должен
    выглядеть одинаково на всех площадках), при сбое отправки падаем обратно
    на og:image источника (image_url), при сбое и того — публикуем без
    картинки, но текст всё равно должен уйти."""
    text = entry["draft_text"]
    fits_caption = visible_length(text) <= CAPTION_LIMIT

    if cover_bytes and fits_caption:
        try:
            tg_send_photo_bytes(token, channel, cover_bytes, caption=text, parse_mode="HTML")
            return True
        except Exception as exc:
            print(f"[WARN] sendPhoto(bytes) не удался: {exc} — пробую og:image", file=sys.stderr)
            cover_bytes = None

    if cover_bytes is None and image_url and fits_caption:
        result = tg_call_safe(token, "sendPhoto", chat_id=channel, photo=image_url, caption=text, parse_mode="HTML")
        if result is not None:
            return True
        print("[WARN] sendPhoto(url) не удался — публикую без картинки в подписи", file=sys.stderr)
        image_url = None

    if cover_bytes or image_url:
        # Не влезает в подпись — картинка отдельным сообщением перед текстом,
        # чтобы она всё равно оказалась вверху поста.
        try:
            if cover_bytes:
                tg_send_photo_bytes(token, channel, cover_bytes)
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


def publish_everywhere(token: str, entry: dict, service=None) -> dict:
    """Публикует ОДИН И ТОТ ЖЕ пост на все настроенные площадки — тот же
    текст, та же картинка (баннер темы рендерится один раз здесь и байты
    передаются в каждую площадку, а не перерендериваются/берутся с сайта
    отдельно для каждой — решение владелицы 30.07.2026: везде должно быть
    одинаково). Площадка без секретов (собственник ещё не подключил канал/
    группу) молча пропускается — это не ошибка, а ожидаемое переходное
    состояние. Возвращает {"Telegram"/"Max"/"VK": True/False} — только для
    реально подключённых площадок, чтобы штамп в чате показывал, куда пост
    действительно ушёл. `service` — клиент Google Drive для баннера темы."""
    results = {}
    cover_bytes = render_banner(entry, service)
    image_url = entry.get("image_url")

    tg_channel = os.environ.get("TELEGRAM_CHANNEL")
    if tg_channel:
        results["Telegram"] = publish_to_channel(token, tg_channel, entry, cover_bytes, image_url)
    else:
        print("[WARN] TELEGRAM_CHANNEL не задан — канал ещё не подключен", file=sys.stderr)

    max_token = os.environ.get("MAX_BOT_TOKEN")
    max_chat = os.environ.get("MAX_CHAT_ID")
    if max_token and max_chat:
        results["Max"] = publish_max.publish(
            max_token, max_chat, entry["draft_text"], image_url=image_url, image_bytes=cover_bytes,
        )

    vk_token = os.environ.get("VK_GROUP_TOKEN")
    vk_group = os.environ.get("VK_GROUP_ID")
    if vk_token and vk_group:
        results["VK"] = publish_vk.publish(
            vk_token, vk_group, entry["draft_text"], image_url=image_url, image_bytes=cover_bytes,
            user_token=os.environ.get("VK_USER_TOKEN"),
        )

    return results
