"""Подбор и учёт баннеров-фонов из Google Drive (БАННЕРЫ/<тема>/).

Использованный баннер переезжает в подпапку <тема>/использовано/ — это и
есть учёт: не нужен отдельный файл-реестр, в самом Drive сразу видно, что
ещё свежее (лежит в корне темы), а что уже пошло в дело (в "использовано").
Владелица ориентируется по тому же признаку, когда сама смотрит на Диск.
"""
import sys
from pathlib import Path

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

from common import DATA_DIR, read_json, require_env, write_json

SERVICE_ACCOUNT_FILE = Path(__file__).resolve().parent.parent / "secrets" / "drive-service-account.json"
USED_FOLDER_NAME = "использовано"
# БАННЕРЫ/ внутри leadyup-генерация — см. память drive-service-account-banners.
BANNERS_ROOT_ID = "10VdhlkEV_qdPJZ_qMjiwVFvrUHM60G1h"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-4.5"  # дёшево — просто сопоставить тему с названием папки


def get_service():
    creds = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT_FILE),
        scopes=["https://www.googleapis.com/auth/drive"],  # не readonly — нужно двигать файлы
    )
    return build("drive", "v3", credentials=creds)


def _list_children(service, folder_id: str):
    resp = service.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id,name,mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=200,
    ).execute()
    return resp.get("files", [])


def _get_or_create_used_folder(service, topic_folder_id: str) -> str:
    for f in _list_children(service, topic_folder_id):
        if f["mimeType"] == "application/vnd.google-apps.folder" and f["name"] == USED_FOLDER_NAME:
            return f["id"]
    created = service.files().create(
        body={
            "name": USED_FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [topic_folder_id],
        },
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]


def list_available(service, topic_folder_id: str) -> list:
    """Картинки в корне папки темы — те, что ещё не в "использовано"."""
    return [
        f for f in _list_children(service, topic_folder_id)
        if f["mimeType"] != "application/vnd.google-apps.folder"
    ]


def mark_used(service, file_id: str, topic_folder_id: str) -> None:
    used_folder_id = _get_or_create_used_folder(service, topic_folder_id)
    service.files().update(
        fileId=file_id,
        addParents=used_folder_id,
        removeParents=topic_folder_id,
        supportsAllDrives=True,
    ).execute()


def pick_and_mark(service, topic_folder_id: str) -> dict:
    """Берёт первый доступный (неиспользованный) баннер темы, скачивает байты
    и сразу помечает использованным (переносит в "использовано"). Бросает
    RuntimeError, если в теме больше нет свежих баннеров."""
    available = list_available(service, topic_folder_id)
    if not available:
        raise RuntimeError(f"В папке {topic_folder_id} не осталось неиспользованных баннеров")
    chosen = available[0]
    data = service.files().get_media(fileId=chosen["id"]).execute()
    mark_used(service, chosen["id"], topic_folder_id)
    print(f"  баннер: {chosen['name']} -> помечен использованным", file=sys.stderr)
    return {"id": chosen["id"], "name": chosen["name"], "bytes": data}


def list_topic_folders(service) -> list:
    """Подпапки внутри БАННЕРЫ/ — каждая своя тематическая категория,
    названа владелицей вручную (Google, SEO, AEO, ChatGPT и т.д.), не
    привязана жёстко к рубрикам пайплайна."""
    return [
        f for f in _list_children(service, BANNERS_ROOT_ID)
        if f["mimeType"] == "application/vnd.google-apps.folder"
    ]


UNIVERSAL_FOLDER_NAME = "универсальные"


