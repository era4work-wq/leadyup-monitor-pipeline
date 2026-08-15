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

import drive_banners
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
    перекачивая страницу заново.

    Темы «по болям» (plan_pains.py, rubric «боль-и-решение») не привязаны к
    внешней статье — их link указывает на leadyup.com, не на источник с
    фактурой. Тянуть его через fetch_article было бы неправильно: модель
    получила бы обрывки собственного лендинга как «факты источника» вместо
    настоящего материала (а «почему» уже полностью задан в why из
    plan-pains.md). Для таких тем article_text всегда пустой."""
    cache_path = DATA_DIR / "articles" / f"{item['id']}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    if item.get("rubric") == "боль-и-решение":
        article = {"image_url": None, "image_urls": [], "text": None}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({**article, "link": item["link"], "title": item["title"]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return article

    article = fetch_article(item["link"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({**article, "link": item["link"], "title": item["title"]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return article


def call_model(api_key: str, messages: list[dict], max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    # Длинные ответы (статьи, max_tokens в тысячах) иногда обрываются на
    # сетевом уровне (ChunkedEncodingError) — чаще на локальной машине
    # (Python 3.9 / LibreSSL), но и в CI сеть не идеальна. Пара повторов
    # дешевле, чем ронять весь прогон из-за одного обрыва потока.
    last_exc = None
    for attempt in range(3):
        try:
            response = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://github.com/leadyup-monitor-pipeline",
                    "X-Title": "leadyup-monitor-pipeline",
                },
                json={"model": MODEL, "max_tokens": max_tokens, "messages": messages},
                timeout=180,
            )
            response.raise_for_status()
            body = response.json()
            return body["choices"][0]["message"]["content"].strip()
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            print(f"  [WARN] запрос к модели не удался (попытка {attempt + 1}/3): {exc}", file=sys.stderr)
    raise last_exc


HOOK_RE = re.compile(r"<b>(.+?)</b>")
TAG_RE = re.compile(r"<[^>]+>")

BADGE_LABEL = {
    "дайджест": "ДАЙДЖЕСТ",
    "кейс-с-цифрами": "КЕЙС",
    "лайфхак-инструкция": "ЛАЙФХАК",
    "ai-инструмент": "AI-ИНСТРУМЕНТ",
    "боль-и-решение": "РЕШЕНИЕ",
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


def humanize_draft(api_key: str, humanize_prompt: str, text: str, max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    """Второй проход — вычищает признаки нейросетевого текста (адаптация
    скилла «Антидетектор», Георгий Ривера, prompts/humanize.md), сохраняя
    факты и разметку (Telegram HTML для постов, Markdown для статей)
    нетронутыми. max_tokens нужно поднимать для длинных статей — иначе
    вывод обрезается на дефолтном лимите поста."""
    messages = [
        {"role": "system", "content": humanize_prompt},
        {"role": "user", "content": text},
    ]
    return call_model(api_key, messages, max_tokens=max_tokens)


def write_one(api_key: str, style: str, humanize_prompt: str, item: dict, article_text: Optional[str]) -> str:
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
    text = humanize_draft(api_key, humanize_prompt, text)

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


def get_post_banner(service, item: dict, draft_text: str) -> Optional[dict]:
    """Тот же общий кэш баннера/заголовка, что у статьи и карусели темы
    (drive_banners.get_or_pick_banner) — пост больше не единственный формат
    без наложенного заголовка. Возвращает None, если подбор не удался
    (Drive недоступен и т.п.) — тогда notify_final.py падает обратно на
    og:image источника."""
    hook = extract_hook(draft_text, item["title"])
    badge = BADGE_LABEL.get(item.get("rubric", ""), "AI РАДАР")
    banner = drive_banners.get_or_pick_banner(service, item, headline=hook, badge=badge)
    return {
        "id": banner["id"],
        "source": banner.get("source", "drive"),
        "name": banner["name"],
        "headline": banner.get("headline", hook),
        "badge": banner.get("badge", badge),
    }


def main():
    api_key = require_env("OPENROUTER_API_KEY")
    style = (ROOT / "prompts" / "write-style.md").read_text(encoding="utf-8")
    humanize_prompt = (ROOT / "prompts" / "humanize.md").read_text(encoding="utf-8")
    service = drive_banners.get_service()

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
        text = write_one(api_key, style, humanize_prompt, item, article_text)
        # ИИ-обложка с русским текстом временно выключена — модели ломают
        # кириллицу (см. память проекта); generate_cover() остаётся в коде
        # неактивным на будущее. Основной путь теперь — общий баннер темы
        # (drive_banners), тот же, что у статьи и карусели; image_url
        # (og:image источника) остаётся как запасной вариант, если подбор
        # баннера не удался (Drive недоступен и т.п.).
        try:
            banner = get_post_banner(service, item, text)
            print(f"  баннер: {banner['name']}", file=sys.stderr)
        except Exception as exc:
            print(f"  [WARN] не удалось подобрать баннер: {exc}", file=sys.stderr)
            banner = None
        image_url = article.get("image_url")
        drafts.append({
            "id": item["id"],
            "title": item["title"],
            "source": item["source"],
            "link": item["link"],
            "rubric": item.get("rubric", ""),
            "draft_text": text,
            "banner": banner,
            "image_url": image_url,
        })

    out_path = DATA_DIR / "drafts" / f"{today()}.json"
    existing = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else []
    existing.extend(drafts)
    write_json(out_path, existing)
    print(f"Черновиков написано: {len(drafts)} → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
