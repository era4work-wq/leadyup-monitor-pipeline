"""Стадии 4 и 7: опрос Telegram на нажатия кнопок согласования.

Запускается часто (см. .github/workflows/poll-approvals.yml). Обрабатывает
два независимых круга кнопок в одной и той же группе:
  - take/skip  — согласование ТЕМ (data/pending/) → data/approved/
  - publish/reject — финальное согласование ГОТОВЫХ ПОСТОВ (data/final_pending/)
    → publish публикует пост в канал (TELEGRAM_CHANNEL) и остаётся в
    data/published/ для истории.
"""
import base64
import os
import sys
from datetime import datetime, timezone

import requests

import publish_max
import publish_vk
from common import DATA_DIR, ROOT, STATE_DIR, require_env, read_json, today, visible_length, write_json
from notify_final import send_final_card
from notify_telegram import FORMAT_ACTION, build_topic_keyboard, tg_send_photo_bytes
from write_draft import get_article, write_one

API_BASE = "https://api.telegram.org/bot{token}/{method}"
LOOKBACK_DAYS = 14

# action -> (папка с карточками, финальные статусы для да/нет)
ACTION_DOMAINS = {
    "take": ("pending", "взято"),
    "skip": ("pending", "пропущено"),
    "publish": ("final_pending", "опубликовано"),
    "reject": ("final_pending", "отклонено"),
    "redo": ("final_pending", None),  # обрабатывается отдельно, не общей веткой
}

FORMAT_BY_ACTION = {v: k for k, v in FORMAT_ACTION.items()}  # "fmtpost" -> "пост"


def tg_call(token: str, method: str, **params):
    url = API_BASE.format(token=token, method=method)
    resp = requests.post(url, json=params, timeout=20)
    if not resp.ok:
        raise RuntimeError(f"Telegram API {method} failed ({resp.status_code}): {resp.text}")
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {body}")
    return body["result"]


def tg_call_safe(token: str, method: str, **params):
    """Как tg_call, но не роняет весь прогон (например, если callback_query
    протух — Telegram отвечает 400, а нам всё равно нужно обработать
    остальные апдейты и сдвинуть offset)."""
    try:
        return tg_call(token, method, **params)
    except Exception as exc:
        print(f"[WARN] {method} не удался: {exc}", file=sys.stderr)
        return None


def load_recent_files(domain: str):
    domain_dir = DATA_DIR / domain
    if not domain_dir.exists():
        return []
    files = sorted(domain_dir.glob("*.json"), reverse=True)
    return files[:LOOKBACK_DAYS]


def find_entry(domain: str, item_id: str):
    for path in load_recent_files(domain):
        data = read_json(path, {})
        if item_id in data and data[item_id]["status"] == "ждёт":
            return path, data
    return None, None


CAPTION_LIMIT = 1024  # лимит Telegram для подписи к фото (у обычных сообщений — 4096)


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


def regenerate_draft(entry: dict) -> dict:
    """Пишет пост по той же теме заново (та же статья — берётся из кэша
    data/articles/, повторно не скачивается)."""
    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-style.md").read_text(encoding="utf-8")
    article = get_article(entry)
    text = write_one(api_key, style, entry, article.get("text"))
    return {**entry, "draft_text": text, "image_url": article.get("image_url")}


def update_draft_record(item_id: str, draft_text: str, cover_image_b64) -> None:
    """Обновляет исторический черновик в data/drafts/ — чтобы там тоже была
    актуальная версия текста и картинки, не только в final_pending."""
    drafts_dir = DATA_DIR / "drafts"
    if not drafts_dir.exists():
        return
    for path in drafts_dir.glob("*.json"):
        items = read_json(path, [])
        changed = False
        for it in items:
            if it.get("id") == item_id:
                it["draft_text"] = draft_text
                it["cover_image_b64"] = cover_image_b64
                changed = True
        if changed:
            write_json(path, items)
            return


