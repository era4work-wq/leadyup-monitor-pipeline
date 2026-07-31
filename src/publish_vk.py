"""Публикация готового поста в ВК-группу — тот же текст/картинка, что и в
Telegram/MAX. collect_approvals.py вызывает publish() только если
VK_GROUP_TOKEN/VK_GROUP_ID заданы, иначе платформа тихо пропускается.

VK REST API, https://api.vk.com/method/. Публикация фото на стену — три
шага (в отличие от Telegram/MAX, просто URL картинки не принимается):
  1. photos.getWallUploadServer — получить upload_url
  2. multipart-загрузка байтов картинки на upload_url
  3. photos.saveWallPhoto — сохранить как фото сообщества, получить id
Затем wall.post с attachments=photo{owner_id}_{photo_id}.

**Два разных типа токена нужны для разных методов (нащупано 31.07.2026,
эмпирически, официальная документация об этом не предупреждает):**
- `wall.post`/`wall.delete` работают ТОЛЬКО с токеном СООБЩЕСТВА (VK_GROUP_TOKEN,
  «Ключи доступа» в настройках сообщества) — с пользовательским токеном
  падают ошибкой "denied for non-standalone applications" (наше
  приложение — «Плагин для сообщества», не Standalone).
- `photos.getWallUploadServer`/`photos.saveWallPhoto` — наоборот, падают с
  токеном сообщества ("Group authorization failed: method is unavailable
  with group auth"), работают только с ПОЛЬЗОВАТЕЛЬСКИМ токеном
  (VK_USER_TOKEN, OAuth через oauth.vk.com/authorize с client_id
  созданного приложения, scope=wall,photos — без groups и offline, ВК их
  не принимает для этого типа приложения; из-за отсутствия offline токен
  живёт ~24 часа, требует периодического обновления вручную).
Поэтому картинка грузится одним токеном, а сам пост с уже готовым
attachment — другим (attachment можно передать в wall.post любым токеном,
у которого есть доступ к посту, независимо от того, кто грузил фото).
Если VK_USER_TOKEN не задан или протух — публикация идёт без картинки
(мягкая деградация, не ошибка всего поста).
"""
import sys

import requests

from common import html_to_plain_text

API_BASE = "https://api.vk.com/method"
API_VERSION = "5.199"
TEXT_LIMIT = 16000  # реальный лимит VK (~16384) на wall.post


def _call(method: str, token: str, **params):
    params = {**params, "access_token": token, "v": API_VERSION}
    resp = requests.post(f"{API_BASE}/{method}", data=params, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"VK API {method} failed: {body['error']}")
    return body["response"]


def _sniff_image(image_bytes: bytes) -> tuple[str, str]:
    """Имя файла + content-type по магическим байтам, не по источнику —
    баннер темы (render_html.render, JPEG) и og:image источника (может
    быть и PNG, и JPEG) могут прийти сюда одинаково. ВК молча возвращает
    пустой photo в ответе загрузки, если content-type не совпадает с
    реальным форматом файла (поймано на практике 31.07.2026)."""
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "cover.png", "image/png"
    return "cover.jpg", "image/jpeg"  # JPEG-сигнатура (\xff\xd8\xff) и фолбэк по умолчанию


def _upload_photo(token: str, group_id: str, image_bytes: bytes = None, image_url: str = None):
    """Загружает картинку в альбом стены сообщества — готовыми байтами
    (баннер темы, та же картинка, что и в Telegram/Max — см. publish.py),
    если переданы, иначе скачивает по image_url. Возвращает attachment-строку
    "photo{owner_id}_{id}"."""
    if image_bytes is None:
        image_bytes = requests.get(image_url, timeout=30).content
    upload_server = _call("photos.getWallUploadServer", token, group_id=group_id)
    filename, content_type = _sniff_image(image_bytes)
    upload_resp = requests.post(
        upload_server["upload_url"],
        files={"photo": (filename, image_bytes, content_type)},
        timeout=60,
    ).json()
    saved = _call(
        "photos.saveWallPhoto",
        token,
        group_id=group_id,
        photo=upload_resp["photo"],
        server=upload_resp["server"],
        hash=upload_resp["hash"],
    )
    photo = saved[0]
    return f"photo{photo['owner_id']}_{photo['id']}"


def publish(
    token: str,
    group_id: str,
    text: str,
    image_url: str = None,
    image_bytes: bytes = None,
    user_token: str = None,
) -> bool:
    """`token` — токен сообщества, публикует пост (wall.post работает только
    с ним). `user_token` — отдельный пользовательский токен для загрузки
    фото (photos.* работает только с ним, см. docstring модуля); если не
    задан или протух — публикация идёт без картинки, не падает целиком."""
    # У wall.post нет форматирования текста вообще (ни жирного, ни цитат,
    # ни спойлеров) — Telegram HTML разметка черновика иначе показывается
    # сырыми тегами (поймано 31.07.2026). Конвертируем в читаемый plain text.
    text = html_to_plain_text(text)
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1] + "…"

    attachment = None
    if user_token and (image_bytes or image_url):
        try:
            attachment = _upload_photo(user_token, group_id, image_bytes=image_bytes, image_url=image_url)
        except Exception as exc:
            print(f"[WARN] не удалось загрузить картинку в VK — публикую без неё: {exc}", file=sys.stderr)

    try:
        params = {"owner_id": f"-{group_id}", "from_group": 1, "message": text}
        if attachment:
            params["attachments"] = attachment
        _call("wall.post", token, **params)
        return True
    except Exception as exc:
        print(f"[WARN] публикация в VK не удалась: {exc}", file=sys.stderr)
        return False
