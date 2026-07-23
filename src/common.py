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
