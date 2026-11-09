"""PC compiler for the frozen XM5 native prepared-art index/readiness format.

Audio remains an ordinary original file. This binary contains only artwork lookup
metadata; all image validation and index construction happen on the PC.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import struct
import zlib

from .restore_card import safe_relative

INDEX_MAGIC = b"XM5ART2\0"
READY_MAGIC = b"XM5RDY2\0"
VERSION = 2
HEADER_BYTES = 128
RECORD_BYTES = 112
READY_BYTES = 128
MAX_RECORDS = 1_000_000
MAX_INDEX_BYTES = 256 * 1024 * 1024
FLAGS = 3  # immutable managed asset | directly usable native BGRA32 pixels
SIZE_BITS = {80: 1, 150: 2, 160: 4, 330: 8, 46: 16, 48: 32, 90: 64, 100: 128, 180: 256}
NATIVE_SIDES = tuple(sorted(SIZE_BITS))
SIZES_MASK = sum(SIZE_BITS.values())
ROLE_IDS = {"directory": 1, "playback": 2, "playlist": 3}
ROLE_SIZES = {"directory": (80, 160), "playback": NATIVE_SIDES, "playlist": NATIVE_SIDES}
ASSET_LAYOUT = "sha256-base64url-flat-v1"
ASSET_DIRECTORY = "Struct/covers/native"


@dataclass(frozen=True)
class CompiledIndex:
    index: bytes
    ready: bytes
    record_count: int
    recipe_tag: str


def asset_path_for_digest(digest):
    """Exact flat native filename derived from the actual complete BMP SHA-256."""
    raw = bytes.fromhex(digest) if isinstance(digest, str) else digest
    if not isinstance(raw, bytes) or len(raw) != 32:
        raise ValueError("Artwork SHA-256 must contain 32 bytes")
    basename = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{ASSET_DIRECTORY}/{basename}.bmp"


def lookup_key(role, subject):
    if role not in ROLE_IDS:
        raise ValueError("Unsupported artwork role")
    if not isinstance(subject, str) or not subject or any(ord(c) < 32 for c in subject):
        raise ValueError("Artwork subject must be nonempty and contain no control characters")
    encoded = subject.encode("utf-8")
    if len(encoded) > 1023:
        raise ValueError("Artwork subject exceeds 1023 UTF-8 bytes")
    return hashlib.sha256(role.encode("ascii") + b"\0" + encoded).digest()


def compile_index(rows, *, build_id, recipe, asset_reader):
    """Validate actual native BMP bytes and return an index plus ready descriptor.

    ``rows`` contains the real TSV records as dictionaries. ``asset_reader`` reads
    an exact managed card-relative path. Inputs are never rewritten. Callers must
    publish both outputs before committing their manifest/removing INCOMPLETE.
    """
    if not isinstance(build_id, str) or len(build_id) != 32:
        raise ValueError("Build ID must contain exactly 32 hex digits")
    try:
        build_raw = bytes.fromhex(build_id)
    except ValueError as exc:
        raise ValueError("Invalid build ID") from exc
    if len(build_raw) != 16:
        raise ValueError("Build ID must decode to 16 bytes")
    recipe_raw = json.dumps(recipe, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tag = hashlib.sha256(recipe_raw).digest()[:16]
    rows = list(rows)
    if len(rows) > MAX_RECORDS:
        raise ValueError("Native artwork index exceeds 1000000 records")
    prepared = []
    seen, identities, groups, assets = set(), {}, {}, {}
    for row in rows:
        role, subject, side = row["role"], row["subject"], row["side"]
        key = lookup_key(role, subject)
        if type(side) is not int or side not in (0, *ROLE_SIZES[role]):
            raise ValueError("Unsupported artwork role/side")
        if (key, side) in seen:
            raise ValueError("Duplicate artwork lookup key/side")
        seen.add((key, side))
        identity = (role, subject)
        if key in identities and identities[key] != identity:
            raise ValueError("Artwork key collision between different subjects")
        identities[key] = identity
        groups.setdefault(identity, set()).add(side)
        if side == 0:
            if row["asset"] != "-" or row["sha256"] != "-":
                raise ValueError("No-art rows must use the negative sentinel")
            prepared.append((key, side, role, subject, None, b"\0" * 32, 0))
            continue
        asset = safe_relative(row["asset"])
        if asset not in assets:
            data = asset_reader(asset)
            if not isinstance(data, bytes) or not data or len(data) > MAX_INDEX_BYTES:
                raise ValueError("Invalid encoded artwork size")
            if (len(data) != 64 + side * side * 4 or data[:2] != b"BM"
                    or struct.unpack_from("<I", data, 2)[0] != len(data) or data[6:10] != b"\0" * 4
                    or struct.unpack_from("<I", data, 10)[0] != 64
                    or struct.unpack_from("<IiiHHIIiiII", data, 14) != (40, side, -side, 1, 32, 0, side * side * 4, 2835, 2835, 0, 0)
                    or data[54:64] != b"\0" * 10 or data[67::4] != b"\xff" * (side * side)):
                raise ValueError("Artwork must be an exact-size top-down opaque BGRA32 BMP")
            assets[asset] = (hashlib.sha256(data).digest(), len(data), side)
        asset_sha, encoded_bytes, asset_side = assets[asset]
        if asset_side != side or asset_sha.hex() != row["sha256"]:
            raise ValueError("Artwork checksum or dimensions do not match the row")
        if asset != "xm5/" + asset_path_for_digest(asset_sha):
            raise ValueError("Artwork path must use the exact content-addressed BMP basename")
        prepared.append((key, side, role, subject, asset, asset_sha, encoded_bytes))
    for (role, _), sides in groups.items():
        if sides != {0} and sides != set(ROLE_SIZES[role]):
            raise ValueError("Each artwork subject needs all role sizes or one no-art row")
    prepared.sort(key=lambda item: (item[0], item[1]))
    pool_offset = HEADER_BYTES + len(prepared) * RECORD_BYTES
    pool, locations = bytearray(), {}

    def string_location(value):
        if value not in locations:
            encoded = value.encode("utf-8")
            if pool_offset + len(pool) + len(encoded) > MAX_INDEX_BYTES:
                raise ValueError("Native artwork index exceeds 256 MiB")
            locations[value] = (pool_offset + len(pool), len(encoded))
            pool.extend(encoded)
        return locations[value]

    records = bytearray(len(prepared) * RECORD_BYTES)
    for number, (key, side, role, subject, asset, asset_sha, encoded_bytes) in enumerate(prepared):
        subject_offset, subject_length = string_location(subject)
        asset_offset, asset_length = string_location(asset) if asset is not None else (0, 0)
        struct.pack_into("<32s6I32s4I8s", records, number * RECORD_BYTES,
                         key, side, 0 if side else 1, subject_offset, subject_length, asset_offset, asset_length,
                         asset_sha, side, side, encoded_bytes, ROLE_IDS[role], b"\0" * 8)
    body = bytes(records) + bytes(pool)
    header = bytearray(HEADER_BYTES)
    struct.pack_into("<8s8I", header, 0, INDEX_MAGIC, VERSION, HEADER_BYTES, RECORD_BYTES,
                     len(prepared), HEADER_BYTES, pool_offset, len(pool), HEADER_BYTES + len(body))
    header[40:56], header[56:72] = build_raw, tag
    struct.pack_into("<II", header, 72, FLAGS, SIZES_MASK)
    header[80:112] = hashlib.sha256(body).digest()
    index = bytes(header) + body
    ready = bytearray(READY_BYTES)
    struct.pack_into("<8s3I", ready, 0, READY_MAGIC, VERSION, READY_BYTES, len(index))
    ready[20:52] = hashlib.sha256(index).digest()
    ready[52:68], ready[68:84] = build_raw, tag
    struct.pack_into("<II", ready, 84, SIZES_MASK, FLAGS)
    struct.pack_into("<I", ready, 124, zlib.crc32(ready[:124]) & 0xffffffff)
    return CompiledIndex(index=bytes(index), ready=bytes(ready), record_count=len(prepared), recipe_tag=tag.hex())
