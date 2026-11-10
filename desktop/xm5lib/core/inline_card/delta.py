"""File-delta updates of an inline card with a durable native-visible guard.

Unchanged files stay in place. Only prepared new/changed bytes are staged; old
replaced bytes are renamed into a partial backup. Resume and rollback infer a
half-completed rename from exact hashes instead of trusting the last journal
state. The shared legacy transaction implementation remains unchanged.
"""
from __future__ import annotations

import copy
import ctypes
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import uuid

from ..restore_card import (_pinned_directory, _windows_rename, ensure_directory,
                            exclusive_output, replace_owned, safe_relative, unlink_owned)
from . import export_card, organize_card, xm5_inline as xm5

SCHEMA = "xm5.inline.delta-journal"
POINTER = ".xm5-activation.json"
MARKER = "xm5/Struct/INCOMPLETE"
JOURNAL_KEYS = frozenset(("schema", "version", "root", "transaction", "status", "published", "plan_sha256"))


def _lib():
    from . import library
    return library


def top(path):
    path = export_card._directory(path)
    if (path / POINTER).exists() or (path / "xm5/Struct/library.json").is_file():
        return path
    if (path / "Struct/library.json").is_file() or (path / "Struct/INCOMPLETE").exists():
        return path.parent
    return path


def _stat(path):
    return list(xm5._snapshot(Path(path).lstat()))


def _flat(signatures):
    result = {}
    for root, rows in signatures.items():
        for relative, identity, directory in rows:
            if not directory:
                result[os.path.normcase(str(Path(root) / relative))] = identity
    return result


def _companion_index(records):
    result = {}
    for key, identity in records.items():
        if Path(key).suffix.lower() in organize_card.IMAGE_SUFFIXES:
            result.setdefault(Path(key).parent, {})[key] = identity
    return result


def annotate(plan):
    """Freeze inexpensive per-file classifications and the actual stage budget."""
    lib = _lib()
    root, old = lib.load(plan["parent"])
    previous = {row["path"]: row for row in old["files"]}
    current_sources = _flat({row["source"]: row["signature"] for row in plan["roots"]})
    old_sources = _flat(old.get("source_signatures", {}))
    current_companions, old_companions = _companion_index(current_sources), _companion_index(old_sources)
    rows = []
    for entry in plan["files"]:
        source = Path(entry["source"])
        relative = entry["path"]
        before = previous.get(relative)
        identity = current_sources[os.path.normcase(str(source))]
        old_identity = (before or {}).get("source_identity") or old_sources.get(os.path.normcase(str(source)))
        unchanged = bool(before and identity == old_identity and relative not in plan.get("replace_owned", []))
        audio = source.suffix.lower() in export_card.TRACK_SUFFIXES and relative.startswith("xm5/music/")
        if before and not unchanged and relative not in plan.get("replace_owned", []) and before.get("original_sha256"):
            unchanged = lib._hash(source) == before["original_sha256"]
        if audio and unchanged:
            unchanged = (current_companions.get(source.parent, {}) == old_companions.get(source.parent, {})
                and old.get("lyric_hashes", {}).get(relative) == (lib._hash(plan["lyrics"][relative]) if relative in plan["lyrics"] else None)
                and bool(old.get("options", {}).get("gain_limit_mdb")) == bool(plan["options"]["gain_limit_mdb"]))
        rows.append({**entry, "source_identity": identity, "reuse": unchanged,
                     "audio": audio, "original_sha256": before.get("original_sha256") if before else None})
    changed = [row for row in rows if not row["reuse"]]
    metadata = len(json.dumps(old, ensure_ascii=False).encode()) * 2 + 512 * 1024
    metadata += sum(len(text.encode()) for text in plan["presets"].values())
    metadata += sum(Path(source).stat().st_size + 12000 for source in plan["playlist_covers"].values())
    metadata += sum(row["size"] for row in plan.get("lyric_originals", []))
    prepared = sum(row["size"] + xm5.LYRICS_OFFSET + xm5.MAX_LYRICS for row in changed if row["audio"])
    ordinary = sum(row["size"] for row in changed if not row["audio"])
    workspace = max((row["size"] + xm5.LYRICS_OFFSET + xm5.MAX_LYRICS for row in changed if row["audio"]), default=0)
    plan["delta"] = {"version": 1, "files": rows, "metadata_budget": metadata,
                     "required_bytes": prepared + ordinary + workspace + metadata}
    plan["summary"].update(estimated_bytes=plan["delta"]["required_bytes"],
                           changed_files=len(changed), unchanged_files=len(rows)-len(changed), transfer="file_delta")
    return plan


