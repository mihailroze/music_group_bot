from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from app.bot import configure_bot_commands, create_dispatcher
from app.config import Settings, ensure_database_path
from app.db import DatabaseRepository
from app.music import MusicClient


async def run() -> None:
    settings = Settings.from_env()
    ensure_database_path(settings.database_url)

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    repo = DatabaseRepository(settings.database_url)
    music = MusicClient()
    bot = Bot(token=settings.bot_token)
    dp = create_dispatcher(repo, music)

    await repo.init()
    await configure_bot_commands(bot)

    try:
        await dp.start_polling(bot)
    finally:
        await music.close()
        await repo.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
