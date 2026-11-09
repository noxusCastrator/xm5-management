"""Scoped, recoverable file-unit updates of an existing XM5 v2 container.

This is not a whole-library copy or a FAT atomic transaction. An INCOMPLETE
marker guards mixed generations; exact old bytes and a durable journal support
explicit resume/rollback. Unknown files are never adopted or removed. Callers
serialize application mutations while applying; external drift fails closed.
"""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import PurePosixPath
import re
import shutil
import uuid

from . import library_health as health
from .restore_card import (RestoreError, checked_path, ensure_directory,
                           exclusive_output, file_digest, make_new_directory,
                           no_links, path_key, replace_owned, safe_relative,
                           unlink_owned)

MANIFEST, CHECKSUM, CONTROL, OWNER = health.MANIFEST, health.CHECKSUM, health.CONTROL, health.OWNER
INCOMPLETE = "Struct/INCOMPLETE"
PLAN_SCHEMA = "xm5.file-sync-plan"
JOURNAL_SCHEMA = "xm5.file-sync-journal"
MAX_GENERATED_BYTES = 64 * 1024 * 1024
MAX_PLAN_BYTES = 192 * 1024 * 1024
MANIFEST_METADATA_KEYS = frozenset({
    "tracks", "directories", "original_directories", "playlists", "artworks",
    "lyrics", "favorites", "preset_metadata", "native_lyrics_index",
    "native_art_index", "art_index", "native_info_index", "native_asset_index", "info_index",
    "information_index", "information_files", "info_files", "information", "recipe_id",
    "recipe", "peak_options", "warnings", "self_contained", "native_only_art", "device_state", "playlist_binding_index",
})


def _fail(code, message, **details):
    raise RestoreError(code, message, details)


def _encode(data):
    return base64.b64encode(data).decode("ascii")


def _decode(text):
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise RestoreError("invalid_plan", "Invalid frozen bytes") from exc


def _bytes_version(data):
    return {"size": len(data), "sha256": health.sha(data)}


def _fingerprint(plan):
    return health.sha(health.canonical_json({k: v for k, v in plan.items() if k != "plan_sha256"}))


def _token(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value):
        _fail("invalid_transaction", "Expected a lowercase transaction UUID")
    return value


def _control_path(transaction_id):
    return CONTROL + "/transactions/" + _token(transaction_id)


def _stage(path, transaction_id, index, suffix="new"):
    parent = PurePosixPath(path).parent
    name = ".xm5sync-" + transaction_id + "-" + str(index) + "." + suffix
    return safe_relative((parent / name).as_posix())


def _allowed_addition(entry, manifest):
    """New assets need a manifest-declared destination, never just a user path."""
    path, kind = safe_relative(entry["path"]), entry.get("kind", "")
    if kind == "sync_conflict_copy":
        return bool(re.fullmatch(r"Struct/sync-conflicts/[0-9a-f]{32}/[0-9]+\.[^/]+", path))
    if kind in {"generated_playlist", "playlist"}:
        return path.startswith("Struct/playlists/") and PurePosixPath(path).suffix.lower() in {".m3u", ".m3u8"}
    if kind == "favorites":
        return path == "Struct/favorites.json"
    if health.category(entry) == "generated":
        return any(path.startswith(prefix) for prefix in
                   ("Struct/info/", "Struct/indexes/", "Struct/covers/native/", "Struct/lyrics/", "Struct/peq/"))
    if kind.startswith("original_"):
        for root in manifest.get("roots", []) + manifest.get("peq_roots", []):
            if root.get("id") == entry.get("root_id"):
                prefix = root.get("card_prefix")
                return bool(prefix and path.startswith(safe_relative(prefix) + "/") and entry.get("source_relative")
                            and path == prefix + "/" + safe_relative(entry["source_relative"]))
    return False


def _input(descriptor, sources):
    if not isinstance(descriptor, dict):
        _fail("invalid_input", "Expected a declared source descriptor")
    identity, relative = descriptor.get("root_id"), descriptor.get("relative")
    safe_relative(relative)
    root = sources.get(identity)
    if not root or root["status"] != "connected":
        _fail("source_unavailable", "Required source root is not connected", root_id=identity)
    observed = health.version(checked_path(root["path"], relative))
    if observed is None:
        _fail("source_missing", "Required input is missing", root_id=identity, relative=relative)
    if "version" in descriptor and descriptor["version"] != observed:
        _fail("source_changed", "Generated input differs from its frozen read version", root_id=identity, relative=relative)
    return {"root_id": identity, "relative": relative, "version": observed}