def _move(root, source, destination):
    """Exclusive same-volume file rename through pinned, unlinked parents."""
    source, destination = PurePosixPath(safe_relative(source)), PurePosixPath(safe_relative(destination))
    with _pinned_directory(root, "" if str(source.parent) == "." else str(source.parent)) as a, \
            _pinned_directory(root, "" if str(destination.parent) == "." else str(destination.parent), create=True) as b:
        if os.name == "nt":
            _windows_rename(a, source.name, b, destination.name, directory=False, replace=False)
        else:
            native = ctypes.CDLL(None, use_errno=True)
            rename = native.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(a, os.fsencode(source.name), b, os.fsencode(destination.name), 1):
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), destination.as_posix())


def _atomic(root, relative, value):
    temporary = str(PurePosixPath(relative).parent / (".journal-" + uuid.uuid4().hex))
    with exclusive_output(root, temporary) as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    replace_owned(root, temporary, relative)


def _hash(root, relative):
    try:
        path = xm5._card_path(root, relative, allow_new=True)
    except FileNotFoundError:
        return None
    return _lib()._hash(path) if path.exists() else None


def _save(root, journal):
    if "plan_sha256" not in journal:
        frozen = {key: value for key, value in journal.items() if key not in ("status", "published", "error")}
        raw = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        with exclusive_output(root, journal["control"] + "/plan.json") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        journal["plan_sha256"] = sha256(raw).hexdigest()
    mutable = {key: journal[key] for key in JOURNAL_KEYS}
    if "error" in journal:
        mutable["error"] = journal["error"]
    _atomic(root, journal["control"] + "/journal.json", mutable)


def _pointer_bytes(journal):
    return (json.dumps({"schema": SCHEMA, "version": 1, "transaction": journal["transaction"]}) + "\n").encode()


def _publish_pointer(root, journal):
    temporary = journal["control"] + "/.pointer-" + uuid.uuid4().hex
    with exclusive_output(root, temporary) as stream:
        stream.write(_pointer_bytes(journal)); stream.flush(); os.fsync(stream.fileno())
    _move(root, temporary, POINTER)


def _assert_pointer(root, journal):
    if xm5._bounded_read(xm5._card_path(root, POINTER), 8192) != _pointer_bytes(journal):
        raise xm5.InlineError("The activation pointer belongs to another transaction")


def _marker(transaction):
    return (json.dumps({"schema": "xm5.inline.incomplete", "transaction": transaction}) + "\n").encode()


def _check_marker(root, journal, create=False):
    target = xm5._card_path(root, MARKER, allow_new=True)
    expected = _marker(journal["transaction"])
    if not target.exists() and create:
        with exclusive_output(root, MARKER) as stream:
            stream.write(expected); stream.flush(); os.fsync(stream.fileno())
    if not target.is_file() or target.read_bytes() != expected:
        raise xm5.InlineError("The incomplete marker belongs to another operation")


def _version(root, relative, old, *, strict=False):
    try:
        path = xm5._card_path(root, relative, allow_new=True)
    except FileNotFoundError:
        return None
    if not path.exists():
        return None
    if not stat.S_ISREG(path.lstat().st_mode):
        raise xm5.InlineError("Expected a regular card file: " + relative)
    cached = old.get("owned_stats", {}).get(relative)
    if not strict and cached == _stat(path) and relative in old["owned"]:
        return old["owned"][relative]
    return _lib()._hash(path)


def _probe(path):
    if path.suffix.lower() == ".flac":
        with path.open("rb") as stream:
            block = xm5._scan(stream).app
            if block is not None:
                xm5._check_slot_headers(stream, block.body)
            return block is not None
    return organize_card._wrapped_header(path)


