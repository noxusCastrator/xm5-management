"""Bounded deterministic XMI v1 files and native information lookup indexes.

This module is pure: importing it never opens a profile, starts a process or
accesses a device. Information files contain derived data, never audio.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import struct
import uuid

MAGIC = b"XM5INF1\0"
INDEX_MAGIC = b"XM5IXI1\0"
ASSET_INDEX_MAGIC = b"XM5IAC1\0"
HEADER_BYTES = 256
DIRECTORY_BYTES = 64
MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 16
MAX_RECORDS = 1_000_000
INDEX_HEADER_BYTES = 128
INDEX_RECORD_BYTES = 96
NATIVE_SIDES = (46, 48, 80, 90, 100, 150, 160, 180, 330)
ROLE_IDS = {"directory": 1, "playback": 2, "playlist": 3}
TRACK_NAMESPACE = uuid.UUID("355246df-a786-5673-965b-1c82677894cb")
AUX_NAMESPACE = uuid.UUID("9968f305-42ec-5dd0-919b-889ac53a4b24")


class InfoError(ValueError):
    """A malformed or unsupported information artifact."""


@dataclass(frozen=True)
class Entry:
    kind: int
    key: int
    offset: int
    length: int
    sha256: str
    data: bytes


@dataclass(frozen=True)
class InfoFile:
    info_id: str
    source_sha256: str
    source_size: int
    source_path: str
    peak: float | None
    midpoint_dbfs: float
    correction_limit_db: float
    gain_db: float
    flags: int
    entries: tuple[Entry, ...]
    sha256: str

    def payload(self, kind, key=0):
        return next((entry.data for entry in self.entries if (entry.kind, entry.key) == (kind, key)), None)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(data):
    return hashlib.sha256(data).digest()


def _uuid(value):
    try:
        result = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InfoError("Invalid UUID") from exc
    return result


def info_id(stable_identity, *, role="playback"):
    if not isinstance(stable_identity, str) or not stable_identity:
        raise InfoError("A stable subject identity is required")
    if role not in ROLE_IDS:
        raise InfoError("Unknown information role")
    return uuid.uuid5(TRACK_NAMESPACE if role == "playback" else AUX_NAMESPACE,
                      role + "\0" + stable_identity).hex


def info_path(identity):
    return "Struct/info/" + _uuid(identity).hex + ".xmi"


def _path(value, *, empty=False):
    if not isinstance(value, str) or (not value and not empty):
        raise InfoError("Expected a relative UTF-8 path")
    encoded = value.encode("utf-8")
    if len(encoded) > 1023 or "\\" in value or any(ord(c) < 32 for c in value):
        raise InfoError("Invalid or oversized information path")
    if value and (PurePosixPath(value).is_absolute() or ":" in value or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise InfoError("Unsafe information path")
    return encoded


def lookup_key(role, subject):
    if role not in ROLE_IDS or not isinstance(subject, str) or not subject or any(ord(c) < 32 for c in subject):
        raise InfoError("Invalid lookup role/subject")
    encoded = subject.encode("utf-8")
    if len(encoded) > 1023:
        raise InfoError("Lookup subject exceeds 1023 UTF-8 bytes")
    if role != "playlist":
        _path(subject)
        if not subject.startswith("xm5/"):
            raise InfoError("Lookup paths must include the xm5/ prefix")
    return _digest(role.encode("ascii") + b"\0" + encoded)


def _options(midpoint, limit):
    if isinstance(midpoint, bool) or isinstance(limit, bool):
        raise InfoError("Peak correction options must be finite numbers")
    if not math.isfinite(midpoint) or not -60 <= midpoint <= 0 or not math.isfinite(limit) or not 0 <= limit <= 12:
        raise InfoError("Peak correction options outside midpoint -60..0 / limit 0..12")


def calculate_gain(peak, midpoint_dbfs=0.0, correction_limit_db=4.0, enabled=True):
    """Gain based only on original maximum absolute decoded sample amplitude."""
    _options(midpoint_dbfs, correction_limit_db)
    if type(enabled) is not bool:
        raise InfoError("Peak correction enabled must be boolean")
    if peak is not None and (isinstance(peak, bool) or not math.isfinite(peak) or peak < 0):
        raise InfoError("Peak must be a finite nonnegative amplitude or None")
    if not enabled or peak is None or peak == 0:
        return 0.0
    return max(-correction_limit_db, min(correction_limit_db, midpoint_dbfs - 20 * math.log10(peak)))


def _bmp(data, side):
    if (len(data) != 64 + side * side * 4 or data[:2] != b"BM"
            or struct.unpack_from("<I", data, 2)[0] != len(data) or data[6:10] != b"\0" * 4
            or struct.unpack_from("<I", data, 10)[0] != 64
            or struct.unpack_from("<IiiHHIIiiII", data, 14) != (40, side, -side, 1, 32, 0, side * side * 4, 2835, 2835, 0, 0)
            or data[54:64] != b"\0" * 10 or data[67::4] != b"\xff" * (side * side)):
        raise InfoError("Artwork must be an exact native opaque BGRA32 BMP")


def _payload(kind, key, data):
    if not isinstance(data, bytes) or not data:
        raise InfoError("Information payload must be nonempty bytes")
    if kind == 1:
        if key not in NATIVE_SIDES:
            raise InfoError("Unsupported bitmap side")
        _bmp(data, key)
    elif kind in (2, 3, 4) and key == 0:
        if kind in (2, 3):
            try:
                text = data.decode("utf-8")
                if "\0" in text:
                    raise InfoError("NUL in text payload")
                if kind == 3 and not isinstance(json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(InfoError("Nonfinite JSON"))), dict):
                    raise InfoError("Metadata must be a JSON object")
            except (UnicodeError, ValueError, RecursionError) as exc:
                raise InfoError("Invalid UTF-8/JSON payload") from exc
    else:
        raise InfoError("Unknown information payload kind/key")


def build_info(*, identity, source_sha256, source_size, source_path, peak=None,
               midpoint_dbfs=0.0, correction_limit_db=4.0, peak_correction_enabled=True,
               bitmaps=None, normalized_lyrics=None, original_lyrics=None, metadata=None):
    """Build exact frozen v1 bytes. Equal inputs produce equal bytes."""
    path = _path(source_path, empty=True)
    ident = _uuid(identity).bytes
    try:
        source_hash = bytes.fromhex(source_sha256)
    except (TypeError, ValueError) as exc:
        raise InfoError("Invalid source SHA-256") from exc
    if len(source_hash) != 32 or type(source_size) is not int or not 0 <= source_size < 2 ** 64:
        raise InfoError("Invalid source identity/size")
    gain = calculate_gain(peak, midpoint_dbfs, correction_limit_db, peak_correction_enabled)
    # Canonicalize options to the precision actually stored before computing gain.
    midpoint_dbfs, correction_limit_db = struct.unpack("<ff", struct.pack("<ff", midpoint_dbfs, correction_limit_db))
    gain = calculate_gain(peak, midpoint_dbfs, correction_limit_db, peak_correction_enabled)
    payloads = [(1, side, data) for side, data in (bitmaps or {}).items()]
    if bitmaps and set(bitmaps) != set(NATIVE_SIDES):
        raise InfoError("Artwork must include all nine native sizes")
    if normalized_lyrics is not None:
        payloads.append((2, 0, normalized_lyrics))
    if metadata is not None:
        payloads.append((3, 0, canonical_json(metadata)))
    if original_lyrics is not None:
        payloads.append((4, 0, original_lyrics))
    payloads.sort(key=lambda value: value[:2])
    if len(payloads) > MAX_ENTRIES:
        raise InfoError("Too many information entries")
    for kind, key, data in payloads:
        _payload(kind, key, data)
    path_offset = HEADER_BYTES + DIRECTORY_BYTES * len(payloads)
    payload_offset = (path_offset + len(path) + 63) & ~63
    body = bytearray(path + b"\0" * (payload_offset - path_offset - len(path)))
    directory = bytearray()
    offset = payload_offset
    for kind, key, data in payloads:
        if offset + len(data) > MAX_BYTES:
            raise InfoError("Information file exceeds 16 MiB")
        directory.extend(struct.pack("<IIQQ32s8s", kind, key, offset, len(data), _digest(data), b"\0" * 8))
        body.extend(data)
        padding = (-len(data)) % 64
        body.extend(b"\0" * padding)
        offset += len(data) + padding
    total = path_offset + len(body)
    if total > MAX_BYTES:
        raise InfoError("Information file exceeds 16 MiB")
    flags = (1 if peak is not None else 0) | (2 if peak is not None and peak_correction_enabled else 0)
    flags |= 4 if normalized_lyrics is not None or original_lyrics is not None else 0
    flags |= 8 if bitmaps else 0
    header = bytearray(HEADER_BYTES)
    struct.pack_into("<8sIIQII16s32sQdfffIQQ", header, 0, MAGIC, 1, HEADER_BYTES, total, flags,
                     len(payloads), ident, source_hash, source_size, peak if peak is not None else 0.0,
                     midpoint_dbfs, correction_limit_db, gain, len(path), path_offset, HEADER_BYTES)
    header[128:160] = _digest(path)
    header[160:192] = _digest(header + directory + body[:payload_offset - path_offset])
    result = bytes(header + directory + body)
    parse_info(result)
    return result


def _read(value, maximum):
    if isinstance(value, (str, Path)):
        with Path(value).open("rb") as stream:
            data = stream.read(maximum + 1)
    elif isinstance(value, bytes):
        data = value
    else:
        raise InfoError("Expected bytes or a local path")
    if len(data) > maximum:
        raise InfoError("Artifact exceeds format bound")
    return data


def parse_info(value):
    """Validate before exposing paths or payloads; any corrupt input raises InfoError."""
    data = _read(value, MAX_BYTES)
    if len(data) < HEADER_BYTES:
        raise InfoError("Truncated information header")
    magic, version, header, total, flags, count = struct.unpack_from("<8sIIQII", data)
    if magic != MAGIC or version != 1 or header != HEADER_BYTES or total != len(data) or flags & ~15 or count > MAX_ENTRIES:
        raise InfoError("Invalid information header")
    if data[192:256] != b"\0" * 64:
        raise InfoError("Nonzero reserved information header")
    size, peak, midpoint, limit, gain, path_length, path_offset, directory_offset = struct.unpack_from("<QdfffIQQ", data, 80)
    _options(midpoint, limit)
    if not all(math.isfinite(v) for v in (peak, gain)) or peak < 0 or not flags & 1 and peak != 0:
        raise InfoError("Invalid peak/gain")
    if flags & 2 and not flags & 1 or (not flags & 2 or peak == 0) and gain != 0 or abs(gain) > limit + 1e-5:
        raise InfoError("Inconsistent peak correction flags/gain")
    if flags & 2 and abs(gain - calculate_gain(peak, midpoint, limit)) > 2e-5:
        raise InfoError("Gain does not match source peak/options")
    path_end = path_offset + path_length
    first_payload = (path_end + 63) & ~63
    if directory_offset != HEADER_BYTES or path_offset != HEADER_BYTES + count * DIRECTORY_BYTES or path_length > 1023 or first_payload > len(data):
        raise InfoError("Invalid directory/path bounds")
    raw_path = data[path_offset:path_end]
    try:
        source_path = raw_path.decode("utf-8")
    except UnicodeError as exc:
        raise InfoError("Invalid information path encoding") from exc
    _path(source_path, empty=True)
    if _digest(raw_path) != data[128:160] or any(data[path_end:first_payload]):
        raise InfoError("Path checksum/padding mismatch")
    protected = bytearray(data[:first_payload])
    protected[160:192] = b"\0" * 32
    if _digest(protected) != data[160:192]:
        raise InfoError("Information directory checksum mismatch")
    entries, seen, ranges = [], set(), []
    for number in range(count):
        kind, key, offset, length, digest, reserved = struct.unpack_from("<IIQQ32s8s", data, HEADER_BYTES + number * DIRECTORY_BYTES)
        if reserved != b"\0" * 8 or (kind, key) in seen or offset % 64 or offset < first_payload or length == 0 or offset + length > len(data):
            raise InfoError("Invalid information entry bounds/identity")
        seen.add((kind, key))
        blob = data[offset:offset + length]
        if _digest(blob) != digest:
            raise InfoError("Information payload checksum mismatch")
        _payload(kind, key, blob)
        ranges.append((offset, offset + length))
        entries.append(Entry(kind, key, offset, length, digest.hex(), blob))
    ranges.sort()
    cursor = first_payload
    for start, end in ranges:
        if start < cursor or any(data[cursor:start]):
            raise InfoError("Overlapping information payloads/nonzero padding")
        cursor = end
    if any(data[cursor:]):
        raise InfoError("Nonzero trailing information padding")
    sides = {key for kind, key in seen if kind == 1}
    if sides and sides != set(NATIVE_SIDES) or bool(flags & 8) != bool(sides) or bool(flags & 4) != any(k in (2, 4) for k, _ in seen):
        raise InfoError("Information cover/lyric flags mismatch")
    return InfoFile(uuid.UUID(bytes=data[32:48]).hex, data[48:80].hex(), size, source_path,
                    peak if flags & 1 else None, midpoint, limit, gain, flags, tuple(entries), _digest(data).hex())


def compile_index(rows, *, build_id):
    """Rows: role, subject, info_id, sha256 (full exact XMI digest)."""
    records, seen = [], set()
    for number, row in enumerate(rows):
        if number >= MAX_RECORDS:
            raise InfoError("Information index exceeds one million records")
        key, role = lookup_key(row["role"], row["subject"]), ROLE_IDS[row["role"]]
        if (key, role) in seen:
            raise InfoError("Duplicate information lookup")
        seen.add((key, role))
        try:
            digest = bytes.fromhex(row["sha256"])
        except (TypeError, ValueError) as exc:
            raise InfoError("Invalid information checksum") from exc
        if len(digest) != 32:
            raise InfoError("Invalid information checksum")
        records.append((key, role, struct.pack("<II32s16s32s8s", role, 0, key, _uuid(row["info_id"]).bytes, digest, b"\0" * 8)))
    records.sort(key=lambda item: item[:2])
    body = b"".join(record for _, _, record in records)
    header = bytearray(INDEX_HEADER_BYTES)
    struct.pack_into("<8sIIII16s32sQ", header, 0, INDEX_MAGIC, 1, INDEX_HEADER_BYTES, len(records), INDEX_RECORD_BYTES,
                     _uuid(build_id).bytes, _digest(body), INDEX_HEADER_BYTES + len(body))
    return bytes(header) + body


def parse_index(value):
    data = _read(value, INDEX_HEADER_BYTES + MAX_RECORDS * INDEX_RECORD_BYTES)
    if len(data) < INDEX_HEADER_BYTES:
        raise InfoError("Truncated information index")
    magic, version, header, count, record_bytes = struct.unpack_from("<8sIIII", data)
    if (magic != INDEX_MAGIC or version != 1 or header != INDEX_HEADER_BYTES or count > MAX_RECORDS or record_bytes != INDEX_RECORD_BYTES
            or len(data) != INDEX_HEADER_BYTES + count * INDEX_RECORD_BYTES or struct.unpack_from("<Q", data, 72)[0] != len(data)
            or any(data[80:128]) or _digest(data[128:]) != data[40:72]):
        raise InfoError("Invalid information index header/checksum")
    rows, previous = [], None
    for number in range(count):
        role, flags, key, ident, digest, reserved = struct.unpack_from("<II32s16s32s8s", data, 128 + number * 96)
        current = (key, role)
        if role not in ROLE_IDS.values() or flags or any(reserved) or previous is not None and current <= previous:
            raise InfoError("Invalid information index record/order")
        previous = current
        rows.append({"role": role, "key": key.hex(), "info_id": uuid.UUID(bytes=ident).hex, "sha256": digest.hex()})
    return {"build_id": uuid.UUID(bytes=data[24:40]).hex, "records": rows, "sha256": _digest(data).hex()}


def compile_asset_index(rows, *, build_id):
    """Content-addressed BMP lookup: side, sha256, info_id, info_sha256.

    Repeated exact BMP digests select the lexically smallest owning UUID. The
    index contains ownership references only, without duplicate image pixels.
    """
    selected = {}
    for row in rows:
        side = row["side"]
        if type(side) is not int or side not in NATIVE_SIDES:
            raise InfoError("Unsupported native asset side")
        try:
            digest, owner_digest = bytes.fromhex(row["sha256"]), bytes.fromhex(row["info_sha256"])
        except (TypeError, ValueError) as exc:
            raise InfoError("Invalid native asset/owner SHA256") from exc
        if len(digest) != 32 or len(owner_digest) != 32:
            raise InfoError("Invalid native asset/owner SHA256")
        owner = _uuid(row["info_id"]).bytes
        key = digest, side
        previous = selected.get(key)
        if previous and previous[0] == owner and previous[1] != owner_digest:
            raise InfoError("Conflicting full XMI digests for one owner")
        if previous is None or owner < previous[0]:
            selected[key] = owner, owner_digest
        if len(selected) > MAX_RECORDS:
            raise InfoError("Asset index exceeds one million records")
    body = b"".join(struct.pack("<II32s16s32s8s", side, 0, digest, owner, owner_digest, b"\0" * 8)
                    for (digest, side), (owner, owner_digest) in sorted(selected.items()))
    header = bytearray(INDEX_HEADER_BYTES)
    struct.pack_into("<8sIIII16s32sQ", header, 0, ASSET_INDEX_MAGIC, 1, INDEX_HEADER_BYTES, len(selected), INDEX_RECORD_BYTES,
                     _uuid(build_id).bytes, _digest(body), INDEX_HEADER_BYTES + len(body))
    return bytes(header) + body


def parse_asset_index(value):
    data = _read(value, INDEX_HEADER_BYTES + MAX_RECORDS * INDEX_RECORD_BYTES)
    if len(data) < INDEX_HEADER_BYTES:
        raise InfoError("Truncated native asset index")
    magic, version, header, count, record_bytes = struct.unpack_from("<8sIIII", data)
    if (magic != ASSET_INDEX_MAGIC or version != 1 or header != INDEX_HEADER_BYTES or count > MAX_RECORDS or record_bytes != INDEX_RECORD_BYTES
            or len(data) != INDEX_HEADER_BYTES + count * INDEX_RECORD_BYTES or struct.unpack_from("<Q", data, 72)[0] != len(data)
            or any(data[80:128]) or _digest(data[128:]) != data[40:72]):
        raise InfoError("Invalid native asset index header/checksum")
    rows, previous = [], None
    for number in range(count):
        side, flags, key, owner, digest, reserved = struct.unpack_from("<II32s16s32s8s", data, 128 + number * 96)
        current = key, side
        if side not in NATIVE_SIDES or flags or any(reserved) or previous is not None and current <= previous:
            raise InfoError("Invalid native asset index record/order")
        previous = current
        rows.append({"side": side, "sha256": key.hex(), "info_id": uuid.UUID(bytes=owner).hex, "info_sha256": digest.hex()})
    return {"build_id": uuid.UUID(bytes=data[24:40]).hex, "records": rows, "sha256": _digest(data).hex()}
