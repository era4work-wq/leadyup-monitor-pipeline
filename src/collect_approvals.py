"""Стадии 4 и 7: опрос Telegram на нажатия кнопок согласования.

Запускается часто (см. .github/workflows/poll-approvals.yml). Обрабатывает
два независимых круга кнопок в одной и той же группе:
  - take/skip  — согласование ТЕМ (data/pending/) → data/approved/
  - publish/reject — финальное согласование ГОТОВЫХ ПОСТОВ (data/final_pending/)
    → publish НЕ публикует сразу, а кладёт запись в data/publish_queue/ —
    реальная отправка на площадки происходит позже, из publish_queue.py, с
    разрядкой по времени (см. .github/workflows/publish-queue.yml).
"""
import json
import os
import sys
from datetime import datetime, timezone

import requests

import drive_banners
import notify_carousel
import notify_carousel_en
from common import DATA_DIR, ROOT, STATE_DIR, require_env, read_json, today, write_json
from notify_final import send_final_card
from notify_final_en import send_final_card_en
from notify_telegram import FORMAT_ACTION, build_decision_edit, build_topic_keyboard
from write_carousel import build_carousel_record
from write_carousel_en import build_carousel_record_en
from write_draft import get_article, write_one
from write_draft_en import write_one_en

API_BASE = "https://api.telegram.org/bot{token}/{method}"
LOOKBACK_DAYS = 14

