# Group Music Telegram Bot

MVP-бот для музыкальной группы в Telegram:
- очередь треков и skip-голосование;
- совместный плейлист с антидублями;
- режимы `work/party/chill/road`;
- роли DJ;
- статистика активности и жанров.

## Стек
- Python 3.12
- aiogram 3
- SQLAlchemy 2 (SQLite по умолчанию, PostgreSQL через `DATABASE_URL`)
- iTunes Search API для метаданных и рекомендаций

## Быстрый старт
1. Установить зависимости:
```bash
pip install -r requirements.txt
```
2. Скопировать `.env.example` в `.env` и заполнить:
```env
BOT_TOKEN=...
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db
LOG_LEVEL=INFO
```
3. Запустить:
```bash
python main.py
```

## Основные команды
- `/add <ссылка|название>`
- `/queue`
- `/now`
- `/skip` и `/skip force` (DJ/admin)
- `/save`
- `/playlist`
- `/playlist_top`
- `/mode <work|party|chill|road>`
- `/recommend [N]`
- `/set_start [N]` (DJ/admin)
- `/move <from> <to>` (DJ/admin)
- `/dj_add`, `/dj_remove`, `/dj_list`
- `/stats`, `/stats_week`, `/top_users`, `/top_genres`

## Skip-правило
Skip проходит, если:
- набралось 3 голоса, или
- набралось 40% от активных участников (кто взаимодействовал с ботом за последние 7 дней).

## Railway
1. Создать проект и подключить репозиторий.
2. В Variables задать:
   - `BOT_TOKEN`
   - `DATABASE_URL` (рекомендуется PostgreSQL Railway, формат поддерживается автоматически)
   - `LOG_LEVEL=INFO`
3. Деплой: используется `Dockerfile` и `railway.toml`.
