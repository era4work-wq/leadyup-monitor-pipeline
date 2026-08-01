"""Стадия 5b (EN-карусель) — независимая генерация EN-карусели (7 слайдов)
для Instagram/Pinterest. По плану роадмапа это была Фаза 4 (после EN-поста
и EN-статей), но владелица 01.08.2026 попросила перенести её вперёд —
Instagram/Pinterest понадобятся раньше Twitter/Threads. Механика та же, что
у write_carousel.py (структурированный JSON, антидетектор одним проходом по
меткам ### ИМЯ — те функции переиспользуются напрямую, они уже
языконезависимы), другой промпт (prompts/write-carousel-en.md, НЕ перевод
RU-версии).

Баннер — тот же принцип, что у write_draft_en.py: фон переиспользуется
(drive_banners.get_or_pick_banner без headline), но headline/badge — свои,
EN, не пишутся поверх общего кэша (он уже зафиксирован RU-версией темы,
если она есть).

Публикация карусели НЕ автоматизирована даже для RU (Instagram/Pinterest
не подключены, см. notify_carousel.py) — EN-версия ничем не отличается:
кнопка «Одобрить» на карточке согласования просто фиксирует статус, как и
у RU. Модуль полностью выключен без EN_CONTENT_ENABLED=true.
"""
import os
import sys

import drive_banners
from common import DATA_DIR, ROOT, require_env, today, read_json, write_json
from write_carousel import write_carousel
from write_draft import get_article, load_approved
from write_draft_en import BADGE_LABEL_EN

DEFAULT_BADGE_CAROUSEL_EN = "CAROUSEL"


def already_done_ids_en() -> set:
    drafts_dir = DATA_DIR / "carousel_drafts_en"
    if not drafts_dir.exists():
        return set()
    ids = set()
    for path in sorted(drafts_dir.glob("*.json")):
        for entry in read_json(path, []):
            ids.add(entry["id"])
    return ids


def build_carousel_record_en(api_key: str, style: str, humanize_prompt: str, service, item: dict) -> dict:
    """Полный цикл для одной темы — текст + подбор баннера. По образцу
    write_carousel.build_carousel_record, но со своим (EN) headline/badge —
    см. docstring модуля про то, почему нельзя переиспользовать headline
    из общего кэша drive_banners напрямую."""
    article = get_article(item)
    data = write_carousel(api_key, style, humanize_prompt, item, article.get("text"))

    try:
        banner = drive_banners.get_or_pick_banner(service, item)
        banner_meta = {
            "id": banner["id"],
            "name": banner["name"],
            "headline": data["cover_headline"],
            "badge": BADGE_LABEL_EN.get(item.get("rubric", ""), DEFAULT_BADGE_CAROUSEL_EN),
        }
    except Exception as exc:
        print(f"  [WARN] не удалось подобрать баннер: {exc}", file=sys.stderr)
        banner_meta = None

    return {
        "id": item["id"],
        "title": item["title"],
        "source": item["source"],
        "link": item["link"],
        "rubric": item.get("rubric", ""),
        "cover_headline": data["cover_headline"],
        "cover_body": data["cover_body"],
        "slides": data["slides"],
        "cta_headline": data["cta_headline"],
        "cta_body": data["cta_body"],
        "banner": banner_meta,
    }


def main():
    if os.environ.get("EN_CONTENT_ENABLED", "").strip().lower() != "true":
        print("EN_CONTENT_ENABLED не включён — EN-модуль пропущен.", file=sys.stderr)
        return

    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-carousel-en.md").read_text(encoding="utf-8")
    humanize_prompt = (ROOT / "prompts" / "humanize-en.md").read_text(encoding="utf-8")

    approved = load_approved()
    done_ids = already_done_ids_en()
    pending = [
        item for item in approved
        if item["id"] not in done_ids and "EN-карусель" in item.get("formats", [])
    ]

    if not pending:
        print("Нет утверждённых тем с форматом «EN-карусель».", file=sys.stderr)
        return

    service = drive_banners.get_service()
    out_path = DATA_DIR / "carousel_drafts_en" / f"{today()}.json"

    written = 0
    for item in pending:
        print(f"Собираю EN-карусель: {item['title'][:60]}", file=sys.stderr)
        try:
            record = build_carousel_record_en(api_key, style, humanize_prompt, service, item)
        except Exception as exc:
            print(f"  [WARN] не удалось собрать EN-карусель: {exc}", file=sys.stderr)
            continue
        print(f"  готово: обложка + {len(record['slides'])} слайдов пользы", file=sys.stderr)

        # Пишем сразу после каждой карусели (не батчем в конце) — по
        # образцу write_carousel.py, чтобы сетевой сбой на следующей теме
        # не терял уже готовое.
        existing = read_json(out_path, [])
        existing.append(record)
        write_json(out_path, existing)
        written += 1

    if not written:
        print("Ни одной EN-карусели не собрано.", file=sys.stderr)
        return
    print(f"EN-каруселей собрано: {written} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
