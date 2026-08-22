"""Стадия 3: отправка отобранных тем на согласование в приватную TG-группу.

Каждая тема — отдельное сообщение с кнопками [Взять] [Пропустить].
message_id сохраняется в data/pending/<дата>.json, чтобы collect_approvals.py
знал, какое сообщение редактировать после решения.
"""
import json
import os
import sys

import requests

from common import DATA_DIR, require_env, today, read_json, write_json

API_BASE = "https://api.telegram.org/bot{token}/{method}"

RUBRIC_LABEL = {
    "дайджест": "📰 дайджест",
    "кейс-с-цифрами": "📊 кейс с цифрами",
    "лайфхак-инструкция": "🛠 лайфхак",
    "ai-инструмент": "🤖 AI-инструмент",
    "боль-и-решение": "💡 боль и решение",
}


def tg_call(token: str, method: str, **params):
    url = API_BASE.format(token=token, method=method)
    resp = requests.post(url, json=params, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {body}")
    return body["result"]


def tg_send_photo_bytes(token: str, chat_id, image_bytes: bytes, **params):
    """sendPhoto с картинкой как двоичными данными (не URL) — нужна
    multipart-загрузка, не обычный JSON-запрос. reply_markup, если передан,
    сериализуем в JSON-строку вручную — так требует Telegram в multipart."""
    url = API_BASE.format(token=token, method="sendPhoto")
    data = {"chat_id": chat_id}
    for key, value in params.items():
        if value is None:
            continue
        data[key] = json.dumps(value) if isinstance(value, (dict, list)) else value
    files = {"photo": ("cover.png", image_bytes, "image/png")}
    resp = requests.post(url, data=data, files=files, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"Telegram API sendPhoto(bytes) failed ({resp.status_code}): {resp.text}")
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API sendPhoto(bytes) failed: {body}")
    return body["result"]


FORMAT_ORDER = ["пост", "статья", "карусель"]
FORMAT_EMOJI = {"пост": "📝", "статья": "📄", "карусель": "🎠"}
FORMAT_ACTION = {"пост": "fmtpost", "статья": "fmtarticle", "карусель": "fmtcarousel"}

# EN-форматы (Фаза 2/4) — независимая генерация, не перевод (см.
# write_draft_en.py/write_carousel_en.py), отдельный необязательный модуль
# (план 30.07.2026). В отличие от RU-форматов у них нет дефолта "не
# отмечено — значит пост": берутся в работу только по явному тумблеру.
# Отдельный ряд клавиатуры, не смешиваем с RU-рядом, чтобы не расползалось.
FORMAT_ORDER_EN = ["EN-пост", "EN-карусель"]
FORMAT_EMOJI_EN = {"EN-пост": "🇬🇧", "EN-карусель": "🇬🇧🎠"}
FORMAT_ACTION_EN = {"EN-пост": "fmtenpost", "EN-карусель": "fmtencarousel"}

FORMAT_EMOJI.update(FORMAT_EMOJI_EN)
FORMAT_ACTION.update(FORMAT_ACTION_EN)
ALL_FORMATS = FORMAT_ORDER + FORMAT_ORDER_EN


def _format_button(fmt: str, item_id: str, selected: set) -> dict:
    mark = "✅ " if fmt in selected else ""
    # EN-метки уже в нужном регистре — .capitalize() испортил бы "EN" до "En"
    label = fmt if fmt.startswith("EN-") else fmt.capitalize()
    return {
        "text": f"{mark}{FORMAT_EMOJI[fmt]} {label}",
        "callback_data": f"{FORMAT_ACTION[fmt]}:{item_id}",
    }


def build_topic_keyboard(item_id: str, selected_formats: list, rubric: str = None) -> dict:
    """Тумблеры контента (можно выбрать несколько или ни одного — по
    умолчанию берём «пост» при взятии в работу), плюс обычное решение по
    теме.

    «Статья» скрыта для тем НЕ из rubric=«боль-и-решение» (решение
    16.08.2026 — длинные статьи только по болям, из мониторинга больше не
    делаем ни одной; тему из мониторинга физически нельзя утвердить как
    статью, только пост/карусель). Плюс скрыта целиком, если модуль статей
    выключен секретом ARTICLES_ENABLED (решение 22.08.2026 — статьи теперь
    отдельная включаемая линия контента, как EN/pains, а не всегда-доступный
    формат).

    EN-ряд убран из клавиатуры (решение 22.08.2026 — этот репозиторий
    теперь RU-only машина, английский переезжает в отдельный международный
    проект). EN-генерация (write_draft_en.py и т.д.) в коде осталась
    нетронутой, просто отсюда её больше нельзя вызвать тумблером — если
    понадобится вернуть специально для этого репозитория, это одна строка
    (вернуть en_row в inline_keyboard ниже), не переписывание с нуля.

    «Карусель» скрыта целиком, если модуль выключен секретом
    CAROUSELS_ENABLED — тот же паттерн, что у статей (решение 22.08.2026)."""
    articles_enabled = os.environ.get("ARTICLES_ENABLED", "").strip().lower() == "true"
    carousels_enabled = os.environ.get("CAROUSELS_ENABLED", "").strip().lower() == "true"
    statya_allowed = articles_enabled and rubric == "боль-и-решение"
    ru_formats = [
        f for f in FORMAT_ORDER
        if not (f == "статья" and not statya_allowed)
        and not (f == "карусель" and not carousels_enabled)
    ]
    selected = set(selected_formats or [])
    ru_row = [_format_button(fmt, item_id, selected) for fmt in ru_formats]
    return {
        "inline_keyboard": [
            ru_row,
            [
                {"text": "✅ Взять в работу", "callback_data": f"take:{item_id}"},
                {"text": "❌ Пропустить", "callback_data": f"skip:{item_id}"},
            ],
        ]
    }


def tg_send_document_bytes(token: str, chat_id, filename: str, content_bytes: bytes, **params):
    """sendDocument с файлом как двоичными данными — по образцу
    tg_send_photo_bytes, но для произвольного файла (например .md-статьи).
    Используется, когда контент должен прийти именно файлом, не текстом
    сообщения (решение владелицы 29.07.2026 — для статей)."""
    url = API_BASE.format(token=token, method="sendDocument")
    data = {"chat_id": chat_id}
    for key, value in params.items():
        if value is None:
            continue
        data[key] = json.dumps(value) if isinstance(value, (dict, list)) else value
    files = {"document": (filename, content_bytes, "text/markdown")}
    resp = requests.post(url, data=data, files=files, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"Telegram API sendDocument failed ({resp.status_code}): {resp.text}")
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API sendDocument failed: {body}")
    return body["result"]


def tg_send_media_group_bytes(token: str, chat_id, photos: list[bytes], caption: str = None) -> list:
    """sendMediaGroup — альбом фото как двоичные данные (не URL), для
    карусели (7 слайдов одним альбомом, см. notify_carousel.py). Подпись,
    если передана, идёт только на первом фото — так работает Telegram API,
    у альбома нет общей подписи. Возвращает список message-объектов, по
    одному на фото — их message_id нужны, чтобы потом удалить альбом целиком
    при перегенерации (см. redo_car в collect_approvals.py)."""
    url = API_BASE.format(token=token, method="sendMediaGroup")
    media = []
    files = {}
    for i, photo in enumerate(photos):
        key = f"photo{i}"
        entry = {"type": "photo", "media": f"attach://{key}"}
        if i == 0 and caption:
            entry["caption"] = caption
            entry["parse_mode"] = "HTML"
        media.append(entry)
        files[key] = (f"{key}.png", photo, "image/png")
    data = {"chat_id": chat_id, "media": json.dumps(media)}
    resp = requests.post(url, data=data, files=files, timeout=120)
    if not resp.ok:
        raise RuntimeError(f"Telegram API sendMediaGroup failed ({resp.status_code}): {resp.text}")
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API sendMediaGroup failed: {body}")
    return body["result"]


def build_decision_edit(chat_id, message_id, sent_as: str, text: str):
    """Собирает (метод, параметры) для правки карточки решения (взято/в
    очереди/опубликовано/отклонено) — редактирует caption у фото или text у
    обычного сообщения, убирает кнопки. Вызывающий сам решает, каким tg_call
    это отправить (обычным или *_safe) — переиспользуется и в
    collect_approvals.py (сразу после клика), и в publish_queue.py (после
    реальной публикации из очереди)."""
    if sent_as == "photo":
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []},
        }
        return "editMessageCaption", params
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": []},
    }
    return "editMessageText", params


