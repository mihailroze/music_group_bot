# Group Music Telegram Bot

MVP for a music-focused Telegram group with:
- track queue and skip voting;
- shared playlist with anti-duplicates;
- mood modes (`work`, `party`, `chill`, `road`);
- DJ roles and stats;
- voice chat player (stream to the whole group voice chat).

## Architecture

Two services are used:

1. `bot` (`APP_ROLE=bot`)
- Handles Telegram commands.
- Stores queue, playlist, roles, stats in DB.
- Sends playback commands to Redis.

2. `player` (`APP_ROLE=player`)
- Reads playback commands from Redis.
- Uses `Pyrogram + PyTgCalls + ffmpeg` to stream audio to group voice chat.
- Auto-plays next track when current stream ends.

## Stack

- Python 3.12
- aiogram 3
- SQLAlchemy 2
- PostgreSQL (recommended in Railway)
- Redis (command bus between bot and player)
- Pyrogram + PyTgCalls for voice streaming

## Local Run

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Fill `.env` (based on `.env.example`).

3. Run bot:
```bash
python main.py
```

4. Run player (in another terminal):
```bash
set APP_ROLE=player
python main.py
```

## Required ENV

Common:
- `DATABASE_URL`
- `LOG_LEVEL`
- `REDIS_URL`

Bot role:
- `APP_ROLE=bot`
- `BOT_TOKEN`

Player role:
- `APP_ROLE=player`
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `ASSISTANT_SESSION_STRING`
- `TARGET_CHAT_ID` (optional, lock player to one group)

## Voice Commands (in group)

- `/join` prepare player
- `/play` start/resume stream in voice chat
- `/pause`
- `/resume`
- `/stop` stop + leave voice chat
- `/leave`
- `/vstatus`

Only DJ/admin can run playback control commands.

## Important Notes

- Voice stream is visible in the group voice chat (for the whole group).
- Voice stream is tied to chat, not to a forum topic/thread.
- If you see `TelegramConflictError`, another process uses the same bot token/session simultaneously.

## Railway Deployment

Create one Railway project with 4 services:

1. `bot` (repo source)
- Start command: `python main.py`
- Variables:
  - `APP_ROLE=bot`
  - `BOT_TOKEN=...`
  - `DATABASE_URL=${{Postgres.DATABASE_URL}}`
  - `REDIS_URL=${{Redis.REDIS_URL}}`
  - `LOG_LEVEL=INFO`

2. `player` (repo source)
- Start command: `python main.py`
- Variables:
  - `APP_ROLE=player`
  - `DATABASE_URL=${{Postgres.DATABASE_URL}}`
  - `REDIS_URL=${{Redis.REDIS_URL}}`
  - `TELEGRAM_API_ID=...`
  - `TELEGRAM_API_HASH=...`
  - `ASSISTANT_SESSION_STRING=...`
  - `TARGET_CHAT_ID=-100...`
  - `LOG_LEVEL=INFO`

3. `Postgres` (Railway database)
4. `Redis` (Railway database)