def plan_sync(card_root, source_roots=None, *, updates=(), choices=None, repair_only=False,
              manifest_metadata=None):
    """Freeze an exact plan without writing.

    Existing original files follow their root_id/source_relative bindings. Missing
    or disconnected inputs never request deletion. `updates` supplies generated
    bytes or an explicit {source: {root_id, relative}} for one owned file unit.
    Each update has path, kind and managed_id; new originals additionally include
    root_id/source_relative and must fit the existing manifest root card_prefix.
    Generated updates may list input dependencies as {root_id, relative} records.

    Durable card edits and independently modified originals require an explicit
    choice keyed by managed_id: pc, card or keep_both. Card choices carry exact
    return bytes in the plan/result for the Engine's durable intake receipt.
    No deletion or automatic relink is implemented.
    """
    report = health.inspect_library(card_root, source_roots)
    root, manifest, sources = report["root"], report["manifest"], report["sources"]
    input_cache = {(record["source"]["root_id"], record["source"]["relative"]):
                   {"root_id": record["source"]["root_id"], "relative": record["source"]["relative"], "version": record["source"]["version"]}
                   for record in report["records"] if record["source"]["status"] == "present"}

    def frozen_input(descriptor):
        if not isinstance(descriptor, dict):
            _fail("invalid_input", "Expected a declared source descriptor")
        key = (descriptor.get("root_id"), safe_relative(descriptor.get("relative")))
        if key not in input_cache:
            input_cache[key] = _input(descriptor, sources)
        frozen = input_cache[key]
        if "version" in descriptor and descriptor["version"] != frozen["version"]:
            _fail("source_changed", "Generated input differs from the frozen read version", root_id=key[0], relative=key[1])
        return frozen
    transaction_id = uuid.uuid4().hex
    choices = dict(choices or {})
    requests, request_ids = {}, set()
    entries = {entry["path"]: entry for entry in manifest["files"]}
    tracks_by_path = {track["path"]: track for track in manifest.get("tracks", [])}
    ids = {health.managed_id(entry, manifest, tracks_by_path=tracks_by_path): entry["path"] for entry in manifest["files"]}
    portable = {path_key(path): path for path in entries}
    source_checks, actions, issues, returns, adopted = [], [], [], [], []
    after_manifest = copy.deepcopy(manifest)
    metadata_patch = copy.deepcopy(manifest_metadata or {})
    if not isinstance(metadata_patch, dict) or set(metadata_patch) - MANIFEST_METADATA_KEYS:
        _fail("invalid_metadata", "Manifest metadata patch contains an unsupported ownership field")
    if len(health.canonical_json(metadata_patch)) > MAX_GENERATED_BYTES:
        _fail("invalid_metadata", "Manifest metadata patch exceeds 64 MiB")
    after_entries = {entry["path"]: entry for entry in after_manifest["files"]}
    after_tracks_by_path = {track["path"]: track for track in after_manifest.get("tracks", [])}
    for request in updates:
        request = dict(request)
        path = safe_relative(request["path"])
        identity = request.get("managed_id")
        if not isinstance(identity, str) or not identity or identity in request_ids or path in requests:
            _fail("identity_collision", "Updates need unique nonempty managed IDs and paths")
        request_ids.add(identity)
        if path_key(path) in portable and portable[path_key(path)] != path:
            _fail("path_collision", "Update aliases an owned path", path=path)
        if path in entries:
            if identity != health.managed_id(entries[path], manifest, tracks_by_path=tracks_by_path) or request.get("kind") != entries[path].get("kind"):
                _fail("identity_mismatch", "Existing path must retain its exact identity and kind", path=path)
        elif identity in ids:
            _fail("relink_required", "Moving an identity needs a separate explicit relink", managed_id=identity)
        elif not _allowed_addition(request, manifest):
            _fail("unowned_destination", "New path is outside a declared managed asset scope", path=path)
        if path in {MANIFEST, CHECKSUM, INCOMPLETE} or path.startswith(CONTROL + "/") or path.startswith("Struct/device/"):
            _fail("reserved_path", "Control and manifest paths are not file-unit inputs")
        if path not in entries and os.path.lexists(checked_path(root, path)):
            _fail("unowned_destination", "Unknown existing bytes cannot be adopted", path=path)
        if ("data" in request) == ("source" in request):
            _fail("invalid_update", "Supply exactly one of data or declared source")
        metadata = request.get("metadata", {})
        if not isinstance(metadata, dict) or set(metadata) & {
                "path", "kind", "managed_id", "size", "sha256", "root_id", "source_relative", "restore_path", "track_id", "sync_pc_baseline"}:
            _fail("identity_mismatch", "Per-file metadata cannot change ownership or identity")
        if "data" in request:
            if not isinstance(request["data"], bytes) or len(request["data"]) > MAX_GENERATED_BYTES:
                _fail("invalid_update", "Generated output must be bytes no larger than 64 MiB")
            if health.category(request) not in {"generated", "durable"}:
                _fail("original_bytes_required", "Original assets must come from declared source files")
            request["payload"] = {"data_b64": _encode(request.pop("data"))}
            request["desired"] = _bytes_version(_decode(request["payload"]["data_b64"]))
        else:
            descriptor = request.pop("source")
            binding = entries.get(path, request)
            if health.category(request) == "original" and binding.get("root_id") and binding.get("source_relative"):
                if (descriptor.get("root_id"), descriptor.get("relative")) != (binding["root_id"], binding["source_relative"]):
                    _fail("relink_required", "A file-unit update cannot silently change its original source binding", path=path)
            frozen = frozen_input(descriptor)
            request["payload"] = {"source": frozen}
            request["desired"] = frozen["version"]
            source_checks.append(frozen)
        for descriptor in request.pop("inputs", []):
            source_checks.append(frozen_input(descriptor))
        requests[path] = request
    records = list(report["records"])
    for path, request in requests.items():
        if path not in entries:
            records.append({"path": path, "managed_id": request["managed_id"], "kind": request["kind"],
                            "category": health.category(request), "baseline": None, "pc_baseline": None, "pc": None,
                            "card": None, "source": {"status": "not_bound"}})
    for record in records:
        path, identity = record["path"], record["managed_id"]
        base, card = record["baseline"], record["card"]
        pc_base = record["pc_baseline"]
        request = requests.get(path)
        source = record["source"]
        if any(reason != "source_version" for reason in record.get("stale_reasons", [])) and request is None:
            issues.append({"code": "repair_required", "managed_id": identity, "path": path,
                           "reasons": record["stale_reasons"], "blocking": True})
            continue
        if source["status"] in {"present", "missing"}:
            source_checks.append({"root_id": source["root_id"], "relative": source["relative"],
                                  "version": source["version"]})
        desired, payload = pc_base, None
        if request:
            desired, payload = request["desired"], request["payload"]
        elif source["status"] == "present" and not repair_only:
            desired = source["version"]
            payload = {"source": {"root_id": source["root_id"], "relative": source["relative"], "version": desired}}
        elif source["status"] in {"missing", "disconnected", "unselected"}:
            issues.append({"code": "source_" + source["status"], "path": path, "blocking": False})
        state = health.compare_sides(pc_base, base, desired, card)
        selected = card if state == "unchanged" else desired
        if card != base and card != desired:
            if record["category"] == "generated":
                if request is None:
                    issues.append({"code": "repair_required", "managed_id": identity, "path": path, "blocking": True})
                    continue
            else:
                choice = choices.get(identity)
                if choice not in {"pc", "card", "keep_both"}:
                    issues.append({"code": "choice_required", "managed_id": identity, "path": path,
                                   "state": state, "baseline": base, "pc_baseline": pc_base,
                                   "pc": desired, "card": card, "blocking": True})
                    continue
                if choice in {"card", "keep_both"}:
                    if card is None:
                        issues.append({"code": "missing_card_not_deletion", "path": path, "blocking": True})
                        continue
                    receipt = {"managed_id": identity, "path": path, "choice": choice, "version": card}
                    if record["category"] == "durable":
                        if card["size"] > MAX_GENERATED_BYTES:
                            _fail("state_too_large", "Durable state receipt exceeds 64 MiB", path=path)
                        data = checked_path(root, path).read_bytes()
                        if _bytes_version(data) != card:
                            _fail("destination_changed", "Durable state changed while freezing its receipt", path=path)
                        receipt["data_b64"] = _encode(data)
                    returns.append(receipt)
                    selected = card
                    if choice == "keep_both" and desired != card:
                        if payload is None:
                            issues.append({"code": "pc_bytes_unavailable", "path": path, "blocking": True})
                            continue
                        conflict_path = "Struct/sync-conflicts/" + transaction_id + "/" + str(len(actions)) + "." + PurePosixPath(path).name
                        conflict_entry = {"path": conflict_path, "kind": "sync_conflict_copy",
                                          "managed_id": "conflict:" + transaction_id + ":" + identity,
                                          **desired, "original_managed_id": identity}
                        actions.append({"path": conflict_path, "managed_id": conflict_entry["managed_id"],
                                        "before": None, "after": desired, "payload": payload})
                        after_manifest["files"].append(conflict_entry)
        if selected is None:
            issues.append({"code": "deletion_not_authorized", "path": path, "blocking": True})
            continue
        if selected != card:
            if payload is None:
                issues.append({"code": "replacement_bytes_unavailable", "path": path, "blocking": True})
                continue
            actions.append({"path": path, "managed_id": identity, "before": card,
                            "after": selected, "payload": payload})
        if selected != base:
            if path not in after_entries:
                entry = {k: v for k, v in request.items() if k not in {"payload", "desired", "metadata"}}
                after_manifest["files"].append(entry)
                after_entries[path] = entry
            after_entries[path].update(selected)
            adopted.append({"path": path, "managed_id": identity, "version": selected})
            if path in after_tracks_by_path:
                after_tracks_by_path[path]["sha256"] = selected["sha256"]
        if request and request.get("metadata"):
            after_entries[path].update(request["metadata"])
        if selected != desired:
            after_entries[path]["sync_pc_baseline"] = desired
        else:
            after_entries[path].pop("sync_pc_baseline", None)
    after_manifest.update(metadata_patch)
    _validate_topology(manifest, after_manifest)
    planned_directories = {parent.as_posix() for action in actions for parent in PurePosixPath(action["path"]).parents}
    for field in ("directories", "original_directories"):
        for directory in after_manifest.get(field, []):
            path = directory["path"]
            if not checked_path(root, path).is_dir() and path not in planned_directories:
                _fail("missing_directory", "Metadata directory must exist or be created by a planned file unit", path=path)
    tracks_by_id = {str(track["id"]): track["path"] for track in after_manifest.get("tracks", [])}
    for entry in after_manifest["files"]:
        track_path = tracks_by_id.get(str(entry.get("track_id")))
        original = after_entries.get(track_path)
        if (health.category(entry) == "generated" and entry.get("source_sha256") and original
                and entry["source_sha256"] != original["sha256"]):
            issues.append({"code": "generated_source_mismatch", "path": entry["path"], "blocking": True})
    artifact_validation = {}
    if not any(issue["blocking"] for issue in issues):
        try:
            artifact_validation = _validate_information_artifacts(root, sources, manifest, after_manifest, actions)
        except RestoreError as exc:
            issues.append({"code": exc.code, "message": str(exc), **exc.details, "blocking": True})
    for index, action in enumerate(actions):
        action["stage"] = _stage(action["path"], transaction_id, index)
        action["backup"] = _control_path(transaction_id) + "/old/" + str(index)
    reserved = [action["stage"] for action in actions]
    reserved += [_stage(action["path"], transaction_id, index, "restore") for index, action in enumerate(actions)]
    reserved += [path + ".xm5sync-" + transaction_id for path in (MANIFEST, CHECKSUM)]
    for path in reserved:
        if os.path.lexists(checked_path(root, path)):
            _fail("unowned_staging", "A reserved temporary path already exists", path=path)
    manifest_bytes = checked_path(root, MANIFEST).read_bytes()
    checksum_bytes = checked_path(root, CHECKSUM).read_bytes()
    if (_bytes_version(manifest_bytes) != report["manifest_version"] or
            _bytes_version(checksum_bytes) != report["checksum_version"]):
        _fail("destination_changed", "Ownership manifest changed while planning")
    changed = bool(actions or adopted or after_manifest != manifest)
    if changed:
        after_manifest["file_sync_revision"] = transaction_id
    new_bytes = health.canonical_json(after_manifest) + b"\n" if changed else manifest_bytes
    plan = {"schema": PLAN_SCHEMA, "version": 1, "transaction_id": transaction_id,
            "root": root, "build_id": manifest["build_id"], "sources": sources,
            "baseline_manifest_b64": _encode(manifest_bytes), "baseline_checksum_b64": _encode(checksum_bytes),
            "after_manifest_b64": _encode(new_bytes),
            "after_checksum_b64": _encode((health.sha(new_bytes) + "\n").encode("ascii")) if changed else _encode(checksum_bytes),
            "preconditions": [{"path": r["path"], "version": r["card"]} for r in records],
            "source_checks": list({(c["root_id"], c["relative"]): c for c in source_checks}.values()),
            "actions": actions, "adopted": adopted, "returns": returns, "issues": issues,
            "manifest_metadata": metadata_patch,
            "artifact_validation": artifact_validation,
            "reserved_paths": reserved,
            "unknown_files": report["unknown_files"],
            "ready": not any(issue["blocking"] for issue in issues),
            "summary": {"changed_files": len(actions), "staged_bytes": sum(a["after"]["size"] for a in actions),
                        "backup_bytes": sum((a["before"] or {}).get("size", 0) for a in actions),
                        "skipped_files": len(records) - len([a for a in actions if a["path"] in entries]),
                        "metadata_changed": changed}}
    plan["plan_sha256"] = _fingerprint(plan)
    _validate_plan(plan)
    return plan