def _prepare(root, work, plan, old, ffmpeg, cancel, progress):
    lib = _lib()
    stage = root / work / "new"
    for relative in ("xm5/music", "xm5/EQ", "xm5/Struct/playlists"):
        (stage / relative).mkdir(parents=True, exist_ok=True)
    previous = {row["path"]: row for row in old["files"]}
    old_prep = {row["path"]: row for row in old.get("preparation", [])}
    files, preparation, reused, changed = [], [], 0, set()
    for number, row in enumerate(plan["delta"]["files"], 1):
        xm5.check_cancel(cancel)
        relative = row["path"]
        source = Path(row["source"])
        expected = row["source_identity"]
        if list(export_card._identity(source.lstat())) != expected:
            raise xm5.InlineError("Source changed after preview: " + str(source))
        before = previous.get(relative)
        card_hash = _version(root, relative, old)
        expected_card = old["owned"].get(relative)
        if card_hash is not None and card_hash != expected_card:
            if relative not in plan.get("replace_owned", []):
                raise xm5.InlineError("A card file has changed or belongs to the user; it was preserved: " + relative)
        reuse = row["reuse"] and before is not None and card_hash is not None and relative not in plan.get("replace_owned", [])
        if reuse:
            files.append(copy.deepcopy(before))
            files[-1]["source_identity"] = expected
            files[-1]["source"] = str(source)
            if row["audio"]:
                preparation.append({**old_prep.get(relative, {"path": relative, "status": "present"}), "reused": True})
                reused += 1
            continue
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = export_card._copy_file(source, target, tuple(expected), cancel)
        result = None
        if row["audio"]:
            try:
                result = organize_card.organize_one(stage, relative, ffmpeg,
                    source_context=source, refresh=True, lyrics=plan["lyrics"].get(relative),
                    levels=bool(plan["options"]["gain_limit_mdb"]), publish_index=False, cancel=cancel)
            except organize_card.formats.Unsupported as exc:
                result = {"status": "unsupported", "diagnostic": str(exc)}
            preparation.append({"path": relative, "reused": False, **result})
        files.append({"path": relative, "source": str(source), "original_sha256": digest.hex(),
                      "export_sha256": lib._hash(target), "source_identity": expected})
        changed.add(relative)
        if progress:
            progress({"phase": "inline_delta_prepare", "current": number, "total": len(plan["delta"]["files"]), "path": relative})
    decoration = copy.deepcopy(plan)
    decoration["track_paths"] = old["track_paths"] | plan["track_paths"]
    decoration["favorites"] = list(dict.fromkeys([tid for tid in old["favorites"] if tid not in plan["track_paths"]] + plan["favorites"]))
    lib._decorate(decoration, stage, [row for row in files if row["path"] in changed], preparation)
    _, new = lib.load(stage)
    present = {row["path"] for row in files}
    new["files"] = files + [row for row in old["files"] if row["path"] not in present]
    selected_lists = {row["path"] for row in new["playlists"]}
    new["playlists"] += [row for row in old["playlists"] if row["path"] not in selected_lists and (root / row["path"]).is_file()]
    new["source_signatures"] = old.get("source_signatures", {}) | new["source_signatures"]
    new["lyric_hashes"] = old.get("lyric_hashes", {}) | new["lyric_hashes"]
    new["owned"] = old["owned"] | new["owned"]
    new["owned_stats"] = copy.deepcopy(old.get("owned_stats", {}))
    new["playlist_hashes"] = old["playlist_hashes"] | new["playlist_hashes"]
    try:
        index = xm5._read_index(root / xm5.INDEX_PATH)
    except (OSError, ValueError):
        index = {}
    current_stats = {"xm5/" + relative: identity for relative, identity, directory in plan["existing_signature"] if not directory}
    index = {relative: value for relative, value in index.items() if relative in current_stats or relative in changed}
    for relative, identity in current_stats.items():
        active = root / relative
        if active.suffix.lower() not in export_card.TRACK_SUFFIXES or not relative.startswith("xm5/music/") or relative in changed:
            continue
        cached = old.get("owned_stats", {}).get(relative)
        if relative not in index or cached != identity[:4]:
            try:
                index[relative] = _probe(active)
            except (ValueError, OSError):
                index.pop(relative, None)
        if relative not in present and relative in old["owned"]:
            actual = _version(root, relative, old)
            if actual != old["owned"][relative]:
                new["owned"].pop(relative, None)
                new["owned_stats"].pop(relative, None)
                new.setdefault("retained_modified", []).append(relative)
    for row in preparation:
        if row["path"] in changed:
            if row["status"] == "unsupported":
                index.pop(row["path"], None)
            else:
                index[row["path"]] = _probe(stage / row["path"])
    staged_paths = {path.relative_to(stage).as_posix() for path in (stage / "xm5").rglob("*") if path.is_file()}
    for relative in list(new["owned"]):
        if relative not in staged_paths and relative not in current_stats:
            new["owned"].pop(relative, None)
            new["owned_stats"].pop(relative, None)
            new.setdefault("retained_missing", []).append(relative)
    xm5._write_index(stage / xm5.INDEX_PATH, index)
    new["owned"][xm5.INDEX_PATH] = lib._hash(stage / xm5.INDEX_PATH)
    staged = {}
    for path in (stage / "xm5").rglob("*"):
        if path.is_file() and path.relative_to(stage).as_posix() != lib.MANIFEST:
            relative = path.relative_to(stage).as_posix()
            staged[relative] = lib._hash(path)
            new["owned_stats"][relative] = _stat(path)
    for row in files:
        if row["path"] not in changed:
            new["owned_stats"][row["path"]] = _stat(root / row["path"])
    lib._json(stage / lib.MANIFEST, new)
    staged[lib.MANIFEST] = lib._hash(stage / lib.MANIFEST)
    return stage, new, staged, reused


