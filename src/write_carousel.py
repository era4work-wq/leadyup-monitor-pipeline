"""Стадия 5b: генерация карусели (7 слайдов) по утверждённой теме.

По образцу write_draft.py/write_article.py — тот же источник (get_article,
кэш data/articles/), та же модель, но другой промпт (prompts/write-carousel.md)
и другой результат: не пост и не статья, а обложка + 5 слайдов пользы +
финальный CTA (седьмой слайд статичный, не генерируется — см.
notify_carousel.py). PNG не рендерятся здесь и не хранятся в репозитории —
только текст и метаданные баннера, картинки собираются при отправке.
"""
import json
import re
import sys

import drive_banners
from common import DATA_DIR, ROOT, require_env, today, read_json, write_json
from write_draft import BADGE_LABEL, call_model, get_article, load_approved

CAROUSEL_MAX_TOKENS = 2000
ARTICLE_TEXT_LIMIT = 6000  # столько же, сколько посту — карусели тоже нужны только конкретные факты, не вся статья

JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

HUMANIZE_LABELS = ["COVER_HEADLINE", "COVER_BODY"]
for _i in range(1, 6):
    HUMANIZE_LABELS += [f"SLIDE{_i}_HEADLINE", f"SLIDE{_i}_BODY"]
HUMANIZE_LABELS += ["CTA_HEADLINE", "CTA_BODY"]
LABEL_RE = re.compile(r"^### (\S+)\s*$", re.MULTILINE)


def already_done_ids() -> set[str]:
    drafts_dir = DATA_DIR / "carousel_drafts"
    if not drafts_dir.exists():
        return set()
    ids = set()
    for path in sorted(drafts_dir.glob("*.json")):
        for entry in read_json(path, []):
            ids.add(entry["id"])
    return ids


def parse_carousel_json(raw: str) -> dict:
    match = JSON_BLOCK_RE.search(raw)
    if not match:
        raise ValueError(f"В ответе модели нет JSON: {raw[:200]!r}")
    data = json.loads(match.group(0))
    if "cover_headline" not in data or "cover_body" not in data:
        raise ValueError(f"В JSON нет полей обложки: {list(data.keys())}")
    if len(data.get("slides", [])) != 5:
        raise ValueError(f"Ожидалось 5 слайдов пользы, получено {len(data.get('slides', []))}")
    if "cta_headline" not in data or "cta_body" not in data:
        raise ValueError(f"В JSON нет полей финального CTA-слайда: {list(data.keys())}")
    return data


def humanize_carousel(api_key: str, humanize_prompt: str, data: dict) -> dict:
    """Один проход антидетектора на все короткие тексты карусели разом —
    не 7 отдельных вызовов (дорого, дольше и рискует разъехаться с JSON):
    собираем блок с метками ### ИМЯ, модель переписывает по тем же правилам,
    что и пост (prompts/humanize.md), разбираем обратно по меткам. Если
    ответ не совпал по меткам — используем оригинал, не роняем всю карусель."""
    values = [data["cover_headline"], data["cover_body"]]
    for slide in data["slides"]:
        values += [slide["headline"], slide["body"]]
    values += [data["cta_headline"], data["cta_body"]]
    blob = "\n\n".join(f"### {label}\n{value}" for label, value in zip(HUMANIZE_LABELS, values))

    result = call_model(
        api_key,
        [
            {"role": "system", "content": humanize_prompt},
            {
                "role": "user",
                "content": (
                    "Ниже — короткие тексты для слайдов карусели, каждый под меткой ### ИМЯ. "
                    "Перепиши по тем же правилам (убери ИИ-штампы, сохрани факты и голос), "
                    "верни в ТОЧНО том же формате — те же метки ### ИМЯ, каждая с новым текстом "
                    "под ней, ничего не добавляя и не убирая.\n\n" + blob
                ),
            },
        ],
        max_tokens=CAROUSEL_MAX_TOKENS,
    )

    parts = LABEL_RE.split(result)
    rewritten = {}
    for i in range(1, len(parts), 2):
        rewritten[parts[i]] = parts[i + 1].strip()

    if not all(label in rewritten for label in HUMANIZE_LABELS):
        print("  [WARN] антидетектор карусели вернул не все блоки — оставляю оригинал", file=sys.stderr)
        return data

    data["cover_headline"] = rewritten["COVER_HEADLINE"]
    data["cover_body"] = rewritten["COVER_BODY"]
    for i, slide in enumerate(data["slides"], 1):
        slide["headline"] = rewritten[f"SLIDE{i}_HEADLINE"]
        slide["body"] = rewritten[f"SLIDE{i}_BODY"]
    data["cta_headline"] = rewritten["CTA_HEADLINE"]
    data["cta_body"] = rewritten["CTA_BODY"]
    return data