def _validate_information_artifacts(root, sources, old, new, actions):
    """Validate selected bytes once, retaining summaries rather than image pixels.

    The production parser validates every XMI directory/payload and bounds its
    memory use to one <=16 MiB object. Unchanged generic units are not parsed.
    Exact input versions are subsequently rechecked by apply/recovery; no bitmap
    parser or second whole-art traversal is run for each file publication.
    """
    from . import track_info

    info_kinds = {"track_info", "information_file", "info_file"}
    index_kinds = {"info_index", "native_info_index", "native_asset_index"}
    entries = {entry["path"]: entry for entry in new["files"]}
    before = {entry["path"]: entry for entry in old["files"]}
    pending = {action["path"]: action for action in actions}
    tracks = {str(track["id"]): track for track in new.get("tracks", [])}
    relations = []
    for key in ("information", "information_files", "info_files"):
        rows = new.get(key, [])
        relations += list(rows.values()) if isinstance(rows, dict) else rows
    relation_by_path = {row["path"]: row for row in relations}
    info_paths = {path for path, entry in entries.items() if entry.get("kind") in info_kinds} | set(relation_by_path)
    changed_info = {path for path in pending if path in info_paths}
    changed_tracks = {identity for identity, track in tracks.items()
                      if track["path"] in entries and (track["path"] not in before or
                      health.baseline_version(entries[track["path"]]) != health.baseline_version(before[track["path"]]))}
    information_to_track = {track.get("information_id", track_info.info_id(identity)): identity for identity, track in tracks.items()}
    changed_information_ids = {tracks[identity].get("information_id", track_info.info_id(identity)) for identity in changed_tracks}
    bound_paths = set()
    for path in info_paths:
        entry, relation = entries[path], relation_by_path.get(path, {})
        identity = entry.get("info_id") or relation.get("id") or PurePosixPath(path).stem
        track_id = entry.get("track_id") or relation.get("track_id")
        if str(track_id) in changed_tracks or identity in changed_information_ids:
            bound_paths.add(path)
    topology_changed = any(old.get(key) != new.get(key) for key in
                           ("information", "information_files", "info_files", "native_info_index", "native_asset_index", "info_index", "information_index"))
    index_paths = {path for path, entry in entries.items() if entry.get("kind") in index_kinds}
    for key in ("native_info_index", "native_asset_index", "info_index", "information_index"):
        descriptor = new.get(key, {})
        if descriptor.get("path"):
            index_paths.add(descriptor["path"])
        if descriptor.get("assets_path"):
            index_paths.add(descriptor["assets_path"])
    if not (changed_info or bound_paths or topology_changed or set(pending) & index_paths):
        return {"information": {}, "indexes": {}}
    summaries, validated_indexes = {}, {}
    by_id = {}
    for path in info_paths:
        entry, relation = entries[path], relation_by_path.get(path, {})
        identity = entry.get("info_id") or relation.get("id") or PurePosixPath(path).stem
        try:
            identity = uuid.UUID(str(identity)).hex
        except (ValueError, TypeError, AttributeError):
            _fail("invalid_information_identity", "Information UUID is not declared by its exact path", path=path)
        if identity in by_id and by_id[identity] != path:
            _fail("identity_collision", "More than one owned XMI has the same UUID")
        by_id[identity] = path

    def final_bytes(path, maximum, *, parser_hash=False):
        entry = entries.get(path)
        if entry is None or entry["size"] > maximum:
            _fail("invalid_information", "Information asset is absent or oversized", path=path)
        action = pending.get(path)
        if action and "data_b64" in action["payload"]:
            data = _decode(action["payload"]["data_b64"])
        else:
            source = action["payload"].get("source") if action else None
            target = checked_path(sources[source["root_id"]]["path"], source["relative"]) if source else checked_path(root, path)
            with no_links(target).open("rb") as stream:
                data = stream.read(maximum + 1)
        if len(data) > maximum or len(data) != entry["size"] or not parser_hash and health.sha(data) != entry["sha256"]:
            _fail("information_changed", "Selected information bytes differ from their frozen file unit", path=path)
        return data

    def information(path):
        if path in summaries:
            return summaries[path]
        entry, relation = entries[path], relation_by_path.get(path, {})
        try:
            parsed = track_info.parse_info(final_bytes(path, track_info.MAX_BYTES, parser_hash=True))
        except (track_info.InfoError, OSError, ValueError) as exc:
            _fail("invalid_information", "Selected XMI failed production validation", path=path, reason=str(exc))
        if parsed.sha256 != entry["sha256"]:
            _fail("information_changed", "Parsed XMI differs from the selected exact file unit", path=path)
        expected_ids = {str(value) for value in (entry.get("info_id"), relation.get("id")) if value is not None}
        try:
            identifiers_match = all(uuid.UUID(value).hex == parsed.info_id for value in expected_ids)
        except (ValueError, TypeError, AttributeError):
            identifiers_match = False
        if path != track_info.info_path(parsed.info_id) or not identifiers_match:
            _fail("information_identity_mismatch", "XMI UUID differs from its owned path or manifest identity", path=path)
        track_id = entry.get("track_id") or relation.get("track_id")
        if entry.get("track_id") and relation.get("track_id") and str(entry["track_id"]) != str(relation["track_id"]):
            _fail("information_identity_mismatch", "XMI file and topology disagree on the stable track", path=path)
        if track_id is None and entry.get("kind") == "track_info":
            track_id = information_to_track.get(parsed.info_id)
        if track_id is not None:
            track = tracks.get(str(track_id))
            if track is None:
                _fail("unresolved_track", "XMI has no final stable track identity", path=path)
            original = entries[track["path"]]
            expected_id = track.get("information_id", track_info.info_id(str(track_id)))
            try:
                expected_id = uuid.UUID(str(expected_id)).hex
            except (ValueError, TypeError, AttributeError):
                _fail("information_identity_mismatch", "Track has an invalid information UUID", path=path)
            if parsed.info_id != expected_id:
                _fail("information_identity_mismatch", "Track information UUID is inconsistent", path=path)
            if (parsed.source_sha256 != original["sha256"] or parsed.source_size != original["size"]
                    or parsed.source_path != "xm5/" + track["path"]):
                _fail("information_source_mismatch", "Actual XMI header is bound to another original version/path", path=path)
        elif entry.get("kind") == "track_info":
            _fail("unresolved_track", "Track information has no stable track binding", path=path)
        elif parsed.source_path or parsed.flags & 3:
            _fail("information_identity_mismatch", "Auxiliary XMI cannot claim audio gain or an original audio path", path=path)
        for field, actual in (("source_sha256", parsed.source_sha256), ("source_size", parsed.source_size)):
            if field in entry and entry[field] != actual or field in relation and relation[field] != actual:
                _fail("information_provenance_mismatch", "XMI header differs from claimed provenance", path=path, field=field)
        if new.get("native_only_art"):
            role, subject = relation.get("role"), relation.get("subject")
            if role == "playback":
                art_id = tracks.get(str(track_id), {}).get("playback_art")
            elif role == "directory":
                art_id = next((item.get("preview_art") for item in new.get("directories", []) if "xm5/" + item["path"] == subject), None)
            elif role == "playlist":
                art_id = next((item.get("cover_art") for item in new.get("playlists", []) if item["id"] == subject), None)
            else:
                art_id = None
            expected_art = new.get("artworks", {}).get(art_id, {}).get("derivatives", {})
            actual_art = {str(payload.key): {"sha256": payload.sha256, "size": payload.length} for payload in parsed.entries if payload.kind == 1}
            if actual_art != expected_art:
                _fail("information_artwork_mismatch", "XMI bitmap payloads differ from the exact declared artwork identity", path=path)
        summary = {"info_id": parsed.info_id, "sha256": parsed.sha256,
                   "source_sha256": parsed.source_sha256, "source_size": parsed.source_size,
                   "source_path": parsed.source_path,
                   "payloads": [{"kind": payload.kind, "key": payload.key, "sha256": payload.sha256, "length": payload.length}
                                for payload in parsed.entries]}
        summaries[path] = summary
        return summary

    for path in sorted(changed_info | bound_paths):
        information(path)
    expected_records = {}
    for relation in relations:
        if relation.get("role") is None or relation.get("subject") is None:
            continue
        try:
            key = (track_info.lookup_key(relation["role"], relation["subject"]).hex(), track_info.ROLE_IDS[relation["role"]])
        except (track_info.InfoError, KeyError, TypeError) as exc:
            _fail("invalid_information_topology", "Invalid native information role/subject", reason=str(exc))
        if key in expected_records:
            _fail("identity_collision", "Information topology has duplicate native lookup keys")
        expected_records[key] = (str(relation["id"]), relation["sha256"])
    indexes_to_check = set(pending) & index_paths
    if changed_info or bound_paths or topology_changed:
        indexes_to_check |= index_paths
    for path in sorted(indexes_to_check):
        entry = entries[path]
        try:
            data = final_bytes(path, track_info.INDEX_HEADER_BYTES + track_info.MAX_RECORDS * track_info.INDEX_RECORD_BYTES, parser_hash=True)
            is_asset = entry.get("kind") == "native_asset_index" or path == new.get("native_asset_index", {}).get("path") or path == new.get("native_info_index", {}).get("assets_path")
            parsed = track_info.parse_asset_index(data) if is_asset else track_info.parse_index(data)
        except (track_info.InfoError, OSError, ValueError) as exc:
            _fail("invalid_information_index", "Selected native index failed production validation", path=path, reason=str(exc))
        if parsed["sha256"] != entry["sha256"]:
            _fail("information_changed", "Parsed native index differs from the selected file unit", path=path)
        if parsed["build_id"] != uuid.UUID(str(new["build_id"])).hex:
            _fail("information_index_mismatch", "Native index belongs to another build", path=path)
        observed = {}
        for record in parsed["records"]:
            owner = by_id.get(record["info_id"])
            if owner is None:
                _fail("information_index_mismatch", "Native index references an unowned XMI UUID", path=path)
            owner_entry = entries[owner]
            expected_hash = record["info_sha256"] if is_asset else record["sha256"]
            if owner_entry["sha256"] != expected_hash:
                _fail("information_index_mismatch", "Native index references another XMI generation", path=path)
            summary = information(owner)
            if is_asset:
                if not any(payload["kind"] == 1 and payload["key"] == record["side"] and payload["sha256"] == record["sha256"]
                           for payload in summary["payloads"]):
                    _fail("information_asset_mismatch", "Asset index digest is not an owned XMI bitmap payload", path=path)
                observed[(record["sha256"], record["side"])] = (record["info_id"], expected_hash)
            else:
                observed[(record["key"], record["role"])] = (record["info_id"], expected_hash)
        if not is_asset and expected_records and observed != expected_records:
            _fail("information_index_mismatch", "Information index does not match final lookup topology", path=path)
        if is_asset and relations:
            expected_assets = {}
            for relation in relations:
                summary = information(relation["path"])
                for payload in summary["payloads"]:
                    if payload["kind"] == 1:
                        key = (payload["sha256"], payload["key"])
                        candidate = (summary["info_id"], summary["sha256"])
                        if key not in expected_assets or candidate < expected_assets[key]:
                            expected_assets[key] = candidate
            if observed != expected_assets:
                _fail("information_asset_mismatch", "Asset index does not cover the final XMI payload generation", path=path)
        validated_indexes[path] = {"sha256": parsed["sha256"], "record_count": len(parsed["records"])}
    descriptor = new.get("native_info_index", {})
    if descriptor.get("path") in validated_indexes and descriptor.get("ready_path"):
        ready_path = descriptor["ready_path"]
        try:
            ready = json.loads(final_bytes(ready_path, MAX_GENERATED_BYTES).decode("utf-8"))
        except (ValueError, UnicodeError, OSError) as exc:
            _fail("invalid_information_ready", "Information readiness bytes are not valid UTF-8 JSON", path=ready_path, reason=str(exc))
        index_entry = entries[descriptor["path"]]
        if (not isinstance(ready, dict) or ready.get("schema") != "xm5.info-readiness" or ready.get("version") != 1
                or ready.get("build_id") != new["build_id"] or ready.get("index_sha256") != index_entry["sha256"]
                or ready.get("index_size") != index_entry["size"]
                or ready.get("record_count") != validated_indexes[descriptor["path"]]["record_count"]):
            _fail("information_ready_mismatch", "Readiness refers to another selected native index generation", path=ready_path)
        if relations:
            expected = [{key: relation[key] for key in ("id", "path", "size", "sha256")} for relation in relations]
            if ready.get("information") != expected:
                _fail("information_ready_mismatch", "Readiness refers to another selected XMI generation", path=ready_path)
        if descriptor.get("assets_path"):
            assets = ready.get("assets", {})
            owned = entries[descriptor["assets_path"]]
            if (not isinstance(assets, dict) or assets.get("path") != descriptor["assets_path"]
                    or assets.get("sha256") != owned["sha256"] or assets.get("size") != owned["size"]
                    or assets.get("record_count") != validated_indexes[descriptor["assets_path"]]["record_count"]):
                _fail("information_ready_mismatch", "Readiness refers to another selected asset index generation", path=ready_path)
    return {"information": summaries, "indexes": validated_indexes}


