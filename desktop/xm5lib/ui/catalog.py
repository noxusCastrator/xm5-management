"""XM5 library categories derived from the in-memory LibraryView, plus a stop-gap favourites store.

The firmware's library menu offers: Folder, Album, Artist, Genre, Resolution, Favourites,
(Frequently played, Recently played, Recently added -- smart lists, not mirrored here) and Playlists.

BACKEND CONTRACT (for the core owner): everything here is presentation-side grouping of
``LibraryView.tracks``. Only ``Favorites`` persists data; it currently keeps a JSON list of paths in
the ``favorites`` setting. Replace ``Favorites`` with a core implementation exposing the same four
methods (``is_fav``, ``set``, ``toggle``, ``paths``) and nothing in the UI needs to change.
"""
from __future__ import annotations
from .i18n import tr

from dataclasses import dataclass, field
from pathlib import PurePath

LOSSY = {"MP3", "AAC", "OGG", "VORBIS", "OPUS", "WMA", "MP2", "M4A"}
UNKNOWN_GENRE = tr("未知风格")


# ------------------------------------------------------------------ favourites
class Favorites:
    KEY = "favorites"

    def __init__(self, store):
        self.store = store
        self._paths: list[str] = list(store.setting(self.KEY, []) or [])
        self._set = set(self._paths)

    def is_fav(self, path: str) -> bool:
        return path in self._set

    def paths(self) -> list[str]:
        return list(self._paths)

    def set(self, paths: list[str], on: bool) -> None:
        if on:
            for p in paths:
                if p not in self._set:
                    self._paths.append(p)
                    self._set.add(p)
        else:
            drop = set(paths)
            self._paths = [p for p in self._paths if p not in drop]
            self._set -= drop
        self.store.set_setting(self.KEY, self._paths)

    def toggle(self, paths: list[str]) -> bool:
        """Favourite all if any is not yet a favourite; otherwise remove all. Returns the new state."""
        on = not all(self.is_fav(p) for p in paths)
        self.set(paths, on)
        return on


# ------------------------------------------------------------------ genre
def genres(view) -> list[tuple[str, list]]:
    """[(genre, albums)] -- an album appears under every genre one of its tracks carries."""
    groups: dict[str, tuple[str, list]] = {}
    for a in view.albums:
        seen = set()
        for t in a.tracks:
            for g in split_genre(t.get("genre")):
                k = g.casefold()
                if k in seen:
                    continue
                seen.add(k)
                groups.setdefault(k, (g, []))[1].append(a)
    out = sorted(groups.values(), key=lambda x: (x[0] == UNKNOWN_GENRE, x[0].casefold()))
    return out


def genre_tracks(tracks: list[dict]) -> list[tuple[str, list[dict]]]:
    """[(genre, tracks)] -- a song is listed under every genre it carries (the 风格 page lists songs)."""
    groups: dict[str, tuple[str, list]] = {}
    for t in tracks:
        if t.get("error"):
            continue
        seen = set()
        for g in split_genre(t.get("genre")):
            k = g.casefold()
            if k not in seen:
                seen.add(k)
                groups.setdefault(k, (g, []))[1].append(t)
    return sorted(groups.values(), key=lambda x: (x[0] == UNKNOWN_GENRE, x[0].casefold()))


def split_genre(raw) -> list[str]:
    if not raw:
        return [UNKNOWN_GENRE]
    parts = [p.strip() for p in str(raw).replace(";", "/").replace("\x00", "/").split("/")]
    return [p for p in parts if p] or [UNKNOWN_GENRE]


# ------------------------------------------------------------------ resolution
def resolution(t: dict) -> tuple[tuple, str]:
    """(sort key, label). Highest resolution sorts first."""
    codec = (t.get("codec") or "?").upper()
    sr = t.get("sample_rate") or 0
    bits = t.get("bits") or 0
    if codec.startswith("DSD") or codec in ("DSF", "DFF") or sr >= 2_822_400:
        mult = round(sr / 44100) if sr else 64
        return (0, -sr), f"DSD{mult}" if sr else "DSD"
    khz = f"{sr / 1000:g}kHz" if sr else "?kHz"
    if codec in LOSSY:
        return (2, -sr), tr("有损 · {0} {1}", codec, khz)
    return (1, -sr, -bits), f"{khz} / {bits}bit" if bits else khz


def resolutions(view) -> list[tuple[str, list[dict]]]:
    groups: dict[str, tuple[tuple, list]] = {}
    for t in view.tracks:
        if t.get("error"):
            continue
        key, label = resolution(t)
        groups.setdefault(label, (key, []))[1].append(t)
    return [(label, tracks) for label, (key, tracks) in sorted(groups.items(), key=lambda kv: kv[1][0])]


def is_hires(t: dict) -> bool:
    key, _ = resolution(t)
    return key[0] == 0 or (key[0] == 1 and ((t.get("sample_rate") or 0) > 48000 or (t.get("bits") or 0) > 16))


# ------------------------------------------------------------------ folders
@dataclass
class Folder:
    name: str
    path: tuple[str, ...]
    folders: dict = field(default_factory=dict)
    tracks: list = field(default_factory=list)
    total: int = 0
    root: str = ""                  # set on the music-folder nodes: the folder on disk


def folder_tree(view, roots: list[str] | None = None) -> Folder:
    """Top level: one node per music folder (in the order they were added); below it, the folders on
    disk, keyed by the path relative to that music folder (``rel``)."""
    top = Folder(tr("音乐"), ())
    if roots is None:
        roots = sorted({t.get("root") for t in view.tracks if t.get("root")})
    nodes: dict[str, Folder] = {}
    for r in roots:
        base = PurePath(r).name or r.rstrip("\\/") or r
        name, i = base, 2
        while name in top.folders:
            name, i = f"{base} ({i})", i + 1
        node = top.folders[name] = Folder(name, (name,), root=r)
        nodes[r] = node
    for t in view.tracks:
        node = nodes.get(t.get("root"))
        if node is None:
            continue
        rel = t.get("rel") or PurePath(t["path"]).name
        parts = PurePath(rel.replace("\\", "/")).parts
        top.total += 1
        node.total += 1
        for i, part in enumerate(parts[:-1]):
            node = node.folders.setdefault(part, Folder(part, node.path + (part,)))
            node.total += 1
        node.tracks.append(t)
    return top


def find_folder(root: Folder, path: tuple[str, ...]) -> Folder | None:
    node = root
    for part in path:
        node = node.folders.get(part)
        if node is None:
            return None
    return node


__all__ = ["Favorites", "genres", "genre_tracks", "resolutions", "resolution", "folder_tree", "find_folder", "is_hires"]
