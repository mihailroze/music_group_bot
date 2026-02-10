from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


URL_RE = re.compile(r"^https?://", flags=re.IGNORECASE)
NON_WORD_RE = re.compile(r"[^\w]+", flags=re.UNICODE)


def is_url(value: str) -> bool:
    return bool(URL_RE.match(value.strip()))


def normalize_key(value: str) -> str:
    normalized = NON_WORD_RE.sub("", value.strip().lower())
    return normalized


def build_fingerprint(artist: str | None, title: str | None) -> str | None:
    if artist and title:
        value = f"{artist}::{title}"
    elif title:
        value = title
    else:
        return None
    key = normalize_key(value)
    return key or None


def split_artist_title(query: str) -> tuple[str | None, str]:
    for sep in (" - ", " — ", " – "):
        if sep in query:
            artist, title = query.split(sep, maxsplit=1)
            artist = artist.strip()
            title = title.strip()
            if title:
                return artist or None, title
    return None, query.strip()


def display_track(item: dict[str, Any]) -> str:
    artist = item.get("artist")
    title = item.get("title") or "Unknown title"
    if artist:
        return f"{artist} - {title}"
    return title


def short_host(url: str) -> str:
    try:
        host = urlparse(url).netloc
    except ValueError:
        return url
    return host or url