def _validate_topology(old, new):
    """Metadata references grant no ownership: each asset must be in files[]."""
    files = {entry["path"]: entry for entry in new["files"]}

    def unique(rows, field, label):
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get(field), str)
                                             or not row[field] for row in rows):
            _fail("invalid_metadata", "Invalid " + label + " records")
        values = [row[field] for row in rows]
        keys = [path_key(value) for value in values] if field == "path" else values
        if len(set(keys)) != len(keys):
            _fail("identity_collision", "Duplicate " + label + " identities")
        return {row[field]: row for row in rows}

    def asset(path):
        safe_relative(path)
        if path not in files:
            _fail("unowned_metadata_reference", "Metadata asset is not an owned file unit", path=path)
        return files[path]

    tracks = unique(new.get("tracks", []), "id", "track")
    old_tracks = unique(old.get("tracks", []), "id", "track")
    if not old_tracks.keys() <= tracks.keys():
        _fail("metadata_deletion", "Existing track IDs cannot be removed by a metadata patch")
    track_paths = set()
    for identity, track in tracks.items():
        path = track.get("path")
        entry = asset(path)
        if entry.get("kind") != "original_audio" or path_key(path) in track_paths:
            _fail("identity_collision", "Track paths must identify separate original audio units")
        track_paths.add(path_key(path))
        if identity in old_tracks and old_tracks[identity]["path"] != path:
            _fail("relink_required", "Metadata cannot move an existing stable track identity")
        if track.get("sha256", entry["sha256"]) != entry["sha256"]:
            _fail("invalid_metadata", "Track metadata version differs from its owned file", path=path)
    playlists = unique(new.get("playlists", []), "id", "playlist")
    old_playlists = unique(old.get("playlists", []), "id", "playlist")
    if not old_playlists.keys() <= playlists.keys():
        _fail("metadata_deletion", "Existing playlist IDs cannot be removed by a metadata patch")
    playlist_paths = set()
    for identity, playlist in playlists.items():
        path = playlist.get("path")
        entry = asset(path)
        if entry.get("kind") not in {"generated_playlist", "playlist"} or path_key(path) in playlist_paths:
            _fail("identity_collision", "Playlist paths must identify separate durable units")
        playlist_paths.add(path_key(path))
        if identity in old_playlists and old_playlists[identity]["path"] != path:
            _fail("relink_required", "Metadata cannot move an existing playlist identity")
        if not isinstance(playlist.get("tracks", []), list) or any(str(t) not in tracks for t in playlist.get("tracks", [])):
            _fail("unresolved_track", "Playlist metadata contains an unknown track ID")
    artworks = new.get("artworks", {})
    if not isinstance(artworks, dict):
        _fail("invalid_metadata", "Artwork topology must be an object")
    for art in artworks.values():
        if not isinstance(art, dict) or not isinstance(art.get("roles", {}), dict):
            _fail("invalid_metadata", "Invalid artwork topology")
        for side, path in art.get("roles", {}).items():
            if new.get("native_only_art"):
                from .native_art_index import asset_path_for_digest, NATIVE_SIDES
                derivative = art.get("derivatives", {}).get(str(side), {})
                if int(side) not in NATIVE_SIDES or path != asset_path_for_digest(derivative.get("sha256", "")):
                    _fail("invalid_metadata", "Virtual native artwork identity differs from its exact derivative", path=path)
            else:
                asset(path)
    for key in ("directories", "original_directories"):
        directories = unique(new.get(key, []), "path", key)
        for path in directories:
            safe_relative(path)
        for directory in directories.values():
            if directory.get("preview_art") is not None and directory["preview_art"] not in artworks:
                _fail("unresolved_artwork", "Directory cover references unknown artwork")
    for track in tracks.values():
        if track.get("playback_art") is not None and track["playback_art"] not in artworks:
            _fail("unresolved_artwork", "Track cover references unknown artwork")
    for playlist in playlists.values():
        if playlist.get("cover_art") is not None and playlist["cover_art"] not in artworks:
            _fail("unresolved_artwork", "Playlist cover references unknown artwork")
    for key in ("native_lyrics_index", "native_art_index", "art_index", "native_info_index", "native_asset_index", "playlist_binding_index", "info_index", "information_index"):
        descriptor = new.get(key)
        if descriptor is not None:
            if not isinstance(descriptor, dict):
                _fail("invalid_metadata", "Index metadata must be an object")
            for field, digest_field, size_field in (("path", "sha256", "size"), ("ready_path", "ready_sha256", "ready_size"),
                                                     ("assets_path", "assets_sha256", "assets_size")):
                if descriptor.get(field):
                    entry = asset(descriptor[field])
                    if descriptor.get(digest_field, entry["sha256"]) != entry["sha256"]:
                        _fail("invalid_metadata", "Index metadata checksum differs from its file", path=descriptor[field])
                    if descriptor.get(size_field, entry["size"]) != entry["size"]:
                        _fail("invalid_metadata", "Index metadata size differs from its file", path=descriptor[field])
    information_ids = {}
    for key in ("information_files", "info_files", "information"):
        values = new.get(key, [])
        values = list(values.values()) if isinstance(values, dict) else values
        if not isinstance(values, list):
            _fail("invalid_metadata", "Information topology must be records")
        seen, paths = set(), set()
        for record in values:
            if not isinstance(record, dict):
                _fail("invalid_metadata", "Information topology must contain objects")
            entry = asset(record.get("path"))
            identity = str(record.get("id", record.get("managed_id", record.get("track_id", record["path"]))))
            if identity in seen:
                _fail("identity_collision", "Duplicate information identity")
            seen.add(identity)
            if path_key(record["path"]) in paths:
                _fail("identity_collision", "Duplicate information path")
            paths.add(path_key(record["path"]))
            information_ids[identity] = record
            if record.get("track_id") is not None and str(record["track_id"]) not in tracks:
                _fail("unresolved_track", "Information record refers to an unknown track")
            if record.get("sha256", entry["sha256"]) != entry["sha256"]:
                _fail("invalid_metadata", "Information topology checksum differs from its file")
            for field in ("source_sha256", "recipe_id"):
                if field in record and field in entry and record[field] != entry[field]:
                    _fail("invalid_metadata", "Information provenance differs from its file record", field=field)
    for identity, track in tracks.items():
        if track.get("information_id") is not None:
            descriptor = information_ids.get(str(track["information_id"]))
            if descriptor is None or descriptor.get("track_id") != identity:
                _fail("unresolved_information", "Track refers to an unknown or mismatched information identity")
    lyrics = new.get("lyrics", [])
    if not isinstance(lyrics, list):
        _fail("invalid_metadata", "Lyrics topology must be a list")
    for record in lyrics:
        if not isinstance(record, dict):
            _fail("invalid_metadata", "Invalid lyric record")
        if record.get("track_id") is not None and str(record["track_id"]) not in tracks:
            _fail("unresolved_track", "Lyric record refers to an unknown track")
        for key in ("path", "original_path", "normalized_path", "info_path"):
            if record.get(key):
                asset(record[key])
    favorites = new.get("favorites", [])
    if not isinstance(favorites, list) or any(str(identity) not in tracks for identity in favorites):
        _fail("unresolved_track", "Favorites metadata contains an unknown track ID")


