"""Стадия 4: опрос Telegram на нажатия кнопок согласования.

Запускается часто (см. .github/workflows/poll-approvals.yml). Найденные
решения помечаются в data/pending/<дата>.json и копируются в
data/approved/<дата решения>.json — это вход для будущей стадии генерации
черновиков.
"""
import sys
from datetime import datetime, timezone

import requests

from common import DATA_DIR, STATE_DIR, require_env, read_json, today, write_json

API_BASE = "https://api.telegram.org/bot{token}/{method}"
PENDING_LOOKBACK_DAYS = 7


def tg_call(token: str, method: str, **params):
    url = API_BASE.format(token=token, method=method)
    resp = requests.post(url, json=params, timeout=20)
    resp.raise_for_status()
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


def load_recent_pending_files():
    pending_dir = DATA_DIR / "pending"
    if not pending_dir.exists():
        return []
    files = sorted(pending_dir.glob("*.json"), reverse=True)
    return files[:PENDING_LOOKBACK_DAYS]


def find_pending_entry(item_id: str):
    for path in load_recent_pending_files():
        pending = read_json(path, {})
        if item_id in pending and pending[item_id]["status"] == "ждёт":
            return path, pending
    return None, None


def main():
    token = require_env("TELEGRAM_BOT_TOKEN")
    offset_path = STATE_DIR / "tg_offset.txt"
    offset = int(offset_path.read_text().strip()) if offset_path.exists() else 0

    updates = tg_call(token, "getUpdates", offset=offset, timeout=0)
    if not updates:
        print("Новых апдейтов нет.", file=sys.stderr)
        return

    approved_today: list[dict] = []
    max_update_id = offset - 1

    for update in updates:
        max_update_id = max(max_update_id, update["update_id"])
        callback = update.get("callback_query")
        if not callback:
            continue

        action, _, item_id = callback.get("data", "").partition(":")
        if action not in ("take", "skip") or not item_id:
            continue

        path, pending = find_pending_entry(item_id)
        if pending is None:
            tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text="Тема уже не найдена (устарела?)")
            continue

        entry = pending[item_id]
        approver = callback["from"].get("first_name", "кто-то")
        decided_at = datetime.now(timezone.utc).strftime("%d.%m %H:%M")
        entry["status"] = "взято" if action == "take" else "пропущено"
        entry["approver"] = approver
        entry["decided_at"] = decided_at
        write_json(path, pending)

        stamp = "✅ Взято" if action == "take" else "❌ Пропущено"
        tg_call_safe(
            token,
            "editMessageText",
            chat_id=callback["message"]["chat"]["id"],
            message_id=callback["message"]["message_id"],
            text=f"{entry['text']}\n\n{stamp} · {approver}, {decided_at}",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup={"inline_keyboard": []},
        )
        tg_call_safe(token, "answerCallbackQuery", callback_query_id=callback["id"], text=stamp)

        if action == "take":
            approved_today.append(entry)

    offset_path.parent.mkdir(parents=True, exist_ok=True)
    offset_path.write_text(str(max_update_id + 1))

    if approved_today:
        out_path = DATA_DIR / "approved" / f"{today()}.json"
        existing = read_json(out_path, [])
        existing_ids = {e["id"] for e in existing}
        existing.extend(e for e in approved_today if e["id"] not in existing_ids)
        write_json(out_path, existing)
        print(f"Утверждено тем: {len(approved_today)} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
