"""Copy selected local roots into a new, prepared XM5 card tree."""

import argparse
import ctypes
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import unicodedata

from . import organize_card as organizer
from . import xm5_inline as xm5


TRACK_SUFFIXES = organizer.ADAPTERS | {".flac"}
WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789\u00b9\u00b2\u00b3"
}


@dataclass(frozen=True)
class Entry:
    relative: Path
    identity: tuple
    directory: bool


@dataclass(frozen=True)
class Root:
    source: Path
    category: str
    entries: tuple[Entry, ...]
    label: str


def _name_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def _component(name):
    if (not name or name[-1] in " ." or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
            or name.partition(".")[0].upper() in WINDOWS_NAMES):
        raise xm5.InlineError(f"File name is unsuitable for the card: {name!r}")


def _directory(path):
    path = Path(path)
    if ".." in path.parts:
        raise xm5.InlineError(f"Choose a directory without '..': {path}")
    absolute = path.absolute()
    for item in [*reversed(absolute.parents), absolute]:
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or xm5._reparse(info):
            raise xm5.InlineError(f"Expected an unlinked directory: {item}")
    return absolute


def _identity(info):
    return (*xm5._snapshot(info), info.st_ctime_ns, info.st_mode)


def _read_tree(root):
    entries = []
    pending = [Path(".")]
    while pending:
        relative = pending.pop()
        path = root / relative
        info = path.lstat()
        if xm5._reparse(info):
            raise xm5.InlineError(f"Source contains a link or reparse point: {path}")
        directory = stat.S_ISDIR(info.st_mode)
        if not directory and not stat.S_ISREG(info.st_mode):
            raise xm5.InlineError(f"Source contains a non-regular file: {path}")
        entries.append(Entry(relative, _identity(info), directory))
        if directory:
            children = sorted(path.iterdir(), key=lambda child: child.name)
            names = set()
            for child in children:
                _component(child.name)
                key = _name_key(child.name)
                if key in names:
                    raise xm5.InlineError(f"Source has colliding names: {child}")
                names.add(key)
            pending.extend(relative / child.name for child in reversed(children))
    return tuple(entries)


def _reject_destination(parent):
    for entry in parent.iterdir():
        if _name_key(entry.name) == "xm5":
            raise FileExistsError(f"An xm5 entry already exists: {entry}")


def _plan(parent, music_roots, eq_roots):
    selected = []
    destination = parent / "xm5"
    for category, supplied in (("music", music_roots), ("EQ", eq_roots)):
        labels = set()
        sources = [_directory(item) for item in supplied]
        for source in sources:
            _component(source.name)
            duplicate = sum(_name_key(item.name) == _name_key(source.name) for item in sources) > 1
            label = source.name + ("-" + sha256(str(source).encode()).hexdigest()[:8] if duplicate else "")
            if _name_key(label) in labels:
                raise xm5.InlineError(f"Repeated or ambiguous source: {source}")
            labels.add(_name_key(label))
            if source.is_relative_to(destination) or destination.is_relative_to(source):
                raise xm5.InlineError(f"Source overlaps target: {source}")
            selected.append(Root(source, category, _read_tree(source), label))
    return selected


def _copy_file(source, target, expected, cancel=None):
    with source.open("rb") as input_file, target.open("xb") as output_file:
        before = os.fstat(input_file.fileno())
        if xm5._snapshot(before) != expected[:4]:
            raise xm5.InlineError(f"Source changed before copy: {source}")
        digest = xm5._copy_digest(input_file, 0, before.st_size, output_file, cancel)
        if input_file.read(1):
            raise xm5.InlineError(f"Source grew during copy: {source}")
        output_file.flush()
        os.fsync(output_file.fileno())
    if _identity(source.lstat()) != expected:
        raise xm5.InlineError(f"Source changed during copy: {source}")
    with target.open("rb") as written:
        if os.fstat(written.fileno()).st_size != before.st_size or (
            xm5._copy_digest(written, 0, before.st_size, cancel=cancel) != digest
        ):
            raise xm5.InlineError(f"Copied file failed readback: {target}")
    return digest