def _validate_plan(plan, *, validate_sources=True):
    if len(health.canonical_json(plan)) > MAX_PLAN_BYTES:
        _fail("plan_too_large", "Frozen plan exceeds 192 MiB; use declared prepared-file sources or smaller batches")
    if (plan.get("schema") != PLAN_SCHEMA or plan.get("version") != 1 or
            plan.get("plan_sha256") != _fingerprint(plan)):
        _fail("invalid_plan", "Frozen plan checksum/schema mismatch")
    transaction_id = _token(plan["transaction_id"])
    root = no_links(plan["root"])
    if root.name != "xm5" or not root.is_dir() or str(root) != plan["root"]:
        _fail("invalid_root", "Expected the exact existing v2 xm5 container")
    old_bytes = _decode(plan["baseline_manifest_b64"])
    old, new = json.loads(old_bytes), json.loads(_decode(plan["after_manifest_b64"]))
    if (old.get("schema") != "xm5.card-library" or old.get("version") != 2 or old.get("status") != "complete"
            or old.get("build_id") != plan["build_id"] or new.get("build_id") != plan["build_id"]
            or new.get("schema") != old["schema"] or new.get("version") != 2 or new.get("status") != "complete"
            or _decode(plan["baseline_checksum_b64"]).decode("ascii").strip() != health.sha(old_bytes)
            or _decode(plan["after_checksum_b64"]).decode("ascii").strip() != health.sha(_decode(plan["after_manifest_b64"]))):
        _fail("invalid_plan", "Frozen ownership manifest mismatch")
    old_entries = {entry["path"]: entry for entry in old["files"]}
    new_entries = {entry["path"]: entry for entry in new["files"]}
    old_tracks = {track["path"]: track for track in old.get("tracks", [])}
    new_tracks = {track["path"]: track for track in new.get("tracks", [])}
    if len(new_entries) != len(new["files"]) or not old_entries.keys() <= new_entries.keys():
        _fail("invalid_plan", "Owned files cannot be removed or aliased")
    keys = [path_key(path) for path in new_entries]
    if len(set(keys)) != len(keys):
        _fail("path_collision", "Frozen manifest aliases paths")
    for path, entry in new_entries.items():
        if path not in old_entries and not _allowed_addition(entry, old):
            _fail("unowned_destination", "Frozen plan adds an undeclared path")
        if path in old_entries and health.managed_id(entry, new, tracks_by_path=new_tracks) != health.managed_id(old_entries[path], old, tracks_by_path=old_tracks):
            _fail("identity_mismatch", "Frozen plan changes an owned identity")
    patch = plan.get("manifest_metadata", {})
    if not isinstance(patch, dict) or set(patch) - MANIFEST_METADATA_KEYS:
        _fail("invalid_metadata", "Frozen manifest patch changes unsupported ownership fields")
    for key in set(old) | set(new):
        if key not in {"files", "tracks", "file_sync_revision"} | set(patch) and old.get(key) != new.get(key):
            _fail("invalid_metadata", "Frozen manifest changes an unplanned section", field=key)
    _validate_topology(old, new)
    seen = set()
    for index, action in enumerate(plan["actions"]):
        path = safe_relative(action["path"])
        if path in seen or path not in new_entries or path in {MANIFEST, CHECKSUM, INCOMPLETE} or path.startswith(CONTROL + "/"):
            _fail("invalid_plan", "Invalid file-unit target")
        seen.add(path)
        if (action["stage"] != _stage(path, transaction_id, index) or
                action["backup"] != _control_path(transaction_id) + "/old/" + str(index) or
                action["after"] != health.baseline_version(new_entries[path])):
            _fail("invalid_plan", "Staging or output binding mismatch")
        if "data_b64" in action["payload"]:
            if _bytes_version(_decode(action["payload"]["data_b64"])) != action["after"]:
                _fail("invalid_plan", "Frozen output checksum mismatch")
        else:
            source = action["payload"].get("source", {})
            if source.get("root_id") not in plan["sources"] or source.get("version") != action["after"]:
                _fail("invalid_plan", "Undeclared replacement source")
            safe_relative(source["relative"])
    # Recheck overlap even when a serialized plan was supplied by another layer.
    if validate_sources:
        health.declared_sources(root, {identity: record["path"] for identity, record in plan["sources"].items()})
    return old, new


