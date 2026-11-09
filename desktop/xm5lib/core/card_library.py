"""Lossless directory-preserving exports, prepared art and explicit return proposals.

Planning and intake only read inputs. Execution writes a fresh destination and never tags audio.
No function starts a listener, playback engine, card operation or autorun handler.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import uuid

import mutagen
from PIL import __version__ as PILLOW_VERSION

from . import card_layout as layout, covers, lyrics_index, native_art_index, peq, tags, track_info, peak_scan, device_state, native_art_prepare
from .restore_card import (CHECKSUM, INCOMPLETE, MANIFEST, META, SCHEMA, VERSION,
                           RestoreError, checked_path, file_digest, load_manifest,
                           new_destination, no_links, path_key, restore_export,
                           safe_relative, verify_export, detect_layout, playlist_files,
                           exclusive_output, ensure_directory, make_new_directory, replace_owned, unlink_owned,
                           rename_owned_directory)

CardLibraryError = RestoreError
IMAGE_EXTENSIONS = set(covers.IMAGE_EXT) | {".gif", ".tif", ".tiff", ".avif"}
RECIPE_ID = "square-black-fit-v5-bgra32-native-ui"
RECIPE = {"id": RECIPE_ID, "format": "bmp-top-down-bgra32", "sizes": list(native_art_index.NATIVE_SIDES),
          "asset_layout": native_art_index.ASSET_LAYOUT,
          "pixel_order": "BGRA", "pixel_offset": 64, "row_order": "top-down",
          "orientation": "exif-transpose", "fit": "contain-no-upscale", "background": "black",
          "alpha": "opaque-composite-black", "resampling": "lanczos",
          "pillow_version": PILLOW_VERSION}
RESERVED_ROOTS = {".xm5", "xm5_inbox", "_explaylist_data", "eq", "lyrics", "favorites", "presets", "restore_library.py", "playlist covers",
                  "card additions", "card playlists", "restore_incomplete", "recovery_report.json"}
MAX_INDEX_LINE = 2208
ART_INDEX_SCHEMA = "xm5.art-index"
ART_INDEX_VERSION = 2
ART_SIZES = native_art_index.NATIVE_SIDES
ROLE_SIZES = native_art_index.ROLE_SIZES


def _index_capabilities():
    return {"schema": ART_INDEX_SCHEMA, "version": ART_INDEX_VERSION,
            "asset_layout": native_art_index.ASSET_LAYOUT,
            "sizes": list(ART_SIZES), "role_sizes": {role: list(sizes) for role, sizes in ROLE_SIZES.items()},
            "columns": ["role", "subject", "asset", "sha256", "side"], "encoding": "utf-8",
            "negative_asset": "-", "negative_side": 0,
            "subject_scope": "card-root-relative", "asset_scope": "card-root-relative",
            "playlist_subject_scope": "stable-playlist-id"}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _fail(code, message, **details):
    raise CardLibraryError(code, message, details)


def _kind(path):
    ext = path.suffix.lower()
    return "audio" if ext in tags.AUDIO_EXTENSIONS else "image" if ext in IMAGE_EXTENSIONS else "other"


def _walk(root):
    no_links(root)
    def onerror(error):
        raise error
    for folder, directories, files in os.walk(root, followlinks=False, onerror=onerror):
        directories.sort(key=lambda x: (x.casefold(), x))
        files.sort(key=lambda x: (x.casefold(), x))
        for name in [*directories, *files]:
            no_links(Path(folder) / name)
        yield Path(folder), directories, files


def _source_art(reference):
    source = no_links(reference["source"])
    if reference["kind"] == "embedded":
        audio = mutagen.File(source)
        data = tags.embedded_picture(audio) if audio is not None else None
        if not data:
            _fail("art_changed", "Embedded artwork disappeared", source=str(source))
    else:
        data = source.read_bytes()
    if _sha(data) != reference["sha256"]:
        _fail("art_changed", "Artwork changed after planning", source=str(source))
    return data


def _embedded(path, warnings):
    try:
        audio = mutagen.File(path)
        if audio is None:
            warnings.append({"code": "metadata_unrecognized", "source": str(path)})
            return None
        data = tags.embedded_picture(audio)
        return {"kind": "embedded", "source": str(path), "sha256": _sha(data)} if data else None
    except Exception as exc:
        warnings.append({"code": "metadata_unreadable", "source": str(path), "message": str(exc)})
        return None


def _folder_art(folder, image_paths, warnings):
    candidates = sorted(image_paths, key=lambda p: (p.name.casefold(), p.name))
    for preferred in covers.FOLDER_NAMES:
        matches = [p for p in candidates if p.stem.casefold() == preferred]
        if len(matches) == 1:
            p = matches[0]
            return {"kind": "file", "source": str(p), "sha256": file_digest(p)[1]}
        if len(matches) > 1:
            warnings.append({"code": "ambiguous_folder_art", "folder": str(folder), "candidates": [str(p) for p in matches]})
            return None
    if len(candidates) == 1:
        p = candidates[0]
        return {"kind": "file", "source": str(p), "sha256": file_digest(p)[1]}
    if candidates:
        warnings.append({"code": "ambiguous_folder_art", "folder": str(folder), "candidates": [str(p) for p in candidates]})
    return None


def _add_art(plan, ref):
    if ref is None:
        return None
    art_id = _sha(_json({"recipe": RECIPE, "source_sha256": ref["sha256"]}))
    if art_id not in plan["artworks"]:
        try:
            derived = native_art_prepare.prepare(_source_art(ref))
        except Exception as exc:
            _fail("invalid_art", "Artwork cannot be prepared", source=ref["source"], reason=str(exc))
        derivatives = {str(side): {"sha256": _sha(derived[side]),
                                   "size": len(derived[side])} for side in ART_SIZES}
        plan["artworks"][art_id] = {"source_sha256": ref["sha256"], "recipe": RECIPE_ID,
                                   "source": ref, "derivatives": derivatives,
                                   "roles": {side: native_art_index.asset_path_for_digest(info["sha256"])
                                             for side, info in derivatives.items()}}
    return art_id


def _plan_hash(plan):
    return _sha(_json({k: v for k, v in plan.items() if k != "plan_sha256"}))


def _derived_outputs(plan):
    if plan.get("native_only_art"):
        return {}
    outputs = {}
    for art in plan["artworks"].values():
        for side, path in art["roles"].items():
            expected = art["derivatives"][side]
            if path in outputs and outputs[path] != expected:
                _fail("path_collision", "Conflicting planned native artwork identity", path=path)
            outputs[path] = expected
    return outputs


def _estimate_export_bytes(plan, derived_outputs):
    """Conservative file-content estimate; filesystem cluster overhead is separate."""
    tsv_bytes, records, pool_strings = 0, 0, set()
    for role, items, subject_key, art_key in (
            ("directory", plan["directories"], "path", "preview_art"),
            ("playback", plan["tracks"], "path", "playback_art"),
            ("playlist", plan["playlists"], "id", "cover_art")):
        for item in items:
            subject, art_id = item[subject_key], item[art_key]
            if role != "playlist":
                subject = layout.card_path(subject)
            pool_strings.add(subject)
            if art_id is None:
                records += 1
                tsv_bytes += len(f"{role}\t{subject}\t-\t-\t0\n".encode("utf-8"))
            else:
                art = plan["artworks"][art_id]
                for side in ROLE_SIZES[role]:
                    asset, digest = layout.card_path(art["roles"][str(side)]), art["derivatives"][str(side)]["sha256"]
                    records += 1
                    pool_strings.add(asset)
                    tsv_bytes += len(f"{role}\t{subject}\t{asset}\t{digest}\t{side}\n".encode("utf-8"))
    native_bytes = 128 + records * 112 + sum(len(value.encode("utf-8")) for value in pool_strings)
    if plan.get("native_only_art"):
        tsv_bytes, native_bytes = 0, 0
    playlist_bytes = sum(3 + sum(len(("A:\\" + path.replace("/", "\\")).encode("utf-8")) + 2
                                 for path in playlist["card_paths"]) for playlist in plan["playlists"])
    preset_bytes = sum(len(preset["text"].encode("utf-8")) for preset in plan["presets"])
    restore_bytes = Path(__file__).with_name("restore_card.py").stat().st_size
    tracked_files = len(plan["files"]) + len(plan["playlist_covers"]) + len(derived_outputs) + len(plan["playlists"]) + len(plan["presets"]) + len(plan["information"]) + 12
    # Planned metadata already contains the original/path/track/artwork information.
    # Add 1 KiB per tracked file and 64 KiB fixed headroom for generated file records,
    # capabilities, newly added estimate fields and compact-manifest bookkeeping.
    manifest_allowance = len(_json(plan)) + tracked_files * 1024 + 65536
    # Budget allocation slack and directory entries as well as content bytes.
    # 64 KiB is the conservative floor; a larger reported allocation unit wins.
    # FAT/exFAT full-tree atomicity is not implied by a space preflight.
    allocation_unit = _allocation_unit(plan)
    directory_count = len(plan["directories"]) + len(plan.get("original_directories", [])) + tracked_files + 16
    allocation_headroom = (tracked_files + 3 + directory_count) * allocation_unit + tracked_files * 1024
    components = {"allocation_headroom_bytes": allocation_headroom,
                  "information_bytes": sum(spec["size"] for spec in plan["information"]),
                  "native_info_index_bytes": plan["native_info_index"]["size"],
                  "native_info_ready_bytes": plan["native_info_index"]["ready_size"],
                  "native_asset_index_bytes": plan["native_asset_index"]["size"],
                  "native_lyrics_index_bytes": plan["native_lyrics_index"]["size"],
                  "native_lyrics_ready_bytes": plan["native_lyrics_index"]["ready_size"],
                  "auxiliary_bytes": len(_json(plan["lyrics"])) + len(_json(plan["favorites"])) + len(_json(plan["preset_metadata"])) + 4096, "tsv_bytes": tsv_bytes, "native_index_bytes": native_bytes, "ready_bytes": 128,
                  "generated_playlist_bytes": playlist_bytes, "generated_preset_bytes": preset_bytes,
                  "restore_tool_bytes": restore_bytes, "manifest_allowance_bytes": manifest_allowance,
                  "manifest_checksum_bytes": 65}
    summary = plan["summary"]
    summary["prepared_art_bytes"] = sum(info["size"] for info in derived_outputs.values())
    summary["estimated_bytes"] = summary["original_bytes"] + summary["prepared_art_bytes"] + sum(components.values())
    summary["total_bytes"] = summary["estimated_bytes"]
    summary["estimate_components"] = {**components, "tracked_files": tracked_files,
                                      "manifest_allowance_formula": "compact-plan-bytes + 1024*tracked-files + 65536",
                                      "allocation_unit_budget": allocation_unit,
                                      "filesystem_cluster_overhead_included": True}


def _allocation_unit(plan):
    parent = Path(plan["activation"]["active_root"]).parent if plan.get("activation") else Path(plan["snapshot"]["active_root"]) if plan.get("snapshot") else Path(plan["destination"]).parent
    unit = 65536
    if os.name == "nt":
        # GetDiskFreeSpaceW only observes volume geometry; no handle/device writes.
        import ctypes
        from ctypes import wintypes
        sectors, bytes_per_sector, free, total = (wintypes.DWORD() for _ in range(4))
        volume = parent.anchor
        if ctypes.windll.kernel32.GetDiskFreeSpaceW(str(volume), ctypes.byref(sectors), ctypes.byref(bytes_per_sector), ctypes.byref(free), ctypes.byref(total)):
            unit = max(unit, sectors.value * bytes_per_sector.value)
    elif hasattr(os, "statvfs"):
        unit = max(unit, os.statvfs(parent).f_frsize)
    return unit


def inspect_destination(destination):
    """Explicit read-only ownership/conflict report; unknown bytes stay unowned."""
    root = no_links(destination)
    result = {"root": str(root), "exists": os.path.lexists(root), "recognized": False,
              "unknown_files": [], "unknown_directories": [], "drifted_files": [], "update_mode": "clean_update_or_snapshot", "clean": False,
              "fingerprint": None, "pending_transactions": pending_updates(root.parent)}
    if not result["exists"]:
        return result
    try:
        detected = detect_layout(root)
        manifest = load_manifest(root)
        result.update(recognized=True, version=manifest["version"], build_id=manifest["build_id"], layout=detected)
        known = {path_key(e["path"]) for e in manifest["files"]}
        known.update(path_key(detected[k]) for k in ("manifest", "checksum"))
        known_dirs = {"music", "eq", "struct", "struct/inbox", "struct/playlists", "struct/covers/native", "struct/device"} if manifest["version"] == 2 else {".xm5", "xm5_inbox", "_explaylist_data", ".xm5/art/native"}
        for relative in tuple(known_dirs):
            parts = relative.split("/")
            known_dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        for relative in [e["path"] for e in manifest["files"]] + [detected["manifest"], detected["checksum"]]:
            parts = relative.split("/")
            known_dirs.update(path_key("/".join(parts[:i])) for i in range(1, len(parts)))
        known_dirs.update(path_key(d["path"]) for d in manifest.get("directories", []) + manifest.get("original_directories", []))
        for folder, _, files in _walk(root):
            relative_folder = folder.relative_to(root).as_posix()
            if relative_folder != "." and path_key(relative_folder) not in known_dirs:
                result["unknown_directories"].append(relative_folder)
            for name in files:
                relative = (folder / name).relative_to(root).as_posix()
                if path_key(relative) not in known:
                    result["unknown_files"].append(relative)
        verification = verify_export(root)
        result["verification"] = verification
        result["drifted_files"] = verification.get("problems", []) + verification.get("modified_return_files", [])
        result["fingerprint"] = _tree_fingerprint(root)
        result["clean"] = manifest["version"] == 2 and verification["ok"] and not result["unknown_files"] and not result["unknown_directories"] and not result["drifted_files"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["error"] = {"code": getattr(exc, "code", "unowned_destination"), "message": str(exc)}
    return result


inspect_existing = inspect_destination


def _tree_fingerprint(root):
    records = []
    for folder, _, files in _walk(root):
        records.append(["directory", folder.relative_to(root).as_posix()])
        for name in files:
            path = folder / name
            records.append(["file", path.relative_to(root).as_posix(), *file_digest(path)])
    return _sha(_json(records))


def _read_transaction(control):
    control = no_links(control)
    match = re.fullmatch(r"\.xm5-update-([0-9a-f]{32})", control.name)
    if not match:
        _fail("unowned_transaction", "Expected an owned update transaction directory")
    build_id = match.group(1)
    owner = json.loads(checked_path(control, "OWNER.json").read_text(encoding="utf-8"))
    if owner != {"schema": "xm5.update-owner", "version": 1, "build_id": build_id}:
        _fail("unowned_transaction", "Update ownership marker does not match")
    journal = json.loads(checked_path(control, "transaction.json").read_text(encoding="utf-8"))
    if journal.get("schema") != "xm5.update-transaction" or journal.get("build_id") != build_id:
        _fail("unowned_transaction", "Update journal does not match ownership")
    return {"build_id": build_id, "control": str(control), "active_root": str(control.parent / "xm5"),
            "backup": str(control.parent / (".xm5-backup-" + build_id)),
            "fingerprint": journal["original_fingerprint"]}, journal


def pending_updates(parent):
    """Read only valid owned journals; never adopt similarly named user folders."""
    parent = no_links(parent)
    found = []
    if not parent.is_dir():
        return found
    for child in sorted(parent.iterdir()):
        if not re.fullmatch(r"\.xm5-update-[0-9a-f]{32}", child.name):
            continue
        try:
            _, journal = _read_transaction(child)
            if journal.get("status") not in {"complete", "rolled_back", "cancelled"}:
                found.append({"transaction": str(child), "status": journal.get("status"), "action": "recover_update"})
        except (OSError, ValueError, KeyError):
            # Unknown parent data is never modified or automatically claimed.
            continue
    return found


def _write_update_journal(activation, status, **details):
    control = no_links(activation["control"])
    payload = {"schema": "xm5.update-transaction", "version": 1, "build_id": activation["build_id"],
               "status": status, "original_fingerprint": activation["fingerprint"],
               "active_root": activation["active_root"], "backup": activation["backup"],
               "full_tree_atomic": False, **details}
    previous = control / "transaction.json"
    if previous.exists():
        old = json.loads(previous.read_text(encoding="utf-8"))
        if "candidate_fingerprint" in old:
            payload.setdefault("candidate_fingerprint", old["candidate_fingerprint"])
    temporary_name = "transaction.json.tmp-" + uuid.uuid4().hex
    with exclusive_output(control, temporary_name) as stream:
        stream.write(_json(payload) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    replace_owned(control, temporary_name, "transaction.json")


def recover_update(transaction):
    from .restore_card import recover_update as standalone_recover
    return standalone_recover(transaction)


def _rename_owned(source, destination):
    source, destination = no_links(source), no_links(destination)
    if os.path.lexists(destination):
        _fail("activation_collision", "Activation destination must be absent", path=str(destination))
    rename_owned_directory(source, destination)


def _inspect_update_with_state(root, acknowledged_playlists=(), acknowledged_device_state=None):
    from .update_receipts import inspect_update
    inspection=inspect_update(root,acknowledged_playlists)
    if acknowledged_device_state is not None:
        inspection=device_state.acknowledge_export_inspection(root,inspection,acknowledged_device_state)
    return inspection


def _activate_update(activation, candidate, cancel=None):
    active = no_links(activation["active_root"])
    inspection = _inspect_update_with_state(active,activation.get("acknowledged_playlists",()),activation.get("acknowledged_device_state"))
    if not inspection["clean"] or inspection["fingerprint"] != activation["fingerprint"]:
        _fail("destination_changed", "Existing export changed during candidate preparation")
    if _cancelled(cancel):
        _fail("cancelled", "Cancelled before activation")
    candidate_fp = _tree_fingerprint(candidate)
    backup_parent = no_links(activation["backup"])
    make_new_directory(backup_parent.parent, backup_parent.name)
    with exclusive_output(backup_parent, "OWNER.json") as stream:
        stream.write(_json({"schema": "xm5.backup-owner", "version": 1, "build_id": activation["build_id"]}) + b"\n")
    backup = backup_parent / "xm5"
    _write_update_journal(activation, "prepared", candidate_fingerprint=candidate_fp)
    drift_rolled_back = False
    try:
        if _tree_fingerprint(active) != activation["fingerprint"]:
            _fail("destination_changed", "Existing export changed before activation")
        _write_update_journal(activation, "moving_original")
        _rename_owned(active, backup)
        _write_update_journal(activation, "original_moved")
        if _tree_fingerprint(backup) != activation["fingerprint"]:
            # Preserve a last-moment user edit as the active original, not merely
            # as an unnoticed file in an archival backup.
            _rename_owned(backup, active)
            _write_update_journal(activation,"rolled_back",reason="original_changed_during_move")
            drift_rolled_back = True
            _fail("destination_changed", "Original changed during activation; original restored")
        if _cancelled(cancel):
            _fail("cancelled", "Cancelled during activation")
        _write_update_journal(activation, "moving_candidate")
        _rename_owned(candidate, active)
        _write_update_journal(activation, "candidate_moved")
        if not verify_export(active)["ok"] or _tree_fingerprint(active) != candidate_fp:
            _fail("activation_verification", "Activated export did not verify")
        _write_update_journal(activation, "complete", backup_root=str(backup))
    except Exception:
        if not drift_rolled_back:recover_update(activation["control"])
        raise
    return active, backup


def _write_snapshot_journal(snapshot, status, **details):
    # Creation is inside the new transaction namespace only. No active metadata
    # changes are made, so rollback means retaining the old active snapshot.
    data = _json({"schema": "xm5.snapshot-transaction", "version": 1, "status": status,
                  "active_root": snapshot["active_root"], "rollback": "active export untouched",
                  "full_tree_atomic": False, **details}) + b"\n"
    transaction_root = no_links(snapshot["transaction"])
    temporary_name = "transaction.json.tmp-" + uuid.uuid4().hex
    with exclusive_output(transaction_root, temporary_name) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    replace_owned(transaction_root, temporary_name, "transaction.json")


def _plan_peq_originals(plan, roots, source_ids, source_labels):
    labels = set()
    for root in roots:
        identity = source_ids.get(os.path.normcase(str(root)), str(root))
        key = os.path.normcase(str(root))
        base = source_labels.get(key, root.name if key not in source_ids else "peq")
        label = safe_relative(base + "~" + _sha(identity.encode("utf-8"))[:8])
        if "/" in label:
            _fail("invalid_source_label", "Source label must be one filename")
        if path_key(label) in labels:
            _fail("root_label_collision", "PEQ root labels collide")
        labels.add(path_key(label))
        root_id = source_ids.get(os.path.normcase(str(root)), "peq_" + _sha(identity.encode("utf-8"))[:24])
        prefix = "EQ/" + label
        plan["peq_roots"].append({"id": root_id, "source": str(root), "original_name": root.name,
                                  "label": label, "card_prefix": prefix, "restore_prefix": prefix})
        for folder, _, names in _walk(root):
            rel = folder.relative_to(root).as_posix()
            relative = prefix + ("/" + rel if rel != "." else "")
            plan["original_directories"].append({"path": relative, "restore_path": relative, "root_id": root_id})
            for name in names:
                source = no_links(folder / name)
                source_rel = source.relative_to(root).as_posix()
                path = safe_relative(prefix + "/" + source_rel)
                size, digest = file_digest(source)
                plan["files"].append({"source": str(source), "root_id": root_id, "source_relative": source_rel,
                                       "path": path, "restore_path": path, "kind": "original_peq", "size": size, "sha256": digest})


def _plan_lyrics_and_favorites(plan, lyrics, favorites):
    track_ids = {t["id"] for t in plan["tracks"]}
    entries = {path_key(f["path"]): f for f in plan["files"]}
    for record in lyrics:
        record = dict(record)
        source = no_links(record["source"]).resolve()
        size, digest = file_digest(source)
        if record.get("sha256", digest) != digest or record.get("size", size) != size:
            _fail("source_changed", "Lyrics source differs from the supplied record", source=str(source))
        track_id = record.get("track_id") or None
        if track_id is not None and str(track_id) not in track_ids:
            _fail("lyric_track_unresolved", "Lyrics track is outside selected export", track_id=track_id)
        relative = safe_relative(record.get("relative", "Struct/lyrics/originals/" + digest + source.suffix.lower()))
        if not relative.startswith("Struct/lyrics/") or relative == "Struct/lyrics/mappings.json":
            _fail("invalid_lyric_path", "Lyrics must stay beneath Struct/lyrics; mappings.json is reserved")
        original_path = safe_relative(record.get("original_path", relative))
        if not original_path.startswith(("Struct/lyrics/", "music/")):
            _fail("invalid_lyric_path", "Lyric original mapping must be a container-relative managed path")
        key = path_key(relative)
        if key in entries:
            if (entries[key]["size"], entries[key]["sha256"]) != (size, digest) or entries[key]["path"] != relative:
                _fail("path_collision", "Lyrics output collision", path=relative)
        else:
            restore = "Lyrics/" + relative.removeprefix("Struct/lyrics/")
            entry = {"source": str(source), "path": relative, "restore_path": restore, "kind": "original_lyrics",
                     "size": size, "sha256": digest}
            plan["files"].append(entry)
            entries[key] = entry
        plan["lyrics"].append({"path": relative, "original_path": original_path, "track_id": str(track_id) if track_id is not None else None,
                                "kind": str(record.get("kind", "lyrics-original")), "provenance": record.get("provenance", {}),
                                "size": size, "sha256": digest})
        plan["lyric_inputs"].append({"source": str(source), "relative": relative, "kind": str(record.get("kind", "lyrics-original")),
                                     "size": size, "sha256": digest, "track_id": str(track_id) if track_id is not None else None,
                                     "provenance": record.get("provenance", {})})
    favorite_ids = favorites.get("track_ids", []) if isinstance(favorites, dict) else favorites
    for item in favorite_ids:
        track_id = str(item.get("id", item.get("track_id"))) if isinstance(item, dict) else str(item)
        if track_id not in track_ids:
            _fail("favorite_track_unresolved", "Favorite is outside selected export", track_id=track_id)
        plan["favorites"].append(track_id)


def _lyric_compiled_metadata(compiled):
    if compiled["index_path"] != "Struct/indexes/lyrics.bin" or compiled["ready_path"] != "Struct/indexes/lyrics.ready":
        _fail("invalid_lyrics_index", "Lyrics compiler paths differ from the frozen layout")
    return {"schema": "xm5.native-lyrics-index", "version": 1, "path": compiled["index_path"],
            "sha256": _sha(compiled["index"]), "size": len(compiled["index"]),
            "ready_path": compiled["ready_path"], "ready_sha256": _sha(compiled["ready"]), "ready_size": len(compiled["ready"]),
            "records": compiled["records"], "subject_scope": "card-root-relative", "asset_scope": "card-root-relative"}


def _info_input(entry):
    """Bounded, exact planned lyric input. Original bytes are never normalized here."""
    with no_links(entry["source"]).open("rb") as stream:
        blob = stream.read(track_info.MAX_BYTES + 1)
    if len(blob) > track_info.MAX_BYTES or (len(blob), _sha(blob)) != (entry["size"], entry["sha256"]):
        _fail("lyrics_changed", "Information-file lyric input changed or exceeds 16 MiB", source=entry["source"])
    return blob


def _selected_info_lyrics(plan):
    inputs = plan["lyric_inputs"]
    maps = [e for e in inputs if e["kind"] == "lyrics-map"]
    selected = {}
    if len(maps) > 1:
        _fail("ambiguous_lyrics", "Multiple lyric association maps")
    if maps:
        document = json.loads(_info_input(maps[0]))
        if document.get("schema") != "xm5.lyrics-associations" or document.get("version") != 1:
            _fail("invalid_lyrics", "Unsupported lyric association map")
        by_path = {e["relative"]: e for e in inputs}
        for association in document["associations"]:
            if not association.get("selected"):
                continue
            tid = association["track_id"]
            if tid in selected:
                _fail("ambiguous_lyrics", "More than one selected local lyric", track_id=tid)
            original = by_path.get(association["original_path"])
            normalized = by_path.get(association["normalized_path"])
            if original is None or normalized is None or original["kind"] != "lyrics-original" or normalized["kind"] != "lyrics-rendered":
                _fail("invalid_lyrics", "Selected lyric original/rendering missing", track_id=tid)
            selected[tid] = {"original": original, "normalized": normalized, "provenance": association}
    else:
        # Small library callers may supply one explicit original and rendering
        # per track without constructing a managed association database.
        for entry in inputs:
            tid = entry.get("track_id")
            if not tid or entry["kind"] not in {"lyrics-original", "lyrics-rendered"}:
                continue
            kind = "original" if entry["kind"] == "lyrics-original" else "normalized"
            record = selected.setdefault(tid, {"provenance": entry.get("provenance", {})})
            if kind in record:
                _fail("ambiguous_lyrics", "Multiple explicit lyrics need a selected association map", track_id=tid)
            record[kind] = entry
    return selected


def _build_information(plan, spec, art_cache=None):
    art = plan["artworks"].get(spec["art_id"])
    bitmaps = None
    if art:
        if art_cache is not None and spec["art_id"] in art_cache:
            bitmaps = art_cache[spec["art_id"]]
        else:
            bitmaps = native_art_prepare.prepare(_source_art(art["source"]))
            if art_cache is not None:
                if len(art_cache) >= 4:
                    art_cache.pop(next(iter(art_cache)))
                art_cache[spec["art_id"]] = bitmaps
    lyric = spec.get("lyric", {})
    normalized = _info_input(lyric["normalized"]) if "normalized" in lyric else None
    original = _info_input(lyric["original"]) if "original" in lyric else None
    return track_info.build_info(identity=spec["id"], source_sha256=spec["source_sha256"], source_size=spec["source_size"],
                                 source_path=spec["source_path"], peak=spec["peak"], bitmaps=bitmaps,
                                 normalized_lyrics=normalized, original_lyrics=original, metadata=spec["metadata"],
                                 **plan["peak_options"])


def _plan_information(plan, scanner, cancel):
    selected = _selected_info_lyrics(plan)
    art_cache = {}
    originals = {entry["path"]: entry for entry in plan["files"]}
    specs, rows = [], []
    for track in plan["tracks"]:
        if _cancelled(cancel):
            _fail("cancelled", "Information-file planning cancelled")
        entry = originals[track["path"]]
        try:
            analysis = scanner.scan(track["source"], expected_sha256=entry["sha256"], expected_size=entry["size"], cancel=cancel)
        except peak_scan.PeakCancelled:
            _fail("cancelled", "Sample-peak scan cancelled")
        except peak_scan.SourceChangedError as exc:
            _fail("source_changed", str(exc), source=track["source"])
        if analysis.status != "ok":
            plan["warnings"].append({"code": "peak_" + analysis.status, "track_id": track["id"], "diagnostic": analysis.diagnostic})
        metadata = {"schema": "xm5.track-information", "version": 1, "track_id": track["id"],
                    "source": {"root_id": track["root_id"], "relative": track["source_relative"],
                               "sha256": entry["sha256"], "size": entry["size"]},
                    "cover": {"art_id": track["playback_art"], "origin": track["art_origin"], "recipe": plan["recipe"]},
                    "peak_analysis": {"status": analysis.status, "diagnostic": analysis.diagnostic,
                                      "recipe": analysis.recipe, "recipe_sha256": analysis.recipe_sha256},
                    "lyrics": selected.get(track["id"], {}).get("provenance")}
        spec = {"id": track_info.info_id(track["id"]), "track_id": track["id"], "role": "playback", "subject": layout.card_path(track["path"]),
                "source_path": layout.card_path(track["path"]), "source_sha256": entry["sha256"], "source_size": entry["size"],
                "peak": analysis.peak, "art_id": track["playback_art"], "lyric": selected.get(track["id"], {}), "metadata": metadata}
        track["information_id"] = spec["id"]
        specs.append(spec)
    for role, records, subject_key, art_key in (("directory", plan["directories"], "path", "preview_art"),
                                                ("playlist", plan["playlists"], "id", "cover_art")):
        for record in records:
            subject = record[subject_key] if role == "playlist" else layout.card_path(record[subject_key])
            art = plan["artworks"].get(record[art_key])
            source = _source_art(art["source"]) if art else b""
            specs.append({"id": track_info.info_id(subject, role=role), "track_id": None, "role": role, "subject": subject,
                          "source_path": "", "source_sha256": _sha(source), "source_size": len(source), "peak": None,
                          "art_id": record[art_key], "metadata": {"schema": "xm5.aux-information", "version": 1,
                          "role": role, "subject": subject, "cover": {"art_id": record[art_key], "recipe": plan["recipe"]}}})
    for spec in specs:
        data = _build_information(plan, spec, art_cache)
        recipe = {"format": "xm5-information-v1", "art": plan["recipe"], "peak_options": plan["peak_options"],
                  "peak_analysis": spec["metadata"].get("peak_analysis", {}).get("recipe"), "lyrics": "selected-local-bytes-v1"}
        spec.update(path=track_info.info_path(spec["id"]), size=len(data), sha256=_sha(data),
                    recipe_id="xm5-information-v1-" + _sha(_json(recipe)))
        rows.append({"role": spec["role"], "subject": spec["subject"], "info_id": spec["id"], "sha256": spec["sha256"]})
    index = track_info.compile_index(rows, build_id=plan["build_id"])
    asset_rows = [{"side": int(side), "sha256": derivative["sha256"], "info_id": spec["id"], "info_sha256": spec["sha256"]}
                  for spec in specs if spec["art_id"] for side, derivative in plan["artworks"][spec["art_id"]]["derivatives"].items()]
    assets = track_info.compile_asset_index(asset_rows, build_id=plan["build_id"])
    plan["native_asset_index"] = {"schema": "xm5.native-asset-index", "version": 1, "path": "Struct/indexes/assets.bin",
                                  "sha256": _sha(assets), "size": len(assets), "record_count": (len(assets) - 128) // 96,
                                  "record_bytes": 96, "owner_policy": "minimum-uuid-per-payload-side"}
    descriptor = {"schema": "xm5.native-info-index", "version": 1, "path": "Struct/indexes/info.bin",
                  "sha256": _sha(index), "size": len(index), "record_count": len(rows), "record_bytes": 96,
                  "ready_path": "Struct/indexes/info.ready", "information_count": len(specs),
                  "compatibility_flat_assets": not plan.get("native_only_art", False), "subject_scope": "card-root-relative", "playlist_subject_scope": "stable-playlist-id"}
    descriptor.update(assets_path=plan["native_asset_index"]["path"], assets_sha256=_sha(assets), assets_size=len(assets),
                      assets_record_count=plan["native_asset_index"]["record_count"])
    ready = _json({"schema": "xm5.info-readiness", "version": 1, "build_id": plan["build_id"],
                   "index_sha256": descriptor["sha256"], "index_size": len(index), "record_count": len(rows),
                   "compatibility_flat_assets": descriptor["compatibility_flat_assets"],
                   "assets": plan["native_asset_index"],
                   "information": [{k: spec[k] for k in ("id", "path", "size", "sha256")} for spec in specs]}) + b"\n"
    descriptor.update(ready_sha256=_sha(ready), ready_size=len(ready))
    plan["information"] = specs
    plan["native_info_index"] = descriptor
    return index, ready


def _information_index_outputs(plan):
    rows = [{"role": spec["role"], "subject": spec["subject"], "info_id": spec["id"], "sha256": spec["sha256"]}
            for spec in plan["information"]]
    index = track_info.compile_index(rows, build_id=plan["build_id"])
    asset_rows = [{"side": int(side), "sha256": derivative["sha256"], "info_id": spec["id"], "info_sha256": spec["sha256"]}
                  for spec in plan["information"] if spec["art_id"] for side, derivative in plan["artworks"][spec["art_id"]]["derivatives"].items()]
    assets = track_info.compile_asset_index(asset_rows, build_id=plan["build_id"])
    if (len(assets), _sha(assets)) != (plan["native_asset_index"]["size"], plan["native_asset_index"]["sha256"]):
        _fail("information_changed", "Native asset content index differs from the frozen plan")
    ready = _json({"schema": "xm5.info-readiness", "version": 1, "build_id": plan["build_id"],
                   "index_sha256": _sha(index), "index_size": len(index), "record_count": len(rows),
                   "compatibility_flat_assets": plan["native_info_index"]["compatibility_flat_assets"],
                   "assets": plan["native_asset_index"],
                   "information": [{k: spec[k] for k in ("id", "path", "size", "sha256")} for spec in plan["information"]]}) + b"\n"
    expected = plan["native_info_index"]
    if (len(index), _sha(index), len(ready), _sha(ready)) != (expected["size"], expected["sha256"], expected["ready_size"], expected["ready_sha256"]):
        _fail("information_changed", "Information lookup bytes differ from the frozen plan")
    return index, ready, assets


def plan_export(roots, destination, *, track_ids=None, playlists=None, playlist_covers=None, presets=None,
                peq_roots=(), favorites=(), lyrics=(), existing="refuse", source_ids=None, peq_source_ids=None,
                preset_metadata=None, source_warnings=(), source_labels=None, peq_source_labels=None, acknowledged_playlists=(),
                midpoint_dbfs=0.0, correction_limit_db=4.0, peak_correction_enabled=True,
                peak_cache_dir=None, ffmpeg_path=None, peak_timeout_seconds=3600, peak_scanner=None, cancel=None,
                native_only_art=True, card_id=None, acknowledged_device_state=None):
    """Plan a self-contained export; destination is the absolute chosen_parent/xm5.

    existing='snapshot' creates a new independently restorable snapshot beneath an
    intact recognized v2 export. It does not replace the active export or user bytes.
    """
    playlists = list(playlists or [])
    if len(playlists) > 100:
        _fail("native_playlist_capacity", "The stock XM5 playlist database supports at most100 playlist headers; select100 or fewer playlists for export")
    track_info.calculate_gain(None, midpoint_dbfs, correction_limit_db, peak_correction_enabled)
    scanner = peak_scanner or peak_scan.PeakScanner(ffmpeg_path=ffmpeg_path, cache_dir=peak_cache_dir, timeout_seconds=peak_timeout_seconds)
    roots = [no_links(p).resolve() for p in roots]
    if not roots or any(not p.is_dir() for p in roots):
        _fail("invalid_roots", "Choose one or more source directories")
    for i, root in enumerate(roots):
        for other in roots[:i]:
            if root == other or root.is_relative_to(other) or other.is_relative_to(root):
                _fail("root_overlap", "Selected source roots may not overlap", roots=[str(other), str(root)])
    destination = Path(destination)
    if not destination.is_absolute() or destination.name != layout.CONTAINER:
        _fail("container_name", "Destination must be an absolute path ending in exact xm5")
    if existing not in {"refuse", "snapshot", "update"}:
        _fail("invalid_existing_policy", "Existing destination policy must be refuse, snapshot or update")
    snapshot = None
    activation = None
    inherited_card_id = None
    build_id = uuid.uuid4().hex
    pending = pending_updates(destination.parent)
    if pending:
        _fail("update_recovery_required", "An interrupted owned update needs explicit recovery before export", transactions=pending)
    if os.path.lexists(destination):
        inspection = _inspect_update_with_state(destination,acknowledged_playlists,acknowledged_device_state)
        if existing not in {"snapshot", "update"} or not inspection["recognized"]:
            _fail("destination_exists", "Existing destination requires explicit recognized snapshot export", inspection=inspection)
        inherited_card_id = load_manifest(destination).get("device_state", {}).get("card_id")
        if inspection.get("version") != 2:
            _fail("legacy_update_refused", "Legacy cards remain v1; choose a fresh xm5 destination")
        if existing == "update":
            if not inspection["clean"]:
                _fail("update_conflict", "Active update requires an unchanged owned export without unknown files", inspection=inspection)
            control = no_links(destination.parent / (".xm5-update-" + build_id))
            backup = no_links(destination.parent / (".xm5-backup-" + build_id))
            if os.path.lexists(control) or os.path.lexists(backup):
                _fail("update_collision", "Update namespaces must be absent")
            activation = {"build_id": build_id, "active_root": str(destination), "control": str(control),
                          "backup": str(backup), "fingerprint": inspection["fingerprint"], "acknowledged_playlists": list(acknowledged_playlists),"acknowledged_device_state":acknowledged_device_state}
            dest = control / "xm5"
        else:
        # A snapshot adds a fresh namespace without rewriting tracked or unknown files.
        # The old active root is also the rollback target on every failure.
            transaction = checked_path(destination, "Struct/snapshots/" + build_id)
            if os.path.lexists(transaction):
                _fail("snapshot_collision", "Snapshot namespace already exists")
            snapshot = {"active_root": str(destination), "transaction": str(transaction),
                        "baseline_manifest_sha256": file_digest(destination / MANIFEST)[1]}
            dest = transaction / "xm5"
    else:
        dest = new_destination(destination, roots)
    peq_roots = [no_links(p).resolve() for p in peq_roots]
    if any(not p.is_dir() for p in peq_roots):
        _fail("invalid_peq_roots", "PEQ source roots must be directories")
    all_sources = roots + peq_roots
    for index, source in enumerate(all_sources):
        for other in all_sources[:index]:
            if source == other or source.is_relative_to(other) or other.is_relative_to(source):
                _fail("root_overlap", "Selected music/PEQ source roots may not overlap")
        if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
            _fail("root_overlap", "Sources and export container may not overlap")
    track_ids = {os.path.normcase(str(no_links(p).resolve())): str(tid) for p, tid in (track_ids or {}).items()}
    source_ids = {os.path.normcase(str(no_links(p).resolve())): str(value) for p, value in (source_ids or {}).items()}
    peq_source_ids = {os.path.normcase(str(no_links(p).resolve())): str(value) for p, value in (peq_source_ids or {}).items()}
    source_labels = {os.path.normcase(str(no_links(p).resolve())): str(value) for p, value in (source_labels or {}).items()}
    peq_source_labels = {os.path.normcase(str(no_links(p).resolve())): str(value) for p, value in (peq_source_labels or {}).items()}
    provided_ids = [source_ids[os.path.normcase(str(p))] for p in roots if os.path.normcase(str(p)) in source_ids]
    provided_ids += [peq_source_ids[os.path.normcase(str(p))] for p in peq_roots if os.path.normcase(str(p)) in peq_source_ids]
    if len(set(provided_ids)) != len(provided_ids):
        _fail("source_id_collision", "Selected roots must have distinct source IDs")
    labels = {}
    for root in roots:
        safe_relative(root.name)
        key = os.path.normcase(str(root))
        base = source_labels.get(key, root.name if key not in source_ids else "source")
        if "/" in safe_relative(base):
            _fail("invalid_source_label", "Source label must be one filename")
        count = sum(path_key(p.name) == path_key(base) for p in roots)
        identity = source_ids.get(os.path.normcase(str(root)), str(root))
        labels[str(root)] = base + ("~" + _sha(identity.encode("utf-8"))[:8] if count > 1 or base.casefold() in RESERVED_ROOTS or os.path.normcase(str(root)) in source_ids else "")
    if len({path_key(x) for x in labels.values()}) != len(roots):
        _fail("root_label_collision", "Could not assign distinct root labels")
    plan = {"schema": SCHEMA, "version": VERSION, "build_id": build_id,
            "destination": str(dest), "recipe_id": RECIPE_ID, "recipe": RECIPE,
            "art_index": _index_capabilities(), "native_only_art": bool(native_only_art),
            "snapshot": snapshot, "activation": activation, "self_contained": True, "peq_roots": [], "original_directories": [],
            "lyrics": [], "lyric_inputs": [], "favorites": [], "preset_metadata": {}, "roots": [], "files": [], "directories": [], "tracks": [], "artworks": {},
            "playlists": [], "playlist_covers": [], "presets": [], "warnings": [dict(w) for w in source_warnings]}
    occupied, track_map = set(), {}
    for root in roots:
        label = labels[str(root)]
        root_id = source_ids.get(os.path.normcase(str(root)), "r_" + _sha(str(root).encode("utf-8"))[:24])
        plan["roots"].append({"id": root_id, "source": str(root), "original_name": root.name,
                              "label": label, "card_prefix": layout.MUSIC + "/" + label, "restore_prefix": label})
        for folder, dirs, names in _walk(root):
            relative = folder.relative_to(root)
            restore_dir = label + ("/" + relative.as_posix() if relative.parts else "")
            card_dir = layout.MUSIC + "/" + restore_dir
            key = path_key(card_dir)
            if key in occupied:
                _fail("path_collision", "Portable path collision", path=card_dir)
            occupied.add(key)
            directory = {"path": card_dir, "restore_path": restore_dir, "root_id": root_id, "preview_art": None}
            plan["directories"].append(directory)
            image_paths, folder_tracks = [], []
            for name in names:
                source = no_links(folder / name)
                if not source.is_file():
                    _fail("not_regular", "Only regular files can be copied", source=str(source))
                rel = source.relative_to(root).as_posix()
                restore_path = safe_relative(label + "/" + rel)
                card_path = layout.MUSIC + "/" + restore_path
                key = path_key(card_path)
                if key in occupied:
                    _fail("path_collision", "Portable path collision", path=card_path)
                occupied.add(key)
                size, digest = file_digest(source)
                kind = _kind(source)
                plan["files"].append({"source": str(source), "root_id": root_id, "source_relative": rel,
                                       "path": card_path, "restore_path": restore_path,
                                       "kind": "original_" + kind, "size": size, "sha256": digest})
                if kind == "image":
                    image_paths.append(source)
                if kind == "audio":
                    tid = track_ids.get(os.path.normcase(str(source.resolve())), "t_" + _sha(_json([root_id, rel, digest]))[:32])
                    if not tid or any(t["id"] == tid for t in plan["tracks"]):
                        _fail("track_id_collision", "Track IDs must identify exactly one source", track_id=tid)
                    track = {"id": tid, "path": card_path, "root_id": root_id, "source_relative": rel,
                             "source": str(source), "sha256": digest, "playback_art": None,
                             "art_origin": "no_art"}
                    folder_tracks.append((track, _embedded(source, plan["warnings"])))
                    plan["tracks"].append(track)
                    track_map[os.path.normcase(str(source.resolve()))] = track
            folder_ref = _folder_art(folder, image_paths, plan["warnings"])
            # Directory representative is its own policy; it never overwrites per-track art.
            representative = folder_ref or next((ref for _, ref in folder_tracks if ref), None)
            directory["preview_art"] = _add_art(plan, representative)
            for track, embedded in folder_tracks:
                selected = embedded or folder_ref
                track["playback_art"] = _add_art(plan, selected)
                track["art_origin"] = "embedded" if embedded else "folder" if folder_ref else "no_art"
    seen_playlists = set()
    for playlist in playlists or []:
        pid, name = str(playlist["id"]), str(playlist["name"])
        safe_relative(name)
        if "/" in name or pid in seen_playlists or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", pid):
            _fail("invalid_playlist", "Playlist names must be filenames and IDs unique", playlist_id=pid)
        seen_playlists.add(pid)
        selected = []
        for source in playlist.get("tracks", []):
            key = os.path.normcase(str(no_links(source).resolve()))
            if key not in track_map:
                _fail("playlist_track_unresolved", "Playlist track is outside the selected export", source=str(source), playlist_id=pid)
            reference='A:\\'+layout.card_path(track_map[key]['path']).replace('/','\\')
            if len(reference.encode('utf-8'))>1000 or '#ext' in reference.casefold():
                _fail('unsupported_native_playlist_path','Stock playlist parser cannot safely read this path',source=str(source))
            selected.append(track_map[key])
        native_name=name.encode('utf-8')[:54].decode('utf-8',errors='ignore')
        filename = native_name + "-" + _sha(pid.encode("utf-8"))[:8] + ".m3u8"
        if len(Path(filename).stem.encode("utf-8"))>63:
            _fail("native_playlist_name", "Native exported playlist stem exceeds63 UTF-8 bytes")
        plan["playlists"].append({"id": pid, "name": name, "path": layout.PLAYLISTS + "/" + filename,
                                   "tracks": [t["id"] for t in selected], "card_paths": [layout.card_path(t["path"]) for t in selected],
                                   "base_snapshot": {"tracks": [t["id"] for t in selected], "revision": playlist.get("revision")},
                                   "cover_original": None, "cover_art": None})
    for pid, image in (playlist_covers or {}).items():
        matching = next((p for p in plan["playlists"] if p["id"] == str(pid)), None)
        if matching is None:
            _fail("playlist_cover_unresolved", "Cover has no selected playlist", playlist_id=str(pid))
        source = no_links(image).resolve()
        if not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS:
            _fail("invalid_playlist_cover", "Expected an original image file", source=str(source))
        size, digest = file_digest(source)
        group = _sha(str(pid).encode("utf-8"))[:16]
        playlist=next((p for p in (playlists or []) if str(p['id'])==str(pid)),{})
        cover=playlist.get('cover') or {}
        original_name=cover.get('original_name',source.name) if cover.get('path')==str(source) else source.name
        if '/' in original_name or '\\' in original_name:
            _fail('invalid_playlist_cover','Original cover name must be one filename')
        original_name=safe_relative(original_name)
        path = safe_relative(f"{META}/originals/playlist-covers/{group}/{original_name}")
        record = {"source": str(source), "path": path, "restore_path": f"Playlist covers/{group}/{original_name}",
                  "kind": "original_playlist_cover", "size": size, "sha256": digest}
        plan["playlist_covers"].append(record)
        matching["cover_original"] = path
        matching["cover_art"] = _add_art(plan, {"kind": "file", "source": str(source), "sha256": digest})
    for name, text in (presets or {}).items():
        name, text = str(name), str(text)
        safe_relative(name)
        if "/" in name or not name.isascii() or name.startswith("."):
            _fail("invalid_preset_name", "Preset names must be safe ASCII filenames", name=name)
        filename = name if name.lower().endswith(".txt") else name + ".txt"
        if len(filename.encode("ascii")) > 63:
            _fail("invalid_preset_name", "Preset filename exceeds 63 bytes", name=name)
        parsed, notes = peq.parse_apo_text(text, name)
        errors = peq.validate(parsed)
        if notes or errors:
            _fail("unsupported_preset", "Preset contains unsupported/ignored content", notes=notes, errors=errors)
        plan["presets"].append({"name": name, "path": layout.PEQ_DERIVED + "/" + filename, "text": text})
    for key, info in (preset_metadata or {}).items():
        matching = next((p for p in plan["presets"] if p["name"] == str(key)), None)
        if matching is None or not isinstance(info, dict):
            _fail("invalid_preset_metadata", "Preset metadata must identify a selected normalized preset", name=str(key))
        record = {k: info[k] for k in ("label", "source_id", "relative_path", "source_sha256", "format") if k in info}
        if "relative_path" in record:
            safe_relative(record["relative_path"])
        if "source_sha256" in record and not re.fullmatch(r"[0-9a-fA-F]{64}", str(record["source_sha256"])):
            _fail("invalid_preset_metadata", "Preset source checksum must be SHA-256", name=str(key))
        record.update(path=matching["path"], sha256=_sha(matching["text"].encode("utf-8")))
        plan["preset_metadata"][str(key)] = record
    _plan_peq_originals(plan, peq_roots, peq_source_ids, peq_source_labels)
    if len({r["id"] for r in plan["roots"] + plan["peq_roots"]}) != len(plan["roots"]) + len(plan["peq_roots"]):
        _fail("source_id_collision", "Selected roots must have distinct source IDs")
    _plan_lyrics_and_favorites(plan, lyrics, favorites)
    if card_id is not None and inherited_card_id is not None and card_id != inherited_card_id:
        _fail("card_identity_conflict", "Existing card identity cannot be silently replaced")
    try:
        selected_card_id = uuid.UUID(card_id or inherited_card_id).hex if card_id or inherited_card_id else None
    except (ValueError, TypeError):
        _fail("card_identity_invalid", "Card identity must be a UUID")
    plan["device_state"] = device_state.manifest_state(plan, card_id=selected_card_id)
    binding_data = device_state.compile_playlist_bindings(plan["device_state"])
    plan["playlist_binding_index"] = {"path": "Struct/indexes/playlists.bin", "size": len(binding_data), "sha256": _sha(binding_data)}
    plan["peak_options"] = {"midpoint_dbfs": midpoint_dbfs, "correction_limit_db": correction_limit_db,
                            "peak_correction_enabled": peak_correction_enabled}
    _plan_information(plan, scanner, cancel)
    plan["native_lyrics_index"] = _lyric_compiled_metadata(lyrics_index.compile_index(plan["tracks"], plan["lyric_inputs"], plan["build_id"]))
    plan["summary"] = {"roots": len(roots), "original_files": len(plan["files"]), "tracks": len(plan["tracks"]),
                       "distinct_artworks": len(plan["artworks"]), "playlists": len(plan["playlists"]),
                       "original_bytes": sum(f["size"] for f in plan["files"] + plan["playlist_covers"])}
    # Detect output/restoration aliases before creating a destination.
    outputs = [f["path"] for f in plan["files"] + plan["playlist_covers"]]
    outputs += [p["path"] for p in plan["playlists"] + plan["presets"]]
    derived_outputs = _derived_outputs(plan)
    outputs += list(derived_outputs)
    plan["summary"]["distinct_native_assets"] = len(derived_outputs)
    outputs += [MANIFEST, CHECKSUM, INCOMPLETE, layout.RESTORE_TOOL, "Struct/lyrics/mappings.json", "Struct/favorites.json", "Struct/favorites.m3u8", "Struct/peq/index.json"]
    if not plan.get("native_only_art"):
        outputs += [layout.ART_INDEX, layout.ART_READY, layout.ART_TSV]
    outputs += [plan["native_lyrics_index"]["path"], plan["native_lyrics_index"]["ready_path"]]
    outputs += [spec["path"] for spec in plan["information"]]
    outputs += [plan["native_info_index"]["path"], plan["native_info_index"]["ready_path"]]
    outputs += [plan["native_asset_index"]["path"], plan["playlist_binding_index"]["path"]]
    if len({path_key(p) for p in outputs}) != len(outputs):
        _fail("path_collision", "Export paths collide")
    output_keys = {path_key(p) for p in outputs}
    directory_spellings = {}
    for relative in outputs:
        parts = relative.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            key = path_key(parent)
            if key in output_keys:
                _fail("path_collision", "Export file/directory paths collide", path=relative)
            if directory_spellings.setdefault(key, parent) != parent:
                _fail("path_collision", "Portable parent directory spelling collides", path=relative)
    restores = [f["restore_path"] for f in plan["files"] + plan["playlist_covers"]]
    if len({path_key(p) for p in restores}) != len(restores):
        _fail("restore_collision", "Restoration paths collide")
    for subject in [layout.card_path(d["path"]) for d in plan["directories"]] + [layout.card_path(t["path"]) for t in plan["tracks"]] + [p["id"] for p in plan["playlists"]]:
        if any(c in subject for c in "\t\r\n") or len(subject.encode("utf-8"))>1023:
            _fail("unsupported_index_subject", "Artwork index subject is unsupported/too long", subject=subject)
    _estimate_export_bytes(plan, derived_outputs)
    plan["plan_sha256"] = _plan_hash(plan)
    return plan


def _cancelled(cancel):
    return bool(cancel() if callable(cancel) else cancel.is_set() if hasattr(cancel, "is_set") else cancel)


def _verify_source_listing(plan):
    """Detect additions, removals, renames and empty-directory drift too."""
    for record in plan["roots"] + plan.get("peq_roots", []):
        root = no_links(record["source"])
        expected_files = {f["source_relative"] for f in plan["files"] if f.get("root_id") == record["id"]}
        prefix = record["card_prefix"]
        expected_dirs = {d["path"][len(prefix):].lstrip("/") for d in plan["directories"] + plan.get("original_directories", [])
                         if d.get("root_id") == record["id"]}
        actual_files, actual_dirs = set(), set()
        for folder, _, files in _walk(root):
            relative = folder.relative_to(root).as_posix()
            actual_dirs.add("" if relative == "." else relative)
            actual_files.update((folder / name).relative_to(root).as_posix() for name in files)
        if actual_dirs != expected_dirs or actual_files != expected_files:
            _fail("source_changed", "Source directory contents changed after planning", source=str(root))


def execute_export(plan, progress=None, cancel=None):
    if (plan.get("schema") != SCHEMA or plan.get("version") != VERSION
            or plan.get("recipe_id") != RECIPE_ID or plan.get("recipe") != RECIPE
            or plan.get("art_index") != _index_capabilities()
            or plan.get("plan_sha256") != _plan_hash(plan)):
        _fail("invalid_plan", "Export plan is unsupported or changed")
    for art in plan["artworks"].values():
        if set(art["roles"]) != {str(side) for side in ART_SIZES} or set(art.get("derivatives", {})) != set(art["roles"]):
            _fail("invalid_plan", "Planned artwork profile is incomplete")
        for side, asset in art["roles"].items():
            expected = art["derivatives"][side]
            if (expected.get("size") != 64 + int(side) * int(side) * 4
                    or asset != native_art_index.asset_path_for_digest(expected["sha256"])):
                _fail("invalid_plan", "Planned artwork path/checksum/size mismatch", asset=asset)
    roots = [Path(r["source"]) for r in plan["roots"] + plan.get("peq_roots", [])]
    snapshot = plan.get("snapshot")
    activation = plan.get("activation")
    if activation:
        inspection = _inspect_update_with_state(activation["active_root"],activation.get("acknowledged_playlists",()),activation.get("acknowledged_device_state"))
        if not inspection["clean"] or inspection["fingerprint"] != activation["fingerprint"]:
            _fail("destination_changed", "Existing export changed after planning")
        if os.path.lexists(activation["control"]) or os.path.lexists(activation["backup"]):
            _fail("update_collision", "Update namespaces must be absent")
        dest = no_links(plan["destination"])
    elif snapshot:
        active = no_links(snapshot["active_root"])
        inspection = inspect_destination(active)
        if not inspection["recognized"] or file_digest(active / MANIFEST)[1] != snapshot["baseline_manifest_sha256"]:
            _fail("destination_changed", "Active export metadata changed after planning")
        transaction = no_links(snapshot["transaction"])
        if os.path.lexists(transaction):
            _fail("snapshot_collision", "Snapshot namespace already exists")
        dest = no_links(plan["destination"])
    else:
        dest = new_destination(plan["destination"], roots)
    _verify_source_listing(plan)
    originals = plan["files"] + plan["playlist_covers"]
    for entry in originals:
        safe_relative(entry["path"])
        safe_relative(entry["restore_path"])
        if file_digest(entry["source"]) != (entry["size"], entry["sha256"]):
            _fail("source_changed", "Source changed after planning", source=entry["source"])
    if _cancelled(cancel):
        return {"status": "cancelled", "root": str(dest), "error": {"code": "cancelled", "message": "Cancelled before writes"}}
    space_root = no_links(activation["active_root"]).parent if activation else no_links(snapshot["active_root"]) if snapshot else dest.parent
    free = shutil.disk_usage(space_root).free
    if free < plan["summary"]["estimated_bytes"]:
        _fail("insufficient_space", "Not enough free space for conservative export estimate", required=plan["summary"]["estimated_bytes"], available=free)
    if activation:
        control = no_links(activation["control"])
        make_new_directory(control.parent, control.name)
        with exclusive_output(control, "OWNER.json") as stream:
            stream.write(_json({"schema": "xm5.update-owner", "version": 1, "build_id": activation["build_id"]}) + b"\n")
        _write_update_journal(activation, "preparing")
    elif snapshot:
        make_new_directory(snapshot["active_root"], transaction.relative_to(snapshot["active_root"]).as_posix())
        _write_snapshot_journal(snapshot, "preparing")
    make_new_directory(dest.parent, dest.name)
    ensure_directory(dest, META)
    ensure_directory(dest, layout.MUSIC)
    ensure_directory(dest, layout.PEQ_ORIGINALS)
    with exclusive_output(dest, INCOMPLETE) as marker:
        marker.write(b"Export has not committed.\n")
    ensure_directory(dest, layout.INBOX)
    ensure_directory(dest, layout.PLAYLISTS)
    manifest = {k: plan[k] for k in ("schema", "version", "build_id", "recipe_id", "recipe", "roots", "peq_roots", "directories", "original_directories", "lyrics", "favorites", "preset_metadata", "native_lyrics_index", "native_info_index", "native_asset_index", "peak_options", "self_contained", "warnings", "native_only_art", "device_state", "playlist_binding_index")}
    manifest.update(status="incomplete", files=[], tracks=[{k: v for k, v in t.items() if k != "source"} for t in plan["tracks"]],
                    artworks={k: {a: b for a, b in v.items() if a != "source"} for k, v in plan["artworks"].items()},
                    playlists=plan["playlists"], presets=[{k: v for k, v in p.items() if k != "text"} for p in plan["presets"]])
    manifest["information"] = [{k: spec[k] for k in ("id", "path", "track_id", "role", "subject", "size", "sha256", "source_sha256", "recipe_id")} for spec in plan["information"]]
    def save_bytes(relative, data, kind, restore_path=None):
        with exclusive_output(dest, relative) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        manifest["files"].append({"path": relative, "kind": kind, "restore_path": restore_path,
                                  "size": len(data), "sha256": _sha(data)})
    try:
        for directory in plan["directories"] + plan.get("original_directories", []):
            ensure_directory(dest, directory["path"])
        if not plan.get("native_only_art"):
            ensure_directory(dest, native_art_index.ASSET_DIRECTORY)
        total = len(originals) + len(plan["artworks"]) + len(plan["playlists"]) + len(plan["presets"]) + len(plan["information"]) + 1
        count = 0
        def step(path):
            nonlocal count
            if _cancelled(cancel):
                _fail("cancelled", "Export cancelled")
            count += 1
            if progress:
                progress({"phase": "export", "current": count, "total": total, "path": path})
        for entry in originals:
            step(entry["path"])
            target = checked_path(dest, entry["path"])
            with no_links(entry["source"]).open("rb") as source, exclusive_output(dest, entry["path"]) as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if file_digest(target) != (entry["size"], entry["sha256"]):
                _fail("source_changed", "Source changed during copy", source=entry["source"])
            manifest["files"].append({k: v for k, v in entry.items() if k != "source"})
        written_art = set()
        for art in (() if plan.get("native_only_art") else plan["artworks"].values()):
            step(art["roles"]["80"])
            derived = native_art_prepare.prepare(_source_art(art["source"]))
            for side in ART_SIZES:
                data, asset = derived[side], art["roles"][str(side)]
                actual_sha = _sha(data)
                if (art["derivatives"][str(side)] != {"sha256": actual_sha, "size": len(data)}
                        or asset != native_art_index.asset_path_for_digest(actual_sha)):
                    _fail("prepared_art_changed", "Prepared artwork differs from planned bytes", asset=asset)
                if asset in written_art:
                    if checked_path(dest, asset).read_bytes() != data:
                        _fail("asset_collision", "Existing native artwork has different bytes", asset=asset)
                else:
                    save_bytes(asset, data, "derived_art")
                    written_art.add(asset)
        for playlist in plan["playlists"]:
            step(playlist["path"])
            # This encoding matches the existing stock playlist import boundary.
            refs = ["A:\\" + path.replace("/", "\\") for path in playlist["card_paths"]]
            data = b"\xef\xbb\xbf" + ("\r\n".join(refs) + ("\r\n" if refs else "")).encode("utf-8")
            save_bytes(playlist["path"], data, "generated_playlist")
        for preset in plan["presets"]:
            step(preset["path"])
            save_bytes(preset["path"], preset["text"].encode("utf-8"), "generated_preset")
        if not plan.get("native_only_art"):
            # Bounded native lookup input: exact subjects, no JSON parser or basename aliases.
            file_hashes = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
            index, native_rows = [], []
            for role, records, subject_key, art_key in (
                    ("directory", manifest["directories"], "path", "preview_art"),
                    ("playback", manifest["tracks"], "path", "playback_art"),
                    ("playlist", manifest["playlists"], "id", "cover_art")):
                for record in records:
                    subject, art_id = record[subject_key], record[art_key]
                    if role != "playlist":
                        subject = layout.card_path(subject)
                    if any(c in subject for c in "\t\r\n"):
                        _fail("unsupported_index_subject", "Artwork index subjects cannot contain TAB/CR/LF", subject=subject)
                    if art_id is None:
                        index.append(f"{role}\t{subject}\t-\t-\t0\n")
                        native_rows.append({"role": role, "subject": subject, "asset": "-", "sha256": "-", "side": 0})
                    else:
                        for side in ROLE_SIZES[role]:
                            asset = manifest["artworks"][art_id]["roles"][str(side)]
                            digest = file_hashes[asset]
                            asset = layout.card_path(asset)
                            index.append(f"{role}\t{subject}\t{asset}\t{digest}\t{side}\n")
                            native_rows.append({"role": role, "subject": subject, "asset": asset,
                                                "sha256": digest, "side": side})
            save_bytes(layout.ART_TSV, "".join(index).encode("utf-8"), "art_index")
            manifest["art_index"] = {**_index_capabilities(), "path": layout.ART_TSV,
                                     "sha256": manifest["files"][-1]["sha256"]}
            compiled = native_art_index.compile_index(native_rows, build_id=plan["build_id"], recipe=plan["recipe"],
                                                      asset_reader=lambda asset: checked_path(dest.parent, asset).read_bytes())
            save_bytes(layout.ART_INDEX, compiled.index, "native_art_index")
            save_bytes(layout.ART_READY, compiled.ready, "native_art_ready")
            manifest["native_art_index"] = {"schema": "xm5.native-art-index", "version": 2,
                                            "asset_layout": native_art_index.ASSET_LAYOUT,
                                            "path": layout.ART_INDEX, "sha256": _sha(compiled.index),
                                            "ready_path": layout.ART_READY, "ready_sha256": _sha(compiled.ready),
                                            "record_count": compiled.record_count, "record_bytes": 112,
                                            "recipe_tag": compiled.recipe_tag, "sizes_mask": native_art_index.SIZES_MASK, "flags": 3,
                                            "max_records": native_art_index.MAX_RECORDS,
                                            "max_index_bytes": native_art_index.MAX_INDEX_BYTES,
                                            "format": "bmp-top-down-bgra32", "pixel_offset": 64,
                                            "sizes": list(ART_SIZES), "role_sizes": _index_capabilities()["role_sizes"]}
        save_bytes("Struct/lyrics/mappings.json", _json(plan["lyrics"]) + b"\n", "lyrics_mapping", "Lyrics/mappings.json")
        save_bytes("Struct/favorites.json", _json(plan["favorites"]) + b"\n", "favorites", "Favorites/favorites.json")
        favorite_paths = {track["id"]: "/mnt/mmc/" + layout.card_path(track["path"]) for track in plan["tracks"]}
        favorite_text = "\r\n".join(favorite_paths[identity] for identity in plan["favorites"])
        save_bytes("Struct/favorites.m3u8", b"\xef\xbb\xbf" + (favorite_text + ("\r\n" if favorite_text else "")).encode("utf-8"), "favorites", "Favorites/favorites.m3u8")
        binding_data = device_state.compile_playlist_bindings(plan["device_state"])
        if (len(binding_data), _sha(binding_data)) != (plan["playlist_binding_index"]["size"], plan["playlist_binding_index"]["sha256"]):
            _fail("playlist_bindings_changed", "Playlist binding index differs from frozen plan")
        save_bytes(plan["playlist_binding_index"]["path"], binding_data, "native_playlist_bindings")
        ensure_directory(dest, "Struct/device")
        save_bytes("Struct/peq/index.json", _json(plan["preset_metadata"]) + b"\n", "preset_metadata", "Presets/index.json")
        compiled_lyrics = lyrics_index.compile_index(plan["tracks"], plan["lyric_inputs"], plan["build_id"])
        if _lyric_compiled_metadata(compiled_lyrics) != plan["native_lyrics_index"]:
            _fail("lyrics_index_changed", "Compiled lyric lookup differs from planned bytes")
        save_bytes(compiled_lyrics["index_path"], compiled_lyrics["index"], "native_lyrics_index")
        save_bytes(compiled_lyrics["ready_path"], compiled_lyrics["ready"], "native_lyrics_ready")
        art_cache = {}
        for spec in plan["information"]:
            step(spec["path"])
            data = _build_information(plan, spec, art_cache)
            if (len(data), _sha(data)) != (spec["size"], spec["sha256"]):
                _fail("information_changed", "Information file differs from the frozen plan", path=spec["path"])
            save_bytes(spec["path"], data, "track_info" if spec["role"] == "playback" else "info_file")
            manifest["files"][-1].update(track_id=spec["track_id"], info_id=spec["id"], role=spec["role"], subject=spec["subject"],
                                         source_sha256=spec["source_sha256"], recipe_id=spec["recipe_id"],
                                         managed_id="info:" + spec["track_id"] if spec["track_id"] else "info:" + spec["role"] + ":" + spec["id"])
        info_index, info_ready, asset_index = _information_index_outputs(plan)
        save_bytes(plan["native_info_index"]["path"], info_index, "native_info_index")
        save_bytes(plan["native_asset_index"]["path"], asset_index, "native_asset_index")
        # Readiness is published only after all exact immutable XMI files and
        # the index have been written and flushed. INCOMPLETE still gates the
        # entire export until the durable manifest/checksum commit below.
        save_bytes(plan["native_info_index"]["ready_path"], info_ready, "native_info_ready")
        step(layout.RESTORE_TOOL)
        save_bytes(layout.RESTORE_TOOL, Path(__file__).with_name("restore_card.py").read_bytes(), "restore_tool")
        if _cancelled(cancel):
            _fail("cancelled", "Export cancelled before commit")
        # A source modified after its own copy must still invalidate the export.
        for entry in originals:
            if file_digest(entry["source"]) != (entry["size"], entry["sha256"]):
                _fail("source_changed", "Source changed before commit", source=entry["source"])
        _verify_source_listing(plan)
        manifest["status"] = "complete"
        data = _json(manifest) + b"\n"
        for relative, payload in ((MANIFEST, data), (CHECKSUM, (_sha(data) + "\n").encode("ascii"))):
            temporary_name = relative + ".tmp-" + uuid.uuid4().hex
            with exclusive_output(dest, temporary_name) as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            replace_owned(dest, temporary_name, relative)
        unlink_owned(dest, INCOMPLETE)
        verification = verify_export(dest)
        if not verification["ok"]:
            with exclusive_output(dest, INCOMPLETE) as marker:
                marker.write(b"Verification failed.\n")
            _fail("verification_failed", "Export verification failed", report=verification)
        if snapshot:
            _write_snapshot_journal(snapshot, "complete", snapshot_root=str(dest))
        backup = None
        if activation:
            dest, backup = _activate_update(activation, dest, cancel)
            verification = verify_export(dest)
        return {"status": "snapshot" if snapshot else "complete", "active": not bool(snapshot),
                "snapshot_of": snapshot["active_root"] if snapshot else None, "root": str(dest), "build_id": plan["build_id"],
                "backup_root": str(backup) if backup else None,
                "update_transaction": activation["control"] if activation else None,
                "manifest": MANIFEST, "summary": plan["summary"], "root_mapping": plan["roots"],
                "warnings": plan["warnings"], "verification": verification}
    except Exception as exc:
        recovery = None
        if activation:
            try:
                recovery = recover_update(activation["control"])
            except (OSError, ValueError, KeyError) as recovery_error:
                recovery = {"status": "recovery_required", "transaction": activation["control"], "error": str(recovery_error)}
        if snapshot:
            try:
                _write_snapshot_journal(snapshot, "rolled_back", error=str(exc), partial_root=str(dest))
            except OSError:
                # Disk full may prevent the diagnostic update. The old active
                # export is still untouched, and the partial root stays INCOMPLETE.
                pass
        return {"status": "cancelled" if getattr(exc, "code", "") == "cancelled" else "incomplete",
                "active": False, "active_root": activation["active_root"] if activation else snapshot["active_root"] if snapshot else None,
                "recovery": recovery, "root": str(dest), "build_id": plan["build_id"], "incomplete_marker": INCOMPLETE,
                "error": {"code": getattr(exc, "code", "export_failed"), "message": str(exc), "details": getattr(exc, "details", {})}}


def _m3u_lines(path):
    data = no_links(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        text = data.decode("utf-8-sig")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def reconcile_m3u(root, m3u, *, playlist_id=None, current_snapshot=None):
    """Read a stock-returned playlist; return an explicit proposal, never mutate a library."""
    manifest = load_manifest(root)
    root, m3u = no_links(root).resolve(), no_links(m3u).resolve()
    mapping = {path_key(layout.card_path(t["path"]) if manifest["version"] == 2 else t["path"]): t for t in manifest["tracks"]}
    resolved, unresolved = [], []
    for raw in _m3u_lines(m3u):
        path = raw.replace("\\", "/")
        if path[:2].upper() == "A:":
            if not path[2:].startswith("/"):
                unresolved.append({"entry": raw, "reason": "ambiguous_drive_relative"})
                continue
            path = path[2:].lstrip("/")
        elif path.startswith("/mnt/mmc/"):
            path = path[len("/mnt/mmc/"):]
        elif path.startswith("/") or ":" in path:
            unresolved.append({"entry": raw, "reason": "unsupported_drive_or_url"})
            continue
        try:
            safe_relative(path)
            track = mapping.get(path_key(path))
        except RestoreError:
            unresolved.append({"entry": raw, "reason": "unsafe_path"})
            continue
        if track is None:
            unresolved.append({"entry": raw, "reason": "unknown_card_path"})
        else:
            resolved.append({"id": track["id"], "path": track["path"], "root_id": track["root_id"],
                             "source_relative": track["source_relative"], "case_adjusted": path != track["path"]})
    if playlist_id is None and m3u.is_relative_to(root):
        rel = m3u.relative_to(root).as_posix()
        matches = [p for p in manifest["playlists"] if path_key(p["path"]) == path_key(rel)]
        playlist_id = matches[0]["id"] if len(matches) == 1 else None
    playlist = next((p for p in manifest["playlists"] if p["id"] == str(playlist_id)), None)
    base = playlist["base_snapshot"] if playlist else None
    returned = [r["id"] for r in resolved]
    current = current_snapshot if isinstance(current_snapshot, dict) else {"tracks": current_snapshot} if current_snapshot is not None else base
    conflict = None
    if base and current and current.get("tracks") != base["tracks"] and returned != current.get("tracks") and returned != base["tracks"]:
        conflict = {"reason": "both_pc_and_device_changed", "base": base, "current": current, "returned": returned}
    status = "unresolved" if unresolved else "conflict" if conflict else "new_playlist" if base is None else "unchanged" if returned == (current or base).get("tracks") or returned == base["tracks"] else "changed"
    return {"status": status, "playlist_id": playlist_id, "name": playlist["name"] if playlist else m3u.stem,
            "resolved": resolved, "unresolved": unresolved, "base_snapshot": base, "current_snapshot": current,
            "base_snapshot_sha256": _sha(_json(base)) if base else None, "proposed_tracks": returned,
            "conflict": conflict, "applied": False, "build_id": manifest["build_id"]}


def inspect_inbox(root):
    """Recognize ordinary files beneath the exact XM5_INBOX directory; never ingest implicitly."""
    manifest = load_manifest(root)
    inbox = checked_path(root, detect_layout(root)["inbox"])
    entries, errors = [], []
    try:
        if not inbox.is_dir():
            _fail("missing_inbox", "Expected the XM5_INBOX directory")
        for folder, directories, files in _walk(inbox):
            for name in files:
                path = no_links(folder / name)
                size, digest = file_digest(path)
                kind = _kind(path)
                item = {"path": str(path), "inbox_relative": path.relative_to(inbox).as_posix(),
                        "size": size, "sha256": digest, "kind": kind, "action": "review_then_import"}
                if path.suffix.lower() in {".m3u", ".m3u8"}:
                    item.update(kind="playlist", report=reconcile_m3u(root, path))
                elif path.suffix.lower() == ".txt":
                    try:
                        preset, notes = peq.parse_apo_text(path.read_text(encoding="utf-8-sig"), path.stem)
                        if notes or peq.validate(preset):
                            raise ValueError("Unsupported/ignored preset content")
                        item["kind"] = "preset"
                    except (ValueError, UnicodeError) as exc:
                        item.update(kind="unsupported", reason=str(exc))
                elif kind == "other":
                    item.update(kind="unsupported", reason="Unknown file type; no automatic action")
                entries.append(item)
    except (OSError, ValueError) as exc:
        errors.append({"code": getattr(exc, "code", "intake_failed"), "message": str(exc)})
    return {"build_id": manifest["build_id"], "inbox": str(inbox), "entries": entries,
            "errors": errors, "applied": False, "ordinary_files_unchanged": True}
