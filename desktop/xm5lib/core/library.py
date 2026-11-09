"""In-memory view of the index: effective metadata (file tags + managed overrides), albums, search."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .db import EDITABLE_FIELDS


def effective(track: dict) -> dict:
    """Track dict with overrides applied (as strings for tag fields)."""
    t = dict(track)
    t["file_tags"] = {k: track.get(k) for k in EDITABLE_FIELDS}
    for k, v in track.get("overrides", {}).items():
        if k in ("tracknumber", "discnumber"):
            try:
                t[k] = int(v.split("/")[0]) if v else None
            except ValueError:
                t[k] = None
        else:
            t[k] = v or None
    t["edited"] = bool(track.get("overrides"))
    return t


def album_artist(t: dict) -> str:
    return t.get("albumartist") or t.get("artist") or "Unknown Artist"


def album_key(t: dict) -> str:
    album = t.get("album") or f"[{Path(t['path']).parent.name}]"
    return f"{album_artist(t).casefold()}\x1f{album.casefold()}"


def format_rate(t: dict) -> str:
    sr = t.get("sample_rate")
    s = t.get("codec") or "?"
    if sr:
        k = sr / 1000
        s += f" · {k:g}k" if sr % 1000 else f" · {int(k)}k"
    if t.get("bits"):
        s += f"/{t['bits']}"
    return s


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


@dataclass
class Album:
    key: str
    title: str
    artist: str
    year: str = ""
    tracks: list[dict] = field(default_factory=list)
    folder: str = ""

    @property
    def cover_id(self) -> str:
        return hashlib.sha1(self.key.encode("utf-8")).hexdigest()

    @property
    def duration(self) -> float:
        return sum(t.get("duration") or 0 for t in self.tracks)

    @property
    def formats(self) -> str:
        return ", ".join(sorted({format_rate(t) for t in self.tracks}))


def sort_tracks(tracks: list[dict]) -> list[dict]:
    return sorted(tracks, key=lambda t: (t.get("discnumber") or 0, t.get("tracknumber") or 0,
                                         (t.get("title") or "").casefold(), t["path"]))


class LibraryView:
    def __init__(self, raw_tracks: list[dict]):
        self.tracks = [effective(t) for t in raw_tracks]
        self.by_path = {t["path"]: t for t in self.tracks}
        groups: dict[str, Album] = {}
        for t in self.tracks:
            k = album_key(t)
            a = groups.get(k)
            if a is None:
                a = groups[k] = Album(k, t.get("album") or f"[{Path(t['path']).parent.name}]", album_artist(t),
                                      folder=str(Path(t["path"]).parent))
            a.tracks.append(t)
            if not a.year and t.get("date"):
                a.year = str(t["date"])[:4]
        for a in groups.values():
            a.tracks = sort_tracks(a.tracks)
        self.albums = sorted(groups.values(), key=lambda a: (a.artist.casefold(), a.title.casefold()))
        self.album_by_key = {a.key: a for a in self.albums}

    def artists(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        names: dict[str, str] = {}
        for a in self.albums:
            k = a.artist.casefold()
            names.setdefault(k, a.artist)
            counts[k] = counts.get(k, 0) + 1
        return sorted(((names[k], n) for k, n in counts.items()), key=lambda x: x[0].casefold())

    @staticmethod
    def matches(t: dict, query: str) -> bool:
        if not query:
            return True
        hay = " ".join(str(t.get(k) or "") for k in ("title", "artist", "album", "albumartist", "genre", "date",
                                                     "codec", "rel", "note")).casefold()
        return all(word in hay for word in query.casefold().split())

    def search_tracks(self, query: str) -> list[dict]:
        return [t for t in self.tracks if self.matches(t, query)]

    def search_albums(self, query: str, artist: str | None = None) -> list[Album]:
        out = []
        for a in self.albums:
            if artist and a.artist.casefold() != artist.casefold():
                continue
            if not query or any(self.matches(t, query) for t in a.tracks):
                out.append(a)
        return out


__all__ = ["LibraryView", "Album", "effective", "format_rate", "format_duration", "EDITABLE_FIELDS"]
