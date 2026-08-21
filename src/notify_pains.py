"""Стадия 3b (еженедельно): отправка контент-плана «по болям» на согласование.

По образцу notify_telegram.py — тот же формат карточки (format_message) и
та же клавиатура тумблеров (build_topic_keyboard), только: (1) источник —
data/selected_pains/, не data/selected/; (2) стартовые тумблеры формата —
уже ПРЕДЛОЖЕННЫЕ моделью (item['suggested_formats']), не пустые, владелица
только подтверждает/меняет; (3) пишет в data/pending/<дата>.json СЛИЯНИЕМ
(как и notify_telegram.py после фикса) — этот скрипт запускается раз в
неделю, RSS-мониторинг ежедневно, в день пересечения оба пишут в один файл.
"""
import sys

from common import DATA_DIR, require_env, today, read_json, write_json
from notify_telegram import build_topic_keyboard, format_message, tg_call


def main():
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    selected_path = DATA_DIR / "selected_pains" / f"{today()}.json"
    selected = read_json(selected_path, [])
    if not selected:
        print("Нет контент-плана на эту неделю.", file=sys.stderr)
        return

    tg_call(token, "sendMessage", chat_id=chat_id, text=f"💡 Контент-план на неделю: {len(selected)} болей")

    new_pending = {}
    for item in selected:
        text = format_message(item)
        formats = item.get("suggested_formats", [])
        keyboard = build_topic_keyboard(item["id"], formats, rubric=item.get("rubric"))
        result = tg_call(
            token, "sendMessage", chat_id=chat_id, text=text,
            parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard,
        )
        new_pending[item["id"]] = {
            **item,
            "message_id": result["message_id"],
            "text": text,
            "formats": formats,
            "status": "ждёт",
        }

    pending_path = DATA_DIR / "pending" / f"{today()}.json"
    existing = read_json(pending_path, {})
    existing.update(new_pending)
    write_json(pending_path, existing)
    print(f"Отправлено на согласование: {len(new_pending)} болей.", file=sys.stderr)


if __name__ == "__main__":
    main()
