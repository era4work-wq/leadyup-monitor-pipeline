"""Стадия 5: генерация черновика поста по утверждённой теме.

Берёт темы из data/approved/*.json, которых ещё нет в data/drafts/,
тянет полный текст статьи-источника (кэшируется в data/articles/ — этим же
кэшем позже смогут пользоваться другие форматы: статьи, карусели и т.д.,
не перекачивая страницу заново), пишет пост моделью через OpenRouter по
промпту prompts/write-style.md, результат — data/drafts/<дата>.json.
"""
import base64
import json
import re
import sys
from typing import Optional

import requests

from common import DATA_DIR, ROOT, fetch_article, generate_cover_image, require_env, today, write_json

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Модель для написания текста — не для отбора (там Haiku). Пробуем Opus
# вместо Sonnet 23.07 — жалобы на связность/странные формулировки на длинных
# постах, собранных из полной статьи.
MODEL = "anthropic/claude-opus-4.5"
MAX_OUTPUT_TOKENS = 1500
LOOKBACK_DAYS = 14

MARKDOWN_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def sanitize_markup(text: str) -> str:
    """Модель периодически пишет markdown (**жирный**) вместо Telegram HTML
    (<b>жирный</b>), несмотря на прямой запрет в промпте — Telegram это не
    рендерит, показывает звёздочки как есть. Не полагаемся только на то, что
    модель послушается инструкции — чиним принудительно."""
    return MARKDOWN_BOLD_RE.sub(r"<b>\1</b>", text)

ARTICLE_TEXT_LIMIT = 6000  # символов текста статьи, которые кладём в промпт

# Не таргет по длине (её теперь определяет смысл, не формат публикации) —
# только защита от совсем убежавшего вывода модели.
SANITY_LIMIT = 2200
MAX_SHORTEN_ATTEMPTS = 1


def load_approved() -> list[dict]:
    approved_dir = DATA_DIR / "approved"
    if not approved_dir.exists():
        return []
    items = []
    for path in sorted(approved_dir.glob("*.json")):
        items.extend(json.loads(path.read_text(encoding="utf-8")))
    return items


def already_drafted_ids() -> set[str]:
    drafts_dir = DATA_DIR / "drafts"
    if not drafts_dir.exists():
        return set()
    ids = set()
    for path in sorted(drafts_dir.glob("*.json")):
        for entry in json.loads(path.read_text(encoding="utf-8")):
            ids.add(entry["id"])
    return ids


def get_article(item: dict) -> dict:
    """Полный текст + обложка статьи, с кэшем в data/articles/<id>.json —
    чтобы карусели/статьи позже могли переиспользовать тот же текст, не
    перекачивая страницу заново."""
    cache_path = DATA_DIR / "articles" / f"{item['id']}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    article = fetch_article(item["link"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({**article, "link": item["link"], "title": item["title"]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return article


def call_model(api_key: str, messages: list[dict]) -> str:
    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/leadyup-monitor-pipeline",
            "X-Title": "leadyup-monitor-pipeline",
        },
        json={"model": MODEL, "max_tokens": MAX_OUTPUT_TOKENS, "messages": messages},
        timeout=90,
    )
    response.raise_for_status()
    body = response.json()
    return body["choices"][0]["message"]["content"].strip()


def build_image_prompt(item: dict) -> str:
    return (
        "Create a clean, modern, abstract conceptual illustration to use as a cover image "
        f"for a social media post about: {item['title']}. "
        "Style: minimalist tech/marketing illustration, soft gradients and simple geometric "
        "shapes, professional and modern, suitable for a Telegram channel about AI and marketing. "
        "STRICT RULES: absolutely no text, letters, numbers, captions or logos anywhere in the "
        "image. No realistic human faces or recognizable people."
    )


def generate_cover(api_key: str, item: dict) -> Optional[str]:
    """Обложка поста — генерируем сами (не берём с сайта источника: там
    английский текст на картинке и чужие лица, заказчику не подошло).
    Возвращает base64 PNG или None, если генерация не удалась."""
    image_bytes = generate_cover_image(api_key, build_image_prompt(item))
    return base64.b64encode(image_bytes).decode("ascii") if image_bytes else None


def write_one(api_key: str, style: str, item: dict, article_text: Optional[str]) -> str:
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
    text = call_model(api_key, messages)

    # Защита не от превышения таргета длины (его больше нет), а от совсем
    # убежавшего вывода — на всякий случай, не должно срабатывать часто.
    for attempt in range(MAX_SHORTEN_ATTEMPTS):
        if len(text) <= SANITY_LIMIT:
            break
        print(f"  черновик {len(text)} знаков — явный перебор, прошу сократить", file=sys.stderr)
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": f"Это слишком длинно ({len(text)} знаков) даже для содержательного поста. Сократи примерно вдвое, сохранив главную мысль.",
        })
        text = call_model(api_key, messages)

    return sanitize_markup(text)


def main():
    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-style.md").read_text(encoding="utf-8")

    approved = load_approved()
    drafted_ids = already_drafted_ids()
    pending = [item for item in approved if item["id"] not in drafted_ids]

    if not pending:
        print("Нет утверждённых тем без черновика.", file=sys.stderr)
        return

    drafts = []
    for item in pending:
        print(f"Пишу черновик: {item['title'][:60]}", file=sys.stderr)
        article = get_article(item)
        article_text = article.get("text")
        print(
            f"  статья: {'{} знаков'.format(len(article_text)) if article_text else 'не вытащилась'}",
            file=sys.stderr,
        )
        text = write_one(api_key, style, item, article_text)
        cover_b64 = generate_cover(api_key, item)
        print(f"  обложка: {'сгенерирована' if cover_b64 else 'не удалось'}", file=sys.stderr)
        drafts.append({
            "id": item["id"],
            "title": item["title"],
            "source": item["source"],
            "link": item["link"],
            "rubric": item.get("rubric", ""),
            "draft_text": text,
            "cover_image_b64": cover_b64,
        })

    out_path = DATA_DIR / "drafts" / f"{today()}.json"
    existing = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else []
    existing.extend(drafts)
    write_json(out_path, existing)
    print(f"Черновиков написано: {len(drafts)} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