def _unchanged_guard(root, journal):
    changed = {operation["path"] for operation in journal["operations"]}
    for relative, identity, directory in journal["existing_signature"]:
        relative = "xm5" if relative == "." else "xm5/" + relative
        if relative in changed:
            continue
        path = xm5._card_path(root, relative)
        actual = list(export_card._identity(path.lstat()))
        same = actual[:2] == identity[:2] if directory else actual == identity
        if not same:
            raise xm5.InlineError("An untouched card path changed during activation: " + relative)


def _publish_one(root, journal, operation):
    path, before, after = operation["path"], operation["before"], operation["after"]
    saved = journal["backup"] + "/" + path
    staged = journal["control"] + "/new/" + path
    active_hash = _hash(root, path)
    backup_hash = _hash(root, saved)
    if active_hash == after:
        if before is not None and backup_hash != before:
            raise xm5.InlineError("Published file lacks its exact previous backup: " + path)
        return
    if before is not None:
        if active_hash == before and backup_hash is None:
            _move(root, path, saved)
            _save(root, journal)
        elif active_hash is not None or backup_hash != before:
            raise xm5.InlineError("Card bytes changed before replacement: " + path)
    elif active_hash is not None or backup_hash is not None:
        raise xm5.InlineError("An unowned file appeared during activation: " + path)
    if _hash(root, staged) != after:
        raise xm5.InlineError("Staged bytes failed recovery verification: " + path)
    _move(root, staged, path)
    if _hash(root, path) != after:
        raise xm5.InlineError("Published bytes failed verification: " + path)


def _result(root, journal):
    return {"status": "complete" if journal["status"] == "completed" else journal["status"],
            "root": str(root / "xm5"), "destination": str(root / "xm5"), "build_id": journal["build_id"],
            "transaction_id": journal["transaction"], "recovery_required": journal["status"] not in ("completed", "rolled_back"),
            "previous_tree": str(root / journal["backup"] / "xm5"), "backup_root": str(root / journal["backup"]),
            "reused_tracks": journal["reused_tracks"], "verification": {"ok": journal["status"] == "completed",
                "method": "changed_file_hashes_and_unchanged_stat_cache"}, "error": journal.get("error")}


def _run(root, journal, cancel=None, progress=None):
    try:
        _assert_pointer(root, journal)
        _check_marker(root, journal, create=True)
        journal["status"] = "publishing"
        _save(root, journal)
        for number, operation in enumerate(journal["operations"], 1):
            xm5.check_cancel(cancel)
            _assert_pointer(root, journal)
            _publish_one(root, journal, operation)
            journal["published"] = number
            _save(root, journal)
            if progress:
                progress({"phase": "inline_delta_publish", "current": number, "total": len(journal["operations"]), "path": operation["path"]})
        _unchanged_guard(root, journal)
        _assert_pointer(root, journal)
        journal["status"] = "completed"
        journal.pop("error", None)
        _save(root, journal)
        _check_marker(root, journal)
        unlink_owned(root, MARKER)
        unlink_owned(root, POINTER)
    except (OSError, ValueError) as exc:
        journal["status"] = "cancelled" if isinstance(exc, InterruptedError) else "interrupted"
        journal["error"] = {"code": "CANCELLED" if isinstance(exc, InterruptedError) else "INLINE_INTERRUPTED", "message": str(exc)}
        _save(root, journal)
    return _result(root, journal)


