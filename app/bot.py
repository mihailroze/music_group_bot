from __future__ import annotations

import math
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message

from app.db import DatabaseRepository
from app.music import (
    MODE_SEEDS,
    MusicClient,
    TrackLookup,
    yandex_public_url_from_source,
    yandex_source_url,
    yandex_track_id_from_url,
)
from app.utils import build_fingerprint, display_track, is_url, short_host
from app.voice import VoiceBus, VoiceCommand


MODES = {
    "work": "work",
    "party": "party",
    "chill": "chill",
    "road": "road",
}


HELP_TEXT = """Команды:
/add <ссылка|запрос> - добавить трек в очередь
/add_top <ссылка|запрос> - добавить трек в начало (DJ/admin)
/queue - показать очередь
/now - текущий трек
/skip - голос за skip
/skip force - принудительный skip (DJ/admin)
/save - сохранить текущий трек в плейлист
/playlist - плейлист группы
/playlist_top - топ артистов в плейлисте
/mode <work|party|chill|road> - режим группы
/recommend [N] - рекомендации под режим
/set_start [N] - добавить N рекомендаций в очередь (DJ/admin)
/move <from> <to> - переставить трек (DJ/admin)
/dj_add <@user|id> или ответом - выдать DJ (admin)
/dj_remove <@user|id> или ответом - снять DJ (admin)
/dj_list - список DJ
/stats - общая статистика
/stats_week - статистика за 7 дней
/top_users - самые активные по добавлениям
/top_genres - самые популярные жанры

Voice chat:
/join - активировать плеер для группы (DJ/admin)
/play - начать или продолжить стрим в voice chat (DJ/admin)
/pause - пауза (DJ/admin)
/resume - продолжить (DJ/admin)
/stop - остановить и выйти из voice chat (DJ/admin)
/leave - выйти из voice chat (DJ/admin)
/vstatus - статус voice-плеера

Voice chat из лички:
- если задан TARGET_CHAT_ID: просто пиши /join, /play, /pause и т.д.
- иначе указывай id группы: например /play -1001234567890"""


def _format_user_label(user_data: dict[str, Any] | None, fallback_user_id: int) -> str:
    if not user_data:
        return str(fallback_user_id)
    username = user_data.get("username")
    full_name = user_data.get("full_name")
    if username:
        return f"@{username}"
    if full_name:
        return full_name
    return str(fallback_user_id)


def _track_url(track_data: dict[str, Any]) -> str | None:
    return yandex_public_url_from_source(track_data.get("source_url"))


def _format_track_with_source(track_data: dict[str, Any]) -> str:
    title = display_track(track_data)
    url = _track_url(track_data)
    if not url:
        return title
    return f"{title} [{short_host(url)}]"


def _track_lookup_to_payload(track: TrackLookup) -> dict[str, Any]:
    fingerprint = build_fingerprint(track.artist, track.title)
    return {
        "source_url": track.source_url,
        "query_text": track.query_text,
        "artist": track.artist,
        "title": track.title,
        "genre": track.genre,
        "fingerprint": fingerprint,
    }


def _render_voice_state(state: dict[str, str]) -> str:
    if not state:
        return "Voice player: нет данных (возможно, player-сервис не запущен)."

    status = state.get("status", "unknown")
    message = state.get("message", "")
    track_title = state.get("track_title", "")
    track_url = state.get("track_url", "")
    updated_at = state.get("updated_at", "-")

    lines = [f"Voice player status: {status}"]
    if message:
        lines.append(f"Message: {message}")
    if track_title:
        lines.append(f"Track: {track_title}")
    if track_url:
        lines.append(f"Source: {track_url}")
    lines.append(f"Updated at (UTC): {updated_at}")
    return "\n".join(lines)


