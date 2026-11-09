"""Prepare a fresh folder whose *contents* can be copied to the root of a blank TF card.

Layout (relative to the export folder)::

    Music/<Album Artist>/<Album>/<source file name>   ordinary audio, never transcoded
    Music/<Album Artist>/<Album>/folder.jpg           330x330 playback/fallback JPEG
    .downpic/s_<stock basename MD5>.jpg               80x80 stock folder-view previews
    EQ/<Preset>.txt                                   firmware PEQ text subset
    _explaylist_data/<name>.m3u8                     stock import (default)
    Playlists/<name>.m3u8   or   <name>.m3u8          optional desktop M3U8
    Playlist Folders/<name>/NN - <file>               optional fallback: duplicated audio
    XM5-LIBRARY-MANIFEST.json, READ-ME.txt            PC-side record; not read by the XM5

Guarantees: the destination folder must not exist; files are created exclusively (never
overwritten); sources are opened read-only and re-checked (size/mtime) after copying; copies
without tag/cover edits are verified byte-identical by SHA-256; edited copies are verified to have
an unchanged audio payload. ``_EXPORT-INCOMPLETE.txt`` stays in the folder unless every step
succeeded.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from . import covers, payload, peq, playlists, tags
from .. import __version__

INCOMPLETE = "_EXPORT-INCOMPLETE.txt"
MANIFEST = "XM5-LIBRARY-MANIFEST.json"
CHUNK = 1 << 20
COVER_SIZES = (covers.PLAYBACK_SIDE,)

_BAD = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def component(name: str, limit: int = 120) -> str:
    """One FAT32/exFAT-safe path component; Unicode is preserved."""
    s = _BAD.sub("_", name).strip().rstrip(". ")
    if not s:
        s = "_"
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    if len(s) > limit:
        stem, ext = os.path.splitext(s)
        s = stem[: limit - len(ext)].rstrip(". ") + ext
    return s


@dataclass
class Options:
    name: str
    all_music: bool = True
    playlist_ids: list[int] = field(default_factory=list)
    preset_names: list[str] = field(default_factory=list)
    covers: bool = True
    cover_max: int = COVER_SIZES[0]
    apply_overrides: bool = True
    playlists_in_folder: bool = True  # Playlists/x.m3u8 with ../Music paths, else root x.m3u8
    folder_fallback: bool = False     # duplicate audio per playlist (counts toward size)
    native_playlists: bool = True     # explicit import, not automatic on card insertion


@dataclass
class Item:
    source: str
    rel: str
    size: int
    kind: str = "music"                       # "music" | "fallback"
    tags: dict[str, str] = field(default_factory=dict)  # overrides to write into the copy


@dataclass
class Plan:
    options: Options
    root: Path
    items: list[Item] = field(default_factory=list)
    covers: list[tuple[str, list[str]]] = field(default_factory=list)  # (album rel dir, source tracks)
    playlists: list[tuple[str, str, list[tuple[str, float | None, str]]]] = field(default_factory=list)
    eq: list[tuple[str, str]] = field(default_factory=list)            # (rel file, text)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    free_bytes: int | None = None

    @property
    def audio_bytes(self) -> int:
        return sum(i.size for i in self.items)

    @property
    def estimated_bytes(self) -> int:
        # covers are estimated generously; EQ/playlists are tiny but counted
        art_bytes = (len(self.covers) + len(self.items)) * 300_000 if self.options.covers else 0
        return (self.audio_bytes + art_bytes
                + sum(len(t.encode()) for _, t in self.eq)
                + sum(len(e) * 200 for _, _, e in self.playlists) + 64_000)

    def summary(self) -> dict:
        return {"root": str(self.root), "tracks": sum(i.kind == "music" for i in self.items),
                "fallback_copies": sum(i.kind == "fallback" for i in self.items),
                "audio_bytes": self.audio_bytes, "estimated_bytes": self.estimated_bytes,
                "free_bytes": self.free_bytes, "covers": len(self.covers), "playlists": len(self.playlists),
                "eq": len(self.eq), "errors": list(self.errors), "warnings": list(self.warnings)}


def make_plan(view, store, options: Options, parent: Path) -> Plan:
    """``view``: core.library.LibraryView; ``store``: core.db.Store."""
    if not options.name.strip():
        raise ValueError("Export folder name is empty")
    if options.cover_max not in COVER_SIZES:
        raise ValueError(f"cover size must be one of {COVER_SIZES}")
    root = Path(parent) / component(options.name)
    plan = Plan(options, root)
    if root.exists():
        plan.errors.append(f"Destination already exists: {root}. Choose a new name; nothing is overwritten.")
    if not Path(parent).is_dir():
        plan.errors.append(f"Parent folder does not exist: {parent}")
    else:
        plan.free_bytes = shutil.disk_usage(parent).free

    chosen_lists = []
    for pid in options.playlist_ids:
        chosen_lists.append((store.playlist_name(pid), store.playlist_paths(pid)))
    if options.all_music:
        selected = [t["path"] for t in view.tracks]
    else:
        seen, selected = set(), []
        for _, paths in chosen_lists:
            for p in paths:
                if p not in seen:
                    seen.add(p)
                    selected.append(p)
    if not selected:
        plan.errors.append("Nothing selected: choose all music or at least one non-empty playlist")

    used: set[str] = set()

    def unique(rel: str) -> str:
        base, ext = os.path.splitext(rel)
        cand, n = rel, 2
        while cand.casefold() in used:
            cand = f"{base} ({n}){ext}"
            n += 1
        used.add(cand.casefold())
        return cand

    mapping: dict[str, str] = {}
    album_dirs: dict[str, list[str]] = {}
    for p in selected:
        t = view.by_path.get(p)
        if t is None:
            plan.errors.append(f"Not in the library index (rescan or remove from playlist): {p}")
            continue
        src = Path(p)
        try:
            size = src.stat().st_size
        except OSError as exc:
            plan.errors.append(f"Source unavailable: {p} ({exc.strerror})")
            continue
        artist = component(t.get("albumartist") or t.get("artist") or "Unknown Artist")
        album = component(t.get("album") or src.parent.name)
        rel = unique(f"Music/{artist}/{album}/{component(src.name)}")
        mapping[p] = rel
        overrides = dict(t.get("overrides") or {}) if options.apply_overrides else {}
        if overrides and src.suffix.lower() not in tags.WRITABLE_CODECS:
            plan.warnings.append(f"{src.name}: tag edits are not written to {src.suffix} copies; copied unchanged")
            overrides = {}
        plan.items.append(Item(p, rel, size, "music", overrides))
        album_dirs.setdefault(rel.rsplit("/", 1)[0], []).append(p)
    if options.covers:
        plan.covers = sorted(album_dirs.items())

    folder_prefix = "_explaylist_data/" if options.native_playlists else ("Playlists/" if options.playlists_in_folder else "")
    list_names: set[str] = set()
    for name, paths in chosen_lists:
        fname = component(name, 80)
        if options.native_playlists:
            # Stock names occupy a 64-byte field including NUL.
            fname = fname.encode("utf-8")[:63].decode("utf-8", errors="ignore")
        base_name, serial = fname, 2
        while fname.casefold() in list_names:
            suffix = f" ({serial})"
            fname = (base_name.encode("utf-8")[:63-len(suffix)].decode("utf-8", errors="ignore")
                     if options.native_playlists else base_name) + suffix
            serial += 1
        list_names.add(fname.casefold())
        entries = []
        for p in paths:
            if p not in mapping:
                continue
            target = mapping[p]
            ref = "A:\\" + target.replace("/", "\\") if options.native_playlists else ("../" if folder_prefix else "") + target
            if options.native_playlists and ("#ext" in ref.casefold() or len(ref.encode("utf-8")) > 1000):
                plan.errors.append(f"Playlist '{name}': stock parser cannot safely read path: {target}")
            t = view.by_path[p]
            entries.append((ref, t.get("duration"), playlists.display_title(t)))
        dropped = len(paths) - len(entries)
        if dropped:
            plan.errors.append(f"Playlist '{name}': {dropped} entr{'y' if dropped == 1 else 'ies'} cannot be exported")
        plan.playlists.append((name, f"{folder_prefix}{fname}.m3u8", entries))
        if options.folder_fallback:
            width = max(2, len(str(len(paths))))
            for n, p in enumerate((x for x in paths if x in mapping), 1):
                src = Path(p)
                rel = unique(f"Playlist Folders/{fname}/{n:0{width}d} - {component(src.name, 100)}")
                music = next(i for i in plan.items if i.source == p and i.kind == "music")
                plan.items.append(Item(p, rel, music.size, "fallback", dict(music.tags)))

    presets = {p["name"]: p for p in store.presets()}
    eq_names: set[str] = set()
    for name in options.preset_names:
        if name not in presets:
            plan.errors.append(f"Preset not found: {name}")
            continue
        pr = peq.Preset.from_dict(presets[name])
        try:
            text = peq.to_apo_text(pr)
            peq.parse_apo_text(text, pr.name)  # self-check: our own output must re-parse
        except ValueError as exc:
            plan.errors.append(f"Preset '{name}' cannot be exported: {exc}")
            continue
        fname = peq.safe_filename(name)
        while fname.casefold() in eq_names:
            fname = fname[:-4] + "_.txt"
        eq_names.add(fname.casefold())
        plan.eq.append((f"EQ/{fname}", text))

    if plan.free_bytes is not None and plan.estimated_bytes > plan.free_bytes:
        plan.errors.append(f"Not enough free space: need about {plan.estimated_bytes / 1e9:.2f} GB, "
                           f"{plan.free_bytes / 1e9:.2f} GB free")
    return plan


class Cancelled(Exception):
    pass


def _copy_exclusive(src: Path, dst: Path, cancel: threading.Event | None) -> str:
    """Copy into a new file (fails if it exists). Returns the SHA-256 of the bytes read from src."""
    h = hashlib.sha256()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fi, open(dst, "xb") as fo:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            b = fi.read(CHUNK)
            if not b:
                break
            h.update(b)
            fo.write(b)
        fo.flush()
        os.fsync(fo.fileno())
    return h.hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(data)


def execute(plan: Plan, progress: Callable[[int, int, str], None] | None = None,
            cancel: threading.Event | None = None) -> dict:
    if plan.errors:
        raise ValueError("Plan has errors:\n" + "\n".join(plan.errors))
    root = plan.root
    os.mkdir(root)  # raises FileExistsError: never write into an existing folder
    started = time.time()
    _write_new(root / INCOMPLETE, b"This export did not finish. Do not copy it to a card as a complete library.\n")
    manifest = {"format": "xm5-library-export", "app_version": __version__, "pc_only": True,
                "note": "Desktop record of this export. The XM5 firmware does not read this file.",
                "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
                "options": plan.options.__dict__, "status": "incomplete", "tracks": [], "eq": [], "playlists": [],
                "covers": [], "warnings": list(plan.warnings), "errors": []}
    total = len(plan.items) + len(plan.covers) + len(plan.eq) + len(plan.playlists)
    done = 0

    def tick(label):
        nonlocal done
        done += 1
        if progress:
            progress(done, total, label)

    try:
        art_by_source: dict[str, covers.ExportArt] = {}
        for rel_dir, sources in plan.covers:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            data, origin = covers.source_art(sources)
            if data:
                try:
                    art = covers.export_art(data)
                except (OSError, ValueError, Image.DecompressionBombError) as exc:
                    manifest["warnings"].append(f"{rel_dir}: artwork not optimized ({exc})")
                else:
                    _write_new(root / rel_dir / "folder.jpg", art.playback)
                    manifest["covers"].append({"path": f"{rel_dir}/folder.jpg", "origin": origin,
                        "role": "playback", "max_side": covers.PLAYBACK_SIDE,
                        "sha256": hashlib.sha256(art.playback).hexdigest()})
                    for source in sources:
                        art_by_source[source] = art
            tick(f"Covers: {rel_dir}")

        # Resolve stock's basename-only collisions before emitting any cache file.
        previews: dict[str, tuple[bytes, list[str]]] = {}
        collisions: set[str] = set()
        unsupported: set[str] = set()
        for item in plan.items:
            art = art_by_source.get(item.source)
            if art is None:
                continue
            if Path(item.source).suffix.lower() not in tags.WRITABLE_CODECS:
                unsupported.add(Path(item.source).suffix.lower())
            try:
                keys = covers.stock_preview_keys(Path(item.rel).name)
            except ValueError as exc:
                manifest["warnings"].append(f"{item.rel}: preview cache omitted ({exc})")
                continue
            for key in keys:
                if key in previews:
                    previous, refs = previews[key]
                    if previous != art.preview:
                        collisions.add(key)
                    refs.append(item.rel)
                else:
                    previews[key] = (art.preview, [item.rel])
        for key in sorted(collisions):
            manifest["warnings"].append(f".downpic/{key}: different covers share a stock basename key; "
                                        "cache omitted; stock fallback remains available")
        for key, (jpeg, refs) in previews.items():
            if key not in collisions:
                _write_new(root / ".downpic" / key, jpeg)
                manifest["covers"].append({"path": f".downpic/{key}", "role": "preview",
                    "max_side": covers.PREVIEW_SIDE, "tracks": refs,
                    "sha256": hashlib.sha256(jpeg).hexdigest()})
        if unsupported:
            manifest["warnings"].append("Embedded artwork kept unchanged for " + ", ".join(sorted(unsupported))
                                        + "; only standard external covers/cache are prepared")

        for item in plan.items:
            src, dst = Path(item.source), root / item.rel
            before = os.stat(src)
            # keep the real extension last: format detection and tag writing key off it
            partial = dst.with_name(f"{dst.stem}.partial{dst.suffix}")
            try:
                src_hash = _copy_exclusive(src, partial, cancel)
                entry = {"source": item.source, "path": item.rel, "kind": item.kind, "bytes": item.size,
                         "source_sha256": src_hash}
                art = art_by_source.get(item.source)
                replace_art = art is not None and src.suffix.lower() in tags.WRITABLE_CODECS
                if item.tags or replace_art:
                    src_payload = payload.payload_hash(src)
                    if item.tags:
                        tags.write_tags(partial, item.tags)
                        entry["tags_written"] = item.tags
                    if replace_art:
                        tags.write_playback_cover(partial, art.playback)
                        entry["artwork_written"] = {"role": "playback", "side": covers.PLAYBACK_SIDE,
                                                    "sha256": hashlib.sha256(art.playback).hexdigest()}
                    copy_payload = payload.payload_hash(partial)
                    if src_payload is None or src_payload != copy_payload:
                        raise RuntimeError(f"audio payload changed while tagging {item.rel}; aborted")
                    entry.update(audio_payload_sha256=copy_payload,
                                 copy_sha256=_sha256(partial))
                else:
                    copy_hash = _sha256(partial)
                    if copy_hash != src_hash:
                        raise RuntimeError(f"copy verification failed for {item.rel}")
                    entry["copy_sha256"] = copy_hash
                after = os.stat(src)
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise RuntimeError(f"source changed during export: {src}")
                os.rename(partial, dst)  # Windows rename never replaces an existing file
            except BaseException:
                if partial.exists():
                    partial.unlink()
                raise
            manifest["tracks"].append(entry)
            tick(item.rel)
        for rel, text in plan.eq:
            _write_new(root / rel, text.encode("utf-8"))
            manifest["eq"].append({"path": rel, "sha256": hashlib.sha256(text.encode()).hexdigest()})
            tick(rel)
        for name, rel, entries in plan.playlists:
            data = (("\r\n".join(ref for ref, _, _ in entries) + "\r\n").encode("utf-8-sig")
                    if plan.options.native_playlists else playlists.m3u8_text(entries).encode("utf-8"))
            _write_new(root / rel, data)
            manifest["playlists"].append({"name": name, "path": rel, "entries": len(entries)})
            tick(rel)
        manifest["status"] = "complete"
    except Cancelled:
        manifest["status"] = "cancelled"
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["errors"].append(f"{type(exc).__name__}: {exc}")
    manifest["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    manifest["counts"] = {"tracks": len(manifest["tracks"]), "planned_items": len(plan.items),
                          "eq": len(manifest["eq"]), "playlists": len(manifest["playlists"]),
                          "covers": len(manifest["covers"])}
    _write_new(root / MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    _write_new(root / "READ-ME.txt", _readme(manifest).encode("utf-8"))
    if manifest["status"] == "complete":
        (root / INCOMPLETE).unlink()
    return manifest


def _readme(m: dict) -> str:
    return (
        "XM5 Library export\n"
        "==================\n\n"
        f"Status: {m['status'].upper()}\n\n"
        "Copy the CONTENTS of this folder to the root of a blank TF card.\n\n"
        "Music/      ordinary audio files, copied without transcoding or resampling.\n"
        "EQ/         PEQ presets in the XM5 PEQ text subset. On the player: rescan, select, enable PEQ.\n"
        "_explaylist_data/ stock-import M3U8: on XM5, open Playlists and select Import.\n"
        "Import replaces same-name playlists. Card insertion alone does not import them.\n"
        "Playlists/ (desktop mode only) ordinary relative-path M3U8 files.\n"
        "Playlist Folders/ (optional) numbered copies for folder playback; duplicates audio.\n\n"
        "Artwork (if enabled): existing standard cover fields only, no custom player format.\n"
        "330x330 baseline JPEG: folder.jpg and one embedded front cover in FLAC/MP3/M4A copies.\n"
        "80x80 baseline JPEG: existing .downpic/s_<basename MD5>.jpg folder-view cache.\n"
        "Other formats retain their embedded artwork. Source-library artwork is never changed.\n"
        "Stock may still decode/re-encode playback art; no firmware code is changed.\n\n"
        "Not included / not claimed:\n"
        "- No native XM5 playlist database or album-grid cache is written. Whether the player lists the\n"
        "  M3U8 playlists has not been verified on the device.\n"
        "- XM5-LIBRARY-MANIFEST.json is a PC-side record only; the XM5 does not read it.\n"
        "- No firmware or updater is included.\n"
    )


def verify_export(root: Path) -> dict:
    """Re-check a finished export against its manifest and the current source files."""
    root = Path(root)
    m = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    checked, problems = 0, []
    for t in m["tracks"]:
        copy, src = root / t["path"], Path(t["source"])
        if not copy.is_file():
            problems.append(f"missing {t['path']}")
            continue
        if _sha256(src) != t["source_sha256"]:
            problems.append(f"source changed since export: {src}")
        if "audio_payload_sha256" in t:
            if payload.payload_hash(copy) != payload.payload_hash(src):
                problems.append(f"audio payload differs: {t['path']}")
        elif _sha256(copy) != t["source_sha256"]:
            problems.append(f"copy differs from source: {t['path']}")
        if "copy_sha256" in t and _sha256(copy) != t["copy_sha256"]:
            problems.append(f"export copy changed: {t['path']}")
        checked += 1
    for art in m.get("covers", []):
        image = root / art["path"]
        try:
            if art.get("sha256") and _sha256(image) != art["sha256"]:
                problems.append(f"cover changed: {art['path']}")
            with Image.open(image) as im:
                if art.get("role") in ("preview", "playback"):
                    if im.format != "JPEG" or im.size != (art["max_side"], art["max_side"]) or im.info.get("progressive"):
                        problems.append(f"invalid fixed-size cover: {art['path']}")
        except (OSError, ValueError) as exc:
            problems.append(f"cover unavailable: {art['path']} ({exc})")
    return {"status": m["status"], "checked": checked, "problems": problems,
            "incomplete_marker": (root / INCOMPLETE).exists()}
