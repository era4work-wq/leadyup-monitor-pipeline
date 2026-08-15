"""Подбор и учёт баннеров-фонов из Google Drive (БАННЕРЫ/<тема>/).

Использованный баннер переезжает в подпапку <тема>/использовано/ — это и
есть учёт: не нужен отдельный файл-реестр, в самом Drive сразу видно, что
ещё свежее (лежит в корне темы), а что уже пошло в дело (в "использовано").
Владелица ориентируется по тому же признаку, когда сама смотрит на Диск.
"""
import os
import random
import sys
from pathlib import Path

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

from common import DATA_DIR, generate_topic_banner, read_json, require_env, write_json

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


# Часть папок — смесь фирменных баннеров (с зайцем, маскотом канала) и
# старых generic-баннеров без него. Если в файле встречается "зая"/"зайц" —
# считаем его фирменным и предпочитаем при выборе (разобрано 05.08.2026,
# после жалобы на «баннер не из нашей базы с зайцами» — конвенция по имени
# уже соблюдается почти везде, код просто начал её учитывать).
def _is_branded(filename: str) -> bool:
    return "зая" in filename.lower()


def pick_and_mark(service, topic_folder_id: str) -> dict:
    """Берёт доступный (неиспользованный) баннер темы, скачивает байты и
    сразу помечает использованным (переносит в "использовано"). Среди
    доступных предпочитает фирменные (с зайцем в имени файла), если такие
    есть; выбор — случайный (не всегда первый по порядку Drive), чтобы
    подряд идущие темы получали разные картинки. Бросает RuntimeError, если
    в теме больше нет свежих баннеров."""
    available = list_available(service, topic_folder_id)
    if not available:
        raise RuntimeError(f"В папке {topic_folder_id} не осталось неиспользованных баннеров")
    branded = [f for f in available if _is_branded(f["name"])]
    pool = branded or available
    chosen = random.choice(pool)
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


# Пул папок для тем «по болям» (rubric «боль-и-решение») — эти темы не
# привязаны к внешней теме источника (Google/SEO/Meta и т.п. — категории
# мониторинга, к болям не относятся), поэтому вместо подбора папки моделью
# по смыслу берём случайную из заведомо подходящих: «универсальные» —
# основной запас брендированных баннеров, остальные — тематические папки
# под конверсию/UX/боли, которые владелица завела именно под этот контент.
# Разобрано 05.08.2026 — одна фиксированная папка («универсальные») быстро
# исчерпывалась и не давала разнообразия; пул из нескольких папок и
# случайный выбор среди непустых решают обе проблемы разом. Если ни одной
# из этого списка не существует (например, в шаблоне для нового клиента
# ещё не заведены) — тихо падаем обратно на обычный подбор по названию.
UNIVERSAL_FOLDER_NAME = "универсальные"
PAIN_FOLDER_NAMES = [
    UNIVERSAL_FOLDER_NAME,
    "персонализация, аналитика, триггеры",
    "UX, воронка, оптимизация",
    "доверие, отзывы, UX",
    "формы, заказы, оплата",
    "основы конверсии",
]


# Подбор из Drive-папок (ниже) остаётся ЗАПАСНЫМ путём. Основной путь —
# генерация баннера под конкретную тему через GPT Image 2 (common.py:
# generate_topic_banner), с эталонным референс-паком персонажа (см. ниже),
# чтобы заяц не перерисовывался с нуля каждый раз. Проверено вживую с
# владелицей 15.08.2026 на реальной теме — устроило.
#
# Сгенерированные баннеры хранятся ЛОКАЛЬНО (data/banners/ai/<id>.png), не в
# Drive: попытка загрузить туда файл упала с "Service Accounts do not have
# storage quota" — у сервисных аккаунтов нет собственной квоты хранения на
# личном Диске владелицы (это ограничение Google, снимается только Shared
# Drive или delegation на стороне заказчика — не настроено). Читать/двигать
# файлы в её Диске сервисный аккаунт может (доступ дан на папку), а вот
# создавать новые с содержимым — нет. Если это будет мешать (например
# захочется видеть архив сгенерированного в самом Диске) — нужен один из
# этих двух шагов с их стороны, не код.
AI_GENERATED_FOLDER_NAME = "сгенерированные-ИИ"
AI_BANNER_DIR = DATA_DIR / "banners" / "ai"
REFERENCE_COUNT = 2

# Эталонный референс-пак персонажа — НЕ в БАННЕРЫ/, отдельное дерево
# ассеты/персонажи/заяц/референс-пак/ (найдено владелицей 15.08.2026 после
# того, как я по умолчанию брала случайные готовые баннеры со СЦЕНОЙ —
# заяц с графиком, в конкретной позе — а не чистый референс дизайна
# персонажа; сцена в референсе тянется в новую генерацию вместе с зайцем,
# путает модель). Сервисный аккаунт видит эту папку — доступ дан на всю
# leadyup-генерация/, не только на БАННЕРЫ/ (см. память
# drive-service-account-banners). Фиксированные 2 чистых ракурса (фронт +
# три четверти), не случайные — тут не нужно разнообразие, нужна
# стабильность одного и того же эталона.
CANON_REFERENCE_FOLDER_NAME = "референс-пак"
CANON_REFERENCE_FILENAMES = ["01-фронт.png", "02-три-четверти.png"]


