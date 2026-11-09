"""Bounded native state exchange. These functions only read explicit PC files.

Native IDs are opaque hints. Paths are never normalized into a PC source path.
Unknown JSON members and unresolved occurrences remain in the parsed payload.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
import uuid

MAGIC = b"XM5STA1\0"
HEADER_BYTES = 128
MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_ID = (1 << 63) - 1


class DeviceStateError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fail(code, message):
    raise DeviceStateError(code, message)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _integer(value, label):
    if type(value) is not int or not 0 <= value <= MAX_ID:
        fail("STATE_INTEGER", label + " must be a nonnegative bounded integer")


def _text(value, label, maximum):
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum or "\0" in value:
        fail("STATE_TEXT", label + " must be bounded UTF-8 text without NUL")


def path_is_tf(path):
    """Only exact relative TF paths may match the manifest; others are retained."""
    return (isinstance(path, str) and path and not path.startswith("/") and "\\" not in path
            and ":" not in path and all(part not in ("", ".", "..") for part in path.split("/")))


def validate_payload(payload):
    def bounded(value, depth=0):
        if depth > 64:
            fail("STATE_LIMIT", "JSON nesting exceeds 64")
        if type(value) is int and not -MAX_ID <= value <= MAX_ID:
            fail("STATE_INTEGER", "JSON integer exceeds signed 63-bit magnitude")
        if isinstance(value, float) and not math.isfinite(value):
            fail("STATE_JSON", "JSON numbers must be finite")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str): fail("STATE_JSON", "JSON member names must be text")
                bounded(child, depth + 1)
        elif isinstance(value, list):
            for child in value: bounded(child, depth + 1)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            fail("STATE_JSON", "Snapshot values must be JSON data")
    bounded(payload)
    if not isinstance(payload, dict) or payload.get("schema") != "xm5.device-state" or payload.get("version") != 1:
        fail("STATE_SCHEMA", "Unsupported device-state schema")
    if type(payload.get("complete")) is not bool:
        fail("STATE_SCHEMA", "complete must be boolean")
    favorites, playlists = payload.get("favorites"), payload.get("playlists")
    if not isinstance(favorites, list) or not isinstance(playlists, list) or len(playlists) > 10_000:
        fail("STATE_LIMIT", "Favorites/playlists must be bounded arrays")
    count = len(favorites)
    def entry(item):
        if not isinstance(item, dict):
            fail("STATE_ENTRY", "Track entries must be objects")
        _text(item.get("path"), "path", 4096)
        _integer(item.get("native_track_id"), "native_track_id")
    for item in favorites:
        entry(item)
    ids = set()
    for playlist in playlists:
        if not isinstance(playlist, dict):
            fail("STATE_PLAYLIST", "Playlist entries must be objects")
        identity = playlist.get("native_id")
        _integer(identity, "native_id")
        if identity in ids:
            fail("STATE_DUPLICATE_ID", "Native playlist IDs must be distinct")
        ids.add(identity)
        _text(playlist.get("name"), "name", 1024)
        for optional in ("pc_id", "source_export_name"):
            if optional in playlist:
                _text(playlist[optional], optional, 1024)
        if not playlist["name"] or any(c in playlist["name"] for c in "\r\n"):
            fail("STATE_TEXT", "Playlist name must be nonempty single-line text")
        if not isinstance(playlist.get("entries"), list):
            fail("STATE_PLAYLIST", "Playlist entries must be an array")
        count += len(playlist["entries"])
        if count > MAX_ENTRIES:
            fail("STATE_LIMIT", "State exceeds 100,000 occurrences")
        for item in playlist["entries"]:
            entry(item)
    if count > MAX_ENTRIES:
        fail("STATE_LIMIT", "State exceeds 100,000 occurrences")
    return payload


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("STATE_JSON", "Duplicate JSON member")
        result[key] = value
    return result


def parse_snapshot(raw):
    raw = bytes(raw)
    if not HEADER_BYTES <= len(raw) <= MAX_BYTES:
        fail("STATE_SIZE", "Snapshot is truncated or exceeds 16 MiB")
    if raw[:8] != MAGIC:
        fail("STATE_MAGIC", "Not an XM5 state snapshot")
    version, header, total, sequence = struct.unpack_from("<IIQQ", raw, 8)
    payload_size, flags = struct.unpack_from("<QI", raw, 80)
    if version != 1 or header != HEADER_BYTES or total != len(raw) or payload_size != len(raw) - HEADER_BYTES:
        fail("STATE_HEADER", "Snapshot header lengths/version disagree")
    if flags or any(raw[92:128]) or sequence > MAX_ID:
        fail("STATE_HEADER", "Unsupported flags/reserved bytes/sequence")
    payload_raw = raw[HEADER_BYTES:]
    if hashlib.sha256(payload_raw).digest() != raw[48:80]:
        fail("STATE_DIGEST", "Snapshot payload digest differs")
    try:
        payload = json.loads(payload_raw.decode("utf-8"), object_pairs_hook=_pairs,
                             parse_constant=lambda value: fail("STATE_JSON", "Nonfinite JSON value"))
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        fail("STATE_JSON", "Invalid UTF-8 JSON: " + str(exc))
    validate_payload(payload)
    return {"schema": "xm5.snapshot", "version": version, "build_id": raw[32:48].hex(),
            "sequence": sequence, "sha256": hashlib.sha256(raw).hexdigest(),
            "payload_sha256": raw[48:80].hex(), "bytes": len(raw), "payload": payload}


def build_snapshot(payload, *, build_id, sequence):
    """Fixture/reference writer; it never opens a path or operates a device."""
    validate_payload(payload)
    _integer(sequence, "sequence")
    try:
        identity = uuid.UUID(str(build_id)).bytes
    except (ValueError, AttributeError) as exc:
        fail("STATE_GENERATION", "build_id must be a UUID")
    body = json_bytes(payload)
    if len(body) + HEADER_BYTES > MAX_BYTES:
        fail("STATE_LIMIT", "Snapshot exceeds 16 MiB")
    header = bytearray(HEADER_BYTES)
    header[:8] = MAGIC
    struct.pack_into("<IIQQ", header, 8, 1, HEADER_BYTES, HEADER_BYTES + len(body), sequence)
    header[32:48], header[48:80] = identity, hashlib.sha256(body).digest()
    struct.pack_into("<Q", header, 80, len(body))
    return bytes(header) + body


def manifest_state(plan, *, card_id=None):
    """Capture both export-side baselines and the exact constrained name seam."""
    return {"schema": "xm5.state-baseline", "version": 1,
            "card_id": card_id or uuid.uuid4().hex, "build_id": plan["build_id"],
            "favorites": sorted(set(plan["favorites"])),
            "favorites_native_applied": False, "native_import_required": True,
            "playlists": [{"pc_id": item["id"], "name": item["name"],
                           "exported_name": Path(item["path"]).stem,
                           "track_ids": item["tracks"], "revision": item["base_snapshot"].get("revision")}
                          for item in plan["playlists"]]}


def compile_playlist_bindings(descriptor):
    """Prepared exact-name index; absent native IDs use UINT64_MAX."""
    records, pool, interned = bytearray(), bytearray(), {}
    def text(value):
        if value not in interned:
            raw = value.encode("utf-8")
            interned[value] = len(pool), len(raw)
            pool.extend(raw)
        return interned[value]
    rows = sorted(descriptor["playlists"], key=lambda row: row["pc_id"])
    if len(rows) > 10_000 or len({row["pc_id"] for row in rows}) != len(rows):
        fail("STATE_BINDINGS", "Playlist binding IDs must be distinct and bounded")
    for row in rows:
        native_id = row.get("native_id", (1 << 64) - 1)
        if native_id != (1 << 64) - 1:
            _integer(native_id, "native_id")
        records.extend(struct.pack("<QIIIIII", native_id, *text(row["pc_id"]), *text(row["exported_name"]), *text(row["name"])))
    body = records + pool
    header = bytearray(128)
    header[:8] = b"XM5PLB1\0"
    struct.pack_into("<IIQII", header, 8, 1, 128, 128 + len(body), len(rows), 32)
    header[32:48] = uuid.UUID(descriptor["build_id"]).bytes
    header[48:80] = hashlib.sha256(body).digest()
    struct.pack_into("<I", header, 80, len(pool))
    if len(header) + len(body) > MAX_BYTES:
        fail("STATE_LIMIT", "Playlist binding index exceeds 16 MiB")
    return bytes(header) + body


def parse_playlist_bindings(raw):
    raw = bytes(raw)
    if not 128 <= len(raw) <= MAX_BYTES or raw[:8] != b"XM5PLB1\0":
        fail("STATE_BINDINGS", "Invalid playlist binding index")
    version, header, total, count, stride = struct.unpack_from("<IIQII", raw, 8)
    pool_size = struct.unpack_from("<I", raw, 80)[0]
    if (version != 1 or header != 128 or total != len(raw) or stride != 32 or count > 10_000
            or 128 + count * stride + pool_size != len(raw) or any(raw[84:128])
            or hashlib.sha256(raw[128:]).digest() != raw[48:80]):
        fail("STATE_BINDINGS", "Playlist binding index validation failed")
    pool = raw[128 + count * stride:]
    def text(offset, length):
        if offset + length > len(pool): fail("STATE_BINDINGS", "Binding string exceeds pool")
        try: value = pool[offset:offset + length].decode("utf-8")
        except UnicodeError: fail("STATE_BINDINGS", "Invalid binding UTF-8")
        _text(value, "binding", 1024)
        return value
    rows = []
    for position in range(count):
        native, p, pl, e, el, d, dl = struct.unpack_from("<QIIIIII", raw, 128 + position * stride)
        row = {"pc_id": text(p, pl), "exported_name": text(e, el), "name": text(d, dl)}
        if native != (1 << 64) - 1:
            _integer(native, "native_id")
            row["native_id"] = native
        rows.append(row)
    if len({row["pc_id"] for row in rows}) != len(rows): fail("STATE_BINDINGS", "Duplicate binding PC ID")
    return {"build_id": raw[32:48].hex(), "playlists": rows}


def resolve_entries(entries, manifest):
    paths = {"xm5/" + item["path"]: item["id"] for item in manifest["tracks"]}
    resolved, unresolved = [], []
    for position, entry in enumerate(entries):
        identity = paths.get(entry["path"]) if path_is_tf(entry["path"]) else None
        if identity is None:
            unresolved.append({"position": position, "entry": entry,
                               "reason": "unmapped_path" if path_is_tf(entry["path"]) else "non_tf_path"})
        else:
            resolved.append({"position": position, "track_id": identity, "entry": entry})
    return resolved, unresolved


def acknowledge_export_inspection(root, inspection, acknowledgement):
    """Admit only receipt-bound exact state bytes for a full container rebuild.

    This does not authorize deletion or a generation guess. The exporter retains
    the entire old tree in its normal recoverable backup before activation.
    """
    from .restore_card import load_manifest, no_links, detect_layout, file_digest
    root=no_links(root)
    if not isinstance(acknowledgement,dict) or acknowledgement.get("schema")!="xm5.device-state-export-ack" or acknowledgement.get("version")!=1:
        fail("STATE_ACK", "An exact acknowledged device-state receipt is required")
    if str(root.resolve())!=str(Path(acknowledgement.get("root","")).resolve()):
        fail("STATE_ACK", "State acknowledgement belongs to another export root")
    manifest=load_manifest(root)
    if (acknowledgement.get("build_id")!=manifest["build_id"] or acknowledgement.get("card_id")!=manifest.get("device_state",{}).get("card_id")
            or acknowledgement.get("manifest_sha256")!=file_digest(root/detect_layout(root)["manifest"])[1]):
        fail("STATE_ACK", "State acknowledgement belongs to another manifest generation")
    if no_links(root/"Struct/device/state.pending").exists():
        fail("STATE_PENDING", "Pending native export cannot be acknowledged for generation replacement")
    allowed={"Struct/device/state.xms":MAX_BYTES,"Struct/device/playlists.bind":MAX_BYTES,"Struct/device/import.status":4096}
    files=acknowledgement.get("files")
    if not isinstance(files,list) or not files or len(files)>3:
        fail("STATE_ACK", "Acknowledgement must name the exact bounded state files")
    accepted={}
    for item in files:
        relative=item.get("path") if isinstance(item,dict) else None
        if relative not in allowed or relative in accepted:
            fail("STATE_ACK", "Acknowledgement contains an unknown/duplicate device path")
        size,sha=file_digest(no_links(root/relative))
        if size>allowed[relative] or size!=item.get("size") or sha!=item.get("sha256"):
            fail("STATE_ACK", "Device-state bytes changed after acknowledgement")
        accepted[relative]=sha
    if "Struct/device/state.xms" not in accepted or accepted["Struct/device/state.xms"]!=acknowledgement.get("snapshot_sha256"):
        fail("STATE_ACK", "Acknowledgement must bind the accepted full snapshot")
    snapshot=parse_snapshot((root/"Struct/device/state.xms").read_bytes())
    if not snapshot["payload"]["complete"] or snapshot["build_id"]!=uuid.UUID(manifest["build_id"]).hex:
        fail("STATE_ACK", "Accepted snapshot is incomplete or belongs to another generation")
    result=dict(inspection)
    result["unknown_files"]=[path for path in result["unknown_files"] if path not in accepted]
    result["acknowledged_device_state"]={"receipt_id":acknowledgement.get("receipt_id"),"files":accepted,"full_tree_backup_required":True}
    result["clean"]=bool(result.get("recognized") and result.get("version")==2 and result.get("verification",{}).get("ok")
                         and not result["unknown_files"] and not result["unknown_directories"] and not result["drifted_files"])
    return result
