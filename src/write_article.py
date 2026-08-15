"""Стадия 5c: генерация SEO-статьи для VC.ru по утверждённой теме.

По образцу write_draft.py — те же источники (get_article, кэш data/articles/),
та же модель и второй проход через антидетектор (humanize_draft), но другой
промпт (prompts/write-article.md) и другой результат: не короткий пост, а
статья на 1500-2500 слов, которая не публикуется автоматически (у VC.ru нет
API — см. движок/площадки-и-форматы.md), а сдаётся файлом в чат согласования
через notify_article.py.
"""
import re
import sys

import drive_banners
from common import DATA_DIR, ROOT, require_env, today, read_json, write_json
from common import MARKDOWN_IMAGE_RE
from write_draft import BADGE_LABEL, call_model, get_article, humanize_draft, load_approved

H1_RE = re.compile(r"^#{1,2}\s+(.+)$", re.MULTILINE)  # модель иногда пишет заголовок как ## вместо #

ARTICLE_TEXT_LIMIT = 12000  # статье нужно больше фактуры источника, чем посту
# 1500-2500 слов статьи на русском — это ~6000-8500 токенов вывода, дефолтный
# лимит write_draft.MAX_OUTPUT_TOKENS (1500, рассчитан на короткий пост) тут
# обрежет текст на середине — поднимаем явно для обоих проходов (черновик и антидетектор).
ARTICLE_MAX_TOKENS = 8000


def extract_title(article_text: str, fallback: str) -> str:
    """Заголовок для показа/имени файла — русский H1, который написала
    модель, а не английское название источника (item['title'])."""
    match = H1_RE.search(article_text)
    return match.group(1).strip() if match else fallback


def already_written_ids() -> set[str]:
    drafts_dir = DATA_DIR / "article_drafts"
    if not drafts_dir.exists():
        return set()
    ids = set()
    for path in sorted(drafts_dir.glob("*.json")):
        for entry in read_json(path, []):
            ids.add(entry["id"])
    return ids


def write_article(api_key: str, style: str, humanize_prompt: str, item: dict, article_text) -> str:
    import json

    payload = {
        "title": item["title"],
        "source": item["source"],
        "link": item["link"],
        "why": item.get("why", ""),
        "rubric": item.get("rubric", ""),
        "article_text": (article_text or "")[:ARTICLE_TEXT_LIMIT] or None,
    }
    messages = [
        {"role": "system", "content": style},
        {"role": "user", "content": "Тема (JSON):\n" + json.dumps(payload, ensure_ascii=False)},
    ]
    text = call_model(api_key, messages, max_tokens=ARTICLE_MAX_TOKENS)
    if text.strip() == "НЕДОСТАТОЧНО ФАКТУРЫ":
        return text.strip()
    return humanize_draft(api_key, humanize_prompt, text, max_tokens=ARTICLE_MAX_TOKENS)


def main():
    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-article.md").read_text(encoding="utf-8")
    humanize_prompt = (ROOT / "prompts" / "humanize.md").read_text(encoding="utf-8")

    approved = load_approved()
    done_ids = already_written_ids()
    pending = [
        item for item in approved
        if item["id"] not in done_ids and "статья" in item.get("formats", [])
    ]

    if not pending:
        print("Нет утверждённых тем с форматом «статья» без готового черновика.", file=sys.stderr)
        return

    service = drive_banners.get_service()
    out_path = DATA_DIR / "article_drafts" / f"{today()}.json"

    written = 0
    for item in pending:
        print(f"Пишу статью: {item['title'][:60]}", file=sys.stderr)
        article = get_article(item)
        article_text = article.get("text")
        if not article_text:
            print("  нет текста источника — пропускаю (нельзя писать без фактуры)", file=sys.stderr)
            continue
        text = write_article(api_key, style, humanize_prompt, item, article_text)
        if text == "НЕДОСТАТОЧНО ФАКТУРЫ":
            print("  модель отказалась — фактуры недостаточно", file=sys.stderr)
            continue
        print(f"  готово: {len(text)} знаков", file=sys.stderr)

        title = extract_title(text, item["title"])
        try:
            banner = drive_banners.get_or_pick_banner(
                service, item,
                headline=title,
                badge=BADGE_LABEL.get(item.get("rubric", ""), "СТАТЬЯ"),
            )
            # banner['headline']/['badge'] — не обязательно то, что мы сами
            # предложили: если для темы уже есть кэш от поста/карусели,
            # get_or_pick_banner вернёт УЖЕ зафиксированные значения, чтобы
            # у всех форматов темы была одна и та же картинка с одним и тем
            # же текстом (см. drive_banners.get_or_pick_banner). Рендерится
            # при отправке, не здесь — см. notify_article.py.
            banner_meta = {
                "id": banner["id"],
                "source": banner.get("source", "drive"),
                "name": banner["name"],
                "headline": banner.get("headline", title),
                "badge": banner.get("badge", "СТАТЬЯ"),
            }
        except Exception as exc:
            print(f"  [WARN] не удалось подобрать баннер: {exc}", file=sys.stderr)
            banner_meta = None

        # Реальные картинки из источника, которые модель вставила в статью —
        # чтобы владелица не переходила по markdown-ссылкам вручную, шлём их
        # отдельными фото вместе с файлом (см. notify_article.py).
        image_urls = []
        for src in MARKDOWN_IMAGE_RE.findall(text):
            if src not in image_urls:
                image_urls.append(src)

        # Пишем сразу после каждой статьи (не батчем в конце) — генерация
        # долгая и дорогая (Opus, длинный текст), при сетевом сбое на
        # следующей теме уже готовое не должно теряться.
        existing = read_json(out_path, [])
        existing.append({
            "id": item["id"],
            "title": title,
            "source_title": item["title"],
            "source": item["source"],
            "link": item["link"],
            "article_text": text,
            "banner": banner_meta,
            "image_urls": image_urls,
        })
        write_json(out_path, existing)
        written += 1

    if not written:
        print("Ни одной статьи не написано.", file=sys.stderr)
        return
    print(f"Статей написано: {written} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