def main():
    token = require_env("TELEGRAM_BOT_TOKEN")
    offset_path = STATE_DIR / "tg_offset.txt"
    offset = int(offset_path.read_text().strip()) if offset_path.exists() else 0

    updates = tg_call(token, "getUpdates", offset=offset, timeout=0)
    if not updates:
        print("Новых апдейтов нет.", file=sys.stderr)
        return

    approved_today: list[dict] = []
    published_today: list[dict] = []
    max_update_id = offset - 1

    for update in updates:
        max_update_id = max(max_update_id, update["update_id"])
        callback = update.get("callback_query")
        if not callback:
            print(f"[DEBUG] апдейт без callback_query: {list(update.keys())}", file=sys.stderr)
            continue

        action, _, item_id = callback.get("data", "").partition(":")

        if action in FORMAT_BY_ACTION and item_id:
            fmt = FORMAT_BY_ACTION[action]
            path, data = find_entry("pending", item_id)
            if data is None:
                tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text="Тема уже не найдена")
                continue
            entry = data[item_id]
            formats = set(entry.get("formats", []))
            formats.symmetric_difference_update({fmt})
            entry["formats"] = [f for f in ("пост", "статья", "карусель") if f in formats]
            write_json(path, data)
            tg_call_safe(
                token, "editMessageReplyMarkup",
                chat_id=callback["message"]["chat"]["id"],
                message_id=callback["message"]["message_id"],
                reply_markup=build_topic_keyboard(item_id, entry["formats"]),
            )
            tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text=f"Формат: {fmt}")
            continue

        if action not in ACTION_DOMAINS or not item_id:
            print(f"[DEBUG] callback с нераспознанным data={callback.get('data')!r}", file=sys.stderr)
            continue

        domain, status_word = ACTION_DOMAINS[action]
        path, data = find_entry(domain, item_id)
        if data is None:
            tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text="Карточка уже не найдена (устарела?)")
            continue

        entry = data[item_id]
        approver = callback["from"].get("first_name", "кто-то")
        decided_at = datetime.now(timezone.utc).strftime("%d.%m %H:%M")
        chat_id = callback["message"]["chat"]["id"]

        if action == "redo":
            tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text="Пересобираю пост…")
            try:
                new_entry = regenerate_draft(entry)
            except Exception as exc:
                print(f"[WARN] перегенерация не удалась: {exc}", file=sys.stderr)
                tg_call_safe(
                    token, "sendMessage", chat_id=chat_id,
                    text=f"⚠️ Не удалось перегенерировать «{entry.get('title', '')[:60]}»: {exc}",
                )
                continue
            # старую карточку убираем, чтобы в чате не копились версии одного поста
            tg_call_safe(token, "deleteMessage", chat_id=chat_id, message_id=callback["message"]["message_id"])
            new_card = send_final_card(token, chat_id, new_entry)
            data[item_id] = new_card
            write_json(path, data)
            update_draft_record(item_id, new_entry["draft_text"], new_entry.get("cover_image_b64"))
            print(f"Перегенерирован пост: {entry.get('title', '')[:60]}", file=sys.stderr)
            continue

        platform_results = None
        if action == "publish":
            platform_results = publish_everywhere(token, entry)
            ok = any(platform_results.values())
            status_word = "опубликовано" if ok else "ошибка публикации"

        entry["status"] = status_word
        entry["approver"] = approver
        entry["decided_at"] = decided_at
        write_json(path, data)

        stamp_map = {
            "взято": "✅ Взято",
            "пропущено": "❌ Пропущено",
            "опубликовано": "📤 Опубликовано в канал",
            "ошибка публикации": "⚠️ Ошибка публикации — см. лог",
            "отклонено": "❌ Отклонено",
        }
        stamp = stamp_map[status_word]
        if status_word == "взято" and entry.get("formats"):
            # Видно, под какой именно контент взяли тему — чтобы отследить.
            stamp += " (" + ", ".join(entry["formats"]) + ")"
        if platform_results:
            # Мультиплатформенная публикация — видно, куда реально долетело,
            # а не только общий да/нет (площадки без секретов сюда не попадают).
            done = [name for name, r in platform_results.items() if r]
            failed = [name for name, r in platform_results.items() if not r]
            parts = []
            if done:
                parts.append("📤 Опубликовано: " + ", ".join(done))
            if failed:
                parts.append("⚠️ ошибка: " + ", ".join(failed))
            if parts:
                stamp = " · ".join(parts)
        new_text = f"{entry['text']}\n\n{stamp} · {approver}, {decided_at}"
        if entry.get("sent_as") == "photo":
            # Сообщение с картинкой редактируется через caption, не text
            tg_call_safe(
                token,
                "editMessageCaption",
                chat_id=callback["message"]["chat"]["id"],
                message_id=callback["message"]["message_id"],
                caption=new_text,
                parse_mode="HTML",
                reply_markup={"inline_keyboard": []},
            )
        else:
            tg_call_safe(
                token,
                "editMessageText",
                chat_id=callback["message"]["chat"]["id"],
                message_id=callback["message"]["message_id"],
                text=new_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup={"inline_keyboard": []},
            )
        tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text=stamp)

        if action == "take":
            if not entry.get("formats"):
                entry["formats"] = ["пост"]  # ничего не отмечали — по умолчанию только пост
            approved_today.append(entry)
        if status_word == "опубликовано":
            published_today.append(entry)

    offset_path.parent.mkdir(parents=True, exist_ok=True)
    offset_path.write_text(str(max_update_id + 1))

    if approved_today:
        out_path = DATA_DIR / "approved" / f"{today()}.json"
        existing = read_json(out_path, [])
        existing_ids = {e["id"] for e in existing}
        existing.extend(e for e in approved_today if e["id"] not in existing_ids)
        write_json(out_path, existing)
        print(f"Утверждено тем: {len(approved_today)} → {out_path}", file=sys.stderr)

    if published_today:
        out_path = DATA_DIR / "published" / f"{today()}.json"
        existing = read_json(out_path, [])
        existing_ids = {e["id"] for e in existing}
        existing.extend(e for e in published_today if e["id"] not in existing_ids)
        write_json(out_path, existing)
        print(f"Опубликовано постов: {len(published_today)} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
