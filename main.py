from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from app.bot import configure_bot_commands, create_dispatcher
from app.config import Settings, ensure_database_path
from app.db import DatabaseRepository
from app.music import MusicClient
from app.voice import VoiceBus
from player_main import run_player


async def run() -> None:
    settings = Settings.from_env()
    if settings.app_role == "player":
        await run_player()
        return

    if not settings.bot_token:
        raise ValueError("BOT_TOKEN is required for bot role")

    ensure_database_path(settings.database_url)

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    repo = DatabaseRepository(settings.database_url)
    music = MusicClient()
    bot = Bot(token=settings.bot_token)
    voice_bus = VoiceBus(settings.redis_url) if settings.redis_url else None
    dp = create_dispatcher(
        repo=repo,
        music=music,
        voice_bus=voice_bus,
        target_chat_id=settings.target_chat_id,
    )

    await repo.init()
    await configure_bot_commands(bot)

    try:
        await dp.start_polling(bot)
    finally:
        if voice_bus:
            await voice_bus.close()
        await music.close()
        await repo.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
