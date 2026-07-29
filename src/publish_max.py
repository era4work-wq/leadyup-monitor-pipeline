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


def publish(token: str, chat_id: str, text: str, image_url: str = None) -> bool:
    """Публикует пост в чат/канал MAX. Картинка — по прямому URL (MAX это
    поддерживает без предварительной загрузки, как и sendPhoto в Telegram)."""
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1] + "…"

    attachments = []
    if image_url:
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
