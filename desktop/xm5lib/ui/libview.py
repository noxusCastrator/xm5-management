"""The library as the pages show it: core.library's tracks and albums, with the album grouping chosen in
系统设置 › 曲库 › 专辑无视歌手.

Normally an album is (album artist, album title): the same title by two artists is two albums. With
``ignore_artist`` an album is its title alone, so an album whose songs carry different artists stays one
album with several artists. Songs without an album tag are grouped by their folder either way.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core import library
from ..core.library import album_artist, effective, sort_tracks
from .i18n import tr


def album_title(t: dict) -> str:
    return t.get("album") or f"[{Path(t['path']).parent.name}]"


def artists_label(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return tr("{0}、{1}", names[0], names[1])
    return tr("{0} 等 {1} 位艺术家", names[0], len(names))


@dataclass
class Album(library.Album):
    artists: list = field(default_factory=list)     # every album artist on the album, first seen first


class View(library.LibraryView):
    def __init__(self, raw_tracks: list[dict], ignore_artist: bool = False):
        self.ignore_artist = bool(ignore_artist)
        self.tracks = [effective(t) for t in raw_tracks]
        self.by_path = {t["path"]: t for t in self.tracks}
        groups: dict[str, Album] = {}
        for t in self.tracks:
            k = self.album_key(t)
            a = groups.get(k)
            if a is None:
                a = groups[k] = Album(k, album_title(t), album_artist(t), folder=str(Path(t["path"]).parent))
            name = album_artist(t)
            if all(n.casefold() != name.casefold() for n in a.artists):
                a.artists.append(name)
            a.tracks.append(t)
            if not a.year and t.get("date"):
                a.year = str(t["date"])[:4]
        for a in groups.values():
            a.tracks = sort_tracks(a.tracks)
            a.artist = artists_label(a.artists)
        self.albums = sorted(groups.values(), key=lambda a: (a.artists[0].casefold(), a.title.casefold()))
        self.album_by_key = {a.key: a for a in self.albums}

    def album_key(self, t: dict) -> str:
        if not t.get("album"):                              # untagged: the folder is the album
            folder = str(Path(t["path"]).parent).casefold()
            return folder if self.ignore_artist else f"{album_artist(t).casefold()}\x1f{folder}"
        title = t["album"].casefold()
        return title if self.ignore_artist else f"{album_artist(t).casefold()}\x1f{title}"

    def album_of(self, t: dict):
        return self.album_by_key.get(self.album_key(t))

    def artists(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        names: dict[str, str] = {}
        for a in self.albums:
            for n in a.artists:
                k = n.casefold()
                names.setdefault(k, n)
                counts[k] = counts.get(k, 0) + 1
        return sorted(((names[k], n) for k, n in counts.items()), key=lambda x: x[0].casefold())

    def search_albums(self, query: str, artist: str | None = None) -> list:
        out = []
        for a in self.albums:
            if artist and all(n.casefold() != artist.casefold() for n in a.artists):
                continue
            if not query or any(self.matches(t, query) for t in a.tracks):
                out.append(a)
        return out


__all__ = ["View", "Album", "album_title", "artists_label"]
