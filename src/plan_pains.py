"""Стадия 1.5 (еженедельно): контент-план «по болям» — не из мониторинга
внешних источников, а напрямую из CA Research (банк VoC-цитат, встроен в
prompts/plan-pains.md). Отдельный от ежедневного RSS-конвейера поток:
модель сама выбирает N болей на неделю (не повторяя уже использованные,
см. data/pains_used.json), результат по форме — «тема»-подобные записи
(id/title/source/link/why/rubric/persona), совместимые с остальным
пайплайном один в один — collect_approvals.py/write_draft.py и т.д. не
знают и не должны знать, откуда пришла тема.

Ссылка (`link`) всегда leadyup.com, не внешний источник — эти материалы не
дайджест по чужой статье, а прямая демонстрация того, как сервис решает
конкретную боль (решение владелицы, план Фаза 1.5, 29.07.2026).

Запускается раз в неделю (.github/workflows/plan-pains.yml) — согласование
всей недели разом в той же TG-группе, тем же интерфейсом тумблеров формата,
что и у тем из мониторинга (см. notify_pains.py). Дальше — генерация и
финальное согласование идут по расписанию write-drafts.yml как обычно,
никакого отдельного кода для этого не нужно: write_draft.py/write_article.py/
write_carousel.py и их EN-аналоги уже фильтруют по formats/id, им всё равно,
собственник темы — RSS или банк болей.
"""
import json
import re
import sys

import requests

from common import DATA_DIR, ROOT, item_id, load_content_plan, require_env, today, read_json, write_json

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-4.5"  # выбор + короткий why — не творческое письмо, дёшево
MAX_OUTPUT_TOKENS = 2000

ALLOWED_FORMATS = {"пост", "статья", "карусель", "EN-пост", "EN-карусель"}
USED_PATH = DATA_DIR / "pains_used.json"


def load_used_pain_ids() -> list:
    return read_json(USED_PATH, [])


def save_used_pain_ids(used: list) -> None:
    write_json(USED_PATH, used)


def extract_json(text: str):
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"В ответе модели не найден JSON-массив:\n{text[:500]}")
    return json.loads(match.group(0))


def plan_week(api_key: str, used_pain_ids: list, pains_per_week: int) -> list:
    prompt = (ROOT / "prompts" / "plan-pains.md").read_text(encoding="utf-8")
    user_content = (
        f"Уже использованные pain_id (не выбирай их снова, если есть из чего выбрать): "
        f"{json.dumps(used_pain_ids, ensure_ascii=False)}\n"
        f"Сколько болей выбрать на эту неделю: {pains_per_week}"
    )
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
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    text = body["choices"][0]["message"]["content"]
    return extract_json(text)


def build_items(plan: list) -> list:
    items = []
    for entry in plan:
        pain_id = entry.get("pain_id", "")
        if not pain_id:
            print(f"  [WARN] запись без pain_id, пропускаю: {entry}", file=sys.stderr)
            continue
        formats = [f for f in entry.get("suggested_formats", []) if f in ALLOWED_FORMATS]
        items.append({
            "id": item_id(f"pain:{pain_id}:{today()}"),
            "title": entry.get("title", pain_id),
            "source": "Исследование ЦА",
            "link": "https://leadyup.com",
            "why": entry.get("why", ""),
            "rubric": "боль-и-решение",
            "persona": entry.get("persona", ""),
            "suggested_formats": formats,  # notify_pains.py стартует тумблеры с этого набора
            "pain_id": pain_id,  # для отметки в data/pains_used.json, не используется остальным пайплайном
        })
    return items


def main():
    api_key = require_env("OPENROUTER_API_KEY")
    used_pain_ids = load_used_pain_ids()
    pains_per_week = load_content_plan()["pains_per_week"]

    plan = plan_week(api_key, used_pain_ids, pains_per_week)
    if not plan:
        print("Модель не предложила ни одной боли на неделю.", file=sys.stderr)
        return

    items = build_items(plan)
    if not items:
        print("Ни одна запись плана не прошла валидацию.", file=sys.stderr)
        return

    out_path = DATA_DIR / "selected_pains" / f"{today()}.json"
    write_json(out_path, items)

    used_pain_ids.extend(item["pain_id"] for item in items)
    save_used_pain_ids(used_pain_ids)

    print(f"Контент-план на неделю: {len(items)} болей → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
