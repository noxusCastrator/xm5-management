"""Read and publish ordinary card-relative M3U8 playlists."""

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Iterable


FAVORITES = "xm5/Struct/playlists/Favorites.m3u8"


@dataclass(frozen=True)
class Entry:
    reference: str
    card_path: Path
    present: bool


def _root(path: str | os.PathLike[str]) -> Path:
    root = Path(path).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    return root


def _resolve(root: Path, base: Path, reference: str, *, writing: bool = False) -> Path:
    if not reference or reference[0] in "/\\" or any(char in reference for char in ":\r\n\0"):
        raise ValueError(f"Expected a relative card path: {reference!r}")
    path = base
    for component in reference.replace("\\", "/").split("/"):
        if component in ("", "."):
            continue
        candidate = path.parent if component == ".." else path / component
        path = candidate.resolve()
        if not path.is_relative_to(root) or (writing and path != candidate):
            raise ValueError(f"Path leaves its allowed card location: {reference!r}")
    return path


def _playlist(root: Path, path: str | os.PathLike[str], *, writing: bool = False) -> Path:
    reference = os.fspath(path)
    result = _resolve(root, root, reference, writing=writing)
    if result.suffix.lower() != ".m3u8":
        raise ValueError(f"Expected a .m3u8 file: {reference!r}")
    return result


def _entries(root: Path, playlist: Path) -> list[Entry]:
    entries = []
    text = playlist.read_text(encoding="utf-8-sig")
    for line in text.split("\n"):
        if not line.strip() or line.startswith("#"):
            continue
        target = _resolve(root, playlist.parent, line)
        entries.append(Entry(line, target.relative_to(root), target.is_file()))
    return entries


def read_playlist(card_root: str | os.PathLike[str], path: str | os.PathLike[str]) -> list[Entry]:
    """Return the editing view, including repeated and missing tracks."""
    root = _root(card_root)
    return _entries(root, _playlist(root, path))


def playback_paths(card_root: str | os.PathLike[str], path: str | os.PathLike[str]) -> list[Path]:
    """Return absolute paths for the currently available tracks, in order."""
    root = _root(card_root)
    return [root / entry.card_path for entry in _entries(root, _playlist(root, path)) if entry.present]


def _reference(target: Path, directory: Path) -> str:
    relative = Path(os.path.relpath(target, directory)).as_posix()
    return "./" + relative if relative.startswith("#") else relative


def _publish(root: Path, destination: Path, references: Iterable[str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=".xm5-playlist-",
            suffix=".tmp", dir=destination.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write("#EXTM3U\n")
            for reference in references:
                stream.write(reference + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _playlist(root, destination.relative_to(root), writing=True)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_playlist(
    card_root: str | os.PathLike[str], path: str | os.PathLike[str],
    tracks: Iterable[str | os.PathLike[str]],
) -> None:
    """Publish one playlist from card-relative track paths."""
    root = _root(card_root)
    playlist = _playlist(root, path, writing=True)
    references = [_reference(_resolve(root, root, os.fspath(track)), playlist.parent) for track in tracks]
    _publish(root, playlist, references)


def toggle_favorite(card_root: str | os.PathLike[str], track: str | os.PathLike[str]) -> bool:
    """Toggle the track and return whether it is now a favorite."""
    root = _root(card_root)
    playlist = _playlist(root, FAVORITES, writing=True)
    target = _resolve(root, root, os.fspath(track))
    key = target.relative_to(root)
    try:
        entries = _entries(root, playlist)
    except FileNotFoundError:
        entries = []
    added = all(entry.card_path != key for entry in entries)
    seen = {key}
    references = []
    for entry in entries:
        if entry.card_path not in seen:
            references.append(entry.reference)
            seen.add(entry.card_path)
    if added:
        references.append(_reference(target, playlist.parent))
    _publish(root, playlist, references)
    return added
