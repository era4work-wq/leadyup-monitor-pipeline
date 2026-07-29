"""Подбор и учёт баннеров-фонов из Google Drive (БАННЕРЫ/<тема>/).

Использованный баннер переезжает в подпапку <тема>/использовано/ — это и
есть учёт: не нужен отдельный файл-реестр, в самом Drive сразу видно, что
ещё свежее (лежит в корне темы), а что уже пошло в дело (в "использовано").
Владелица ориентируется по тому же признаку, когда сама смотрит на Диск.
"""
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = Path(__file__).resolve().parent.parent / "secrets" / "drive-service-account.json"
USED_FOLDER_NAME = "использовано"


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