def execute(plan, *, ffmpeg=None, progress=None, cancel=None):
    lib = _lib()
    root, old = lib.load(plan["parent"])
    if "delta" not in plan:
        annotate(plan)
    if shutil.disk_usage(root).free < plan["delta"]["required_bytes"]:
        raise xm5.InlineError("Not enough free space for the selected file delta")
    transaction = uuid.uuid4().hex
    control, backup = ".xm5-library-" + transaction, ".xm5-before-" + transaction
    ensure_directory(root, control)
    retained = False
    try:
        stage, new, staged, reused = _prepare(root, control, plan, old, ffmpeg, cancel, progress)
        operations = []
        for relative, after in staged.items():
            before = _version(root, relative, old, strict=True)
            if before == after:
                continue
            expected = old["owned"].get(relative)
            if relative == lib.MANIFEST:
                expected = lib._hash(root / lib.MANIFEST)
            acknowledged = plan.get("acknowledged_playlists", {}).get(relative) == before
            repair = relative in plan.get("replace_owned", []) and relative in old["owned"]
            if before is not None and before != expected and not acknowledged and not repair:
                raise xm5.InlineError("Changed or unowned card file was preserved: " + relative)
            operations.append({"path": relative, "before": before, "after": after})
        operations.sort(key=lambda row: (3 if row["path"] == lib.MANIFEST else 2 if row["path"] == xm5.INDEX_PATH
            else 0 if row["path"].startswith(("xm5/music/", "xm5/EQ/")) else 1, row["path"]))
        for operation in operations:
            path = stage / operation["path"]
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
        for row in plan["roots"]:
            if lib.signature(row["source"], cancel=cancel) != row["signature"]:
                raise xm5.InlineError("Source changed during preparation")
        if lib.signature(root / "xm5", cancel=cancel) != plan["existing_signature"]:
            raise xm5.InlineError("Card changed during preparation")
        xm5.check_cancel(cancel)
        journal = {"schema": SCHEMA, "version": 1, "root": str(root), "root_identity": _stat(root)[:2],
                   "transaction": transaction, "control": control, "backup": backup,
                   "build_id": new["build_id"], "operations": operations, "published": 0,
                   "existing_signature": plan["existing_signature"], "status": "prepared", "reused_tracks": reused}
        _save(root, journal)
        try:
            _publish_pointer(root, journal)
        finally:
            retained = (root / POINTER).is_file() and (root / POINTER).read_bytes() == _pointer_bytes(journal)
        return _run(root, journal, cancel, progress)
    finally:
        if not retained:
            target = (root / control).resolve()
            if target.parent != root.resolve() or not target.name.startswith(".xm5-library-"):
                raise xm5.InlineError("Unexpected isolated delta cleanup path")
            shutil.rmtree(target)


def _load(root, transaction=None):
    root = top(root)
    if transaction is None:
        pointer = json.loads(xm5._bounded_read(root / POINTER, 8192))
        if pointer.get("schema") != SCHEMA:
            raise xm5.InlineError("This is not an inline file-delta transaction")
        transaction = pointer["transaction"]
    if not isinstance(transaction, str) or len(transaction) != 32 or any(c not in "0123456789abcdef" for c in transaction):
        raise xm5.InlineError("Invalid inline transaction identity")
    control = ".xm5-library-" + transaction
    journal = json.loads(xm5._bounded_read(xm5._card_path(root, control + "/journal.json"), 64 * 1024))
    if (not isinstance(journal, dict) or not JOURNAL_KEYS <= journal.keys()
            or journal.keys() - JOURNAL_KEYS - {"error"}):
        raise xm5.InlineError("Unexpected mutable delta journal fields")
    raw = xm5._bounded_read(xm5._card_path(root, control + "/plan.json"), 128 * 1024 * 1024)
    if sha256(raw).hexdigest() != journal.get("plan_sha256"):
        raise xm5.InlineError("The frozen delta plan failed its journal checksum")
    frozen = json.loads(raw)
    if not isinstance(frozen, dict) or any(key in frozen for key in ("status", "published", "error", "plan_sha256")):
        raise xm5.InlineError("Invalid frozen delta plan fields")
    if any(frozen.get(key) != journal.get(key) for key in ("schema", "version", "root", "transaction")):
        raise xm5.InlineError("Journal does not match its frozen delta plan")
    # The checksum protects the complete operation list and source/card snapshots.
    # Mutable state must never shadow those frozen fields during recovery.
    journal = {**frozen, **{key: journal[key] for key in ("status", "published", "plan_sha256", "error") if key in journal}}
    if (journal.get("schema") != SCHEMA or journal.get("root") != str(root)
            or journal.get("transaction") != transaction or journal.get("control") != control
            or journal.get("backup") != ".xm5-before-" + transaction or journal.get("root_identity") != _stat(root)[:2]):
        raise xm5.InlineError("Journal does not belong to this card")
    for operation in journal["operations"]:
        path = safe_relative(operation["path"])
        if not path.startswith(("xm5/music/", "xm5/EQ/", "xm5/Struct/")) or path == MARKER:
            raise xm5.InlineError("Journal path is outside the managed namespace")
    if type(journal.get("published")) is not int or not 0 <= journal["published"] <= len(journal["operations"]):
        raise xm5.InlineError("Invalid journal publication count")
    if (root / POINTER).exists():
        pointer = json.loads(xm5._bounded_read(root / POINTER, 8192))
        if pointer.get("schema") != SCHEMA or pointer.get("transaction") != transaction:
            raise xm5.InlineError("Another transaction owns this card activation")
    return root, journal


