"""Публикация готового поста в ВК-группу — тот же текст/картинка, что и в
Telegram/MAX. Пока не активирован: группа ещё не создана (см. память
проекта), нужен токен сообщества с scope "wall" (ВК выдаёт его не всем
токенам по умолчанию — может потребоваться заявка в поддержку ВК при
создании группы). collect_approvals.py вызывает publish() только если
VK_GROUP_TOKEN/VK_GROUP_ID заданы, иначе платформа тихо пропускается.

VK REST API, https://api.vk.com/method/. Публикация фото на стену — три
шага (в отличие от Telegram/MAX, просто URL картинки не принимается):
  1. photos.getWallUploadServer — получить upload_url
  2. multipart-загрузка байтов картинки на upload_url
  3. photos.saveWallPhoto — сохранить как фото сообщества, получить id
Затем wall.post с attachments=photo{owner_id}_{photo_id}.
"""
import sys

import requests

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


def _upload_photo(token: str, group_id: str, image_url: str):
    """Загружает картинку по image_url в альбом стены сообщества.
    Возвращает attachment-строку "photo{owner_id}_{id}" или None при неудаче."""
    upload_server = _call("photos.getWallUploadServer", token, group_id=group_id)
    image_bytes = requests.get(image_url, timeout=30).content
    upload_resp = requests.post(
        upload_server["upload_url"],
        files={"photo": ("cover.jpg", image_bytes, "image/jpeg")},
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


def publish(token: str, group_id: str, text: str, image_url: str = None) -> bool:
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1] + "…"

    attachment = None
    if image_url:
        try:
            attachment = _upload_photo(token, group_id, image_url)
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
