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

from common import DATA_DIR, ROOT, fetch_article, generate_cover_image, require_env, today, visible_length, write_json

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

# Картинка должна быть ВМЕСТЕ с текстом одним сообщением (решение 29.07.2026,
# отменяет более раннее «длина не ограничена форматом публикации» от 23.07) —
# значит видимый текст (без HTML-тегов, именно так Telegram считает лимит
# подписи к фото) обязан укладываться в 1024. Небольшой запас на случай
# погрешности подсчёта.
CAPTION_VISIBLE_LIMIT = 1000
MAX_SHORTEN_ATTEMPTS = 3


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


HOOK_RE = re.compile(r"<b>(.+?)</b>")
TAG_RE = re.compile(r"<[^>]+>")

BADGE_LABEL = {
    "дайджест": "ДАЙДЖЕСТ",
    "кейс-с-цифрами": "КЕЙС",
    "лайфхак-инструкция": "ЛАЙФХАК",
    "ai-инструмент": "AI-ИНСТРУМЕНТ",
}


def extract_hook(draft_text: str, fallback_title: str) -> str:
    match = HOOK_RE.search(draft_text)
    hook = match.group(1) if match else fallback_title
    return TAG_RE.sub("", hook).strip()


def build_image_prompt(item: dict, draft_text: str) -> str:
    hook = extract_hook(draft_text, item["title"])
    badge = BADGE_LABEL.get(item.get("rubric", ""), "AI РАДАР")
    return (
        "Create a bold, modern cover image for a Telegram post, in the style of a professional "
        "webinar/conference title card: dark (near-black or dark navy) background with a subtle "
        "gradient or soft glow. "
        f'A small rounded pill-shaped badge near the top-left corner with the text "{badge}" in it. '
        f'Below the badge, the headline in large, bold, bright blue or white sans-serif letters: '
        f'"{hook}" — this exact Russian text must be spelled correctly, clean and perfectly legible, '
        "filling most of the image width. "
        "Clean, modern, professional graphic design, similar to a tech conference webinar cover. "
        "16:9 landscape composition. "
        "STRICT RULES: do not include any real people's photos, faces, or third-party company logos "
        "or brand names anywhere in the image — only the badge text and headline text described above."
    )


def generate_cover(api_key: str, item: dict, draft_text: str) -> Optional[str]:
    """Обложка поста — генерируем сами (не берём с сайта источника: там
    английский текст на картинке и чужие лица, заказчику не подошло), с
    русским заголовком поста прямо на картинке, в стиле референса заказчика.
    Возвращает base64 PNG или None, если генерация не удалась."""
    image_bytes = generate_cover_image(api_key, build_image_prompt(item, draft_text))
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

    # Картинка должна попасть в подпись к фото вместе с текстом одним
    # сообщением — жмём до тех пор, пока видимый текст не влезет в лимит.
    for attempt in range(MAX_SHORTEN_ATTEMPTS):
        vlen = visible_length(text)
        if vlen <= CAPTION_VISIBLE_LIMIT:
            break
        print(f"  черновик {vlen} видимых знаков — сокращаю (попытка {attempt + 1})", file=sys.stderr)
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": (
                f"Это {vlen} знаков видимого текста, а нужно не больше {CAPTION_VISIBLE_LIMIT} — "
                f"иначе картинка не поместится в одно сообщение вместе с текстом. Сократи, "
                f"сохранив хук, спойлер и главную мысль — режь вступление и общие слова в первую очередь."
            ),
        })
        text = call_model(api_key, messages)

    return sanitize_markup(text)


def main():
    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-style.md").read_text(encoding="utf-8")

    approved = load_approved()
    drafted_ids = already_drafted_ids()
    # Без формата (старые записи до этой фичи) — считаем, что «пост» подразумевался.
    pending = [
        item for item in approved
        if item["id"] not in drafted_ids and "пост" in item.get("formats", ["пост"])
    ]

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
        # ИИ-обложка с русским текстом временно выключена — модели ломают
        # кириллицу (см. память проекта). Пока используем og:image источника;
        # generate_cover() остаётся в коде для рендера через HTML/шрифты позже.
        image_url = article.get("image_url")
        print(f"  обложка источника: {'найдена' if image_url else 'не найдена'}", file=sys.stderr)
        drafts.append({
            "id": item["id"],
            "title": item["title"],
            "source": item["source"],
            "link": item["link"],
            "rubric": item.get("rubric", ""),
            "draft_text": text,
            "image_url": image_url,
        })

    out_path = DATA_DIR / "drafts" / f"{today()}.json"
    existing = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else []
    existing.extend(drafts)
    write_json(out_path, existing)
    print(f"Черновиков написано: {len(drafts)} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