def _write(root, relative, data):
    with exclusive_output(root, relative) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_bytes(root, relative, data, suffix):
    temporary = relative + "." + suffix
    if os.path.lexists(checked_path(root, temporary)):
        # This exact temporary name belongs to this journal, not a wildcard.
        unlink_owned(root, temporary)
    _write(root, temporary, data)
    replace_owned(root, temporary, relative)


def _save_journal(root, journal):
    relative = _control_path(journal["transaction_id"]) + "/journal.json"
    # The frozen plan is written once. Per-unit durability must not rewrite all
    # queued generated bytes or a growing list of paths for every publication.
    compact = {key: value for key, value in journal.items() if key not in {"plan", "published"}}
    compact.update(plan_sha256=journal["plan"]["plan_sha256"],
                   published_count=len(journal["published"]),
                   last_published=journal["published"][-1] if journal["published"] else None)
    _replace_bytes(root, relative, health.canonical_json(compact) + b"\n", "writing")


def _marker(transaction_id):
    return health.canonical_json({"schema": "xm5.file-sync-incomplete", "version": 1,
                                  "transaction_id": transaction_id}) + b"\n"


def _assert_marker(root, transaction_id, *, required=False):
    path = checked_path(root, INCOMPLETE)
    if path.exists():
        if path.read_bytes() != _marker(transaction_id):
            _fail("another_transaction", "An unrelated incomplete marker blocks this update")
        return True
    if required:
        _fail("missing_marker", "Active file-unit update has lost its ownership marker")
    return False