def pick_topic_folder(service, item: dict) -> dict:
    """Выбирает подпапку БАННЕРЫ/ под тему поста — по названию папки, без
    просмотра самих картинок (дорого по токенам). Если подпапка одна —
    берём без вызова модели.

    Темы «по болям» (rubric «боль-и-решение») не имеют внешней темы для
    сопоставления с папками (Google/SEO/Meta и т.п. — это категории
    источников мониторинга, к болям не относятся) — Haiku на таких темах
    систематически попадал в тематические папки конверсии/форм/UX, которые
    либо пустые, либо содержат старые баннеры без фирменного зайца (см.
    докстринг модуля/память проекта, разобрано 05.08.2026). Вместо гадания
    такие темы всегда идут в UNIVERSAL_FOLDER_NAME напрямую — там основной
    запас брендированных баннеров с зайцем. Если этой папки почему-то нет
    (например, в шаблоне для нового клиента она ещё не создана) — тихо
    падаем обратно на обычный подбор по названию."""
    folders = list_topic_folders(service)
    if not folders:
        raise RuntimeError("В БАННЕРЫ/ нет ни одной подпапки")

    if item.get("rubric") == "боль-и-решение":
        universal = next((f for f in folders if f["name"] == UNIVERSAL_FOLDER_NAME), None)
        if universal:
            return universal

    if len(folders) == 1:
        return folders[0]

    api_key = require_env("OPENROUTER_API_KEY")
    names = [f["name"] for f in folders]
    prompt = (
        f'Тема поста: "{item["title"]}"\n'
        + (f'О чём: {item["why"]}\n' if item.get("why") else "")
        + "\nВыбери из списка НАЗВАНИЕ подпапки, которая лучше всего подходит по смыслу "
        "для фонового баннера этой темы. Названия папок:\n"
        + "\n".join(f"- {n}" for n in names)
        + "\n\nОтветь ТОЛЬКО одним названием из списка, без пояснений."
    )
    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/leadyup-monitor-pipeline",
            "X-Title": "leadyup-monitor-pipeline",
        },
        json={"model": MODEL, "max_tokens": 50, "messages": [{"role": "user", "content": prompt}]},
        timeout=30,
    )
    response.raise_for_status()
    choice = response.json()["choices"][0]["message"]["content"].strip()
    for f in folders:
        if f["name"] == choice or f["name"] in choice:
            return f
    print(f"[WARN] pick_topic_folder: ответ модели {choice!r} не совпал ни с одной папкой — беру первую", file=sys.stderr)
    return folders[0]


def get_or_pick_banner(service, item: dict, headline: str = None, badge: str = None) -> dict:
    """Один баннер (и один наложенный заголовок) на все форматы одной темы
    (пост/статья/карусель) — кэшируется в data/banners/<item_id>.json. Кто
    из генераторов первым выбрал баннер и предложил headline/badge, тот и
    зафиксировал их — остальные форматы этой же темы переиспользуют
    ЗАФИКСИРОВАННЫЕ значения (возвращаемые banner['headline']/['badge']),
    даже если сами передали свои — так у поста и статьи в итоге одна и та
    же картинка с одним и тем же текстом (решение владелицы 30.07.2026),
    а не только один и тот же фон. Сам PNG с наложением не кэшируется —
    рендерится заново при каждой отправке (см. render_html.py), чтобы не
    раздувать репозиторий."""
    cache_path = DATA_DIR / "banners" / f"{item['id']}.json"
    if cache_path.exists():
        cached = read_json(cache_path, {})
        if "headline" not in cached and headline:
            # Кэш создан раньше (например до этой функции) — дополняем
            # текстом заголовка от текущего вызова, не перевыбирая баннер.
            cached["headline"] = headline
            cached["badge"] = badge
            write_json(cache_path, cached)
        data = service.files().get_media(fileId=cached["id"]).execute()
        return {**cached, "bytes": data}

    folder = pick_topic_folder(service, item)
    banner = pick_and_mark(service, folder["id"])
    cache_entry = {"id": banner["id"], "name": banner["name"], "folder": folder["name"]}
    if headline:
        cache_entry["headline"] = headline
        cache_entry["badge"] = badge
    write_json(cache_path, cache_entry)
    return {**banner, **cache_entry}
