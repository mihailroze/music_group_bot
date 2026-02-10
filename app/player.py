from __future__ import annotations

import asyncio
import logging
from typing import Any

from pyrogram import Client
import pyrogram.errors as pyrogram_errors
from pytgcalls import PyTgCalls, filters
from pytgcalls.types import GroupCallConfig
from pytgcalls.types.stream import AudioQuality, MediaStream, StreamEnded

from app.db import DatabaseRepository
from app.music import MusicClient
from app.utils import display_track
from app.voice import VoiceBus, VoiceCommand


logger = logging.getLogger(__name__)


def _track_title(track: dict[str, Any]) -> str:
    return display_track(track)


def _ensure_groupcall_forbidden_error() -> None:
    """
    py-tgcalls currently imports `GroupcallForbidden` from `pyrogram.errors`,
    but Pyrogram 2.0.106 doesn't define it. Inject a compatible stub so
    pytgcalls can import and run.
    """

    if hasattr(pyrogram_errors, "GroupcallForbidden"):
        return

    # Best-effort: make it a Forbidden-like RPC error.
    base = getattr(pyrogram_errors, "Forbidden", Exception)

    class GroupcallForbidden(base):  # type: ignore[misc]
        ID = "GROUPCALL_FORBIDDEN"

    setattr(pyrogram_errors, "GroupcallForbidden", GroupcallForbidden)

    # Also patch pyrogram's error mapping so RPC errors can resolve to it.
    try:
        from pyrogram.errors.exceptions.all import exceptions as _exceptions_map  # type: ignore

        for code in (403, 400):
            if code in _exceptions_map and "GROUPCALL_FORBIDDEN" not in _exceptions_map[code]:
                _exceptions_map[code]["GROUPCALL_FORBIDDEN"] = "GroupcallForbidden"
    except Exception:
        # If internals change, the import fix above is still enough to avoid crashing.
        return


