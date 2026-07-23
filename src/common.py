import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import requests

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


def fetch_og_image(url: str) -> Optional[str]:
    """Достаёт obложку статьи (og:image) для более привлекательного поста.
    Best-effort: сайт может не отдать og:image, заблокировать бота или
    ответить с ошибкой — в этом случае просто публикуем без картинки."""
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (leadyup-monitor-bot)"},
        )
        if not resp.ok:
            return None
        html_head = resp.text[:20000]  # og:image почти всегда в <head>
        match = OG_IMAGE_RE.search(html_head) or OG_IMAGE_RE_REV.search(html_head)
        return match.group(1) if match else None
    except Exception:
        return None
