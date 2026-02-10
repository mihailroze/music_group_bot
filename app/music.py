from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from yandex_music import Client as YandexMusicClient
from yandex_music.download_info import DownloadInfo
from yandex_music.exceptions import YandexMusicError


logger = logging.getLogger(__name__)


MODE_SEEDS: dict[str, list[str]] = {
    "work": ["lofi", "focus", "instrumental", "deep house", "ambient"],
    "party": ["dance", "edm", "hip hop", "club", "party"],
    "chill": ["chill", "downtempo", "indie", "ambient", "chillout"],
    "road": ["road trip", "driving", "rock", "highway", "travel"],
}


YANDEX_TRACK_SOURCE_PREFIX = "ym:track:"


_TRACK_URL_RE = re.compile(r"^/track/(?P<track_id>\d+)$")
_ALBUM_TRACK_URL_RE = re.compile(r"^/album/(?P<album_id>\d+)/track/(?P<track_id>\d+)$")


@dataclass(slots=True)
class TrackLookup:
    artist: str | None
    title: str
    genre: str | None
    source_url: str | None
    query_text: str


def yandex_track_id_from_url(url: str) -> str | None:
    """Extract Yandex Music track id from a URL."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None

    host = (parsed.netloc or "").lower()
    if not host.startswith("music.yandex."):
        return None

    path = (parsed.path or "").rstrip("/")
    match = _ALBUM_TRACK_URL_RE.match(path)
    if match:
        return match.group("track_id")
    match = _TRACK_URL_RE.match(path)
    if match:
        return match.group("track_id")
    return None


def yandex_source_url(track_id: str | int) -> str:
    return f"{YANDEX_TRACK_SOURCE_PREFIX}{track_id}"


def yandex_track_id_from_source(source_url: str) -> str | None:
    value = (source_url or "").strip()
    if not value:
        return None
    if value.startswith(YANDEX_TRACK_SOURCE_PREFIX):
        track_id = value[len(YANDEX_TRACK_SOURCE_PREFIX) :].strip()
        return track_id if track_id.isdigit() else None
    if value.startswith("http://") or value.startswith("https://"):
        return yandex_track_id_from_url(value)
    return None


def yandex_public_track_url(track_id: str | int) -> str:
    return f"https://music.yandex.ru/track/{track_id}"


def yandex_public_url_from_source(source_url: str | None) -> str | None:
    if not source_url:
        return None
    track_id = yandex_track_id_from_source(source_url)
    if not track_id:
        return source_url
    return yandex_public_track_url(track_id)


class MusicClient:
    """
    Yandex Music-only backend.

    Notes:
    - `YANDEX_MUSIC_TOKEN` is optional. Without it, playback may be limited to previews.
    - In some regions Yandex Music API may return HTTP 451 for catalog/search endpoints (search/metadata).
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        language: str = "ru",
        cache_dir: str | None = None,
    ) -> None:
        self._token = (token or os.getenv("YANDEX_MUSIC_TOKEN", "")).strip() or None
        self._language = language
        self._client: YandexMusicClient | None = None
        self._lock = asyncio.Lock()

        raw_cache_dir = (cache_dir or os.getenv("YANDEX_CACHE_DIR", "")).strip()
        if raw_cache_dir:
            self._cache_dir = Path(raw_cache_dir)
        else:
            self._cache_dir = Path(tempfile.gettempdir()) / "group_music_ym_cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        # yandex-music client is synchronous (requests); nothing to close.
        return

    async def search_track(self, query: str) -> TrackLookup | None:
        text = query.strip()
        if not text:
            return None

        async with self._lock:
            return await asyncio.to_thread(self._search_track_sync, text)

    async def recommend(self, mode: str, count: int = 5) -> list[TrackLookup]:
        seeds = MODE_SEEDS.get(mode, MODE_SEEDS["chill"])
        candidates: list[TrackLookup] = []
        seen_keys: set[str] = set()

        for term in seeds:
            if len(candidates) >= count:
                break

            track = await self.search_track(term)
            if not track:
                continue

            key = f"{(track.artist or '').lower()}::{track.title.lower()}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(track)

        return candidates[:count]

    async def lookup_track(self, track_id: str) -> TrackLookup | None:
        """
        Fetch track metadata by id.

        This may fail with HTTP 451 in some regions (Yandex Music availability restrictions).
        """
        value = track_id.strip()
        if not value or not value.isdigit():
            return None

        async with self._lock:
            return await asyncio.to_thread(self._lookup_track_sync, value)

    async def resolve_source(self, source_url: str) -> str | None:
        """
        Convert stored source url (ym:track:<id> or Yandex Music URL) into a playable local file path.
        """
        track_id = yandex_track_id_from_source(source_url)
        if not track_id:
            return None

        path = self._cached_track_path(track_id)
        if path.exists() and path.stat().st_size > 0:
            return str(path)

        async with self._lock:
            if path.exists() and path.stat().st_size > 0:
                return str(path)
            try:
                return await asyncio.to_thread(self._download_track_sync, track_id, path)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to resolve Yandex source (track=%s)", track_id)
                return None

    def _get_client(self) -> YandexMusicClient:
        if self._client is not None:
            return self._client

        self._client = YandexMusicClient(token=self._token, language=self._language)
        return self._client

    def _search_track_sync(self, query: str) -> TrackLookup | None:
        try:
            client = self._get_client()
            result = client.search(query, type_="track")
        except (YandexMusicError, Exception) as exc:  # noqa: BLE001
            logger.warning("Yandex Music search failed: %s", exc)
            return None

        if not result or not result.tracks or not result.tracks.results:
            return None

        track = result.tracks.results[0]
        artist = None
        if getattr(track, "artists", None):
            try:
                artist = track.artists[0].name
            except Exception:
                artist = None

        title = (getattr(track, "title", None) or query).strip() or query
        return TrackLookup(
            artist=artist,
            title=title,
            genre=None,
            source_url=yandex_source_url(getattr(track, "id", "")),
            query_text=query,
        )

    def _lookup_track_sync(self, track_id: str) -> TrackLookup | None:
        try:
            client = self._get_client()
            tracks = client.tracks(track_id)
        except (YandexMusicError, Exception) as exc:  # noqa: BLE001
            logger.warning("Yandex Music lookup failed (track=%s): %s", track_id, exc)
            return None

        if not tracks:
            return None

        track = tracks[0]
        artist = None
        if getattr(track, "artists", None):
            try:
                artist = track.artists[0].name
            except Exception:
                artist = None
        title = (getattr(track, "title", None) or f"Track {track_id}").strip()

        return TrackLookup(
            artist=artist,
            title=title,
            genre=None,
            source_url=yandex_source_url(getattr(track, "id", track_id)),
            query_text=yandex_public_track_url(track_id),
        )

    def _cached_track_path(self, track_id: str) -> Path:
        safe_id = track_id.strip()
        if not safe_id.isdigit():
            # fallback: should not happen, but avoid path traversal
            safe_id = re.sub(r"[^\w]+", "_", safe_id)
        return self._cache_dir / f"{safe_id}.mp3"

    def _download_track_sync(self, track_id: str, path: Path) -> str | None:
        client = self._get_client()

        try:
            infos = client.tracks_download_info(track_id)
        except (YandexMusicError, Exception) as exc:  # noqa: BLE001
            logger.warning("Yandex Music download-info failed (track=%s): %s", track_id, exc)
            return None

        best = self._pick_best_download_info(infos)
        if best is None:
            logger.warning("No downloadable variants for track=%s", track_id)
            return None

        tmp_path = path.with_suffix(".part")
        try:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]

            best.download(str(tmp_path))
            tmp_path.replace(path)
            return str(path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    logger.debug("Failed to cleanup tmp file: %s", tmp_path, exc_info=True)

    @staticmethod
    def _pick_best_download_info(infos: Iterable[DownloadInfo]) -> DownloadInfo | None:
        options = list(infos or [])
        if not options:
            return None

        # Prefer full mp3 (preview=False), then higher bitrate.
        mp3 = [info for info in options if getattr(info, "codec", None) == "mp3"] or options
        mp3.sort(key=lambda info: (bool(getattr(info, "preview", True)), -int(getattr(info, "bitrate_in_kbps", 0))))
        return mp3[0]
