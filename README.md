# leadyup-monitor-pipeline

Ежедневный мониторинг источников для Telegram-канала LeadYup: сбор RSS → отбор
Claude Haiku (через OpenRouter) → согласование темами в приватной Telegram-группе.
Работает на GitHub Actions, без своего сервера. Полное описание механики — в
основном проекте: `content/пайплайн-мониторинг-и-согласование.md`.

Репозиторий на GitHub — под аккаунтом/организацией **заказчика**, не личный.

Это «движок» — код, промпты и ключи. На клиентский Google Диск не попадает
ни в каком виде.

## Как это работает (коротко)

1. **`monitor.yml`** — раз в день: `collect.py` (RSS + дедуп) → `select_topics.py`
   (отбор Claude Haiku по `prompts/select-criteria.md`) → `notify_telegram.py`
   (кандидаты уходят в TG-группу с кнопками ✅/❌).
2. **`poll-approvals.yml`** — каждые ~15 минут весь день: `collect_approvals.py`
   собирает нажатия кнопок, правит сообщения, копит утверждённые темы в
   `data/approved/`.
3. Оба workflow коммитят `state/` и `data/` обратно в репозиторий — это и есть
   хранилище состояния, без внешней БД.

Дальше (генерация черновиков постов, финальное согласование, автопубликация в
канал) — следующий этап, ещё не реализован. `data/approved/*.json` — то, с
чего он начнётся.

## Активация — шаги, которые нужно сделать руками

Я не могу создать бота или репозиторий на GitHub за вас — нужны учётки.

1. **Бот:** в Telegram → `@BotFather` → `/newbot` → сохранить токен.
2. **Группа:** создать приватную TG-группу (например, «Ловец — согласование
   постов»), добавить владелицу, себя и бота по username.
3. **chat_id группы:** написать любое сообщение в группу → открыть в браузере
   `https://api.telegram.org/bot<ТОКЕН>/getUpdates` → найти `"chat":{"id": -100...}`.
4. **Ключ OpenRouter API** — на openrouter.ai, раздел Keys → создать ключ,
   пополнить баланс. Модель отбора — `anthropic/claude-haiku-4.5` (уточнить
   актуальный слаг на openrouter.ai/models на момент активации).
5. **Репозиторий на GitHub** — под аккаунтом/организацией заказчика:
   - заказчик создаёт пустой **приватный** репозиторий `leadyup-monitor-pipeline`
     в своём GitHub (без README/.gitignore/license — репозиторий должен
     остаться полностью пустым, иначе конфликт при первом пуше);
   - заказчик добавляет коллаборатора с правом **Write** (Settings репозитория
     → Collaborators and teams → Add people) — по GitHub username, который
     уже настроен локально на этой машине через `gh` (см. `gh api user`);
   - коллаборатор принимает приглашение (иначе push не пройдёт);
   - затем запушить код (HTTPS, не SSH — на этой машине настроен `gh` как
     credential helper для github.com, отдельный SSH-ключ не нужен):
   ```
   cd ~/Projects/leadyup-monitor-pipeline
   git remote add origin https://github.com/<владелец>/leadyup-monitor-pipeline.git
   git add -A && git commit -m "init"
   git push -u origin main
   ```
6. **Секреты репозитория** (Settings → Secrets and variables → Actions):
   - `OPENROUTER_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
7. **Первый прогон вручную:** вкладка Actions → `Мониторинг и отбор тем` →
   Run workflow. Проверить, что кандидаты дошли до группы и кнопки работают,
   прежде чем полагаться на расписание.

## Локальный запуск для отладки

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export OPENROUTER_API_KEY=...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
cd src
python collect.py && python select_topics.py && python notify_telegram.py
python collect_approvals.py   # после того как кто-то нажал кнопку в группе
```

## Реестр источников

`sources.csv` — копия `research/источники-мониторинга.csv` из основного
проекта (84 источника, RSS-URL, скор, приоритет). Если реестр обновится в
основном проекте — скопировать заново вручную, авто-синка нет.
