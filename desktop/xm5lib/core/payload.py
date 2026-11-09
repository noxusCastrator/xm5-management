"""Hash the audio payload of a file, excluding tag/metadata containers.

Used to prove that writing managed tags into an export copy left the audio bytes untouched.
Supported: FLAC (frames after the metadata blocks), MP3 (between ID3v2 / APEv2 / ID3v1),
MP4/M4A (concatenated ``mdat`` contents). Other formats are only ever copied byte-for-byte.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

CHUNK = 1 << 20


def _hash_range(f, start: int, end: int) -> str:
    h = hashlib.sha256()
    f.seek(start)
    left = end - start
    while left > 0:
        b = f.read(min(CHUNK, left))
        if not b:
            break
        h.update(b)
        left -= len(b)
    return h.hexdigest()


def _id3v2_size(head: bytes) -> int:
    if len(head) >= 10 and head[:3] == b"ID3":
        size = (head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F)
        return 10 + size + (10 if head[5] & 0x10 else 0)
    return 0


def flac_payload(path: Path) -> str:
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(0)
        pos = _id3v2_size(f.read(10))
        f.seek(pos)
        if f.read(4) != b"fLaC":
            raise ValueError("not a FLAC stream")
        pos += 4
        while True:
            f.seek(pos)
            hdr = f.read(4)
            if len(hdr) < 4:
                raise ValueError("truncated FLAC metadata")
            last = hdr[0] & 0x80
            pos += 4 + int.from_bytes(hdr[1:4], "big")
            if last:
                break
        return _hash_range(f, pos, size)


def mp3_payload(path: Path) -> str:
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(0)
        start = _id3v2_size(f.read(10))
        end = size
        if end - start >= 128:
            f.seek(end - 128)
            if f.read(3) == b"TAG":
                end -= 128
        if end - start >= 32:
            f.seek(end - 32)
            footer = f.read(32)
            if footer[:8] == b"APETAGEX":
                tag_size = struct.unpack("<I", footer[12:16])[0]
                has_header = struct.unpack("<I", footer[20:24])[0] & 0x80000000
                end -= tag_size + (32 if has_header else 0)
        return _hash_range(f, start, end)


def mp4_payload(path: Path) -> str:
    h = hashlib.sha256()
    found = False
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        pos = 0
        while pos + 8 <= size:
            f.seek(pos)
            n, kind = struct.unpack(">I4s", f.read(8))
            header = 8
            if n == 1:
                n = struct.unpack(">Q", f.read(8))[0]
                header = 16
            elif n == 0:
                n = size - pos
            if n < header:
                raise ValueError("corrupt MP4 atom")
            if kind == b"mdat":
                found = True
                f.seek(pos + header)
                left = n - header
                while left > 0:
                    b = f.read(min(CHUNK, left))
                    if not b:
                        break
                    h.update(b)
                    left -= len(b)
            pos += n
    if not found:
        raise ValueError("no mdat atom")
    return h.hexdigest()


def payload_hash(path: Path) -> str | None:
    ext = Path(path).suffix.lower()
    if ext == ".flac":
        return flac_payload(path)
    if ext == ".mp3":
        return mp3_payload(path)
    if ext in (".m4a", ".mp4", ".alac"):
        return mp4_payload(path)
    return None
