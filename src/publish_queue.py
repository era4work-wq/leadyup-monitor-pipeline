"""Стадия 8: публикация из очереди с разрядкой по времени.

Запускается часто (см. .github/workflows/publish-queue.yml), но публикует
НЕ БОЛЬШЕ ОДНОЙ записи за запуск, и только если сейчас дневное окно и с
прошлой публикации прошло достаточно времени — так несколько постов,
одобренных подряд, не улетают в канал единым залпом, а размазываются по
последующим проверкам. Клик «Опубликовать» в collect_approvals.py только
кладёт запись в data/publish_queue/, реальную отправку делает этот скрипт.
"""
import sys
from datetime import datetime, timedelta, timezone

from common import DATA_DIR, STATE_DIR, read_json, require_env, write_json
from notify_telegram import build_decision_edit
from publish import publish_everywhere, tg_call_safe

LOOKBACK_DAYS = 14
WINDOW_START_UTC = 6   # 09:00 МСК
WINDOW_END_UTC = 18    # 21:00 МСК
MIN_INTERVAL = timedelta(hours=2)

LAST_PUBLISH_PATH = STATE_DIR / "last_publish.json"


def in_daytime_window(now: datetime) -> bool:
    return WINDOW_START_UTC <= now.hour < WINDOW_END_UTC


def enough_time_passed(now: datetime) -> bool:
    last = read_json(LAST_PUBLISH_PATH, {}).get("at")
    if not last:
        return True
    return now - datetime.fromisoformat(last) >= MIN_INTERVAL


def find_next_queued():
    """Самая старая ещё не опубликованная запись — старые файлы (дни)
    вперёд, внутри файла — в порядке появления."""
    queue_dir = DATA_DIR / "publish_queue"
    if not queue_dir.exists():
        return None, None, None
    for path in sorted(queue_dir.glob("*.json"))[-LOOKBACK_DAYS:]:
        items = read_json(path, [])
        for i, item in enumerate(items):
            if item.get("status") == "queued":
                return path, items, i
    return None, None, None


def format_publish_stamp(results: dict) -> str:
    done = [name for name, r in results.items() if r]
    failed = [name for name, r in results.items() if not r]
    parts = []
    if done:
        parts.append("📤 Опубликовано: " + ", ".join(done))
    if failed:
        parts.append("⚠️ ошибка: " + ", ".join(failed))
    return " · ".join(parts) if parts else "⚠️ Ошибка публикации — см. лог"


def main():
    token = require_env("TELEGRAM_BOT_TOKEN")
    now = datetime.now(timezone.utc)

    if not in_daytime_window(now):
        print(f"Не дневное окно ({now.hour}:00 UTC) — жду.", file=sys.stderr)
        return
    if not enough_time_passed(now):
        print("С прошлой публикации прошло меньше минимального интервала — жду.", file=sys.stderr)
        return

    path, items, i = find_next_queued()
    if path is None:
        print("Очередь пуста.", file=sys.stderr)
        return

    entry = items[i]
    results = publish_everywhere(token, entry)
    ok = any(results.values())
    stamp = format_publish_stamp(results)

    new_text = f"{entry['text']}\n\n{stamp} · публикация {now.strftime('%d.%m %H:%M')}"
    method, params = build_decision_edit(
        entry["chat_id"], entry["message_id"], entry.get("sent_as", "text"), new_text,
    )
    tg_call_safe(token, method, **params)

    items[i]["status"] = "published" if ok else "ошибка"
    write_json(path, items)

    if ok:
        write_json(LAST_PUBLISH_PATH, {"at": now.isoformat()})
        print(f"Опубликовано: {entry.get('title', '')[:60]} → {list(results.keys())}", file=sys.stderr)
    else:
        print(f"[WARN] Публикация не удалась: {entry.get('title', '')[:60]}", file=sys.stderr)


if __name__ == "__main__":
    main()
