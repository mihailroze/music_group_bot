from __future__ import annotations

import asyncio
import logging

from app.db import DatabaseRepository
from app.music import MusicClient
from app.player import VoicePlayer
from app.player_config import PlayerSettings
from app.voice import VoiceBus


async def run_player() -> None:
    settings = PlayerSettings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    repo = DatabaseRepository(settings.database_url)
    music = MusicClient()
    bus = VoiceBus(settings.redis_url)
    player = VoicePlayer(
        repo=repo,
        bus=bus,
        music=music,
        telegram_api_id=settings.telegram_api_id,
        telegram_api_hash=settings.telegram_api_hash,
        assistant_session_string=settings.assistant_session_string,
        target_chat_id=settings.target_chat_id,
    )

    try:
        await player.start()
    finally:
        await player.stop()


if __name__ == "__main__":
    asyncio.run(run_player())