def create_dispatcher(
    repo: DatabaseRepository,
    music: MusicClient,
    voice_bus: VoiceBus | None = None,
    target_chat_id: int | None = None,
) -> Dispatcher:
    dp = Dispatcher()
    router = Router()

    def _parse_chat_id_arg(args: str | None) -> int | None:
        if not args:
            return None
        token = args.strip().split()[0]
        if token.lstrip("-").isdigit():
            return int(token)
        return None

    async def resolve_voice_chat_id(message: Message, args: str | None) -> int | None:
        """
        In groups: use current chat id.
        In private messages: use TARGET_CHAT_ID or a numeric chat id from args.
        """
        if not message.chat:
            return None

        if message.chat.type in {"group", "supergroup"}:
            return message.chat.id

        chat_id = _parse_chat_id_arg(args) or target_chat_id
        if chat_id is None:
            await message.answer(
                "Я могу запускать/управлять эфиром из лички, но мне нужен id группы.\n"
                "Пример: `/play -1003549746734`\n"
                "Или задай `TARGET_CHAT_ID` в Railway (bot-сервис).",
                parse_mode="Markdown",
            )
            return None
        return chat_id

    async def touch_for_chat(message: Message, chat_id: int) -> None:
        if not message.from_user:
            return
        await repo.register_user_activity(
            chat_id=chat_id,
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )

    async def touch(message: Message) -> None:
        if not message.chat:
            return
        await touch_for_chat(message, message.chat.id)

    async def is_admin_in_chat(message: Message, chat_id: int) -> bool:
        if not message.from_user:
            return False
        try:
            member = await message.bot.get_chat_member(chat_id, message.from_user.id)
        except TelegramBadRequest:
            return False
        return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}

    async def is_member_in_chat(message: Message, chat_id: int) -> bool:
        if not message.from_user:
            return False
        try:
            member = await message.bot.get_chat_member(chat_id, message.from_user.id)
        except TelegramBadRequest:
            return False
        return member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}

    async def is_admin(message: Message) -> bool:
        if not message.chat:
            return False
        return await is_admin_in_chat(message, message.chat.id)

    async def is_dj_or_admin_in_chat(message: Message, chat_id: int) -> bool:
        if not message.from_user:
            return False
        if await is_admin_in_chat(message, chat_id):
            return True
        return await repo.is_dj(chat_id, message.from_user.id)

    async def is_dj_or_admin(message: Message) -> bool:
        if not message.chat:
            return False
        return await is_dj_or_admin_in_chat(message, message.chat.id)

    async def voice_enabled_for_chat(message: Message, chat_id: int) -> bool:
        if voice_bus is None:
            await message.answer(
                "Voice player не настроен. Нужен REDIS_URL в bot-сервисе и отдельный player-сервис."
            )
            return False
        if target_chat_id is not None and chat_id != target_chat_id:
            await message.answer(
                f"Voice player закреплен за другой группой (TARGET_CHAT_ID={target_chat_id})."
            )
            return False
        return True

    async def send_voice_command(
        message: Message,
        chat_id: int,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not message.from_user or voice_bus is None:
            return False
        await voice_bus.publish(
            VoiceCommand(
                action=action,
                chat_id=chat_id,
                requested_by=message.from_user.id,
                payload=payload or {},
            )
        )
        return True

    async def resolve_track_payload(raw_query: str) -> dict[str, Any]:
        query = raw_query.strip()
        if is_url(query):
            track_id = yandex_track_id_from_url(query)
            if not track_id:
                raise ValueError(
                    "Сейчас поддерживается только Яндекс.Музыка.\n"
                    "Пришли ссылку на трек вида:\n"
                    "- https://music.yandex.ru/track/123\n"
                    "- https://music.yandex.ru/album/123/track/456"
                )

            looked_up = await music.lookup_track(track_id)
            if looked_up:
                return _track_lookup_to_payload(looked_up)

            # Fallback: store canonical YM source, metadata may be resolved by player later.
            title = f"Yandex track {track_id}"
            return {
                "source_url": yandex_source_url(track_id),
                "query_text": query,
                "artist": None,
                "title": title,
                "genre": None,
                "fingerprint": build_fingerprint(None, f"ym:{track_id}") or f"ym{track_id}",
            }

        found = await music.search_track(query)
        if found:
            return _track_lookup_to_payload(found)

        raise ValueError(
            "Не смог найти трек в Яндекс.Музыке.\n"
            "Попробуй другой запрос или пришли ссылку на трек из music.yandex.ru."
        )

    async def resolve_target_user(
        message: Message,
        args: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if message.reply_to_message and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
            payload = {
                "user_id": target.id,
                "username": target.username.lower() if target.username else None,
                "full_name": target.full_name,
            }
            await repo.upsert_user(
                user_id=target.id,
                username=target.username,
                full_name=target.full_name,
            )
            return payload, None

        if not args:
            return None, "Укажи пользователя: /dj_add @username или ответом на сообщение."

        token = args.strip().split()[0]
        if token.startswith("@"):
            user_id = await repo.find_user_id_by_username(token)
            if user_id is None:
                return None, "Не знаю этого username. Пользователь должен сначала написать боту."
            user_data = await repo.get_user(user_id)
            payload = {
                "user_id": user_id,
                "username": user_data["username"] if user_data else token.lstrip("@"),
                "full_name": user_data["full_name"] if user_data else token,
            }
            return payload, None

        if token.lstrip("-").isdigit():
            user_id = int(token)
            user_data = await repo.get_user(user_id)
            if user_data is None:
                user_data = {"username": None, "full_name": str(user_id)}
                await repo.upsert_user(user_id=user_id, username=None, full_name=str(user_id))
            payload = {
                "user_id": user_id,
                "username": user_data.get("username"),
                "full_name": user_data.get("full_name") or str(user_id),
            }
            return payload, None

        return None, "Не смог разобрать пользователя. Используй @username, id или ответ на сообщение."

    async def add_track_to_queue(message: Message, query: str, to_top: bool) -> None:
        if not message.chat or not message.from_user:
            return
        try:
            payload = await resolve_track_payload(query)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        track = await repo.get_or_create_track(payload, added_by=message.from_user.id)
        _, position = await repo.add_queue_item(
            chat_id=message.chat.id,
            track_id=track["id"],
            user_id=message.from_user.id,
            genre=track.get("genre"),
            to_top=to_top,
        )
        await message.answer(f"Добавил: {_format_track_with_source(track)}\nПозиция в очереди: {position}")

    @router.message(CommandStart())
    async def start_handler(message: Message) -> None:
        await touch(message)
        await message.answer(
            "Бот для музыкальной группы готов.\n"
            "Основные команды: /add, /queue, /skip, /save, /mode, /stats\n"
            "Voice: /join, /play, /pause, /resume, /stop, /leave, /vstatus\n\n"
            "Полный список: /help"
        )

    @router.message(Command("help"))
    async def help_handler(message: Message) -> None:
        await touch(message)
        await message.answer(HELP_TEXT)

    @router.message(Command("add"))
    async def add_handler(message: Message, command: CommandObject) -> None:
        await touch(message)
        args = (command.args or "").strip()
        if not args and message.reply_to_message and message.reply_to_message.text:
            args = message.reply_to_message.text.strip()
        if not args:
            await message.answer("Использование: /add <ссылка или название трека>")
            return
        await add_track_to_queue(message, args, to_top=False)

    @router.message(Command("add_top"))
    async def add_top_handler(message: Message, command: CommandObject) -> None:
        await touch(message)
        if not await is_dj_or_admin(message):
            await message.answer("Команда доступна только DJ или администраторам.")
            return
        args = (command.args or "").strip()
        if not args and message.reply_to_message and message.reply_to_message.text:
            args = message.reply_to_message.text.strip()
        if not args:
            await message.answer("Использование: /add_top <ссылка или название трека>")
            return
        await add_track_to_queue(message, args, to_top=True)

    @router.message(Command("queue"))
    async def queue_handler(message: Message) -> None:
        await touch(message)
        if not message.chat:
            return
        queue = await repo.list_queue(message.chat.id, limit=20)
        if not queue:
            await message.answer("Очередь пуста.")
            return
        lines = ["Очередь:"]
        for item in queue:
            lines.append(f"{item['position']}. {_format_track_with_source(item)}")
        await message.answer("\n".join(lines))

    @router.message(Command("now"))
    async def now_handler(message: Message) -> None:
        await touch(message)
        if not message.chat:
            return
        now_item = await repo.get_now(message.chat.id)
        if not now_item:
            await message.answer("Сейчас ничего не играет. Очередь пуста.")
            return
        await message.answer(f"Сейчас: {_format_track_with_source(now_item)}")

    @router.message(Command("skip"))
    async def skip_handler(message: Message, command: CommandObject) -> None:
        await touch(message)
        if not message.chat or not message.from_user:
            return
        now_item = await repo.get_now(message.chat.id)
        if not now_item:
            await message.answer("Нечего скипать, очередь пуста.")
            return

        force = (command.args or "").strip().lower() == "force"
        if force:
            if not await is_dj_or_admin(message):
                await message.answer("Принудительный skip доступен только DJ или администраторам.")
                return
            removed = await repo.pop_now(
                chat_id=message.chat.id,
                removed_by=message.from_user.id,
                event_type="skip_forced",
            )
            if not removed:
                await message.answer("Не удалось выполнить skip.")
                return
            await send_voice_command(message, message.chat.id, "next")
            await message.answer(f"Принудительный skip: {_format_track_with_source(removed)}")
            return

        added, votes = await repo.add_skip_vote(
            chat_id=message.chat.id,
            queue_item_id=now_item["queue_item_id"],
            user_id=message.from_user.id,
        )
        active = max(1, await repo.active_participants(message.chat.id, days=7))
        required_by_activity = max(1, math.ceil(active * 0.4))

        if votes >= 3 or votes >= required_by_activity:
            removed = await repo.pop_now(
                chat_id=message.chat.id,
                removed_by=message.from_user.id,
                event_type="skip_success",
            )
            if not removed:
                await message.answer("Трек уже был удален из очереди.")
                return
            await send_voice_command(message, message.chat.id, "next")
            await message.answer(f"Skip принят ({votes} голосов). Удален: {_format_track_with_source(removed)}")
            return

        if not added:
            await message.answer(
                f"Ты уже голосовал. Сейчас {votes}. Нужно 3 голоса или {required_by_activity} (40% активных)."
            )
            return

        await message.answer(
            f"Голос учтен. Сейчас {votes}. Нужно 3 голоса или {required_by_activity} (40% активных)."
        )

    @router.message(Command("save"))
    async def save_handler(message: Message) -> None:
        await touch(message)
        if not message.chat or not message.from_user:
            return
        status, track = await repo.save_current_track(message.chat.id, message.from_user.id)
        if status == "empty":
            await message.answer("Нечего сохранять: очередь пуста.")
            return
        if track is None:
            await message.answer("Не удалось сохранить трек.")
            return
        if status == "duplicate":
            await message.answer(f"Этот трек уже есть в плейлисте: {_format_track_with_source(track)}")
            return
        await message.answer(f"Сохранил в плейлист: {_format_track_with_source(track)}")

    @router.message(Command("playlist"))
    async def playlist_handler(message: Message) -> None:
        await touch(message)
        if not message.chat:
            return
        playlist = await repo.list_playlist(message.chat.id, limit=25)
        if not playlist:
            await message.answer("Плейлист пуст.")
            return
        lines = ["Плейлист группы:"]
        for index, item in enumerate(playlist, start=1):
            lines.append(f"{index}. {_format_track_with_source(item)}")
        await message.answer("\n".join(lines))

    @router.message(Command("playlist_top"))
    async def playlist_top_handler(message: Message) -> None:
        await touch(message)
        if not message.chat:
            return
        top = await repo.playlist_top_artists(message.chat.id, limit=10)
        if not top:
            await message.answer("Пока недостаточно данных в плейлисте.")
            return
        lines = ["Топ артистов плейлиста:"]
        for index, row in enumerate(top, start=1):
            artist = row.get("artist") or "Unknown artist"
            lines.append(f"{index}. {artist} - {row['count']} трек(ов)")
        await message.answer("\n".join(lines))

    @router.message(Command("mode"))
    async def mode_handler(message: Message, command: CommandObject) -> None:
        await touch(message)
        if not message.chat:
            return
        args = (command.args or "").strip().lower()
        if not args:
            current = await repo.get_mode(message.chat.id)
            await message.answer(
                f"Текущий режим: {current}\n"
                "Доступные режимы: work, party, chill, road\n"
                "Используй: /mode <режим>"
            )
            return
        if args not in MODES:
            await message.answer("Неизвестный режим. Доступно: work, party, chill, road.")
            return

        await repo.set_mode(message.chat.id, args)
        recs = await music.recommend(args, count=3)
        if not recs:
            await message.answer(f"Режим переключен на {args}.")
            return
        lines = [f"Режим переключен на {args}.", "Рекомендации:"]
        for rec in recs:
            lines.append(f"- {display_track({'artist': rec.artist, 'title': rec.title})}")
        await message.answer("\n".join(lines))

    @router.message(Command("recommend"))
    async def recommend_handler(message: Message, command: CommandObject) -> None:
        await touch(message)
        if not message.chat:
            return
        count = 5
        args = (command.args or "").strip()
        if args and args.isdigit():
            count = max(1, min(10, int(args)))
        mode = await repo.get_mode(message.chat.id)
        recs = await music.recommend(mode, count=count)
        if not recs:
            await message.answer("Не удалось получить рекомендации сейчас.")
            return
        lines = [f"Рекомендации для режима {mode}:"]
        for index, rec in enumerate(recs, start=1):
            lines.append(f"{index}. {display_track({'artist': rec.artist, 'title': rec.title})}")
        await message.answer("\n".join(lines))

    @router.message(Command("set_start"))
    async def set_start_handler(message: Message, command: CommandObject) -> None:
        await touch(message)
        if not message.chat or not message.from_user:
            return
        if not await is_dj_or_admin(message):
            await message.answer("Команда доступна только DJ или администраторам.")
            return
        count = 5
        args = (command.args or "").strip()
        if args and args.isdigit():
            count = max(1, min(10, int(args)))

        mode = await repo.get_mode(message.chat.id)
        recs = await music.recommend(mode, count=count)
        if not recs:
            await message.answer("Не удалось подобрать сет по текущему режиму.")
            return

        added = 0
        for rec in recs:
            payload = _track_lookup_to_payload(rec)
            track = await repo.get_or_create_track(payload, added_by=message.from_user.id)
            await repo.add_queue_item(
                chat_id=message.chat.id,
                track_id=track["id"],
                user_id=message.from_user.id,
                genre=track.get("genre"),
                to_top=False,
            )
            added += 1

        await message.answer(f"Добавил сет по режиму {mode}: {added} трек(ов).")

    @router.message(Command("move"))
    async def move_handler(message: Message, command: CommandObject) -> None:
        await touch(message)
        if not message.chat:
            return
        if not await is_dj_or_admin(message):
            await message.answer("Команда доступна только DJ или администраторам.")
            return
        args = (command.args or "").strip().split()
        if len(args) != 2 or not all(item.isdigit() for item in args):
            await message.answer("Использование: /move <from_position> <to_position>")
            return
        from_position, to_position = int(args[0]), int(args[1])
        success = await repo.move_queue_item(message.chat.id, from_position, to_position)
        if not success:
            await message.answer("Не удалось переместить трек. Проверь позиции.")
            return
        await message.answer(f"Переместил трек из позиции {from_position} в {to_position}.")

    @router.message(Command("dj_add"))
    async def dj_add_handler(message: Message, command: CommandObject) -> None:
        await touch(message)
        if not message.chat or not message.from_user:
            return
        if not await is_admin(message):
            await message.answer("Назначать DJ может только администратор.")
            return
        target, error = await resolve_target_user(message, command.args)
        if error:
            await message.answer(error)
            return
        if not target:
            await message.answer("Не удалось определить пользователя.")
            return

        created = await repo.add_dj(message.chat.id, target["user_id"], message.from_user.id)
        if not created:
            await message.answer("Этот пользователь уже DJ.")
            return
        label = f"@{target['username']}" if target.get("username") else target["full_name"]
        await message.answer(f"Выдана роль DJ: {label}")

    @router.message(Command("dj_remove"))
    async def dj_remove_handler(message: Message, command: CommandObject) -> None:
        await touch(message)
        if not message.chat:
            return
        if not await is_admin(message):
            await message.answer("Снимать DJ может только администратор.")
            return
        target, error = await resolve_target_user(message, command.args)
        if error:
            await message.answer(error)
            return
        if not target:
            await message.answer("Не удалось определить пользователя.")
            return

        removed = await repo.remove_dj(message.chat.id, target["user_id"])
        if not removed:
            await message.answer("Пользователь не является DJ.")
            return
        label = f"@{target['username']}" if target.get("username") else target["full_name"]
        await message.answer(f"Роль DJ снята: {label}")

    @router.message(Command("dj_list"))
    async def dj_list_handler(message: Message) -> None:
        await touch(message)
        if not message.chat:
            return
        djs = await repo.list_djs(message.chat.id)
        if not djs:
            await message.answer("DJ пока не назначены.")
            return
        lines = ["Список DJ:"]
        for index, row in enumerate(djs, start=1):
            label = f"@{row['username']}" if row.get("username") else row.get("full_name")
            lines.append(f"{index}. {label} ({row['user_id']})")
        await message.answer("\n".join(lines))

    async def send_stats(message: Message, days: int | None) -> None:
        if not message.chat:
            return
        stats = await repo.get_stats(message.chat.id, days=days)
        top_user = stats.get("top_user")
        top_user_label = _format_user_label(top_user, top_user["user_id"]) if top_user else "-"
        top_user_count = top_user["count"] if top_user else 0
        top_genre = stats.get("top_genre")
        top_genre_label = top_genre["genre"] if top_genre else "-"
        top_genre_count = top_genre["count"] if top_genre else 0
        period = "7 дней" if days else "всё время"
        await message.answer(
            f"Статистика ({period}):\n"
            f"Добавлено в очередь: {stats['total_added']}\n"
            f"Сохранено в плейлист: {stats['total_saved']}\n"
            f"Топ пользователь: {top_user_label} ({top_user_count})\n"
            f"Топ жанр: {top_genre_label} ({top_genre_count})"
        )

    @router.message(Command("stats"))
    async def stats_handler(message: Message) -> None:
        await touch(message)
        await send_stats(message, days=None)

    @router.message(Command("stats_week"))
    async def stats_week_handler(message: Message) -> None:
        await touch(message)
        await send_stats(message, days=7)

    @router.message(Command("top_users"))
    async def top_users_handler(message: Message) -> None:
        await touch(message)
        if not message.chat:
            return
        rows = await repo.top_users(message.chat.id, days=None, limit=10)
        if not rows:
            await message.answer("Пока нет данных.")
            return
        lines = ["Топ пользователей по добавлениям:"]
        for index, row in enumerate(rows, start=1):
            label = _format_user_label(row, row["user_id"])
            lines.append(f"{index}. {label} - {row['count']}")
        await message.answer("\n".join(lines))

    @router.message(Command("top_genres"))
    async def top_genres_handler(message: Message) -> None:
        await touch(message)
        if not message.chat:
            return
        rows = await repo.top_genres(message.chat.id, days=None, limit=10)
        if not rows:
            await message.answer("Пока нет данных по жанрам.")
            return
        lines = ["Топ жанров:"]
        for index, row in enumerate(rows, start=1):
            lines.append(f"{index}. {row['genre']} - {row['count']}")
        await message.answer("\n".join(lines))

    @router.message(Command("join"))
    async def join_handler(message: Message, command: CommandObject) -> None:
        chat_id = await resolve_voice_chat_id(message, command.args)
        if chat_id is None:
            return
        await touch_for_chat(message, chat_id)
        if not await is_member_in_chat(message, chat_id):
            await message.answer("Ты не участник этой группы.")
            return
        if not await is_dj_or_admin_in_chat(message, chat_id):
            await message.answer("Команда доступна только DJ или администраторам.")
            return
        if not await voice_enabled_for_chat(message, chat_id):
            return
        await send_voice_command(message, chat_id, "join")
        await message.answer(
            "Команда отправлена player-сервису. Запусти /play, чтобы начать стрим в voice chat."
        )

    @router.message(Command("play"))
    async def play_handler(message: Message, command: CommandObject) -> None:
        chat_id = await resolve_voice_chat_id(message, command.args)
        if chat_id is None:
            return
        await touch_for_chat(message, chat_id)
        if not await is_member_in_chat(message, chat_id):
            await message.answer("Ты не участник этой группы.")
            return
        if not await is_dj_or_admin_in_chat(message, chat_id):
            await message.answer("Команда доступна только DJ или администраторам.")
            return
        if not await voice_enabled_for_chat(message, chat_id):
            return
        now_item = await repo.get_now(chat_id)
        if not now_item:
            await message.answer("Очередь пуста. Добавь трек через /add.")
            return
        await send_voice_command(message, chat_id, "play")
        await message.answer("Запрос на старт стрима отправлен player-сервису.")

    @router.message(Command("pause"))
    async def pause_handler(message: Message, command: CommandObject) -> None:
        chat_id = await resolve_voice_chat_id(message, command.args)
        if chat_id is None:
            return
        await touch_for_chat(message, chat_id)
        if not await is_member_in_chat(message, chat_id):
            await message.answer("Ты не участник этой группы.")
            return
        if not await is_dj_or_admin_in_chat(message, chat_id):
            await message.answer("Команда доступна только DJ или администраторам.")
            return
        if not await voice_enabled_for_chat(message, chat_id):
            return
        await send_voice_command(message, chat_id, "pause")
        await message.answer("Запрос на паузу отправлен.")

    @router.message(Command("resume"))
    async def resume_handler(message: Message, command: CommandObject) -> None:
        chat_id = await resolve_voice_chat_id(message, command.args)
        if chat_id is None:
            return
        await touch_for_chat(message, chat_id)
        if not await is_member_in_chat(message, chat_id):
            await message.answer("Ты не участник этой группы.")
            return
        if not await is_dj_or_admin_in_chat(message, chat_id):
            await message.answer("Команда доступна только DJ или администраторам.")
            return
        if not await voice_enabled_for_chat(message, chat_id):
            return
        await send_voice_command(message, chat_id, "resume")
        await message.answer("Запрос на продолжение отправлен.")

    @router.message(Command("stop"))
    async def stop_handler(message: Message, command: CommandObject) -> None:
        chat_id = await resolve_voice_chat_id(message, command.args)
        if chat_id is None:
            return
        await touch_for_chat(message, chat_id)
        if not await is_member_in_chat(message, chat_id):
            await message.answer("Ты не участник этой группы.")
            return
        if not await is_dj_or_admin_in_chat(message, chat_id):
            await message.answer("Команда доступна только DJ или администраторам.")
            return
        if not await voice_enabled_for_chat(message, chat_id):
            return
        await send_voice_command(message, chat_id, "stop")
        await message.answer("Запрос на остановку отправлен.")

    @router.message(Command("leave"))
    async def leave_handler(message: Message, command: CommandObject) -> None:
        chat_id = await resolve_voice_chat_id(message, command.args)
        if chat_id is None:
            return
        await touch_for_chat(message, chat_id)
        if not await is_member_in_chat(message, chat_id):
            await message.answer("Ты не участник этой группы.")
            return
        if not await is_dj_or_admin_in_chat(message, chat_id):
            await message.answer("Команда доступна только DJ или администраторам.")
            return
        if not await voice_enabled_for_chat(message, chat_id):
            return
        await send_voice_command(message, chat_id, "leave")
        await message.answer("Запрос на выход из voice chat отправлен.")

    @router.message(Command("vstatus"))
    async def voice_status_handler(message: Message, command: CommandObject) -> None:
        chat_id = await resolve_voice_chat_id(message, command.args)
        if chat_id is None:
            return
        await touch_for_chat(message, chat_id)
        if not await is_member_in_chat(message, chat_id):
            await message.answer("Ты не участник этой группы.")
            return
        if not await voice_enabled_for_chat(message, chat_id):
            return
        state = await voice_bus.get_state(chat_id) if voice_bus else {}
        await message.answer(_render_voice_state(state))

    dp.include_router(router)
    return dp


def get_default_commands() -> list[BotCommand]:
    return [
        BotCommand(command="add", description="Добавить трек в очередь"),
        BotCommand(command="queue", description="Показать очередь"),
        BotCommand(command="now", description="Текущий трек"),
        BotCommand(command="skip", description="Голос за skip"),
        BotCommand(command="save", description="Сохранить трек в плейлист"),
        BotCommand(command="playlist", description="Плейлист группы"),
        BotCommand(command="mode", description="Установить режим"),
        BotCommand(command="recommend", description="Показать рекомендации"),
        BotCommand(command="join", description="Подготовить voice-плеер"),
        BotCommand(command="play", description="Старт стрима в voice chat"),
        BotCommand(command="vstatus", description="Статус voice-плеера"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="help", description="Все команды"),
    ]


async def configure_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(get_default_commands())


def validate_mode(value: str) -> bool:
    return value in MODE_SEEDS