def _activate(source, destination):
    if os.name == "nt":
        # Windows os.rename fails when the destination already exists.
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise xm5.InlineError("This host lacks exclusive directory rename")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def export_card(parent, music_roots, eq_roots=(), *, ffmpeg=None, levels=True, timeout=3600,
                progress=None, cancel=None, decorate=None, lyric_sources=None, reuse_tracks=None):
    """Prepare and publish a new card tree beneath the explicitly chosen parent."""
    xm5.check_cancel(cancel)
    parent = _directory(parent)
    parent_identity = parent.stat().st_dev, parent.stat().st_ino
    _reject_destination(parent)
    music_roots = tuple(music_roots)
    if not music_roots:
        raise xm5.InlineError("Choose at least one music directory")
    if not math.isfinite(timeout) or timeout <= 0:
        raise xm5.InlineError("Timeout must be positive and finite")
    roots = _plan(parent, music_roots, eq_roots)
    destination = parent / "xm5"
    results = []
    copied_files = []
    copied = 0
    reused = set()

    with tempfile.TemporaryDirectory(prefix=".xm5-export-", dir=parent) as staging:
        staging = Path(staging)
        candidate = staging / "xm5"
        for relative in ("music", "EQ", "Struct/playlists"):
            (candidate / relative).mkdir(parents=True)
        tracks = {}
        for root in roots:
            prefix = candidate / root.category / root.label
            for entry in root.entries:
                xm5.check_cancel(cancel)
                target = prefix / entry.relative
                if entry.directory:
                    target.mkdir()
                    continue
                source = root.source / entry.relative
                relative = target.relative_to(staging).as_posix()
                reuse = (reuse_tracks or {}).get(relative)
                if reuse:
                    with source.open("rb") as file:
                        original_digest = xm5._copy_digest(file, 0, os.fstat(file.fileno()).st_size, cancel=cancel)
                    cached = Path(reuse["source"])
                    with cached.open("rb") as file:
                        cached_digest = xm5._copy_digest(file, 0, os.fstat(file.fileno()).st_size, cancel=cancel)
                    if original_digest.hex() == reuse["original_sha256"] and cached_digest.hex() == reuse["export_sha256"]:
                        _copy_file(cached, target, _identity(cached.lstat()), cancel)
                        digest = original_digest
                        reused.add(relative)
                    else:
                        digest = _copy_file(source, target, entry.identity, cancel)
                else:
                    digest = _copy_file(source, target, entry.identity, cancel)
                copied_files.append({"path": target.relative_to(staging).as_posix(),
                                     "source": str(root.source / entry.relative), "original_sha256": digest.hex()})
                copied += 1
                if progress:
                    progress({"phase": "inline_copy", "files": copied,
                              "path": target.relative_to(staging).as_posix()})
                if root.category == "music" and target.suffix.lower() in TRACK_SUFFIXES:
                    tracks[target.relative_to(staging).as_posix()] = digest
        unsupported = set()
        for relative, original_digest in tracks.items():
            xm5.check_cancel(cancel)
            try:
                result = organizer.organize_one(
                    staging, relative, ffmpeg, levels=levels, timeout=timeout, publish_index=False, cancel=cancel,
                    lyrics=(lyric_sources or {}).get(relative),
                )
            except organizer.formats.Unsupported as exc:
                with (staging / relative).open("rb") as track:
                    if xm5._copy_digest(track, 0, os.fstat(track.fileno()).st_size) != original_digest:
                        raise xm5.InlineError(f"Unsupported track changed: {relative}") from exc
                unsupported.add(relative)
                result = {"status": "unsupported", "diagnostic": str(exc)}
            results.append({"path": relative, "reused": relative in reused, **result})
            if progress:
                progress({"phase": "inline_prepare", "current": len(results), "total": len(tracks), "path": relative})
        rebuilt = organizer.rebuild_index(staging, cancel=cancel)
        expected = {row["path"]: row["status"] != "absent" for row in results
                    if row["status"] != "unsupported"}
        if set(rebuilt["skipped"]) != unsupported or xm5._read_index(staging / xm5.INDEX_PATH) != expected:
            raise xm5.InlineError("Prepared track index failed verification")
        if decorate is not None:
            decorate(staging, copied_files, results)
        for root in roots:
            if _directory(root.source) != root.source or _read_tree(root.source) != root.entries:
                raise xm5.InlineError(f"Source listing changed during export: {root.source}")
        observed = _directory(parent).stat()
        if (observed.st_dev, observed.st_ino) != parent_identity:
            raise xm5.InlineError("Card parent changed during export")
        _reject_destination(parent)
        xm5.check_cancel(cancel)
        _activate(candidate, destination)

    return {"destination": str(destination), "files": copied, "prepared": rebuilt["prepared"], "tracks": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parent", type=Path, help="Existing card mount parent")
    parser.add_argument("--music", type=Path, action="append", required=True)
    parser.add_argument("--eq", type=Path, action="append", default=[])
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--no-levels", action="store_true")
    parser.add_argument("--timeout", type=float, default=3600)
    args = parser.parse_args()
    try:
        report = export_card(args.parent, args.music, args.eq, ffmpeg=args.ffmpeg,
                             levels=not args.no_levels, timeout=args.timeout)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
