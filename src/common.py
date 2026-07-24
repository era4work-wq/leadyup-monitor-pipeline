import base64
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import trafilatura

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"


def today() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def item_id(link: str) -> str:
    return hashlib.sha256(link.strip().encode("utf-8")).hexdigest()[:16]


def read_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name}")
    return value


OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
OG_IMAGE_RE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE
)


def _extract_og_image(html: str) -> Optional[str]:
    html_head = html[:20000]  # og:image почти всегда в <head>
    match = OG_IMAGE_RE.search(html_head) or OG_IMAGE_RE_REV.search(html_head)
    return match.group(1) if match else None


def fetch_article(url: str) -> dict:
    """Один поход на сайт источника — достаёт и обложку (og:image), и полный
    текст статьи (через trafilatura, чистый текст без меню/рекламы/сайдбаров).
    Best-effort по обоим полям: сайт может блокировать ботов, быть за
    пэйволлом или просто не поддаться экстракции — тогда соответствующее
    поле будет None, а генерация продолжит работать на заголовке+тизере.
    Возвращает {"image_url": str|None, "text": str|None}."""
    result = {"image_url": None, "text": None}
    try:
        resp = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (leadyup-monitor-bot)"},
        )
        if not resp.ok:
            return result
        html = resp.text
        result["image_url"] = _extract_og_image(html)
        try:
            result["text"] = trafilatura.extract(
                html, url=url, include_comments=False, include_tables=False, favor_recall=True
            )
        except Exception:
            result["text"] = None
        return result
    except Exception:
        return result


IMAGE_MODEL = "google/gemini-2.5-flash-image"  # Nano Banana, дешёвая и быстрая


def generate_cover_image(api_key: str, prompt: str, model: str = IMAGE_MODEL) -> Optional[bytes]:
    """Генерирует обложку поста через OpenRouter (тот же ключ, что и для
    текста — просто другая модель). Возвращает сырые байты картинки (PNG)
    или None при любой проблеме — best-effort, публикация без картинки не
    должна падать из-за этого."""
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/leadyup-monitor-pipeline",
                "X-Title": "leadyup-monitor-pipeline",
            },
            json={
                "model": model,
                "modalities": ["image", "text"],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        if not resp.ok:
            print(f"[WARN] generate_cover_image: {resp.status_code} {resp.text[:300]}")
            return None
        body = resp.json()
        images = (body.get("choices") or [{}])[0].get("message", {}).get("images") or []
        if not images:
            print(f"[WARN] generate_cover_image: модель не вернула картинку: {json.dumps(body)[:300]}")
            return None
        data_url = images[0]["image_url"]["url"]  # "data:image/png;base64,...."
        b64_part = data_url.split(",", 1)[1] if "," in data_url else data_url
        return base64.b64decode(b64_part)
    except Exception as exc:
        print(f"[WARN] generate_cover_image упал: {exc}")
        return None
