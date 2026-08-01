"""Стадия 5b (EN, Фаза 2): независимая генерация англоязычного поста под
Twitter/X и Threads — по той же утверждённой теме, что и русский пост, но
НЕ перевод: свой вызов модели по prompts/write-style-en.md на тех же данных
(article_text/why из общего кэша data/articles/), свой проход-антидетектор
(prompts/humanize-en.md, свой набор ИИ-маркеров под английский, не копия
русского). Решение владелицы 30.07.2026 — EN должен быть отдельным,
необязательным модулем, не переделывающим RU-часть пайплайна.

Модуль полностью выключен, если не задан секрет EN_CONTENT_ENABLED=true —
как и с площадками Max/VK на Фазе 1, отсутствие включения — не ошибка, а
ожидаемое состояние (аккаунты Twitter/X и Threads ещё не готовы, см. план).

Модель просят вернуть ДВЕ строки: `HEADLINE:` (короткий заголовок под
баннер) и `POST:` (сам текст поста, plain text, БЕЗ HTML/markdown — в
отличие от RU-версии, у Twitter/Threads нет рендера разметки вообще, как и
у ВК, поэтому черновик пишется сразу как plain text, конвертация не нужна).
Ссылка на источник в EN-посте остаётся (в RU её убрали по просьбе владелицы
29.07.2026 — см. prompts/write-style.md, п.5 «Структура поста» — правило
касается только русской версии).

Баннер — тот же фон, что уже подобран для темы (drive_banners.get_or_pick_banner
без явного headline: кэш общий на все форматы, и первый формат, который его
заполнил, фиксирует RU-заголовок как канонический — см. docstring
get_or_pick_banner). Поэтому здесь НЕ переиспользуется headline из кэша —
он был бы русским. Вместо этого рендерится свой оверлей с EN headline/badge
поверх того же фонового файла, без повторного выбора картинки и без записи
поверх общего кэша.
"""
import json
import os
import re
import sys
from typing import Optional, Tuple

import drive_banners
from common import DATA_DIR, ROOT, require_env, today, visible_length, read_json, write_json
from write_draft import ARTICLE_TEXT_LIMIT, call_model, get_article, humanize_draft, load_approved

POST_LEN_LIMIT = 260  # см. prompts/write-style-en.md — потолок под Twitter/X, с запасом
MAX_SHORTEN_ATTEMPTS = 3

HEADLINE_RE = re.compile(r"HEADLINE:\s*(.+)")
POST_RE = re.compile(r"POST:\s*(.+)", re.DOTALL)

BADGE_LABEL_EN = {
    "дайджест": "DIGEST",
    "кейс-с-цифрами": "CASE STUDY",
    "лайфхак-инструкция": "HOW-TO",
    "ai-инструмент": "AI TOOL",
    "боль-и-решение": "SOLUTION",
}
DEFAULT_BADGE_EN = "NEWS"


def parse_response(text: str) -> Tuple[str, str]:
    """Разбирает ответ модели в формате `HEADLINE:`/`POST:` (см.
    write-style-en.md). Если модель не выдержала формат (редко, но
    best-effort — не должно ронять весь прогон) — весь ответ идёт в POST,
    а HEADLINE достраивается обрезкой первых слов."""
    headline_match = HEADLINE_RE.search(text)
    post_match = POST_RE.search(text)
    post = post_match.group(1).strip() if post_match else text.strip()
    if headline_match:
        headline = headline_match.group(1).strip()
    else:
        headline = post[:50].rsplit(" ", 1)[0] if len(post) > 50 else post
    return headline, post