def recover(root, transaction=None, *, action="rollback", cancel=None, progress=None):
    root, journal = _load(root, transaction)
    if action not in ("resume", "rollback"):
        raise xm5.InlineError("Recovery action must be resume or rollback")
    if journal["status"] in ("completed", "rolled_back") and not (root / POINTER).exists():
        return _result(root, journal)
    if not (root / POINTER).exists():
        _publish_pointer(root, journal)
    if action == "resume":
        return _run(root, journal, cancel, progress)
    try:
        _assert_pointer(root, journal)
        # Check every required backup before moving any active file. A blocked
        # rollback must keep the currently readable version in place.
        for operation in journal["operations"]:
            path, before, after = operation["path"], operation["before"], operation["after"]
            staged = journal["control"] + "/new/" + path
            saved = journal["backup"] + "/" + path
            active, backup_hash = _hash(root, path), _hash(root, saved)
            if active == before and backup_hash is None:
                continue
            if before is not None and backup_hash != before:
                raise xm5.InlineError("A missing or modified backup blocks rollback: " + path)
            if active == after:
                if _hash(root, staged) not in (None, after):
                    raise xm5.InlineError("Rollback staging contains unrelated bytes")
            elif active is not None:
                raise xm5.InlineError("A user edit blocks rollback: " + path)
        _check_marker(root, journal, create=True)
        journal["status"] = "rolling_back"
        _save(root, journal)
        for operation in reversed(journal["operations"]):
            path, before, after = operation["path"], operation["before"], operation["after"]
            staged, saved = journal["control"] + "/new/" + path, journal["backup"] + "/" + path
            active, backup_hash = _hash(root, path), _hash(root, saved)
            if active == before and backup_hash is None:
                continue
            if before is not None and backup_hash != before:
                raise xm5.InlineError("A missing or modified backup blocks rollback: " + path)
            if active == after:
                if _hash(root, staged) is None:
                    _move(root, path, staged)
                elif _hash(root, staged) == after:
                    unlink_owned(root, path)
                else:
                    raise xm5.InlineError("Rollback staging contains unrelated bytes")
                active = None
            if active is not None or (before is not None and backup_hash != before):
                raise xm5.InlineError("A user edit blocks rollback: " + path)
            if before is not None:
                _move(root, saved, path)
            _save(root, journal)
        journal["status"] = "rolled_back"
        journal.pop("error", None)
        _save(root, journal)
        _check_marker(root, journal)
        unlink_owned(root, MARKER)
        unlink_owned(root, POINTER)
    except (OSError, ValueError) as exc:
        journal["status"] = "interrupted"
        journal["error"] = {"code": "ROLLBACK_BLOCKED", "message": str(exc)}
        _save(root, journal)
    return _result(root, journal)


def transactions(root):
    root = top(root)
    result = []
    for path in root.glob(".xm5-library-*/journal.json"):
        try:
            _, journal = _load(root, path.parent.name.removeprefix(".xm5-library-"))
            result.append({"transaction_id": journal["transaction"], "status": journal["status"],
                           "published": journal["published"], "backup_root": str(root / journal["backup"])})
        except (OSError, ValueError):
            continue
    return result