class VoicePlayer:
    def __init__(
        self,
        *,
        repo: DatabaseRepository,
        bus: VoiceBus,
        music: MusicClient,
        telegram_api_id: int,
        telegram_api_hash: str,
        assistant_session_string: str | None = None,
        bot_token: str | None = None,
        target_chat_id: int | None = None,
    ) -> None:
        self._repo = repo
        self._bus = bus
        self._music = music
        self._target_chat_id = target_chat_id

        client_kwargs: dict[str, Any] = {
            "name": "group_music_player",
            "api_id": telegram_api_id,
            "api_hash": telegram_api_hash,
            "workdir": ".",
        }
        if assistant_session_string:
            client_kwargs["session_string"] = assistant_session_string
        elif bot_token:
            # in_memory avoids writing a local session file in stateless environments
            client_kwargs["bot_token"] = bot_token
            client_kwargs["in_memory"] = True
        else:
            raise ValueError("Player requires assistant_session_string or bot_token")

        self._client = Client(**client_kwargs)
        _ensure_groupcall_forbidden_error()
        self._calls = PyTgCalls(self._client)
        self._current_track_id_by_chat: dict[int, int] = {}
        self._running = False

        @self._calls.on_update(filters.stream_end())
        async def _on_stream_end(_: PyTgCalls, update: StreamEnded) -> None:
            await self._handle_stream_end(update.chat_id)

    async def start(self) -> None:
        await self._repo.init()
        await self._client.start()
        await self._calls.start()
        self._running = True
        logger.info("Voice player started")

        while self._running:
            command = await self._bus.consume(timeout_seconds=5)
            if command is None:
                continue

            if not command.action:
                continue

            if self._target_chat_id is not None and command.chat_id != self._target_chat_id:
                continue

            try:
                await self._handle_command(command)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to handle voice command '%s' for chat=%s",
                    command.action,
                    command.chat_id,
                )
                await self._bus.set_state(
                    command.chat_id,
                    status="error",
                    message=f"Failed command: {command.action}",
                )

    async def stop(self) -> None:
        self._running = False
        for chat_id in list(self._current_track_id_by_chat.keys()):
            await self._safe_leave(chat_id)
        await self._client.stop()
        await self._music.close()
        await self._repo.close()
        await self._bus.close()

    async def _handle_command(self, command: VoiceCommand) -> None:
        action = command.action.lower().strip()
        chat_id = command.chat_id

        if action == "join":
            await self._bus.set_state(
                chat_id,
                status="ready",
                message="Voice player is ready. Use /play to start stream.",
            )
            return

        if action in {"play", "sync", "next"}:
            await self._play_queue_head(chat_id)
            return

        if action == "pause":
            paused = await self._calls.pause(chat_id)
            await self._bus.set_state(
                chat_id,
                status="paused" if paused else "error",
                message="Paused" if paused else "Pause failed (voice call not active)",
            )
            return

        if action == "resume":
            resumed = await self._calls.resume(chat_id)
            await self._bus.set_state(
                chat_id,
                status="playing" if resumed else "error",
                message="Resumed" if resumed else "Resume failed (voice call not active)",
            )
            return

        if action in {"stop", "leave"}:
            await self._safe_leave(chat_id)
            await self._bus.set_state(
                chat_id,
                status="stopped",
                message="Player disconnected from voice chat",
            )
            self._current_track_id_by_chat.pop(chat_id, None)
            return

        await self._bus.set_state(
            chat_id,
            status="error",
            message=f"Unknown action: {action}",
        )

    async def _play_queue_head(self, chat_id: int) -> None:
        now_item = await self._repo.get_now(chat_id)
        if not now_item:
            await self._bus.set_state(
                chat_id,
                status="idle",
                message="Queue is empty",
            )
            self._current_track_id_by_chat.pop(chat_id, None)
            return

        source_url = await self._resolve_source_url(now_item)
        if not source_url:
            await self._bus.set_state(
                chat_id,
                status="error",
                message=f"No playable source for: {_track_title(now_item)}",
            )
            return

        stream = MediaStream(
            source_url,
            audio_parameters=AudioQuality.HIGH,
        )
        await self._calls.play(
            chat_id=chat_id,
            stream=stream,
            config=GroupCallConfig(auto_start=True),
        )

        self._current_track_id_by_chat[chat_id] = int(now_item["track_id"])
        await self._bus.set_state(
            chat_id,
            status="playing",
            message="Streaming in group voice chat",
            track_title=_track_title(now_item),
            track_url=source_url,
            track_id=int(now_item["track_id"]),
        )

    async def _resolve_source_url(self, track: dict[str, Any]) -> str | None:
        existing = (track.get("source_url") or "").strip()
        if existing:
            return existing

        query_parts = [track.get("artist"), track.get("title")]
        query = " - ".join(part for part in query_parts if part)
        if not query:
            query = (track.get("title") or "").strip()
        if not query:
            return None

        looked_up = await self._music.search_track(query)
        if looked_up and looked_up.source_url:
            return looked_up.source_url
        return None

    async def _handle_stream_end(self, chat_id: int) -> None:
        if self._target_chat_id is not None and chat_id != self._target_chat_id:
            return

        try:
            now_item = await self._repo.get_now(chat_id)
            current_track_id = self._current_track_id_by_chat.get(chat_id)

            if now_item and current_track_id and int(now_item["track_id"]) == int(current_track_id):
                await self._repo.pop_now(
                    chat_id=chat_id,
                    removed_by=0,
                    event_type="stream_end",
                )

            next_item = await self._repo.get_now(chat_id)
            if not next_item:
                self._current_track_id_by_chat.pop(chat_id, None)
                await self._bus.set_state(
                    chat_id,
                    status="idle",
                    message="Queue finished",
                )
                return

            await self._play_queue_head(chat_id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to continue playback for chat=%s after stream end", chat_id)
            await self._bus.set_state(
                chat_id,
                status="error",
                message="Failed to auto-continue playback",
            )

    async def _safe_leave(self, chat_id: int) -> None:
        try:
            await self._calls.leave_call(chat_id)
        except Exception:  # noqa: BLE001
            logger.warning("leave_call ignored (chat=%s)", chat_id, exc_info=True)
