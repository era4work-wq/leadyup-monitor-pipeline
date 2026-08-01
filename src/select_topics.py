"""Стадия 2: отбор кандидатов моделью Claude Haiku по критериям канала.

Дешёвая модель — тут не пишем посты, только фильтруем и ранжируем.
Модель для стадии генерации черновиков выбирается отдельно (см. README).

Модель вызывается через OpenRouter (OpenAI-совместимый REST API), а не
напрямую через Anthropic API — ключ и биллинг общие для всех моделей проекта.
"""
import json
import re
import sys

import requests

from common import DATA_DIR, ROOT, require_env, today, write_json

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Проверить актуальный слаг модели на openrouter.ai/models при активации —
# OpenRouter иногда меняет именование относительно нативных ID Anthropic.
MODEL = "anthropic/claude-haiku-4.5"
MAX_OUTPUT_TOKENS = 4000


def load_candidates() -> list[dict]:
    path = DATA_DIR / "candidates" / f"{today()}.json"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def extract_json(text: str):
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"В ответе модели не найден JSON-массив:\n{text[:500]}")
    return json.loads(match.group(0))


def select(candidates: list[dict]) -> list[dict]:
    criteria = (ROOT / "prompts" / "select-criteria.md").read_text(encoding="utf-8")
    api_key = require_env("OPENROUTER_API_KEY")

    payload = [
        {
            **{k: c[k] for k in ("id", "title", "source", "link", "summary", "priority")},
            "age_days": c.get("age_days"),  # None = дата публикации неизвестна, см. select-criteria.md
        }
        for c in candidates
    ]

    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/leadyup-monitor-pipeline",
            "X-Title": "leadyup-monitor-pipeline",
        },
        json={
            "model": MODEL,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [
                {"role": "system", "content": criteria},
                {
                    "role": "user",
                    "content": "Входные кандидаты (JSON):\n" + json.dumps(payload, ensure_ascii=False),
                },
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    text = body["choices"][0]["message"]["content"]
    return extract_json(text)


def main():
    candidates = load_candidates()
    if not candidates:
        print("Нет новых кандидатов — отбор пропущен.", file=sys.stderr)
        write_json(DATA_DIR / "selected" / f"{today()}.json", [])
        return

    selected = select(candidates)
    out_path = DATA_DIR / "selected" / f"{today()}.json"
    write_json(out_path, selected)
    print(f"Отобрано тем: {len(selected)} из {len(candidates)} кандидатов → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
