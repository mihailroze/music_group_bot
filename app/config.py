from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _normalize_database_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        return "sqlite+aiosqlite:///./data/bot.db"

    if url.startswith("postgres://"):
        return f"postgresql+asyncpg://{url[len('postgres://'):]}"

    if url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
        return f"postgresql+asyncpg://{url[len('postgresql://'):]}"

    if url.startswith("sqlite:///") and not url.startswith("sqlite+aiosqlite:///"):
        return f"sqlite+aiosqlite:///{url[len('sqlite:///'):]}"

    return url


def ensure_database_path(database_url: str) -> None:
    if not database_url.startswith("sqlite"):
        return

    if "///" not in database_url:
        return

    raw_path = database_url.split("///", maxsplit=1)[1]
    if not raw_path or raw_path == ":memory:":
        return

    db_path = Path(raw_path)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            raise ValueError("Environment variable BOT_TOKEN is required")

        database_url = _normalize_database_url(os.getenv("DATABASE_URL", "").strip())
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        return cls(bot_token=bot_token, database_url=database_url, log_level=log_level)