def write_carousel(api_key: str, style: str, humanize_prompt: str, item: dict, article_text) -> dict:
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
        {"role": "user", "content": "Тема (JSON):\n" + json.dumps(payload, ensure_ascii=False)},
    ]
    raw = call_model(api_key, messages, max_tokens=CAROUSEL_MAX_TOKENS)
    data = parse_carousel_json(raw)
    return humanize_carousel(api_key, humanize_prompt, data)


def build_carousel_record(api_key: str, style: str, humanize_prompt: str, service, item: dict) -> dict:
    """Полный цикл для одной темы — текст + подбор баннера. Используется и
    из main() (первая генерация), и из collect_approvals.py (перегенерация
    по кнопке «Перегенерировать»)."""
    article = get_article(item)
    data = write_carousel(api_key, style, humanize_prompt, item, article.get("text"))

    try:
        banner = drive_banners.get_or_pick_banner(
            service, item,
            headline=data["cover_headline"],
            badge=BADGE_LABEL.get(item.get("rubric", ""), "КАРУСЕЛЬ"),
        )
        # banner['headline']/['badge'] — не обязательно то, что мы предложили:
        # если для темы уже есть кэш от поста/статьи, get_or_pick_banner
        # вернёт УЖЕ зафиксированные значения (см. drive_banners.py), чтобы
        # у всех форматов темы была одна и та же картинка с одним заголовком.
        banner_meta = {
            "id": banner["id"],
            "name": banner["name"],
            "headline": banner.get("headline", data["cover_headline"]),
            "badge": banner.get("badge", "КАРУСЕЛЬ"),
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
    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-carousel.md").read_text(encoding="utf-8")
    humanize_prompt = (ROOT / "prompts" / "humanize.md").read_text(encoding="utf-8")

    approved = load_approved()
    done_ids = already_done_ids()
    pending = [
        item for item in approved
        if item["id"] not in done_ids and "карусель" in item.get("formats", [])
    ]

    if not pending:
        print("Нет утверждённых тем с форматом «карусель» без готового черновика.", file=sys.stderr)
        return

    service = drive_banners.get_service()
    out_path = DATA_DIR / "carousel_drafts" / f"{today()}.json"

    written = 0
    for item in pending:
        print(f"Собираю карусель: {item['title'][:60]}", file=sys.stderr)
        try:
            record = build_carousel_record(api_key, style, humanize_prompt, service, item)
        except Exception as exc:
            print(f"  [WARN] не удалось собрать карусель: {exc}", file=sys.stderr)
            continue
        print(f"  готово: обложка + {len(record['slides'])} слайдов пользы", file=sys.stderr)

        # Пишем сразу после каждой карусели (не батчем в конце) — при
        # сетевом сбое на следующей теме уже готовое не должно теряться.
        existing = read_json(out_path, [])
        existing.append(record)
        write_json(out_path, existing)
        written += 1

    if not written:
        print("Ни одной карусели не собрано.", file=sys.stderr)
        return
    print(f"Каруселей собрано: {written} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