def _revalidate(plan, *, recovering=False, check_sources=True):
    root = plan["root"]
    if not recovering:
        for path in plan["reserved_paths"]:
            if os.path.lexists(checked_path(root, path)):
                _fail("unowned_staging", "A reserved path appeared after preview", path=path)
    if check_sources:
        current = health.declared_sources(root, {identity: source["path"] for identity, source in plan["sources"].items()})
        if current != plan["sources"]:
            _fail("source_changed", "Source availability changed after preview")
        for item in plan["source_checks"]:
            source = current.get(item["root_id"])
            if not source or source["status"] != "connected" or health.version(checked_path(source["path"], item["relative"])) != item["version"]:
                _fail("source_changed", "Source input changed after preview", root_id=item["root_id"], relative=item["relative"])
    action_map = {action["path"]: action for action in plan["actions"]}
    for condition in plan["preconditions"]:
        actual = health.version(checked_path(root, condition["path"]))
        allowed = [condition["version"]]
        if recovering and condition["path"] in action_map:
            allowed.append(action_map[condition["path"]]["after"])
        if actual not in allowed:
            _fail("destination_changed", "Managed file changed after preview", path=condition["path"])
    # Conflict-copy additions are not preconditions for a former manifest entry.
    for action in plan["actions"]:
        actual = health.version(checked_path(root, action["path"]))
        if actual != action["before"] and not (recovering and actual == action["after"]):
            _fail("destination_changed", "File-unit target changed after preview", path=action["path"])
    for path, old_key, new_key in ((MANIFEST, "baseline_manifest_b64", "after_manifest_b64"),
                                   (CHECKSUM, "baseline_checksum_b64", "after_checksum_b64")):
        data = checked_path(root, path).read_bytes()
        if data != _decode(plan[old_key]) and not (recovering and data == _decode(plan[new_key])):
            _fail("destination_changed", "Ownership metadata changed after preview", path=path)


def _begin(plan):
    root, transaction_id = plan["root"], plan["transaction_id"]
    manifest = json.loads(_decode(plan["baseline_manifest_b64"]))
    if checked_path(root, CONTROL).exists():
        if not health.owned_control(root, manifest):
            _fail("unowned_control", "Existing sync namespace has no matching owner marker")
    else:
        make_new_directory(root, CONTROL)
        _write(root, OWNER, health.canonical_json(health.control_owner(manifest)) + b"\n")
    make_new_directory(root, _control_path(transaction_id))
    _write(root, _control_path(transaction_id) + "/OWNER.json",
           health.canonical_json({"schema": "xm5.file-sync-owner", "version": 1,
                                  "transaction_id": transaction_id, "build_id": plan["build_id"]}) + b"\n")
    journal = {"schema": JOURNAL_SCHEMA, "version": 1, "transaction_id": transaction_id,
               "root": root, "plan": plan, "status": "preparing", "published": [], "error": None}
    _write(root, _control_path(transaction_id) + "/plan.json", health.canonical_json(plan) + b"\n")
    _save_journal(root, journal)
    _write(root, INCOMPLETE, _marker(transaction_id))
    return journal


def _cancelled(cancel):
    return bool(cancel() if callable(cancel) else cancel.is_set() if cancel is not None else False)


def _copy(root, destination, source, expected, cancel=None):
    with no_links(source).open("rb") as incoming, exclusive_output(root, destination) as output:
        while True:
            if _cancelled(cancel):
                _fail("cancelled", "Cancelled while staging a file")
            block = incoming.read(1024 * 1024)
            if not block:
                break
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if health.version(checked_path(root, destination)) != expected:
        _fail("copy_changed", "Staged file does not match the frozen bytes", path=destination)


def _stage_action(plan, action, cancel):
    root = plan["root"]
    current = health.version(checked_path(root, action["path"]))
    if current == action["after"]:
        return
    if current != action["before"]:
        _fail("destination_changed", "Target changed before staging", path=action["path"])
    if action["before"] is not None:
        old = health.version(checked_path(root, action["backup"]))
        if old != action["before"]:
            if old is not None:
                unlink_owned(root, action["backup"])
            _copy(root, action["backup"], checked_path(root, action["path"]), action["before"], cancel)
    stage = health.version(checked_path(root, action["stage"]))
    if stage != action["after"]:
        if stage is not None:
            unlink_owned(root, action["stage"])
        if "data_b64" in action["payload"]:
            _write(root, action["stage"], _decode(action["payload"]["data_b64"]))
        else:
            item = action["payload"]["source"]
            _copy(root, action["stage"], checked_path(plan["sources"][item["root_id"]]["path"], item["relative"]), action["after"], cancel)
    if health.version(checked_path(root, action["stage"])) != action["after"]:
        _fail("stage_checksum", "Staged output did not verify")


def _revalidate_action(plan, action):
    """Bound the per-unit work: never rehash the whole music tree for each file."""
    actual = health.version(checked_path(plan["root"], action["path"]))
    if actual not in [action["before"], action["after"]]:
        _fail("destination_changed", "Target changed before publication", path=action["path"])
    source = action["payload"].get("source")
    if source is not None:
        root = plan["sources"][source["root_id"]]
        if health.version(checked_path(root["path"], source["relative"])) != source["version"]:
            _fail("source_changed", "File-unit source changed before publication", path=action["path"])


def _result(journal):
    return {"status": journal["status"], "root": journal["root"], "transaction_id": journal["transaction_id"],
            "published": list(journal["published"]), "returns": journal["plan"]["returns"],
            "recovery_required": journal["status"] not in {"completed", "rolled_back"},
            "error": journal.get("error"), "summary": journal["plan"]["summary"]}


def _run(journal, cancel=None, progress=None):
    plan, root = journal["plan"], journal["root"]
    try:
        _assert_marker(root, journal["transaction_id"], required=True)
        _revalidate(plan, recovering=True)
        for index, action in enumerate(plan["actions"]):
            if _cancelled(cancel):
                _fail("cancelled", "Cancelled before publishing the next file")
            _stage_action(plan, action, cancel)
            journal["status"] = "publishing"
            _save_journal(root, journal)
            # Full read sets are checked at the transaction boundaries. Per-unit
            # checks keep a batch linear in the total selected input bytes.
            _revalidate_action(plan, action)
            actual = health.version(checked_path(root, action["path"]))
            if actual != action["after"]:
                replace_owned(root, action["stage"], action["path"])
            if health.version(checked_path(root, action["path"])) != action["after"]:
                _fail("publish_checksum", "Published file failed verification", path=action["path"])
            if index >= len(journal["published"]):
                journal["published"].append(action["path"])
            elif journal["published"][index] != action["path"]:
                _fail("invalid_journal", "Published file sequence is not a frozen-plan prefix")
            _save_journal(root, journal)
            if progress:
                progress({"phase": "file_sync", "current": index + 1, "total": len(plan["actions"]), "path": action["path"]})
        if _cancelled(cancel):
            _fail("cancelled", "Cancelled before committing metadata")
        _revalidate(plan, recovering=True)
        journal["status"] = "committing"
        _save_journal(root, journal)
        for path, key in ((MANIFEST, "after_manifest_b64"), (CHECKSUM, "after_checksum_b64")):
            _replace_bytes(root, path, _decode(plan[key]), "xm5sync-" + plan["transaction_id"])
        _revalidate(plan, recovering=True)
        for action in plan["actions"]:
            if health.version(checked_path(root, action["path"])) != action["after"]:
                _fail("publish_checksum", "Output changed before completion", path=action["path"])
        journal["status"] = "completed"
        journal["error"] = None
        _save_journal(root, journal)
        _assert_marker(root, journal["transaction_id"], required=True)
        unlink_owned(root, INCOMPLETE)
        return _result(journal)
    except Exception as exc:
        journal["status"] = "cancelled" if getattr(exc, "code", None) == "cancelled" else "interrupted"
        journal["error"] = {"code": getattr(exc, "code", "io_error"), "message": str(exc)}
        try:
            _save_journal(root, journal)
        except OSError:
            pass  # The preceding durable journal remains the recovery authority.
        return _result(journal)


