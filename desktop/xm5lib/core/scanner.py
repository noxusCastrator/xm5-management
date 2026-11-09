"""Incremental, cancellable, read-only library scan.

Files are only opened for reading (Mutagen parses headers/tags). Unchanged files (same size and
mtime) are skipped. Files that disappear are removed from the index; their overrides, notes and
playlist references are kept, so a temporarily missing drive does not erase user work.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import tags
from .db import Store


@dataclass
class ScanResult:
    root: str
    found: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False
    seconds: float = 0.0


def iter_audio(root: Path, cancel: threading.Event | None = None):
    stack = [root]
    while stack:
        if cancel is not None and cancel.is_set():
            return
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                elif e.is_file() and os.path.splitext(e.name)[1].lower() in tags.AUDIO_EXTENSIONS:
                    st = e.stat()
                    yield Path(e.path), st.st_size, st.st_mtime_ns
            except OSError:
                continue


def scan(store: Store, root: Path, progress: Callable[[int, int, str], None] | None = None,
         cancel: threading.Event | None = None) -> ScanResult:
    t0 = time.monotonic()
    root = Path(root)
    result = ScanResult(str(root))
    if not root.is_dir():
        raise FileNotFoundError(f"Music folder not found: {root}")
    files = list(iter_audio(root, cancel))
    result.found = len(files)
    with store.connect() as c:
        known = {r[0]: (r[1], r[2]) for r in c.execute("SELECT path, size, mtime_ns FROM tracks")}
    seen = set()
    batch = []

    def flush():
        if batch:
            with store.connect() as c:
                c.executemany(
                    "INSERT OR REPLACE INTO tracks(path, rel, size, mtime_ns, title, artist, album, albumartist, "
                    "tracknumber, discnumber, date, genre, comment, codec, sample_rate, bits, channels, bitrate, "
                    "duration, has_embedded_art, error, scanned_at) VALUES(:path, :rel, :size, :mtime_ns, :title, "
                    ":artist, :album, :albumartist, :tracknumber, :discnumber, :date, :genre, :comment, :codec, "
                    ":sample_rate, :bits, :channels, :bitrate, :duration, :has_embedded_art, :error, :scanned_at)",
                    batch)
            batch.clear()

    for i, (path, size, mtime) in enumerate(files, 1):
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            break
        key = str(path)
        seen.add(key)
        if known.get(key) == (size, mtime):
            result.unchanged += 1
        else:
            row = {k: None for k in ("title", "artist", "album", "albumartist", "tracknumber", "discnumber", "date",
                                     "genre", "comment", "codec", "sample_rate", "bits", "channels", "bitrate",
                                     "duration")}
            row.update(path=key, rel=path.relative_to(root).as_posix(), size=size, mtime_ns=mtime,
                       has_embedded_art=0, error=None, scanned_at=time.time())
            try:
                row.update(tags.read(path))
            except Exception as exc:  # recorded and shown, never fatal
                row["error"] = f"{type(exc).__name__}: {exc}"
                row["codec"] = path.suffix[1:].upper()
                result.errors.append((key, row["error"]))
            if not row.get("title"):
                row["title"] = path.stem
            batch.append(row)
            if key in known:
                result.updated += 1
            else:
                result.added += 1
            if len(batch) >= 200:
                flush()
        if progress and (i % 25 == 0 or i == len(files)):
            progress(i, len(files), key)
    flush()
    if not result.cancelled:
        gone = [p for p in known if p not in seen and _under(p, root)]
        with store.connect() as c:
            c.executemany("DELETE FROM tracks WHERE path=?", [(p,) for p in gone])
        result.removed = len(gone)
    result.seconds = round(time.monotonic() - t0, 2)
    return result


def _under(path: str, root: Path) -> bool:
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False
