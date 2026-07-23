"""Стадия 5: генерация черновика поста по утверждённой теме.

Берёт темы из data/approved/*.json, которых ещё нет в data/drafts/,
пишет пост моделью через OpenRouter по промпту prompts/write-style.md,
результат — data/drafts/<дата>.json.
"""
import json
import sys

import requests

from common import DATA_DIR, ROOT, fetch_og_image, require_env, today, write_json

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Модель для написания текста — не для отбора (там Haiku). Sonnet 5 по
# умолчанию; для сложных рубрик можно вручную попробовать Opus 4.8.
MODEL = "anthropic/claude-sonnet-4.5"
MAX_OUTPUT_TOKENS = 1200
LOOKBACK_DAYS = 14

# Лимит подписи к фото в Telegram — 1024. 800 задаём моделью в промпте,
# но модель на практике промахивается — код подстраховывает: если готовый
# текст всё равно длиннее CAPTION_SAFE_LIMIT, просим модель сократить.
CAPTION_SAFE_LIMIT = 950
MAX_SHORTEN_ATTEMPTS = 2


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


def write_one(api_key: str, style: str, item: dict) -> str:
    payload = {
        "title": item["title"],
        "source": item["source"],
        "link": item["link"],
        "why": item.get("why", ""),
        "rubric": item.get("rubric", ""),
        "persona": item.get("persona", ""),
    }
    messages = [
        {"role": "system", "content": style},
        {"role": "user", "content": "Тема (JSON):\n" + json.dumps(payload, ensure_ascii=False)},
    ]
    text = call_model(api_key, messages)

    # Подстраховка: промпт просит уложиться в 800 знаков, но модель иногда
    # промахивается — если черновик всё равно длиннее безопасного лимита
    # подписи к фото, просим сократить явно, вместо того чтобы публиковать
    # без картинки.
    for attempt in range(MAX_SHORTEN_ATTEMPTS):
        if len(text) <= CAPTION_SAFE_LIMIT:
            break
        print(f"  черновик {len(text)} знаков — прошу сократить (попытка {attempt + 1})", file=sys.stderr)
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": (
                f"Слишком длинно — {len(text)} знаков, а лимит подписи к фото в Telegram — 1024. "
                f"Сократи этот же пост до {CAPTION_SAFE_LIMIT} знаков или меньше: убери один пункт "
                "списка и/или спойлер, но сохрани хук, блок-цитату и общий смысл. "
                "Верни только сокращённый текст поста, без пояснений."
            ),
        })
        text = call_model(api_key, messages)

    return text


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
        text = write_one(api_key, style, item)
        image_url = fetch_og_image(item["link"])
        print(f"  обложка: {'найдена' if image_url else 'не найдена'}", file=sys.stderr)
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
