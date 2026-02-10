from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, Chat, DJ, Event, PlaylistItem, QueueItem, SkipVote, Track, User, UserActivity


class DatabaseRepository:
    def __init__(self, database_url: str) -> None:
        self._ensure_sqlite_dir(database_url)
        self._engine: AsyncEngine = create_async_engine(database_url, future=True, pool_pre_ping=True)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False, class_=AsyncSession)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def register_user_activity(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        full_name: str,
    ) -> None:
        now = datetime.utcnow()
        username_norm = username.lower() if username else None
        async with self._session_factory() as session:
            await self._ensure_chat(session, chat_id)

            user = await session.get(User, user_id)
            if user is None:
                session.add(
                    User(
                        user_id=user_id,
                        username=username_norm,
                        full_name=full_name,
                    )
                )
            else:
                user.username = username_norm
                user.full_name = full_name

            activity = await session.get(UserActivity, (chat_id, user_id))
            if activity is None:
                session.add(UserActivity(chat_id=chat_id, user_id=user_id, last_seen=now))
            else:
                activity.last_seen = now

            await session.commit()

    async def upsert_user(self, user_id: int, username: str | None, full_name: str) -> None:
        username_norm = username.lower() if username else None
        async with self._session_factory() as session:
            user = await session.get(User, user_id)
            if user is None:
                session.add(User(user_id=user_id, username=username_norm, full_name=full_name))
            else:
                user.username = username_norm
                user.full_name = full_name
            await session.commit()

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            user = await session.get(User, user_id)
            if user is None:
                return None
            return {
                "user_id": user.user_id,
                "username": user.username,
                "full_name": user.full_name,
            }

    async def find_user_id_by_username(self, username: str) -> int | None:
        username_norm = username.lower().lstrip("@")
        async with self._session_factory() as session:
            stmt = select(User.user_id).where(User.username == username_norm).limit(1)
            row = await session.execute(stmt)
            user_id = row.scalar_one_or_none()
            return int(user_id) if user_id is not None else None

    async def get_mode(self, chat_id: int) -> str:
        async with self._session_factory() as session:
            chat = await session.get(Chat, chat_id)
            if chat is None:
                chat = Chat(chat_id=chat_id, mode="chill")
                session.add(chat)
                await session.commit()
            return chat.mode

    async def set_mode(self, chat_id: int, mode: str) -> None:
        async with self._session_factory() as session:
            chat = await session.get(Chat, chat_id)
            if chat is None:
                chat = Chat(chat_id=chat_id, mode=mode)
                session.add(chat)
            else:
                chat.mode = mode
            await session.commit()

    async def is_dj(self, chat_id: int, user_id: int) -> bool:
        async with self._session_factory() as session:
            dj = await session.get(DJ, (chat_id, user_id))
            return dj is not None

    async def add_dj(self, chat_id: int, user_id: int, added_by: int) -> bool:
        async with self._session_factory() as session:
            await self._ensure_chat(session, chat_id)
            existing = await session.get(DJ, (chat_id, user_id))
            if existing is not None:
                return False
            session.add(DJ(chat_id=chat_id, user_id=user_id, added_by=added_by))
            await session.commit()
            return True

    async def remove_dj(self, chat_id: int, user_id: int) -> bool:
        async with self._session_factory() as session:
            existing = await session.get(DJ, (chat_id, user_id))
            if existing is None:
                return False
            await session.delete(existing)
            await session.commit()
            return True

    async def list_djs(self, chat_id: int) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            stmt = (
                select(DJ.user_id, User.full_name, User.username)
                .outerjoin(User, User.user_id == DJ.user_id)
                .where(DJ.chat_id == chat_id)
                .order_by(DJ.created_at.asc())
            )
            rows = (await session.execute(stmt)).all()
            return [
                {
                    "user_id": row.user_id,
                    "full_name": row.full_name or str(row.user_id),
                    "username": row.username,
                }
                for row in rows
            ]

    async def get_or_create_track(self, track_data: dict[str, Any], added_by: int | None) -> dict[str, Any]:
        source_url = track_data.get("source_url")
        fingerprint = track_data.get("fingerprint")

        async with self._session_factory() as session:
            existing = None
            conditions = []
            if source_url:
                conditions.append(Track.source_url == source_url)
            if fingerprint:
                conditions.append(Track.fingerprint == fingerprint)

            if conditions:
                stmt = select(Track).where(or_(*conditions)).limit(1)
                existing = (await session.execute(stmt)).scalar_one_or_none()

            if existing is not None:
                if not existing.artist and track_data.get("artist"):
                    existing.artist = track_data["artist"]
                if not existing.genre and track_data.get("genre"):
                    existing.genre = track_data["genre"]
                if existing.title == "Unknown title" and track_data.get("title"):
                    existing.title = track_data["title"]
                await session.commit()
                return self._track_payload(existing)

            track = Track(
                source_url=source_url,
                query_text=track_data.get("query_text"),
                artist=track_data.get("artist"),
                title=track_data.get("title") or "Unknown title",
                genre=track_data.get("genre"),
                fingerprint=fingerprint,
                added_by=added_by,
            )
            session.add(track)
            await session.commit()
            return self._track_payload(track)

    async def add_queue_item(
        self,
        chat_id: int,
        track_id: int,
        user_id: int,
        genre: str | None,
        to_top: bool = False,
    ) -> tuple[int, int]:
        async with self._session_factory() as session:
            await self._ensure_chat(session, chat_id)

            if to_top:
                min_position = await session.scalar(
                    select(func.min(QueueItem.position)).where(QueueItem.chat_id == chat_id)
                )
                position = (min_position - 1) if min_position is not None else 1
            else:
                max_position = await session.scalar(
                    select(func.max(QueueItem.position)).where(QueueItem.chat_id == chat_id)
                )
                position = (max_position + 1) if max_position is not None else 1

            queue_item = QueueItem(
                chat_id=chat_id,
                track_id=track_id,
                added_by=user_id,
                position=position,
            )
            session.add(queue_item)
            await session.flush()

            await self._normalize_queue_positions(session, chat_id)
            current_position = await session.scalar(
                select(QueueItem.position).where(QueueItem.id == queue_item.id).limit(1)
            )
            session.add(
                Event(
                    chat_id=chat_id,
                    user_id=user_id,
                    event_type="queue_add",
                    track_id=track_id,
                    genre=genre,
                )
            )
            await session.commit()
            return queue_item.id, int(current_position or 1)

    async def list_queue(self, chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            stmt = (
                select(
                    QueueItem.id.label("queue_item_id"),
                    QueueItem.position.label("position"),
                    QueueItem.added_by.label("added_by"),
                    Track.id.label("track_id"),
                    Track.artist.label("artist"),
                    Track.title.label("title"),
                    Track.genre.label("genre"),
                    Track.source_url.label("source_url"),
                    Track.fingerprint.label("fingerprint"),
                )
                .join(Track, Track.id == QueueItem.track_id)
                .where(QueueItem.chat_id == chat_id)
                .order_by(QueueItem.position.asc(), QueueItem.id.asc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).mappings().all()
            return [dict(row) for row in rows]

    async def get_now(self, chat_id: int) -> dict[str, Any] | None:
        rows = await self.list_queue(chat_id=chat_id, limit=1)
        return rows[0] if rows else None

    async def pop_now(
        self,
        chat_id: int,
        removed_by: int,
        event_type: str = "skip_success",
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            stmt = (
                select(QueueItem, Track)
                .join(Track, Track.id == QueueItem.track_id)
                .where(QueueItem.chat_id == chat_id)
                .order_by(QueueItem.position.asc(), QueueItem.id.asc())
                .limit(1)
            )
            row = (await session.execute(stmt)).first()
            if row is None:
                return None

            queue_item, track = row
            payload = {
                "queue_item_id": queue_item.id,
                "position": queue_item.position,
                "added_by": queue_item.added_by,
                "track_id": track.id,
                "artist": track.artist,
                "title": track.title,
                "genre": track.genre,
                "source_url": track.source_url,
                "fingerprint": track.fingerprint,
            }

            await session.execute(delete(SkipVote).where(SkipVote.queue_item_id == queue_item.id))
            await session.delete(queue_item)
            await self._normalize_queue_positions(session, chat_id)
            session.add(
                Event(
                    chat_id=chat_id,
                    user_id=removed_by,
                    event_type=event_type,
                    track_id=track.id,
                    genre=track.genre,
                )
            )
            await session.commit()
            return payload

    async def add_skip_vote(self, chat_id: int, queue_item_id: int, user_id: int) -> tuple[bool, int]:
        async with self._session_factory() as session:
            existing_vote_stmt = (
                select(SkipVote.id)
                .where(
                    and_(
                        SkipVote.chat_id == chat_id,
                        SkipVote.queue_item_id == queue_item_id,
                        SkipVote.user_id == user_id,
                    )
                )
                .limit(1)
            )
            exists = (await session.execute(existing_vote_stmt)).scalar_one_or_none()
            if exists is not None:
                current_votes = await session.scalar(
                    select(func.count(SkipVote.id)).where(
                        and_(
                            SkipVote.chat_id == chat_id,
                            SkipVote.queue_item_id == queue_item_id,
                        )
                    )
                )
                return False, int(current_votes or 0)

            session.add(SkipVote(chat_id=chat_id, queue_item_id=queue_item_id, user_id=user_id))
            session.add(Event(chat_id=chat_id, user_id=user_id, event_type="skip_vote"))
            await session.flush()

            current_votes = await session.scalar(
                select(func.count(SkipVote.id)).where(
                    and_(
                        SkipVote.chat_id == chat_id,
                        SkipVote.queue_item_id == queue_item_id,
                    )
                )
            )
            await session.commit()
            return True, int(current_votes or 0)

    async def count_skip_votes(self, chat_id: int, queue_item_id: int) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count(SkipVote.id)).where(
                    and_(
                        SkipVote.chat_id == chat_id,
                        SkipVote.queue_item_id == queue_item_id,
                    )
                )
            )
            return int(count or 0)

    async def active_participants(self, chat_id: int, days: int = 7) -> int:
        cutoff = datetime.utcnow() - timedelta(days=days)
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count(UserActivity.user_id)).where(
                    and_(
                        UserActivity.chat_id == chat_id,
                        UserActivity.last_seen >= cutoff,
                    )
                )
            )
            return int(count or 0)

    async def save_current_track(
        self, chat_id: int, user_id: int
    ) -> tuple[str, dict[str, Any] | None]:
        async with self._session_factory() as session:
            stmt = (
                select(QueueItem, Track)
                .join(Track, Track.id == QueueItem.track_id)
                .where(QueueItem.chat_id == chat_id)
                .order_by(QueueItem.position.asc(), QueueItem.id.asc())
                .limit(1)
            )
            row = (await session.execute(stmt)).first()
            if row is None:
                return "empty", None

            queue_item, track = row
            payload = {
                "queue_item_id": queue_item.id,
                "track_id": track.id,
                "artist": track.artist,
                "title": track.title,
                "genre": track.genre,
                "source_url": track.source_url,
                "fingerprint": track.fingerprint,
            }

            duplicate_conditions = []
            if track.source_url:
                duplicate_conditions.append(PlaylistItem.source_url == track.source_url)
            if track.fingerprint:
                duplicate_conditions.append(PlaylistItem.fingerprint == track.fingerprint)

            if duplicate_conditions:
                dup_stmt = (
                    select(PlaylistItem.id)
                    .where(and_(PlaylistItem.chat_id == chat_id, or_(*duplicate_conditions)))
                    .limit(1)
                )
                existing = (await session.execute(dup_stmt)).scalar_one_or_none()
                if existing is not None:
                    return "duplicate", payload

            playlist_item = PlaylistItem(
                chat_id=chat_id,
                track_id=track.id,
                source_url=track.source_url,
                fingerprint=track.fingerprint,
                artist=track.artist,
                title=track.title,
                genre=track.genre,
                added_by=user_id,
            )
            session.add(playlist_item)
            session.add(
                Event(
                    chat_id=chat_id,
                    user_id=user_id,
                    event_type="playlist_save",
                    track_id=track.id,
                    genre=track.genre,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return "duplicate", payload
            return "saved", payload

    async def list_playlist(self, chat_id: int, limit: int = 25) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            stmt = (
                select(
                    PlaylistItem.id.label("playlist_item_id"),
                    PlaylistItem.artist.label("artist"),
                    PlaylistItem.title.label("title"),
                    PlaylistItem.genre.label("genre"),
                    PlaylistItem.source_url.label("source_url"),
                    PlaylistItem.added_by.label("added_by"),
                    PlaylistItem.created_at.label("created_at"),
                )
                .where(PlaylistItem.chat_id == chat_id)
                .order_by(PlaylistItem.created_at.desc(), PlaylistItem.id.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).mappings().all()
            return [dict(row) for row in rows]

    async def playlist_top_artists(self, chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            artist_group = func.coalesce(PlaylistItem.artist, "Unknown artist")
            stmt = (
                select(
                    artist_group.label("artist"),
                    func.count(PlaylistItem.id).label("count"),
                )
                .where(PlaylistItem.chat_id == chat_id)
                .group_by(artist_group)
                .order_by(desc("count"), artist_group.asc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).mappings().all()
            return [dict(row) for row in rows]

    async def move_queue_item(self, chat_id: int, from_position: int, to_position: int) -> bool:
        async with self._session_factory() as session:
            stmt = (
                select(QueueItem)
                .where(QueueItem.chat_id == chat_id)
                .order_by(QueueItem.position.asc(), QueueItem.id.asc())
            )
            items = list((await session.execute(stmt)).scalars().all())
            if not items:
                return False

            if from_position < 1 or to_position < 1:
                return False
            if from_position > len(items) or to_position > len(items):
                return False
            if from_position == to_position:
                return True

            moved = items.pop(from_position - 1)
            items.insert(to_position - 1, moved)
            for index, item in enumerate(items, start=1):
                item.position = index
            await session.commit()
            return True

    async def get_stats(self, chat_id: int, days: int | None = None) -> dict[str, Any]:
        async with self._session_factory() as session:
            base_conditions = [Event.chat_id == chat_id]
            if days is not None:
                cutoff = datetime.utcnow() - timedelta(days=days)
                base_conditions.append(Event.created_at >= cutoff)

            queue_conditions = base_conditions + [Event.event_type == "queue_add"]
            save_conditions = base_conditions + [Event.event_type == "playlist_save"]

            total_added = await session.scalar(select(func.count(Event.id)).where(*queue_conditions))
            total_saved = await session.scalar(select(func.count(Event.id)).where(*save_conditions))

            top_user_stmt = (
                select(
                    Event.user_id.label("user_id"),
                    User.full_name.label("full_name"),
                    User.username.label("username"),
                    func.count(Event.id).label("count"),
                )
                .outerjoin(User, User.user_id == Event.user_id)
                .where(*queue_conditions)
                .group_by(Event.user_id, User.full_name, User.username)
                .order_by(desc("count"))
                .limit(1)
            )
            top_user_row = (await session.execute(top_user_stmt)).mappings().first()

            top_genre_stmt = (
                select(
                    Event.genre.label("genre"),
                    func.count(Event.id).label("count"),
                )
                .where(*queue_conditions, Event.genre.is_not(None))
                .group_by(Event.genre)
                .order_by(desc("count"))
                .limit(1)
            )
            top_genre_row = (await session.execute(top_genre_stmt)).mappings().first()

            return {
                "total_added": int(total_added or 0),
                "total_saved": int(total_saved or 0),
                "top_user": dict(top_user_row) if top_user_row else None,
                "top_genre": dict(top_genre_row) if top_genre_row else None,
            }

    async def top_users(
        self, chat_id: int, days: int | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            conditions = [Event.chat_id == chat_id, Event.event_type == "queue_add"]
            if days is not None:
                cutoff = datetime.utcnow() - timedelta(days=days)
                conditions.append(Event.created_at >= cutoff)

            stmt = (
                select(
                    Event.user_id.label("user_id"),
                    User.full_name.label("full_name"),
                    User.username.label("username"),
                    func.count(Event.id).label("count"),
                )
                .outerjoin(User, User.user_id == Event.user_id)
                .where(*conditions)
                .group_by(Event.user_id, User.full_name, User.username)
                .order_by(desc("count"))
                .limit(limit)
            )
            rows = (await session.execute(stmt)).mappings().all()
            return [dict(row) for row in rows]

    async def top_genres(
        self, chat_id: int, days: int | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            conditions = [Event.chat_id == chat_id, Event.event_type == "queue_add", Event.genre.is_not(None)]
            if days is not None:
                cutoff = datetime.utcnow() - timedelta(days=days)
                conditions.append(Event.created_at >= cutoff)

            stmt = (
                select(
                    Event.genre.label("genre"),
                    func.count(Event.id).label("count"),
                )
                .where(*conditions)
                .group_by(Event.genre)
                .order_by(desc("count"))
                .limit(limit)
            )
            rows = (await session.execute(stmt)).mappings().all()
            return [dict(row) for row in rows]

    async def _ensure_chat(self, session: AsyncSession, chat_id: int) -> None:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            session.add(Chat(chat_id=chat_id, mode="chill"))
            await session.flush()

    async def _normalize_queue_positions(self, session: AsyncSession, chat_id: int) -> None:
        stmt = (
            select(QueueItem)
            .where(QueueItem.chat_id == chat_id)
            .order_by(QueueItem.position.asc(), QueueItem.id.asc())
        )
        items = list((await session.execute(stmt)).scalars().all())
        for index, item in enumerate(items, start=1):
            item.position = index

    @staticmethod
    def _track_payload(track: Track) -> dict[str, Any]:
        return {
            "id": track.id,
            "source_url": track.source_url,
            "query_text": track.query_text,
            "artist": track.artist,
            "title": track.title,
            "genre": track.genre,
            "fingerprint": track.fingerprint,
            "added_by": track.added_by,
        }

    @staticmethod
    def _ensure_sqlite_dir(database_url: str) -> None:
        try:
            parsed = make_url(database_url)
        except Exception:
            return

        if parsed.get_backend_name() != "sqlite":
            return

        db_path = parsed.database
        if not db_path or db_path == ":memory:":
            return

        path = Path(db_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)
