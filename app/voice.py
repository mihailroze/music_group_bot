from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis


VOICE_COMMAND_QUEUE_KEY = "group_music:voice:commands"
VOICE_STATE_KEY_PREFIX = "group_music:voice:state"


def _utc_iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(slots=True)
class VoiceCommand:
    action: str
    chat_id: int
    requested_by: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_iso_now)

    def to_json(self) -> str:
        return json.dumps(
            {
                "action": self.action,
                "chat_id": self.chat_id,
                "requested_by": self.requested_by,
                "payload": self.payload,
                "created_at": self.created_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "VoiceCommand":
        data = json.loads(raw)
        return cls(
            action=str(data.get("action", "")).strip(),
            chat_id=int(data.get("chat_id", 0)),
            requested_by=int(data["requested_by"]) if data.get("requested_by") is not None else None,
            payload=data.get("payload") or {},
            created_at=str(data.get("created_at") or _utc_iso_now()),
        )


class VoiceBus:
    def __init__(
        self,
        redis_url: str,
        command_queue_key: str = VOICE_COMMAND_QUEUE_KEY,
        state_key_prefix: str = VOICE_STATE_KEY_PREFIX,
        state_ttl_seconds: int = 24 * 60 * 60,
    ) -> None:
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._queue_key = command_queue_key
        self._state_prefix = state_key_prefix
        self._state_ttl_seconds = state_ttl_seconds

    async def close(self) -> None:
        await self._redis.aclose()

    async def publish(self, command: VoiceCommand) -> None:
        await self._redis.rpush(self._queue_key, command.to_json())

    async def consume(self, timeout_seconds: int = 5) -> VoiceCommand | None:
        row = await self._redis.blpop(self._queue_key, timeout=timeout_seconds)
        if row is None:
            return None
        _, raw_payload = row
        if not raw_payload:
            return None
        return VoiceCommand.from_json(raw_payload)

    async def set_state(
        self,
        chat_id: int,
        *,
        status: str,
        message: str | None = None,
        track_title: str | None = None,
        track_url: str | None = None,
        track_id: int | None = None,
    ) -> None:
        key = self._state_key(chat_id)
        payload = {
            "status": status,
            "updated_at": _utc_iso_now(),
        }
        if message:
            payload["message"] = message
        if track_title:
            payload["track_title"] = track_title
        if track_url:
            payload["track_url"] = track_url
        if track_id is not None:
            payload["track_id"] = str(track_id)

        pipe = self._redis.pipeline()
        pipe.delete(key)
        pipe.hset(key, mapping=payload)
        pipe.expire(key, self._state_ttl_seconds)
        await pipe.execute()

    async def get_state(self, chat_id: int) -> dict[str, str]:
        key = self._state_key(chat_id)
        return await self._redis.hgetall(key)

    def _state_key(self, chat_id: int) -> str:
        return f"{self._state_prefix}:{chat_id}"
