"""Рендер HTML-шаблонов (design/) в PNG через headless Chromium (Playwright).

Облачный аналог канон/дизайн/render.sh, который дёргал локальный Chrome на
маке владелицы — не работает в GitHub Actions, там браузера нет. Плейсхолдеры
в шаблонах — простые {{KEY}}, подставляются как есть (в т.ч. HTML-теги вроде
<em> для зелёного слова в заголовке — так и было задумано в дизайн-системе).
"""
import uuid
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

DESIGN_DIR = Path(__file__).resolve().parent.parent / "design"


def _fill(template_name: str, substitutions: dict) -> str:
    html = (DESIGN_DIR / template_name).read_text(encoding="utf-8")
    for key, value in substitutions.items():
        html = html.replace(f"{{{{{key}}}}}", value)
    return html


def _render_one(browser, template_name: str, substitutions: dict, width: int, height: int) -> bytes:
    # Временный файл кладём рядом с шаблоном (в design/), а не в /tmp — иначе
    # относительный путь к tokens.css не резолвится (те же грабли, что были
    # с render.sh, см. память drive-service-account-banners).
    html = _fill(template_name, substitutions)
    tmp_path = DESIGN_DIR / f"_tmp-{uuid.uuid4().hex}.html"
    tmp_path.write_text(html, encoding="utf-8")
    try:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"file://{tmp_path}")
        page.wait_for_timeout(300)  # догрузка шрифтов Google Fonts
        png_bytes = page.screenshot()
        page.close()
        return png_bytes
    finally:
        tmp_path.unlink(missing_ok=True)


@contextmanager
def renderer():
    """Держит один запуск Chromium открытым для нескольких рендеров подряд
    (например 7 слайдов карусели) — не поднимаем браузер заново на каждый.
    Использование: `with renderer() as r: png = r("шаблон.html", {...}, w, h)`."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            yield lambda template_name, substitutions, width, height: _render_one(
                browser, template_name, substitutions, width, height
            )
        finally:
            browser.close()


def render(template_name: str, substitutions: dict, width: int, height: int) -> bytes:
    """Рендер одного шаблона — для разовых вызовов (например обложка поста)."""
    with renderer() as r:
        return r(template_name, substitutions, width, height)
