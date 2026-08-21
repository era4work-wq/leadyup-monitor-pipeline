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
import requests
from lxml import html

from common import DATA_DIR, ROOT, STATE_DIR, item_id, read_json, today, write_json

MAX_PER_FEED = 6  # не даём одному источнику залить весь батч
MAX_AGE_DAYS = 4  # свежесть публикации, если дата известна
REQUEST_TIMEOUT = 20
TELEGRAM_TITLE_LIMIT = 100  # у поста в Telegram нет заголовка — отрезаем первую часть текста под заголовок


def load_sources():
    sources = []
    with (ROOT / "sources.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rss = (row.get("RSS") or "").strip()
            if rss.startswith("http"):
                sources.append(row)
    return sources


def entry_age_days(entry):
    """Возраст публикации в днях, или None, если дата в фиде не указана.
    Раньше возврат None означал «не отбрасываем, пусть решает select.py» —
    но select.py никогда не получал саму дату, только не глядя пропускал
    (поймано на практике 01.08.2026: недатированный «вечнозелёный» материал
    с внутренней статистикой за прошлый год прошёл отбор как «свежий»).
    Теперь возраст (или его отсутствие) явно передаётся дальше в кандидате —
    решение принимается на основе данных, а не по умолчанию."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    published_ts = calendar.timegm(parsed)
    return (time.time() - published_ts) / 86400


def entry_is_fresh(age_days) -> bool:
    if age_days is None:
        return True  # дата неизвестна — не отбрасываем на этом шаге, но передаём как есть, см. entry_age_days
    return age_days <= MAX_AGE_DAYS


def is_telegram_source(url: str) -> bool:
    return "t.me/s/" in url


def fetch_telegram_channel(url: str) -> list[dict]:
    """Публичный веб-превью Telegram-канала (t.me/s/<канал>) — без бота, без
    токена, просто HTML-страница, доступная кому угодно. Решение владелицы
    16.08.2026: у большинства нужных RU-сервисов (Авито, Ozon, VK Реклама и
    т.д.) в 2026 году нет RSS вообще, только официальные Telegram-каналы —
    вместо Bot API (туда нужны права админа канала, которых у нас нет)
    читаем ту же публичную страницу, что видит браузер без логина.

    Отдаёт entries в том же виде, что и feedparser (title/link/summary/
    published_parsed) — дальше по пайплайну (entry_age_days, дедуп, сборка
    кандидата) ничего не должно знать, что источник не RSS.

    Репосты (`tgme_widget_message_forwarded_from`) пропускаются — это уже
    не первоисточник, канал просто ретранслирует чужую новость (правило
    владелицы: только то, что компания публикует от своего имени)."""
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (leadyup-monitor-bot)"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    tree = html.fromstring(resp.text)
    messages = tree.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " tgme_widget_message ")]')

    entries = []
    for m in messages:
        if m.xpath('.//div[contains(@class,"tgme_widget_message_forwarded_from")]'):
            continue
        text_el = m.xpath('.//div[contains(@class,"tgme_widget_message_text")]')
        text = text_el[0].text_content().strip() if text_el else ""
        if not text:
            continue  # пост без текста (только фото/видео) — нечего оценивать на отборе
        date_el = m.xpath('.//a[contains(@class,"tgme_widget_message_date")]/time')
        link_el = m.xpath('.//a[contains(@class,"tgme_widget_message_date")]')
        if not date_el or not link_el:
            continue
        dt_str = date_el[0].get("datetime")  # "2026-08-20T09:38:40+00:00" — Telegram отдаёт в UTC
        try:
            published_parsed = time.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            published_parsed = None

        if len(text) <= TELEGRAM_TITLE_LIMIT:
            title, summary = text, ""
        else:
            cut = text.rfind(" ", 0, TELEGRAM_TITLE_LIMIT)
            if cut == -1:
                cut = TELEGRAM_TITLE_LIMIT
            title, summary = text[:cut].strip(), text[cut:].strip()

        entries.append({
            "title": title,
            "link": link_el[0].get("href"),
            "summary": summary,
            "published_parsed": published_parsed,
        })
    return entries


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
            if is_telegram_source(rss_url):
                entries = fetch_telegram_channel(rss_url)
            else:
                feed = feedparser.parse(rss_url, request_headers={"User-Agent": "Mozilla/5.0 (leadyup-monitor-bot)"})
                if feed.bozo and not feed.entries:
                    print(f"[WARN] {name}: фид не распарсился ({feed.bozo_exception})", file=sys.stderr)
                    continue
                entries = feed.entries
        except Exception as exc:  # сеть/парсинг — не роняем весь прогон
            print(f"[WARN] {name}: {exc}", file=sys.stderr)
            continue

        added = 0
        for entry in entries:
            link = entry.get("link", "").strip()
            title = entry.get("title", "").strip()
            if not link or not title:
                continue
            uid = item_id(link)
            if uid in seen:
                continue
            age_days = entry_age_days(entry)
            if not entry_is_fresh(age_days):
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
                    "age_days": round(age_days, 1) if age_days is not None else None,
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
