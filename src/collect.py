"""Стадия 1: сбор RSS + дедуп. Код, без LLM, 0 токенов.

Читает sources.csv (реестр источников), тянет фиды с RSS,
сравнивает с state/seen.json (что уже показывали на отбор),
новое пишет в data/candidates/<дата>.json.
"""
import calendar
import csv
import sys
import time

import feedparser

from common import DATA_DIR, ROOT, STATE_DIR, item_id, read_json, today, write_json

MAX_PER_FEED = 6  # не даём одному источнику залить весь батч
MAX_AGE_DAYS = 4  # свежесть публикации, если дата известна
REQUEST_TIMEOUT = 20


def load_sources():
    sources = []
    with (ROOT / "sources.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rss = (row.get("RSS") or "").strip()
            if rss.startswith("http"):
                sources.append(row)
    return sources


def entry_is_fresh(entry) -> bool:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return True  # нет даты — не отбрасываем, пусть решает select.py
    published_ts = calendar.timegm(parsed)
    age_days = (time.time() - published_ts) / 86400
    return age_days <= MAX_AGE_DAYS


def collect() -> list[dict]:
    seen_path = STATE_DIR / "seen.json"
    seen = read_json(seen_path, {})
    sources = load_sources()
    print(f"Источников с RSS: {len(sources)}", file=sys.stderr)

    candidates = []
    for row in sources:
        name = row["Название"]
        rss_url = row["RSS"].strip()
        try:
            feed = feedparser.parse(rss_url, request_headers={"User-Agent": "Mozilla/5.0 (leadyup-monitor-bot)"})
        except Exception as exc:  # сеть/парсинг — не роняем весь прогон
            print(f"[WARN] {name}: {exc}", file=sys.stderr)
            continue

        if feed.bozo and not feed.entries:
            print(f"[WARN] {name}: фид не распарсился ({feed.bozo_exception})", file=sys.stderr)
            continue

        added = 0
        for entry in feed.entries:
            link = entry.get("link", "").strip()
            title = entry.get("title", "").strip()
            if not link or not title:
                continue
            uid = item_id(link)
            if uid in seen:
                continue
            if not entry_is_fresh(entry):
                continue
            if added >= MAX_PER_FEED:
                break

            summary = (entry.get("summary") or "").strip()
            candidates.append(
                {
                    "id": uid,
                    "title": title,
                    "link": link,
                    "summary": summary[:600],
                    "source": name,
                    "zone": row.get("Зона", ""),
                    "score": row.get("Скор", ""),
                    "priority": row.get("Приоритет", ""),
                }
            )
            seen[uid] = today()
            added += 1

    write_json(seen_path, seen)
    return candidates


def main():
    candidates = collect()
    out_path = DATA_DIR / "candidates" / f"{today()}.json"
    write_json(out_path, candidates)
    print(f"Новых кандидатов: {len(candidates)} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
