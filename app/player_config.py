from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from app.config import normalize_database_url


load_dotenv()


@dataclass(frozen=True)
class PlayerSettings:
    database_url: str
    log_level: str
    redis_url: str
    telegram_api_id: int
    telegram_api_hash: str
    assistant_session_string: str
    target_chat_id: int | None

    @classmethod
    def from_env(cls) -> "PlayerSettings":
        redis_url = os.getenv("REDIS_URL", "").strip()
        if not redis_url:
            raise ValueError("Environment variable REDIS_URL is required for player")

        api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
        if not api_id_raw or not api_id_raw.lstrip("-").isdigit():
            raise ValueError("Environment variable TELEGRAM_API_ID must be numeric")
        telegram_api_id = int(api_id_raw)

        telegram_api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        if not telegram_api_hash:
            raise ValueError("Environment variable TELEGRAM_API_HASH is required")

        assistant_session_string = os.getenv("ASSISTANT_SESSION_STRING", "").strip()
        if not assistant_session_string:
            raise ValueError("Environment variable ASSISTANT_SESSION_STRING is required")

        target_chat_id_raw = os.getenv("TARGET_CHAT_ID", "").strip()
        target_chat_id: int | None = None
        if target_chat_id_raw:
            if not target_chat_id_raw.lstrip("-").isdigit():
                raise ValueError("TARGET_CHAT_ID must be numeric (example: -1001234567890)")
            target_chat_id = int(target_chat_id_raw)

        database_url = normalize_database_url(os.getenv("DATABASE_URL", "").strip())
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()

        return cls(
            database_url=database_url,
            log_level=log_level,
            redis_url=redis_url,
            telegram_api_id=telegram_api_id,
            telegram_api_hash=telegram_api_hash,
            assistant_session_string=assistant_session_string,
            target_chat_id=target_chat_id,
        )