def build_banner_prompt(item: dict) -> str:
    theme = item["title"] + (f' — {item["why"]}' if item.get("why") else "")
    return (
        "Using the exact same rabbit mascot character shown in the reference images "
        "(same species, same fur color, same art style, same proportions, same outfit — do not redesign it), "
        f'create a NEW scene themed around: "{theme}". '
        "CRITICAL — match the reference images' fur rendering EXACTLY: high-end 3D character render "
        "with individually visible fur strands, realistic depth and volume, soft directional studio "
        "lighting that shows texture — NOT smooth, flat, or plush-toy-like fur. "
        "Match the reference images' facial expression and attitude EXACTLY: sharp, alert, confident, "
        "slightly narrowed focused eyes with personality and wit — NOT a soft, neutral, or vacant "
        "plush-toy expression. "
        "Keep the same dark near-black/navy cinematic background style, soft glow lighting, "
        "professional tech-conference-cover aesthetic. "
        "16:9 landscape composition. "
        "STRICT: absolutely no text, letters, numbers, words, logos, or watermarks anywhere in the image."
    )


def _find_folder_by_name(service, name: str):
    """Ищет папку по названию где угодно в доступной сервисному аккаунту
    части Диска, не только под БАННЕРЫ/ — эталонный референс-пак лежит в
    отдельном дереве (ассеты/персонажи/заяц/)."""
    resp = service.files().list(
        q=f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def pick_reference_images(service, n: int = REFERENCE_COUNT) -> list:
    """Эталонные референсы персонажа — фиксированный набор чистых ракурсов
    из CANON_REFERENCE_FOLDER_NAME (без сцены/реквизита/конкретной позы —
    только дизайн персонажа). Пустой список, если папка/файлы не нашлись —
    вызывающий код откатится на обычный подбор из Drive."""
    folder_id = _find_folder_by_name(service, CANON_REFERENCE_FOLDER_NAME)
    if not folder_id:
        return []
    available = {f["name"]: f["id"] for f in _list_children(service, folder_id)}
    file_ids = [available[name] for name in CANON_REFERENCE_FILENAMES if name in available]
    if not file_ids:
        return []
    return [service.files().get_media(fileId=fid).execute() for fid in file_ids[:n]]


def generate_and_store_banner(service, item: dict) -> dict:
    """Генерирует баннер под тему и сохраняет байты локально (data/banners/ai/,
    см. комментарий выше про квоту сервисного аккаунта). Возвращает None,
    если референсов нет или генерация не удалась — вызывающий код откатится
    на обычный подбор из Drive."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    refs = pick_reference_images(service)
    if not refs:
        return None
    image_bytes = generate_topic_banner(api_key, build_banner_prompt(item), refs)
    if not image_bytes:
        return None
    AI_BANNER_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{item['id']}.png"
    (AI_BANNER_DIR / filename).write_bytes(image_bytes)
    print(f"  баннер: сгенерирован ИИ под тему -> {filename}", file=sys.stderr)
    return {"source": "ai", "name": filename, "bytes": image_bytes}


def pick_topic_folder(service, item: dict) -> dict:
    """Выбирает подпапку БАННЕРЫ/ под тему поста — по названию папки, без
    просмотра самих картинок (дорого по токенам). Если подпапка одна —
    берём без вызова модели. Для тем «по болям» — см. PAIN_FOLDER_NAMES."""
    folders = list_topic_folders(service)
    if not folders:
        raise RuntimeError("В БАННЕРЫ/ нет ни одной подпапки")

    if item.get("rubric") == "боль-и-решение":
        pool = [f for f in folders if f["name"] in PAIN_FOLDER_NAMES]
        non_empty = [f for f in pool if list_available(service, f["id"])]
        if non_empty:
            return random.choice(non_empty)
        if pool:
            # Весь пул временно исчерпан (например, после серии тестов) —
            # берём что есть, pick_and_mark сам бросит понятную ошибку,
            # если действительно пусто.
            return random.choice(pool)

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
        # "source" нет у кэша, записанного до этой функции — тогда единственным
        # путём был подбор из Drive, читаем как раньше (обратная совместимость).
        if cached.get("source", "drive") == "ai":
            data = (AI_BANNER_DIR / cached["name"]).read_bytes()
        else:
            data = service.files().get_media(fileId=cached["id"]).execute()
        return {**cached, "bytes": data}

    banner = generate_and_store_banner(service, item)
    if banner is not None:
        # "id" тут не настоящий Drive id (для ИИ-баннера его нет) — стабильная
        # синтетическая метка (= имени локального файла), чтобы весь код ниже
        # по цепочке (write_*.py/notify_*.py), который просто читает banner["id"]
        # для сохранения в drafts/*.json, не падал на отсутствующем ключе.
        # За реальную загрузку байт отвечает "source", см. load_banner_bytes().
        cache_entry = {
            "source": "ai",
            "id": item["id"],
            "name": banner["name"],
            "folder": AI_GENERATED_FOLDER_NAME,
        }
    else:
        folder = pick_topic_folder(service, item)
        banner = pick_and_mark(service, folder["id"])
        cache_entry = {"source": "drive", "id": banner["id"], "name": banner["name"], "folder": folder["name"]}

    if headline:
        cache_entry["headline"] = headline
        cache_entry["badge"] = badge
    write_json(cache_path, cache_entry)
    return {**banner, **cache_entry}


def load_banner_bytes(service, banner: dict) -> bytes:
    """Достаёт сырые байты баннера по метаданным, сохранённым в drafts/*.json
    (id/name/source) — из Drive, если баннер оттуда, или локально
    (data/banners/ai/), если сгенерирован ИИ. Используют notify_*.py/publish.py
    при отправке — они получают только эти метаданные, не сами байты
    (get_or_pick_banner к тому моменту уже отработал в отдельном прогоне)."""
    if banner.get("source") == "ai":
        return (AI_BANNER_DIR / banner["name"]).read_bytes()
    return service.files().get_media(fileId=banner["id"]).execute()
