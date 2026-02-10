from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


MODE_SEEDS: dict[str, list[str]] = {
    "work": ["lofi beats", "focus instrumental", "deep house study", "ambient concentration"],
    "party": ["dance pop hits", "edm festival", "hip hop party", "club anthems"],
    "chill": ["chill indie", "sunset vibes", "downtempo chillout", "bedroom pop"],
    "road": ["road trip rock", "driving songs", "indie roadtrip", "classic highway songs"],
}


@dataclass(slots=True)
class TrackLookup:
    artist: str | None
    title: str
    genre: str | None
    source_url: str | None
    query_text: str


class MusicClient:
    ITUNES_URL = "https://itunes.apple.com/search"

    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "group-music-bot/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search_track(self, query: str) -> TrackLookup | None:
        params = {"term": query, "entity": "song", "limit": 1}
        try:
            response = await self._client.get(self.ITUNES_URL, params=params)
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        payload = response.json()
        results = payload.get("results") or []
        if not results:
            return None

        parsed = self._from_itunes_result(results[0], query)
        return parsed

    async def recommend(self, mode: str, count: int = 5) -> list[TrackLookup]:
        seeds = MODE_SEEDS.get(mode, MODE_SEEDS["chill"])
        candidates: list[TrackLookup] = []
        seen_keys: set[str] = set()

        for term in seeds:
            if len(candidates) >= count:
                break
            params = {"term": term, "entity": "song", "limit": max(5, count)}
            try:
                response = await self._client.get(self.ITUNES_URL, params=params)
                response.raise_for_status()
            except httpx.HTTPError:
                continue

            payload = response.json()
            for row in payload.get("results") or []:
                track = self._from_itunes_result(row, term)
                key = f"{(track.artist or '').lower()}::{track.title.lower()}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                candidates.append(track)
                if len(candidates) >= count:
                    break

        return candidates[:count]

    @staticmethod
    def _from_itunes_result(row: dict[str, Any], query: str) -> TrackLookup:
        artist = row.get("artistName")
        title = row.get("trackName") or row.get("collectionName") or query
        genre = row.get("primaryGenreName")
        # previewUrl is directly playable audio and works better for voice streaming.
        source_url = (
            row.get("previewUrl")
            or row.get("trackViewUrl")
            or row.get("collectionViewUrl")
            or row.get("artistViewUrl")
        )
        return TrackLookup(
            artist=artist,
            title=title,
            genre=genre,
            source_url=source_url,
            query_text=query,
        )