def apply_plan(plan, *, cancel=None, progress=None):
    """Revalidate before any write, then publish only frozen changed file units.

    Normal failure/cancellation returns a recoverable result with its transaction
    ID. Process termination can be recovered by list_transactions()/recover().
    Completed transactions retain old bytes; this module never prunes backups.
    """
    _validate_plan(plan)
    if not plan["ready"]:
        _fail("unresolved_plan", "Resolve the plan's blocking issues before applying", issues=plan["issues"])
    _revalidate(plan)
    if checked_path(plan["root"], INCOMPLETE).exists():
        _fail("another_transaction", "Recover the existing incomplete update first")
    if _cancelled(cancel):
        return {"status": "cancelled", "root": plan["root"], "transaction_id": None,
                "recovery_required": False, "published": [], "returns": [], "summary": plan["summary"]}
    if not plan["summary"]["metadata_changed"]:
        return {"status": "unchanged", "root": plan["root"], "transaction_id": None,
                "recovery_required": False, "published": [], "returns": [], "summary": plan["summary"]}
    required = plan["summary"]["staged_bytes"] + plan["summary"]["backup_bytes"] + len(health.canonical_json(plan)) * 3 + 1024 * 1024
    if shutil.disk_usage(plan["root"]).free < required:
        _fail("insufficient_space", "Not enough space for changed files and retained old bytes", required=required)
    return _run(_begin(plan), cancel, progress)


def _load_journal(card_root, transaction_id):
    root = no_links(card_root)
    relative = _control_path(transaction_id)
    journal_path = checked_path(root, relative + "/journal.json")
    if journal_path.stat().st_size > 256 * 1024 * 1024:
        _fail("invalid_journal", "Journal exceeds 256 MiB")
    journal = json.loads(journal_path.read_text("utf-8"))
    if "plan" not in journal:
        plan_path = checked_path(root, relative + "/plan.json")
        if plan_path.stat().st_size > MAX_PLAN_BYTES + 1:
            _fail("invalid_journal", "Frozen transaction plan exceeds its bound")
        plan = json.loads(plan_path.read_text("utf-8"))
        count = journal.get("published_count")
        if (journal.get("plan_sha256") != plan.get("plan_sha256") or type(count) is not int or count < 0
                or count > len(plan.get("actions", []))):
            _fail("invalid_journal", "Compact journal does not match its frozen plan")
        journal["plan"] = plan
        journal["published"] = [action["path"] for action in plan["actions"][:count]]
    owner = json.loads(checked_path(root, relative + "/OWNER.json").read_text("utf-8"))
    expected = {"schema": "xm5.file-sync-owner", "version": 1,
                "transaction_id": transaction_id, "build_id": journal.get("plan", {}).get("build_id")}
    if (owner != expected or journal.get("schema") != JOURNAL_SCHEMA or journal.get("version") != 1
            or journal.get("transaction_id") != transaction_id or journal.get("root") != str(root)
            or journal.get("plan", {}).get("root") != str(root)):
        _fail("unowned_transaction", "Journal and exact directory owner do not match")
    # Rollback and journal inventory need no access to a disconnected/replaced
    # source path. Resume revalidates those roots before any source is read.
    old, _ = _validate_plan(journal["plan"], validate_sources=False)
    if not health.owned_control(root, old):
        _fail("unowned_control", "Library sync owner marker does not match")
    return journal


def list_transactions(card_root):
    """Read recognized owned journals only; unknown folders are not recovery jobs."""
    root = no_links(card_root)
    directory = checked_path(root, CONTROL + "/transactions")
    if not directory.exists():
        return []
    result = []
    for item in sorted(directory.iterdir()):
        if not re.fullmatch("[0-9a-f]{32}", item.name):
            continue
        try:
            journal = _load_journal(root, item.name)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # Unknown or incomplete control folders are never adopted.
        result.append({"transaction_id": item.name, "status": journal["status"],
                       "published": journal["published"], "error": journal.get("error"),
                       "marker_present": _assert_marker(root, item.name) if checked_path(root, INCOMPLETE).exists()
                       and checked_path(root, INCOMPLETE).read_bytes() == _marker(item.name) else False})
    return result


def recover(card_root, transaction_id, *, action="rollback", cancel=None, progress=None):
    """Resume exact planned inputs, or restore retained old bytes without sources.

    External target or ownership drift stops recovery. Rollback never overwrites
    a version other than the known before/after bytes. No atomic-FAT guarantee is
    made: after power loss the caller must inspect and recover before use.
    """
    journal = _load_journal(card_root, _token(transaction_id))
    plan, root = journal["plan"], journal["root"]
    if action not in {"resume", "rollback"}:
        _fail("invalid_recovery", "Recovery action must be resume or rollback")
    if journal["status"] == "rolled_back":
        return _result(journal)
    marker = _assert_marker(root, transaction_id)
    if journal["status"] == "completed" and not marker:
        if action == "resume":
            return _result(journal)
        _fail("already_completed", "A completed update is not an interrupted recovery")
    _revalidate(plan, recovering=True, check_sources=action == "resume")
    if not marker:
        _write(root, INCOMPLETE, _marker(transaction_id))
    if action == "resume":
        return _run(journal, cancel, progress)
    try:
        journal["status"] = "rolling_back"
        _save_journal(root, journal)
        for index in reversed(range(len(plan["actions"]))):
            item = plan["actions"][index]
            actual = health.version(checked_path(root, item["path"]))
            if actual == item["after"] and actual != item["before"]:
                if item["before"] is None:
                    unlink_owned(root, item["path"])
                else:
                    if health.version(checked_path(root, item["backup"])) != item["before"]:
                        _fail("backup_corrupt", "Retained old bytes are missing or corrupt", path=item["path"])
                    temporary = _stage(item["path"], transaction_id, index, "restore")
                    if health.version(checked_path(root, temporary)) is not None:
                        unlink_owned(root, temporary)
                    _copy(root, temporary, checked_path(root, item["backup"]), item["before"])
                    replace_owned(root, temporary, item["path"])
                if health.version(checked_path(root, item["path"])) != item["before"]:
                    _fail("rollback_checksum", "Restored bytes did not verify", path=item["path"])
            if health.version(checked_path(root, item["stage"])) is not None:
                unlink_owned(root, item["stage"])
        for path, key in ((MANIFEST, "baseline_manifest_b64"), (CHECKSUM, "baseline_checksum_b64")):
            _replace_bytes(root, path, _decode(plan[key]), "xm5sync-" + transaction_id)
        journal["status"], journal["error"] = "rolled_back", None
        _save_journal(root, journal)
        _assert_marker(root, transaction_id, required=True)
        unlink_owned(root, INCOMPLETE)
        return _result(journal)
    except Exception as exc:
        journal["status"] = "interrupted"
        journal["error"] = {"code": getattr(exc, "code", "io_error"), "message": str(exc)}
        _save_journal(root, journal)
        return _result(journal)