# action -> (папка с карточками, финальные статусы для да/нет)
ACTION_DOMAINS = {
    "take": ("pending", "взято"),
    "skip": ("pending", "пропущено"),
    "publish": ("final_pending", "в очереди"),
    "reject": ("final_pending", "отклонено"),
    "redo": ("final_pending", None),  # обрабатывается отдельно, не общей веткой
    "approve_car": ("carousel_pending", "утверждено"),
    "reject_car": ("carousel_pending", "отклонено"),
    "redo_car": ("carousel_pending", None),  # обрабатывается отдельно, не общей веткой
    # EN (Фаза 2) — "утверждено" тут терминальный статус, не "в очереди":
    # площадок (Twitter/X, Threads) ещё нет, публикация не подключена.
    "approve_en": ("final_pending_en", "утверждено"),
    "reject_en": ("final_pending_en", "отклонено"),
    "redo_en": ("final_pending_en", None),  # обрабатывается отдельно, не общей веткой
    # EN-карусель (перенесена из Фазы 4 вперёд 01.08.2026) — тоже терминальное
    # "утверждено", не очередь: Instagram/Pinterest не подключены (как и у
    # RU-карусели — публикация карусели вообще не автоматизирована).
    "approve_car_en": ("carousel_pending_en", "утверждено"),
    "reject_car_en": ("carousel_pending_en", "отклонено"),
    "redo_car_en": ("carousel_pending_en", None),  # обрабатывается отдельно, не общей веткой
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


def regenerate_draft(entry: dict) -> dict:
    """Пишет пост по той же теме заново (та же статья — берётся из кэша
    data/articles/, повторно не скачивается)."""
    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-style.md").read_text(encoding="utf-8")
    humanize_prompt = (ROOT / "prompts" / "humanize.md").read_text(encoding="utf-8")
    article = get_article(entry)
    text = write_one(api_key, style, humanize_prompt, entry, article.get("text"))
    return {**entry, "draft_text": text, "image_url": article.get("image_url")}


def regenerate_draft_en(entry: dict) -> dict:
    """Пишет EN-пост по той же теме заново (по образцу regenerate_draft) —
    баннер (и его EN headline/badge) не трогаем, остаётся зафиксированным
    с первой генерации, как и у RU-поста."""
    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-style-en.md").read_text(encoding="utf-8")
    humanize_prompt = (ROOT / "prompts" / "humanize-en.md").read_text(encoding="utf-8")
    article = get_article(entry)
    _headline, post = write_one_en(api_key, style, humanize_prompt, entry, article.get("text"))
    return {**entry, "draft_text": post, "image_url": article.get("image_url")}


def regenerate_carousel(entry: dict) -> dict:
    """Пересобирает карусель по той же теме заново (та же статья — берётся
    из кэша data/articles/, повторно не скачивается), по образцу
    regenerate_draft() выше."""
    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-carousel.md").read_text(encoding="utf-8")
    humanize_prompt = (ROOT / "prompts" / "humanize.md").read_text(encoding="utf-8")
    service = drive_banners.get_service()
    return build_carousel_record(api_key, style, humanize_prompt, service, entry)


def regenerate_carousel_en(entry: dict) -> dict:
    """Пересобирает EN-карусель по той же теме заново, по образцу
    regenerate_carousel()."""
    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-carousel-en.md").read_text(encoding="utf-8")
    humanize_prompt = (ROOT / "prompts" / "humanize-en.md").read_text(encoding="utf-8")
    service = drive_banners.get_service()
    return build_carousel_record_en(api_key, style, humanize_prompt, service, entry)


def update_draft_record(item_id: str, draft_text: str, banner) -> None:
    """Обновляет исторический черновик в data/drafts/ — чтобы там тоже была
    актуальная версия текста и баннера, не только в final_pending."""
    drafts_dir = DATA_DIR / "drafts"
    if not drafts_dir.exists():
        return
    for path in drafts_dir.glob("*.json"):
        items = read_json(path, [])
        changed = False
        for it in items:
            if it.get("id") == item_id:
                it["draft_text"] = draft_text
                it["banner"] = banner
                changed = True
        if changed:
            write_json(path, items)
            return


def main():
    token = require_env("TELEGRAM_BOT_TOKEN")

    # Мгновенный путь: webhook-relay/ пересылает сюда апдейт в момент клика
    # (см. .github/workflows/poll-approvals.yml, workflow_dispatch inputs) —
    # тогда getUpdates вообще не вызываем (Telegram и не отдаст ничего через
    # getUpdates, пока у бота настроен webhook — эти два режима исключают
    # друг друга). offset_path=None ниже отключает обновление offset — он
    # больше не актуален для доставки апдейтов, только исторический артефакт.
    injected = os.environ.get("TELEGRAM_UPDATE_JSON", "").strip()
    offset_path = None
    if injected:
        updates = [json.loads(injected)]
        max_update_id = 0
    else:
        offset_path = STATE_DIR / "tg_offset.txt"
        offset = int(offset_path.read_text().strip()) if offset_path.exists() else 0
        updates = tg_call(token, "getUpdates", offset=offset, timeout=0)
        if not updates:
            print("Новых апдейтов нет.", file=sys.stderr)
            return
        max_update_id = offset - 1

    approved_today: list[dict] = []
    queued_today: list[dict] = []

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
            entry["formats"] = [f for f in ("пост", "статья", "карусель", "EN-пост", "EN-карусель") if f in formats]
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
            new_card = send_final_card(token, chat_id, new_entry, drive_banners.get_service())
            data[item_id] = new_card
            write_json(path, data)
            update_draft_record(item_id, new_entry["draft_text"], new_entry.get("banner"))
            print(f"Перегенерирован пост: {entry.get('title', '')[:60]}", file=sys.stderr)
            continue

        if action == "redo_car":
            tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text="Пересобираю карусель…")
            try:
                new_entry = regenerate_carousel(entry)
            except Exception as exc:
                print(f"[WARN] перегенерация карусели не удалась: {exc}", file=sys.stderr)
                tg_call_safe(
                    token, "sendMessage", chat_id=chat_id,
                    text=f"⚠️ Не удалось перегенерировать карусель «{entry.get('title', '')[:60]}»: {exc}",
                )
                continue
            # старый альбом (7 фото) и старую карточку с кнопками убираем,
            # чтобы в чате не копились версии одной карусели
            for mid in entry.get("photo_message_ids", []):
                tg_call_safe(token, "deleteMessage", chat_id=chat_id, message_id=mid)
            tg_call_safe(token, "deleteMessage", chat_id=chat_id, message_id=callback["message"]["message_id"])
            new_card = notify_carousel.send_carousel(token, chat_id, new_entry, drive_banners.get_service())
            data[item_id] = new_card
            write_json(path, data)
            print(f"Перегенерирована карусель: {entry.get('title', '')[:60]}", file=sys.stderr)
            continue

        if action == "redo_car_en":
            tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text="Пересобираю EN-карусель…")
            try:
                new_entry = regenerate_carousel_en(entry)
            except Exception as exc:
                print(f"[WARN] перегенерация EN-карусели не удалась: {exc}", file=sys.stderr)
                tg_call_safe(
                    token, "sendMessage", chat_id=chat_id,
                    text=f"⚠️ Не удалось перегенерировать EN-карусель «{entry.get('title', '')[:60]}»: {exc}",
                )
                continue
            for mid in entry.get("photo_message_ids", []):
                tg_call_safe(token, "deleteMessage", chat_id=chat_id, message_id=mid)
            tg_call_safe(token, "deleteMessage", chat_id=chat_id, message_id=callback["message"]["message_id"])
            new_card = notify_carousel_en.send_carousel_en(token, chat_id, new_entry, drive_banners.get_service())
            data[item_id] = new_card
            write_json(path, data)
            print(f"Перегенерирована EN-карусель: {entry.get('title', '')[:60]}", file=sys.stderr)
            continue

        if action == "redo_en":
            tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text="Пересобираю EN-пост…")
            try:
                new_entry = regenerate_draft_en(entry)
            except Exception as exc:
                print(f"[WARN] перегенерация EN-поста не удалась: {exc}", file=sys.stderr)
                tg_call_safe(
                    token, "sendMessage", chat_id=chat_id,
                    text=f"⚠️ Не удалось перегенерировать EN-пост «{entry.get('title', '')[:60]}»: {exc}",
                )
                continue
            tg_call_safe(token, "deleteMessage", chat_id=chat_id, message_id=callback["message"]["message_id"])
            new_card = send_final_card_en(token, chat_id, new_entry, drive_banners.get_service())
            data[item_id] = new_card
            write_json(path, data)
            print(f"Перегенерирован EN-пост: {entry.get('title', '')[:60]}", file=sys.stderr)
            continue

        if action == "publish":
            # Не публикуем сразу — кладём в очередь, реальная отправка идёт
            # позже из publish_queue.py, с разрядкой по времени (см. решение
            # в плане: клик остаётся триггером, но без мгновенного залпа и
            # без «раз в сутки по расписанию»).
            status_word = "в очереди"

        entry["status"] = status_word
        entry["approver"] = approver
        entry["decided_at"] = decided_at
        write_json(path, data)

        stamp_map = {
            "взято": "✅ Взято",
            "пропущено": "❌ Пропущено",
            "в очереди": "🕐 В очереди на публикацию",
            "отклонено": "❌ Отклонено",
            "утверждено": "✅ Утверждено",
        }
        stamp = stamp_map[status_word]
        if status_word == "взято" and entry.get("formats"):
            # Видно, под какой именно контент взяли тему — чтобы отследить.
            stamp += " (" + ", ".join(entry["formats"]) + ")"
        new_text = f"{entry['text']}\n\n{stamp} · {approver}, {decided_at}"
        method, params = build_decision_edit(
            callback["message"]["chat"]["id"],
            callback["message"]["message_id"],
            entry.get("sent_as", "text"),
            new_text,
        )
        tg_call_safe(token, method, **params)
        tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text=stamp)

        if action == "take":
            if not entry.get("formats"):
                entry["formats"] = ["пост"]  # ничего не отмечали — по умолчанию только пост
            approved_today.append(entry)
        if status_word == "в очереди":
            queued_today.append({
                **entry,
                "status": "queued",
                "chat_id": callback["message"]["chat"]["id"],
                "message_id": callback["message"]["message_id"],
            })

    if offset_path is not None:
        offset_path.parent.mkdir(parents=True, exist_ok=True)
        offset_path.write_text(str(max_update_id + 1))

    if approved_today:
        out_path = DATA_DIR / "approved" / f"{today()}.json"
        existing = read_json(out_path, [])
        existing_ids = {e["id"] for e in existing}
        existing.extend(e for e in approved_today if e["id"] not in existing_ids)
        write_json(out_path, existing)
        print(f"Утверждено тем: {len(approved_today)} → {out_path}", file=sys.stderr)

    if queued_today:
        out_path = DATA_DIR / "publish_queue" / f"{today()}.json"
        existing = read_json(out_path, [])
        existing_ids = {e["id"] for e in existing}
        existing.extend(e for e in queued_today if e["id"] not in existing_ids)
        write_json(out_path, existing)
        print(f"Поставлено в очередь на публикацию: {len(queued_today)} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
