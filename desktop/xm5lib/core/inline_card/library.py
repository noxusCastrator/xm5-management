"""Portable card-library owner for inline track metadata and ordinary M3U8 files.

The source tree is read-only. A complete sibling tree is verified before its
activation; updates retain the previous tree and every unowned card file.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from . import export_card, organize_card, xm5_inline as xm5
from . import relative_playlists as playlists

SCHEMA = "xm5.inline.library"
MANIFEST = "xm5/Struct/library.json"
ACTIVATION = ".xm5-activation.json"
RESTORE_SCRIPT = '''"""Copy portable music, original EQ and playlists to a new folder."""
import argparse
from pathlib import Path
import shutil
import stat

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("destination", type=Path, help="A new, absent output folder")
args = parser.parse_args()
source = Path(__file__).resolve().parents[2]
if args.destination.exists():
    parser.error("Destination already exists; choose a new output folder")
for folder in ("music", "EQ", "Struct/playlists"):
    root = source / folder
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        if path.is_symlink() or (getattr(info, "st_file_attributes", 0) & 1024) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            parser.error("Refusing a link or nonregular file: " + str(path))
args.destination.mkdir(parents=True)
for folder, target in (("music", "music"), ("EQ", "EQ"), ("Struct/playlists", "playlists")):
    shutil.copytree(source / folder, args.destination / target)
print("Copied to", args.destination)
'''


def _hash(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                          encoding="utf-8", newline="\n")


def card_root(path):
    path = export_card._directory(path)
    if path.name == "xm5" and (path / "Struct/library.json").is_file():
        return path.parent
    if (path / MANIFEST).is_file():
        return path
    raise xm5.InlineError("No inline XM5 library was found in the chosen folder")


def load(path):
    root = card_root(path)
    data = json.loads(xm5._bounded_read(root / MANIFEST, 32 * 1024 * 1024))
    if data.get("schema") != SCHEMA or data.get("version") != 1:
        raise xm5.InlineError("Unsupported inline library manifest")
    return root, data


def signature(root, *, cancel=None):
    """Pin every regular file and directory without traversing links."""
    result = []
    for entry in export_card._read_tree(Path(root)):
        xm5.check_cancel(cancel)
        result.append([entry.relative.as_posix(), list(entry.identity), entry.directory])
    return result


def gain_options(midpoint=0.0, limit=4.0, enabled=True):
    if type(enabled) is not bool:
        raise xm5.InlineError("Peak correction enable must be a boolean")
    if (isinstance(midpoint, bool) or isinstance(limit, bool) or
            not isinstance(midpoint, (int, float)) or not isinstance(limit, (int, float)) or
            not math.isfinite(midpoint) or not math.isfinite(limit) or
            not -60 <= midpoint <= 0 or not 0 <= limit <= 12):
        raise xm5.InlineError("Peak midpoint must be -60..0 dBFS; correction limit must be 0..12 dB")
    return {"gain_midpoint_mdb": round(midpoint * 1000),
            "gain_limit_mdb": round(limit * 1000) if enabled else 0}


def settings(root, options):
    (Path(root) / "xm5/Struct/settings.ini").write_text(
        "# XM5 original-sample-peak correction; no PEQ-dependent analysis\n" +
        "".join(f"{key}={value}\n" for key, value in options.items()),
        encoding="ascii", newline="\n")


def _safe_stem(name, key):
    clean = "".join(c if c not in '<>:"/\\|?*' and ord(c) >= 32 else "_" for c in name).strip(" .")
    clean = clean[:70] or "Playlist"
    if clean.casefold() == "favorites":
        clean = "Playlist_Favorites"
    return clean + "-" + sha256(key.encode()).hexdigest()[:8]


def make_plan(parent, music_roots, eq_roots, *, options, selected_playlists=(), favorites=(),
              track_paths=None, card_references=None, presets=None, covers=None, lyrics=None, existing="refuse", cancel=None):
    parent = export_card._directory(parent)
    if (parent / ACTIVATION).exists():
        raise xm5.InlineError("Recover the interrupted card activation before previewing an update")
    roots = export_card._plan(parent, music_roots, eq_roots)
    if not any(root.category == "music" for root in roots):
        raise xm5.InlineError("Choose at least one music source")
    destination = parent / "xm5"
    old = None
    if destination.exists():
        if existing != "update":
            raise FileExistsError(destination)
        _, old = load(parent)
    else:
        export_card._reject_destination(parent)
    files = []
    locations = {}
    tracks = 0
    for root in roots:
        for entry in root.entries:
            if entry.directory:
                continue
            source = root.source / entry.relative
            relative = (Path("xm5") / root.category / root.label / entry.relative).as_posix()
            files.append({"source": str(source), "path": relative, "size": entry.identity[2]})
            if root.category == "music" and source.suffix.lower() in export_card.TRACK_SUFFIXES:
                tracks += 1
            locations[os.path.normcase(str(source))] = relative
    ids = {tid: locations[os.path.normcase(str(Path(path).absolute()))] for tid, path in (track_paths or {}).items()
           if os.path.normcase(str(Path(path).absolute())) in locations}
    for tid, relative in (card_references or {}).items():
        # This is a reference, never a file to copy, overwrite or claim ownership of.
        xm5._relative_parts(relative)
        if any(ch in relative for ch in ":\r\n\0"):
            raise xm5.InlineError("Invalid card-only music reference")
        ids.setdefault(tid, relative)
    playlist_rows = []
    for playlist in selected_playlists:
        supplied = playlist.get("track_ids") or [row.get("track_id") for row in playlist.get("items", [])]
        missing = [tid for tid in supplied if tid not in ids]
        if missing:
            raise xm5.InlineError(f"Playlist {playlist['name']!r} contains tracks outside selected sources")
        previous = next((row for row in (old or {}).get("playlists", []) if row["id"] == playlist["id"]), None)
        filename = Path(previous["path"]).name if previous else _safe_stem(playlist["name"], playlist["id"]) + ".m3u8"
        playlist_rows.append({"id": playlist["id"], "name": playlist["name"],
                              "path": "xm5/Struct/playlists/" + filename,
                              "track_ids": supplied, "tracks": [ids[tid] for tid in supplied]})
    favorites = [tid for tid in favorites if tid in ids]
    peak = options
    plan = {"schema": SCHEMA + ".plan", "version": 1, "destination": str(destination),
            "parent": str(parent), "roots": [{"source": str(root.source), "label": root.label,
                "category": root.category, "signature": signature(root.source, cancel=cancel)} for root in roots],
            "files": files, "playlists": playlist_rows, "favorites": favorites,
            "track_paths": ids, "presets": presets or {}, "playlist_covers": covers or {},
            "lyrics": lyrics or {}, "options": peak,
            "existing_signature": signature(destination, cancel=cancel) if old else None,
            "previous_build": old.get("build_id") if old else None,
            "acknowledged_playlists": {},
            "warnings": [], "summary": {"tracks": tracks, "original_files": len(files),
                "playlists": len(playlist_rows), "original_bytes": sum(row["size"] for row in files),
                "estimated_bytes": sum(row["size"] for row in files) + tracks * (xm5.LYRICS_OFFSET + xm5.MAX_LYRICS),
                "distinct_artworks": tracks}}
    if old:
        plan["summary"]["estimated_bytes"] += sum(entry.identity[2] for entry in export_card._read_tree(destination) if not entry.directory)
    return plan


def _name_playlist(root, row):
    playlists.write_playlist(root, row["path"], row["tracks"])
    path = Path(root) / row["path"]
    text = path.read_text(encoding="utf-8")
    name = row["name"].replace("\r", " ").replace("\n", " ")
    path.write_text(text.replace("#EXTM3U\n", "#EXTM3U\n#PLAYLIST:" + name + "\n", 1),
                    encoding="utf-8", newline="\n")


def _decorate(plan, staging, copied_files, results):
    struct = staging / "xm5/Struct"
    settings(staging, plan["options"])
    for row in plan.get("lyric_originals", []):
        relative = "xm5/" + row["relative"]
        if xm5._relative_parts(relative)[:3] != ["xm5", "Struct", "lyrics"]:
            raise xm5.InlineError("Invalid retained original lyric path")
        raw = xm5._bounded_read(Path(row["source"]), 16 * 1024 * 1024)
        if sha256(raw).hexdigest() != row["sha256"]:
            raise xm5.InlineError("Original lyrics changed after preview")
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != raw:
            raise xm5.InlineError("Conflicting retained lyric originals")
        target.write_bytes(raw)
    recovery = struct / "recovery"
    recovery.mkdir()
    (recovery / "RESTORE_LIBRARY.py").write_text(RESTORE_SCRIPT, encoding="utf-8", newline="\n")
    for row in plan["playlists"]:
        _name_playlist(staging, row)
        source = plan["playlist_covers"].get(row["id"])
        if source:
            data = xm5._bounded_read(Path(source), organize_card.MAX_ART_BYTES)
            stem = Path(row["path"]).stem
            original = struct / "playlist-covers/original" / (stem + Path(source).suffix.lower())
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(data)
            for side, jpeg in organize_card.prepare_images(data).items():
                (struct / "playlist-covers" / f"{stem}.{side}.jpg").write_bytes(jpeg)
    favorite = {"name": "Favorites", "path": playlists.FAVORITES,
                "tracks": [plan["track_paths"][tid] for tid in plan["favorites"]]}
    _name_playlist(staging, favorite)
    if plan["presets"]:
        directory = struct / "peq"
        directory.mkdir()
        for name, text in plan["presets"].items():
            filename = name if name.lower().endswith(".txt") else name + ".txt"
            export_card._component(filename)
            if len(filename.encode()) > 63 or not filename.isascii():
                raise xm5.InlineError("Converted PEQ filenames must be ASCII and no longer than 63 bytes")
            (directory / filename).write_text(text, encoding="utf-8", newline="\n")
    for row in copied_files:
        row["export_sha256"] = _hash(staging / row["path"])
        row["source_identity"] = list(export_card._identity(Path(row["source"]).lstat()))
    manifest = {"schema": SCHEMA, "version": 1, "build_id": uuid.uuid4().hex,
                "files": copied_files, "track_paths": plan["track_paths"],
                "playlists": plan["playlists"], "favorites": plan["favorites"],
                "source_signatures": {row["source"]: row["signature"] for row in plan["roots"]},
                "lyric_hashes": {relative: _hash(source) for relative, source in plan["lyrics"].items()},
                "preparation": results,
                "options": plan["options"], "playlist_hashes": {}, "owned": {}, "owned_stats": {}}
    for row in [*plan["playlists"], favorite]:
        manifest["playlist_hashes"][row["path"]] = _hash(staging / row["path"])
    for path in (staging / "xm5").rglob("*"):
        if path.is_file():
            manifest["owned"][path.relative_to(staging).as_posix()] = _hash(path)
            manifest["owned_stats"][path.relative_to(staging).as_posix()] = list(xm5._snapshot(path.lstat()))
    _json(staging / MANIFEST, manifest)


def _merge_existing(plan, staging, cancel=None):
    root, old = load(plan["parent"])
    new_root = staging / "xm5"
    _, new = load(staging)
    old_owned = old.get("owned", {})
    old_playlists = old.get("playlist_hashes", {})
    for entry in export_card._read_tree(root / "xm5"):
        xm5.check_cancel(cancel)
        source = root / "xm5" / entry.relative
        target = new_root / entry.relative
        relative = "xm5/" + entry.relative.as_posix()
        if entry.directory:
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists():
            if relative in (MANIFEST, "xm5/Struct/tracks.tsv"):
                continue
            old_hash = _hash(source)
            if old_hash == _hash(target):
                continue
            acknowledged = relative in old_playlists and plan.get("acknowledged_playlists", {}).get(relative) == old_hash
            if relative in old_playlists and old_hash != old_playlists[relative] and not acknowledged:
                raise xm5.InlineError("Bring back the changed XM5 playlist before updating: " + relative)
            replace_owned = relative in plan.get("replace_owned", []) and relative in old_owned
            if not acknowledged and not replace_owned and (relative not in old_owned or old_owned[relative] != old_hash):
                raise xm5.InlineError("A card file has changed or belongs to the user; it was preserved: " + relative)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            export_card._copy_file(source, target, entry.identity, cancel)
            if relative in old_owned:
                if _hash(source) == old_owned[relative]:
                    new["owned"][relative] = old_owned[relative]
                else:
                    new.setdefault("retained_modified", []).append(relative)
    # Keep unselected sources in place. There is no implicit deletion operation.
    current = {row["path"] for row in new["files"]}
    new["files"].extend(row for row in old.get("files", []) if row["path"] not in current)
    retained = {row["path"] for row in new["playlists"]}
    new["playlists"].extend(row for row in old.get("playlists", [])
                            if row["path"] not in retained and (staging / row["path"]).is_file())
    for path, value in old.get("playlist_hashes", {}).items():
        new["playlist_hashes"].setdefault(path, value)
    new["track_paths"] = old.get("track_paths", {}) | new["track_paths"]
    organize_card.rebuild_index(staging, cancel=cancel)
    new["owned"]["xm5/Struct/tracks.tsv"] = _hash(staging / "xm5/Struct/tracks.tsv")
    _json(staging / MANIFEST, new)


def execute(plan, *, ffmpeg=None, progress=None, cancel=None):
    parent = export_card._directory(plan["parent"])
    destination = parent / "xm5"
    for row in plan["roots"]:
        if signature(row["source"], cancel=cancel) != row["signature"]:
            raise xm5.InlineError("Source changed after preview: " + row["source"])
    current = signature(destination, cancel=cancel) if destination.exists() else None
    if current != plan["existing_signature"]:
        raise xm5.InlineError("Card changed after preview; preview again")
    if current is not None:
        from . import delta
        return delta.execute(plan, ffmpeg=ffmpeg, progress=progress, cancel=cancel)
    reserve = plan["summary"]["estimated_bytes"] + 16 * 1024 * 1024
    if shutil.disk_usage(parent).free < reserve:
        raise xm5.InlineError("Not enough free space for an isolated verified card update")
    reuse = {}
    if current:
        _, old = load(parent)
        eligible = {row["path"] for row in old.get("preparation", []) if row["status"] not in ("unsupported", "absent")}
        unchanged = [row for row in plan["roots"] if row["category"] == "music"
                     and old.get("source_signatures", {}).get(row["source"]) == row["signature"]]
        same_analysis = bool(old.get("options", {}).get("gain_limit_mdb")) == bool(plan["options"]["gain_limit_mdb"])
        if same_analysis:
            for row in old.get("files", []):
                relative = row["path"]
                lyric = plan["lyrics"].get(relative)
                if (relative in eligible and any(relative.startswith("xm5/music/" + root["label"] + "/") for root in unchanged)
                        and old.get("lyric_hashes", {}).get(relative) == (_hash(lyric) if lyric else None)):
                    reuse[relative] = {"source": str(xm5._card_path(parent, relative)),
                        "original_sha256": row["original_sha256"], "export_sha256": row["export_sha256"]}
    backup = None
    with tempfile.TemporaryDirectory(prefix=".xm5-library-", dir=parent) as scratch:
        staging = Path(scratch)
        export_card.export_card(staging,
            [row["source"] for row in plan["roots"] if row["category"] == "music"],
            [row["source"] for row in plan["roots"] if row["category"] == "EQ"],
            ffmpeg=ffmpeg, levels=bool(plan["options"]["gain_limit_mdb"]), progress=progress, cancel=cancel,
            lyric_sources=plan["lyrics"], reuse_tracks=reuse,
            decorate=lambda root, files, rows: _decorate(plan, root, files, rows))
        if current:
            _merge_existing(plan, staging, cancel)
        verified = verify(staging, cancel=cancel)
        if not verified["ok"]:
            raise xm5.InlineError("Staged library verification failed: " + str(verified["errors"][:5]))
        for path in (staging / "xm5/Struct").rglob("*"):
            if path.is_file():
                xm5.check_cancel(cancel)
                with path.open("r+b") as stream:
                    os.fsync(stream.fileno())
        if (signature(destination, cancel=cancel) if destination.exists() else None) != current:
            raise xm5.InlineError("Card changed during preparation; original remains active")
        xm5.check_cancel(cancel)
        if current:
            backup = parent / (".xm5-before-" + uuid.uuid4().hex)
        journal = {"schema": "xm5.inline.activation", "version": 1,
                   "candidate": str(staging / "xm5"), "active": str(destination),
                   "backup": str(backup) if backup else None}
        with (parent / ACTIVATION).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(journal) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if current:
            os.rename(destination, backup)
        try:
            export_card._activate(staging / "xm5", destination)
        except BaseException:
            if backup is not None and not destination.exists():
                os.rename(backup, destination)
            (parent / ACTIVATION).unlink(missing_ok=True)
            raise
        (parent / ACTIVATION).unlink()
    _, manifest = load(parent)
    return {"status": "complete", "root": str(destination), "destination": str(destination),
            "build_id": manifest["build_id"], "verification": verified,
            "reused_tracks": sum(row.get("reused", False) for row in manifest.get("preparation", [])),
            "unprepared_tracks": [row["path"] for row in manifest.get("preparation", []) if row["status"] == "unsupported"],
            "previous_tree": str(backup) if backup else None}


def verify(root, *, cancel=None):
    root, manifest = load(root)
    errors = []
    for relative, expected in manifest["owned"].items():
        xm5.check_cancel(cancel)
        # Native playlists/settings are expected to be editable after export.
        if relative.startswith("xm5/Struct/playlists/") or relative == "xm5/Struct/settings.ini":
            continue
        try:
            path = xm5._card_path(root, relative)
            if _hash(path) != expected:
                errors.append(relative + ": changed")
        except (OSError, ValueError) as exc:
            errors.append(relative + ": " + str(exc))
    return {"ok": not errors, "errors": errors, "files": len(manifest["owned"])}


def reconcile(root, source):
    root, manifest = load(root)
    source = Path(source).absolute()
    relative = source.relative_to(root).as_posix()
    if source.suffix.lower() != ".m3u8" or not relative.startswith("xm5/Struct/playlists/"):
        raise xm5.InlineError("Choose an M3U8 below xm5/Struct/playlists")
    record = next((row for row in manifest["playlists"] if row["path"] == relative), None)
    favorite = relative == playlists.FAVORITES
    missing = not source.exists()
    if missing and not record and not favorite:
        raise FileNotFoundError(source)
    entries = [] if missing else playlists.read_playlist(root, relative)
    reverse = {path: tid for tid, path in manifest["track_paths"].items()}
    proposed, unresolved, ordered = [], [], []
    for entry in entries:
        path = entry.card_path.as_posix()
        if path in reverse:
            proposed.append(reverse[path])
            ordered.append({"track_id": reverse[path], "card_path": path})
        else:
            unresolved.append({"path": path, "present": entry.present})
            ordered.append({"card_path": path})
    text = "" if missing else source.read_text(encoding="utf-8-sig")
    name = next((line[10:] for line in text.splitlines() if line.startswith("#PLAYLIST:")),
                record["name"] if record else "Favorites" if favorite else source.stem)
    return {"format": "XM5I", "build_id": manifest["build_id"], "playlist_id": record["id"] if record else None,
            "name": name, "favorites": favorite, "proposed_tracks": proposed, "unresolved": unresolved,
            "entries": ordered,
            "missing": missing, "deleted": missing and not favorite,
            "scope_track_ids": list(manifest["track_paths"]),
            "base_snapshot": {"tracks": manifest["favorites"] if favorite else record["track_ids"],
                              "name": "Favorites" if favorite else record["name"]} if favorite or record else None,
            "input_sha256": input_digest(source)}


def input_digest(source):
    return _hash(source) if Path(source).exists() else sha256(b"XM5 missing M3U8\0" + os.fsencode(Path(source).name)).hexdigest()


def playlist_paths(root):
    root, manifest = load(root)
    paths = {root / row["path"] for row in manifest["playlists"]}
    paths.add(root / playlists.FAVORITES)
    paths.update((root / "xm5/Struct/playlists").glob("*.m3u8"))
    return sorted(paths)


def restore(root, destination):
    root, manifest = load(root)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    for category in ("music", "EQ", "Struct/playlists"):
        export_card._read_tree(root / "xm5" / category)
    destination.mkdir()
    # The per-source shell is the original imported folder. Ordinary metadata
    # blocks are valid in other players; audio and original artwork are intact.
    for category in ("music", "EQ"):
        shutil.copytree(root / "xm5" / category, destination / category)
    shutil.copytree(root / "xm5/Struct/playlists", destination / "playlists")
    return {"status": "complete", "destination": str(destination), "original_audio_preserved": True}


def refresh_owned(root, selected=()):
    """Refresh receipts only for explicit app writes, never for an inspection."""
    root, manifest = load(root)
    for relative in [*selected, "xm5/Struct/tracks.tsv"]:
        path = xm5._card_path(root, relative)
        manifest["owned"][relative] = _hash(path)
        for row in manifest["files"]:
            if row["path"] == relative:
                row["export_sha256"] = manifest["owned"][relative]
    path = root / MANIFEST
    fd, temporary = tempfile.mkstemp(prefix=".library-", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        _json(temporary, manifest)
        with open(temporary, "r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def recover_activation(parent):
    parent = export_card._directory(parent)
    journal_path = parent / ACTIVATION
    raw = xm5._bounded_read(journal_path, 8192)
    journal = json.loads(raw)
    from . import delta
    if journal.get("schema") == delta.SCHEMA:
        return delta.recover(parent)
    if journal.get("schema") != "xm5.inline.activation" or journal.get("version") != 1:
        raise xm5.InlineError("Unknown activation journal")
    active = Path(journal["active"]).absolute()
    candidate = Path(journal["candidate"]).absolute()
    backup = Path(journal["backup"]).absolute() if journal.get("backup") else None
    if (active != parent / "xm5" or candidate.parent.parent != parent or candidate.name != "xm5"
            or not candidate.parent.name.startswith(".xm5-library-")
            or (backup and (backup.parent != parent or not backup.name.startswith(".xm5-before-")))):
        raise xm5.InlineError("Activation journal paths do not match the selected card")
    if active.exists():
        load(parent)
        outcome = "active_tree_retained"
    elif backup and backup.is_dir():
        # Validate the old tree without following any journal path outside this
        # explicitly chosen parent. No source drive is consulted.
        export_card._read_tree(backup)
        data = json.loads(xm5._bounded_read(backup / "Struct/library.json", 32 * 1024 * 1024))
        if data.get("schema") != SCHEMA:
            raise xm5.InlineError("The retained previous tree is not an inline library")
        os.rename(backup, active)
        outcome = "previous_tree_restored"
    elif not backup and candidate.is_dir():
        if not verify(candidate.parent)["ok"]:
            raise xm5.InlineError("The staged new library did not verify")
        export_card._activate(candidate, active)
        outcome = "new_tree_activated"
    else:
        raise xm5.InlineError("No intact activation source was found; no files were changed")
    journal_path.unlink()
    return {"status": "complete", "outcome": outcome, "root": str(active)}