def write_one_en(api_key: str, style: str, humanize_prompt: str, item: dict, article_text: Optional[str]) -> Tuple[str, str]:
    payload = {
        "title": item["title"],
        "source": item["source"],
        "link": item["link"],
        "why": item.get("why", ""),
        "rubric": item.get("rubric", ""),
        "persona": item.get("persona", ""),
        "article_text": (article_text or "")[:ARTICLE_TEXT_LIMIT] or None,
    }
    messages = [
        {"role": "system", "content": style},
        {"role": "user", "content": "Topic (JSON):\n" + json.dumps(payload, ensure_ascii=False)},
    ]
    text = call_model(api_key, messages)
    text = humanize_draft(api_key, humanize_prompt, text)
    headline, post = parse_response(text)

    for attempt in range(MAX_SHORTEN_ATTEMPTS):
        vlen = visible_length(post)
        if vlen <= POST_LEN_LIMIT:
            break
        print(f"  EN-пост {vlen} знаков — сокращаю (попытка {attempt + 1})", file=sys.stderr)
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": (
                f"POST is {vlen} visible characters, needs to be under {POST_LEN_LIMIT} — shorten it, "
                "keep the hook and the link, cut filler first. Keep the exact HEADLINE:/POST: format."
            ),
        })
        text = call_model(api_key, messages)
        headline, post = parse_response(text)

    return headline, post


def get_post_banner_en(service, item: dict, headline_en: str) -> Optional[dict]:
    """Тот же фоновый файл, что уже выбран для темы (общий кэш
    data/banners/), но со своим EN headline/badge — см. docstring модуля."""
    banner = drive_banners.get_or_pick_banner(service, item)
    badge = BADGE_LABEL_EN.get(item.get("rubric", ""), DEFAULT_BADGE_EN)
    return {
        "id": banner["id"],
        "name": banner["name"],
        "headline": headline_en,
        "badge": badge,
    }


def already_drafted_ids_en() -> set[str]:
    drafts_dir = DATA_DIR / "drafts_en"
    if not drafts_dir.exists():
        return set()
    ids = set()
    for path in sorted(drafts_dir.glob("*.json")):
        for entry in read_json(path, []):
            ids.add(entry["id"])
    return ids


def main():
    if os.environ.get("EN_CONTENT_ENABLED", "").strip().lower() != "true":
        print("EN_CONTENT_ENABLED не включён — EN-модуль пропущен.", file=sys.stderr)
        return

    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-style-en.md").read_text(encoding="utf-8")
    humanize_prompt = (ROOT / "prompts" / "humanize-en.md").read_text(encoding="utf-8")
    service = drive_banners.get_service()

    approved = load_approved()
    drafted_ids = already_drafted_ids_en()
    # В отличие от RU-поста, нет дефолта "если формат не указан" — EN только
    # по явному тумблеру "🇬🇧 EN-пост" в согласовании темы (см. notify_telegram.py).
    pending = [item for item in approved if item["id"] not in drafted_ids and "EN-пост" in item.get("formats", [])]

    if not pending:
        print("Нет утверждённых тем с форматом EN-пост.", file=sys.stderr)
        return

    drafts = []
    for item in pending:
        print(f"Пишу EN-пост: {item['title'][:60]}", file=sys.stderr)
        article = get_article(item)
        article_text = article.get("text")
        headline, post = write_one_en(api_key, style, humanize_prompt, item, article_text)
        try:
            banner = get_post_banner_en(service, item, headline)
            print(f"  баннер: {banner['name']}", file=sys.stderr)
        except Exception as exc:
            print(f"  [WARN] не удалось подобрать баннер: {exc}", file=sys.stderr)
            banner = None
        drafts.append({
            "id": item["id"],
            "title": item["title"],
            "source": item["source"],
            "link": item["link"],
            "rubric": item.get("rubric", ""),
            "draft_text": post,
            "headline": headline,
            "banner": banner,
            "image_url": article.get("image_url"),
        })

    out_path = DATA_DIR / "drafts_en" / f"{today()}.json"
    existing = read_json(out_path, [])
    existing.extend(drafts)
    write_json(out_path, existing)
    print(f"EN-черновиков написано: {len(drafts)} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
