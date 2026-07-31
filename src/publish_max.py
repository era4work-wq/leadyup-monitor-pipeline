"""Публикация готового поста в MAX (мессенджер) — тот же текст/картинка,
что уходят в Telegram-канал, просто другая площадка доставки.

REST API MAX: https://platform-api2.max.ru, метод POST /messages.
Токен передаётся в заголовке Authorization (без префикса Bearer).

Важный нюанс инфраструктуры: platform-api2.max.ru отдаёт TLS-сертификат,
подписанный российским НУЦ Минцифры — он не входит в стандартный набор
доверенных корневых сертификатов (в т.ч. на раннерах GitHub Actions).
Поэтому verify= указывает на локальный бандл (certs/russian_trusted_ca_bundle.pem)
именно для этих запросов, а не меняется доверие для всего процесса/остальных
API (OpenRouter, Telegram и т.д.).
"""
import sys

import requests

from common import ROOT

API_BASE = "https://platform-api2.max.ru"
CA_BUNDLE = ROOT / "certs" / "russian_trusted_ca_bundle.pem"
TEXT_LIMIT = 4000  # лимит MAX на длину text в сообщении


def _call(token: str, method: str, path: str, **kwargs):
    url = f"{API_BASE}{path}"
    resp = requests.request(
        method,
        url,
        headers={"Authorization": token, "Content-Type": "application/json"},
        verify=str(CA_BUNDLE),
        timeout=30,
        **kwargs,
    )
    if not resp.ok:
        raise RuntimeError(f"MAX API {path} failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _upload_image(token: str, image_bytes: bytes) -> str:
    """Двухшаговая загрузка байтов (та же схема, что у VK): POST /uploads
    даёт одноразовый upload_url, туда мультипартом заливаются байты (поле
    "data"), в ответ приходит token — им наполняется attachments.payload.
    См. dev.max.ru/docs-api/methods/POST/uploads."""
    upload_info = _call(token, "POST", "/uploads", params={"type": "image"})
    resp = requests.post(
        upload_info["url"],
        files={"data": ("banner.png", image_bytes, "image/png")},
        verify=str(CA_BUNDLE),
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"MAX upload failed ({resp.status_code}): {resp.text}")
    return resp.json()["token"]


def publish(token: str, chat_id: str, text: str, image_url: str = None, image_bytes: bytes = None) -> bool:
    """Публикует пост в чат/канал MAX. Картинка — сначала image_bytes (баннер
    темы, та же картинка, что и в Telegram/VK — см. publish.py), если задан
    и загрузка удалась; иначе image_url по прямому URL (MAX поддерживает
    его без загрузки, как sendPhoto в Telegram)."""
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1] + "…"

    attachments = []
    if image_bytes:
        try:
            img_token = _upload_image(token, image_bytes)
            attachments.append({"type": "image", "payload": {"token": img_token}})
        except Exception as exc:
            print(f"[WARN] загрузка баннера в MAX не удалась: {exc} — пробую og:image", file=sys.stderr)
            if image_url:
                attachments.append({"type": "image", "payload": {"url": image_url}})
    elif image_url:
        attachments.append({"type": "image", "payload": {"url": image_url}})

    body = {"text": text, "format": "html"}
    if attachments:
        body["attachments"] = attachments

    try:
        _call(token, "POST", "/messages", params={"chat_id": chat_id}, json=body)
        return True
    except Exception as exc:
        print(f"[WARN] публикация в MAX не удалась: {exc}", file=sys.stderr)
        return False