def format_message(item: dict) -> str:
    rubric = RUBRIC_LABEL.get(item.get("rubric", ""), item.get("rubric", ""))
    persona_line = f"Кому: {item['persona']}\n" if item.get("persona") else ""
    return (
        f"<b>{item['title']}</b>\n"
        f"Источник: {item['source']} · {rubric}\n"
        f"{persona_line}"
        f"{item.get('why', '')}\n"
        f"{item['link']}"
    )


def main():
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    selected_path = DATA_DIR / "selected" / f"{today()}.json"
    selected = read_json(selected_path, [])
    if not selected:
        print("Нечего отправлять на согласование сегодня.", file=sys.stderr)
        return

    tg_call(token, "sendMessage", chat_id=chat_id, text=f"Кандидаты на {today()}: {len(selected)} тем")

    pending = {}
    for item in selected:
        text = format_message(item)
        keyboard = build_topic_keyboard(item["id"], [], rubric=item.get("rubric"))
        result = tg_call(
            token,
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        pending[item["id"]] = {
            **item,
            "message_id": result["message_id"],
            "text": text,
            "formats": [],
            "status": "ждёт",
        }

    # Слияние, не перезапись — plan_pains.py/notify_pains.py (еженедельно)
    # может писать в тот же дневной файл в тот же день, что и этот скрипт
    # (ежедневно). Блайндная перезапись стёрла бы уже отправленные записи.
    pending_path = DATA_DIR / "pending" / f"{today()}.json"
    existing = read_json(pending_path, {})
    existing.update(pending)
    write_json(pending_path, existing)
    print(f"Отправлено на согласование: {len(pending)} тем.", file=sys.stderr)


if __name__ == "__main__":
    main()
