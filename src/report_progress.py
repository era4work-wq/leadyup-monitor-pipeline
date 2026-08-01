"""Еженедельный отчёт о выполнении контент-плана (content-plan.json) —
запускается тем же workflow, что и plan_pains.py (перед новым планом на
неделю, чтобы сразу было видно и как прошла предыдущая, и что предлагается
дальше). Считает ФАКТ с начала текущего календарного месяца по данным,
уже лежащим в data/ (никакого отдельного учёта не ведётся — годится и для
ручного запуска в любой день), сравнивает с целями из content-plan.json.

Что считается «фактом» по каждому формату — ближайшая надёжная веха этого
формата, не обязательно «дошло до читателя»:
  - статьи — data/article_sent/ (status="отправлено") — доставлены файлом,
    дальше публикация вручную, это конец того, что делает пайплайн;
  - посты RU — data/publish_queue/ (status="published") — реально ушли на
    площадки;
  - посты EN — data/final_pending_en/ (status="утверждено") — площадок ещё
    нет, дальше некуда публиковать, это терминальное состояние (см.
    notify_final_en.py); отдельной цели в content-plan.json пока нет,
    считается informationally;
  - карусели RU/EN — data/carousel_pending*/ (status="утверждено") —
    Instagram/Pinterest не подключены ни для RU, ни для EN, это тоже
    терминальное состояние на сегодня.
"""
import calendar
import sys
from datetime import date

from common import DATA_DIR, load_content_plan, require_env, read_json
from notify_telegram import tg_call

PAINS_SOURCE = "Исследование ЦА"


def month_bounds() -> tuple[str, int, int]:
    today_date = date.today()
    days_in_month = calendar.monthrange(today_date.year, today_date.month)[1]
    return today_date.strftime("%Y-%m"), today_date.day, days_in_month


def load_month_entries(domain: str, month_prefix: str) -> list:
    """Все записи из data/<domain>/<месяц>-*.json — файлы дневные/недельные,
    но всегда именем начинаются с даты, месяц вычленяется по префиксу имени."""
    domain_dir = DATA_DIR / domain
    if not domain_dir.exists():
        return []
    entries = []
    for path in sorted(domain_dir.glob(f"{month_prefix}-*.json")):
        data = read_json(path, {})
        values = data.values() if isinstance(data, dict) else data
        entries.extend(values)
    return entries


def is_pains(entry: dict) -> bool:
    return entry.get("source") == PAINS_SOURCE


def count_by_status(entries: list, status: str) -> list:
    return [e for e in entries if e.get("status") == status]


def line(label: str, actual: int, expected: float, extra: str = "") -> str:
    diff = actual - expected
    if diff >= 0:
        mark = "✅"
    elif diff >= -1:
        mark = "🟡"
    else:
        mark = "🔴"
    expected_str = f"{expected:.1f}".rstrip("0").rstrip(".")
    return f"{mark} {label}: факт {actual}, к сегодня ожидалось ~{expected_str}{extra}"


def build_report() -> str:
    month_prefix, day_of_month, days_in_month = month_bounds()
    plan = load_content_plan()
    pace = day_of_month / days_in_month

    articles = count_by_status(load_month_entries("article_sent", month_prefix), "отправлено")
    articles_pains = [a for a in articles if is_pains(a)]
    articles_monitoring = [a for a in articles if not is_pains(a)]
    art_target = plan["articles_per_month"]
    art_mon_expected = art_target["monitoring"] * pace
    art_pains_expected = art_target["pains"] * pace

    posts_ru = count_by_status(load_month_entries("publish_queue", month_prefix), "published")
    posts_ru_pains = [p for p in posts_ru if is_pains(p)]
    posts_target_month = plan["posts_per_day"] * days_in_month
    posts_expected = posts_target_month * pace

    posts_en = count_by_status(load_month_entries("final_pending_en", month_prefix), "утверждено")
    posts_en_pains = [p for p in posts_en if is_pains(p)]

    car_ru = count_by_status(load_month_entries("carousel_pending", month_prefix), "утверждено")
    car_ru_pains = [c for c in car_ru if is_pains(c)]
    car_target_month = plan["carousels_per_day"] * days_in_month
    car_expected = car_target_month * pace

    car_en = count_by_status(load_month_entries("carousel_pending_en", month_prefix), "утверждено")
    car_en_pains = [c for c in car_en if is_pains(c)]

    lines = [
        f"📊 Контент-план — статус на {date.today().strftime('%d.%m')} (день {day_of_month}/{days_in_month} месяца)",
        "",
        line(
            "Статьи (мониторинг)", len(articles_monitoring), art_mon_expected,
            f" из {art_target['monitoring']}/мес",
        ),
        line(
            "Статьи (боли)", len(articles_pains), art_pains_expected,
            f" из {art_target['pains']}/мес",
        ),
        "",
        line(
            "Посты RU (опубликовано)", len(posts_ru), posts_expected,
            f" из ~{posts_target_month}/мес, по болям {len(posts_ru_pains)}",
        ),
        f"ℹ️ Посты EN (утверждено, ждут площадок): {len(posts_en)}, из них по болям {len(posts_en_pains)}",
        "",
        line(
            "Карусели RU (утверждено)", len(car_ru), car_expected,
            f" из ~{car_target_month}/мес, по болям {len(car_ru_pains)}",
        ),
        f"ℹ️ Карусели EN (утверждено, ждут площадок): {len(car_en)}, из них по болям {len(car_en_pains)}",
    ]
    return "\n".join(lines)


def main():
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    report = build_report()
    tg_call(token, "sendMessage", chat_id=chat_id, text=report)
    print(report, file=sys.stderr)


if __name__ == "__main__":
    main()
