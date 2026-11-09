"""Read-only, hash-based inspection of an explicitly selected v2 library.

Importing this module has no profile, drive-discovery or application side effects.
Source roots are caller supplied; paths stored in a card manifest are never followed
implicitly. A missing source root is not evidence that its files were deleted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re

from .restore_card import (RestoreError, checked_path, file_digest, load_manifest,
                           no_links, path_key, safe_relative)

MANIFEST = "Struct/manifest.json"
CHECKSUM = "Struct/manifest.sha256"
CONTROL = "Struct/file-sync"
OWNER = CONTROL + "/OWNER.json"
GENERATED_KINDS = frozenset({
    "derived_art", "art_index", "native_art_index", "native_art_ready",
    "native_lyrics_index", "native_lyrics_ready", "normalized_lyrics",
    "peak_cache", "track_info", "information_file", "info_file",
    "info_index", "native_info_index", "native_info_ready", "native_asset_index", "native_playlist_bindings", "generated_preset", "restore_tool",
})
DURABLE_KINDS = frozenset({
    "generated_playlist", "playlist", "favorites", "notes", "lyrics_mapping",
    "preset_metadata", "cover_mapping", "lyric_mapping", "user_state",
    "sync_conflict_copy",
})


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def version(path):
    """Return an exact byte version, None for absent, or raise on unsafe inputs."""
    path = no_links(path)
    if not os.path.lexists(path):
        return None
    size, digest = file_digest(path)
    return {"size": size, "sha256": digest}


def baseline_version(entry):
    return {"size": entry["size"], "sha256": entry["sha256"]}


def category(entry):
    kind = entry.get("kind", "")
    if kind in DURABLE_KINDS:
        return "durable"
    if kind in GENERATED_KINDS:
        return "generated"
    return "original" if kind.startswith("original_") else "preserved"


def managed_id(entry, manifest, *, tracks_by_path=None):
    """Identity is explicit or comes from a stable track ID, never basename."""
    if entry.get("managed_id"):
        return str(entry["managed_id"])
    track = (tracks_by_path.get(entry["path"]) if tracks_by_path is not None else
             next((t for t in manifest.get("tracks", []) if t.get("path") == entry["path"]), None))
    if track and entry.get("kind") == "original_audio":
        return "track:" + str(track["id"])
    if entry.get("track_id") and category(entry) == "generated":
        return entry.get("kind", "generated") + ":" + str(entry["track_id"])
    return "file:" + entry["path"]


def compare_versions(base, pc, card):
    """Three-way exact-byte classification; None is a real missing file."""
    if pc == card:
        return "unchanged" if pc == base else "both_same"
    if pc == base:
        return "card_changed"
    if card == base:
        return "pc_changed"
    return "conflict"


def compare_sides(pc_base, card_base, pc, card):
    """Accepted divergent sides each retain their exact last-observed baseline."""
    if pc == pc_base and card == card_base:
        return "unchanged"
    if pc == card:
        return "both_same"
    if pc == pc_base:
        return "card_changed"
    if card == card_base:
        return "pc_changed"
    return "conflict"


def declared_sources(card_root, roots):
    """Validate only roots explicitly supplied by the caller, without creating them."""
    card_root = no_links(card_root)
    result = {}
    for identity, value in (roots or {}).items():
        if not isinstance(identity, str) or not identity:
            raise RestoreError("invalid_source_id", "Source IDs must be nonempty strings")
        if value is None:
            result[identity] = {"path": None, "status": "disconnected"}
            continue
        source = no_links(value)
        if source == card_root or source.is_relative_to(card_root) or card_root.is_relative_to(source):
            raise RestoreError("root_overlap", "Source and managed library must not overlap")
        if source.exists() and not source.is_dir():
            raise RestoreError("not_directory", "Declared source root must be a directory")
        result[identity] = {"path": str(source),
                            "status": "connected" if source.is_dir() else "disconnected"}
    return result


def source_observation(entry, sources):
    identity, relative = entry.get("root_id"), entry.get("source_relative")
    if not identity or not relative:
        return {"status": "not_bound", "version": None}
    safe_relative(relative)
    record = sources.get(identity)
    if record is None:
        return {"status": "unselected", "version": None}
    if record["status"] != "connected":
        return {"status": "disconnected", "version": None}
    observed = version(checked_path(record["path"], relative))
    return {"status": "present" if observed is not None else "missing",
            "root_id": identity, "relative": relative, "version": observed}


def control_owner(manifest):
    return {"schema": "xm5.file-sync-root", "version": 1,
            "library_id": manifest["build_id"]}


def owned_control(root, manifest):
    control = checked_path(root, CONTROL)
    if not control.exists():
        return False
    marker = checked_path(root, OWNER)
    try:
        return marker.stat().st_size < 4096 and json.loads(marker.read_text("utf-8")) == control_owner(manifest)
    except (OSError, ValueError):
        return False


def walk_versions(root, *, skip_control=False):
    """Inventory regular files without following links or accepting path aliases."""
    result, seen = {}, set()
    root = no_links(root)
    for folder, directories, files in os.walk(root, followlinks=False,
                                               onerror=lambda error: (_ for _ in ()).throw(error)):
        directories.sort()
        files.sort()
        for name in list(directories):
            path = no_links(Path(folder) / name)
            relative = path.relative_to(root).as_posix()
            if skip_control and relative == CONTROL:
                directories.remove(name)
                continue
            key = path_key(relative)
            if key in seen:
                raise RestoreError("path_collision", "Aliased filesystem paths", {"path": relative})
            seen.add(key)
        for name in files:
            path = no_links(Path(folder) / name)
            relative = path.relative_to(root).as_posix()
            key = path_key(relative)
            if key in seen:
                raise RestoreError("path_collision", "Aliased filesystem paths", {"path": relative})
            seen.add(key)
            result[relative] = version(path)
    return result


def inspect_library(card_root, source_roots=None, *, find_relinks=False, expected_recipes=None):
    """Inspect a recognized v2 container, preserving B/P/C and unknown ownership.

    Hash matches elsewhere are suggestions only. Even one unambiguous match does
    not change a track ID, path, playlist occurrence or manifest entry.
    """
    root = no_links(card_root)
    manifest = load_manifest(root)
    if manifest["version"] != 2:
        raise RestoreError("unsupported_format", "File-unit sync requires card-library v2")
    sources = declared_sources(root, source_roots)
    observed = walk_versions(root, skip_control=owned_control(root, manifest))
    known = {entry["path"] for entry in manifest["files"]} | {MANIFEST, CHECKSUM}
    records, identities = [], set()
    tracks_by_path = {track["path"]: track for track in manifest.get("tracks", [])}
    for entry in manifest["files"]:
        identity = managed_id(entry, manifest, tracks_by_path=tracks_by_path)
        if identity in identities:
            raise RestoreError("identity_collision", "Managed identities must be unique", {"managed_id": identity})
        identities.add(identity)
        base, card = baseline_version(entry), observed.get(entry["path"])
        pc_base = entry.get("sync_pc_baseline", base)
        if (not isinstance(pc_base, dict) or type(pc_base.get("size")) is not int or pc_base["size"] < 0
                or not isinstance(pc_base.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", pc_base["sha256"])):
            raise RestoreError("invalid_manifest", "Invalid exact PC synchronization baseline", {"path": entry["path"]})
        source = source_observation(entry, sources)
        pc = source["version"] if source["status"] in {"present", "missing"} else pc_base
        state = compare_sides(pc_base, base, pc, card)
        records.append({"managed_id": identity, "path": entry["path"], "kind": entry.get("kind"),
                        "category": category(entry), "baseline": base, "pc_baseline": pc_base, "pc": pc, "card": card,
                        "source": source, "state": state,
                        "repairable": category(entry) == "generated" and card != base})
    # Freshness is explicit manifest metadata, never an inferred filename link.
    by_path = {r["path"]: r for r in records}
    track_paths = {str(t["id"]): t["path"] for t in manifest.get("tracks", [])}
    for entry, record in zip(manifest["files"], records):
        stale = []
        if category(entry) == "generated":
            original = by_path.get(track_paths.get(str(entry.get("track_id"))))
            if entry.get("source_sha256") and original:
                current = (original["source"]["version"] if original["source"]["status"] == "present"
                           and original["source"]["version"] != original["pc_baseline"] else original["card"])
                if current and current["sha256"] != entry["source_sha256"]:
                    stale.append("source_version")
            expected = (expected_recipes or {}).get(entry.get("kind"))
            if expected is not None and entry.get("recipe_id", entry.get("recipe")) != expected:
                stale.append("recipe_version")
        record["stale_reasons"] = stale
        record["repairable"] = record["repairable"] or bool(stale)
    unknown = [{"path": path, **value} for path, value in observed.items() if path not in known]
    candidates = []
    if find_relinks:
        for identity, source in sources.items():
            if source["status"] != "connected":
                continue
            listing = walk_versions(source["path"])
            bound = {entry.get("source_relative") for entry in manifest["files"] if entry.get("root_id") == identity}
            by_content = {}
            for path, value in listing.items():
                if path not in bound:
                    by_content.setdefault((value["size"], value["sha256"]), []).append(path)
            for record in records:
                if record["source"].get("root_id") != identity or record["source"]["status"] != "missing":
                    continue
                matches = by_content.get((record["baseline"]["size"], record["baseline"]["sha256"]), [])
                if matches:
                    candidates.append({"managed_id": record["managed_id"], "side": "pc", "root_id": identity,
                                       "paths": matches, "basis": "exact_content", "automatic": False})
        unknown_by_content = {}
        for entry in unknown:
            unknown_by_content.setdefault((entry["size"], entry["sha256"]), []).append(entry["path"])
        for record in records:
            if record["card"] is None:
                matches = unknown_by_content.get((record["baseline"]["size"], record["baseline"]["sha256"]), [])
                if matches:
                    candidates.append({"managed_id": record["managed_id"], "side": "card", "paths": matches,
                                       "basis": "exact_content", "automatic": False})
    return {"schema": "xm5.library-health", "version": 1, "root": str(root),
            "build_id": manifest["build_id"], "manifest": manifest,
            "manifest_version": version(checked_path(root, MANIFEST)),
            "checksum_version": version(checked_path(root, CHECKSUM)),
            "sources": sources, "records": records, "unknown_files": unknown,
            "relink_candidates": candidates,
            "issues": [{"managed_id": r["managed_id"], "path": r["path"],
                        "code": "generated_stale" if r["stale_reasons"] else
                        "generated_missing_or_corrupt" if r["repairable"] else r["state"]}
                       for r in records if r["state"] != "unchanged" or r["repairable"]]}


def plan_repair(card_root, source_roots=None, *, generator, choices=None, expected_recipes=None):
    """Prepare missing/corrupt generated files using caller-retained inputs.

    generator(entry, report) returns bytes or {data: bytes, inputs: [...]}.
    No fallback bytes or synthetic user state are manufactured here. Inputs are
    explicit {root_id, relative} dependencies rehashed by the sync planner.
    """
    from .differential_sync import plan_sync
    report = inspect_library(card_root, source_roots, expected_recipes=expected_recipes)
    by_path = {entry["path"]: entry for entry in report["manifest"]["files"]}
    frozen_inputs = {(r["source"]["root_id"], r["source"]["relative"]): r["source"]["version"]
                     for r in report["records"] if r["source"]["status"] in {"present", "missing"}}
    updates = []
    for record in report["records"]:
        if not record["repairable"]:
            continue
        entry = by_path[record["path"]]
        generated = generator(dict(entry), report)
        generated = {"data": generated} if isinstance(generated, bytes) else generated
        if not isinstance(generated, dict) or not isinstance(generated.get("data"), bytes):
            raise RestoreError("repair_unavailable", "Generator did not supply retained-input output",
                               {"path": record["path"]})
        generated = dict(generated)
        dependencies = []
        for descriptor in generated.get("inputs", []):
            descriptor = dict(descriptor)
            key = (descriptor.get("root_id"), descriptor.get("relative"))
            if key in frozen_inputs:
                descriptor["version"] = frozen_inputs[key]
            elif "version" not in descriptor:
                raise RestoreError("unfrozen_input", "Extra repair inputs require their exact pre-generation version")
            dependencies.append(descriptor)
        generated["inputs"] = dependencies
        updates.append({"path": record["path"], "managed_id": record["managed_id"],
                        "kind": entry.get("kind"), **generated})
    plan = plan_sync(card_root, source_roots, updates=updates, choices=choices, repair_only=True)
    if plan["sources"] != report["sources"]:
        raise RestoreError("source_changed", "Repair inputs changed during generation")
    # Bound source observations in the original report predate every generator.
    if sha(base64.b64decode(plan["baseline_manifest_b64"])) != report["manifest_version"]["sha256"]:
        raise RestoreError("destination_changed", "Manifest changed during repair generation")
    before = {record["path"]: record["card"] for record in report["records"]}
    after = {record["path"]: record["version"] for record in plan["preconditions"]}
    if before != after:
        raise RestoreError("destination_changed", "Managed files changed during repair generation")
    for dependency in plan["source_checks"]:
        key = (dependency["root_id"], dependency["relative"])
        if key in frozen_inputs and dependency["version"] != frozen_inputs[key]:
            raise RestoreError("source_changed", "Source changed during repair generation")
    return plan
