"""Locate and replace XM5 information in selected non-FLAC card copies."""

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import struct
import tempfile
from typing import BinaryIO


MARKER = b"XM5I"
OWNER = b"org.xm5.inline\0"
BOX_ID = bytes.fromhex("fbc24971c0c149d29fecb899b4c8bef2")
MAX_PAYLOAD = 512 * 1024
MAX_TAG = 32 * 1024 * 1024
BUFFER_BYTES = 64 * 1024
OFFSET_FRAMES = frozenset((b"CHAP", b"ASPI", b"SEEK", b"RBUF"))


class Unsupported(ValueError):
    """The container or XM5 record is outside the supported profile."""


@dataclass(frozen=True)
class PayloadSpan:
    offset: int
    size: int


@dataclass(frozen=True)
class Edit:
    header: bytes
    start: int
    stop: int
    record: bytes


@dataclass(frozen=True)
class ChunkLayout:
    payload: PayloadSpan | None
    chunk_start: int
    chunk_end: int
    aiff: bool


@dataclass(frozen=True)
class Mp3Layout:
    version: int
    tag_end: int
    payload: PayloadSpan | None
    frame_start: int
    frame_end: int


@dataclass(frozen=True)
class Mp4Layout:
    payload: PayloadSpan | None
    box_start: int
    box_end: int


def _read_at(stream: BinaryIO, offset: int, size: int) -> bytes:
    stream.seek(offset)
    value = stream.read(size)
    if len(value) != size:
        raise Unsupported("Truncated container")
    return value


def _checked_payload(stream: BinaryIO, offset: int, size: int) -> PayloadSpan:
    if not 4 <= size <= MAX_PAYLOAD or _read_at(stream, offset, 4) != MARKER:
        raise Unsupported("Invalid XM5 payload")
    return PayloadSpan(offset, size)


def _chunk_layout(stream: BinaryIO, size: int, aiff: bool) -> ChunkLayout:
    endian = ">" if aiff else "<"
    signature, declared_size, form = struct.unpack(endian + "4sI4s", _read_at(stream, 0, 12))
    expected = (b"FORM", b"AIFF") if aiff else (b"RIFF", b"WAVE")
    if (signature, form) != expected or declared_size != size - 8:
        raise Unsupported("Expected one complete standard AIFF or RIFF WAVE form")
    format_kind, data_kind = (b"COMM", b"SSND") if aiff else (b"fmt ", b"data")
    saw_format = saw_data = False
    payload = None
    chunk_start = chunk_end = size
    pos = 12
    while pos < size:
        kind, length = struct.unpack(endian + "4sI", _read_at(stream, pos, 8))
        start = pos + 8
        end = start + length + (length & 1)
        if end > size:
            raise Unsupported("Chunk exceeds the form")
        if kind == format_kind:
            if saw_format or length < (18 if aiff else 16):
                raise Unsupported("Invalid format chunk")
            saw_format = True
        elif kind == data_kind:
            if not saw_format or saw_data:
                raise Unsupported("Expected one audio chunk after the format chunk")
            if aiff and (length < 8 or int.from_bytes(_read_at(stream, start, 4), "big") > length - 8):
                raise Unsupported("Invalid AIFF sound-data offset")
            saw_data = True
        elif ((not aiff and kind == MARKER)
              or (aiff and kind == b"APPL" and length >= 4 and _read_at(stream, start, 4) == MARKER)):
            if payload is not None:
                raise Unsupported("Duplicate XM5 chunk")
            payload = _checked_payload(stream, start, length)
            chunk_start, chunk_end = pos, end
        pos = end
    if not saw_data:
        raise Unsupported("File has no audio chunk")
    return ChunkLayout(payload, chunk_start, chunk_end, aiff)


def _mp4_layout(stream: BinaryIO, size: int) -> Mp4Layout:
    payload = None
    box_start = box_end = size
    saw_movie = saw_audio = False
    pos = 0
    while pos < size:
        length, kind = struct.unpack(">I4s", _read_at(stream, pos, 8))
        header = 8
        if length == 1:
            length = struct.unpack(">Q", _read_at(stream, pos + header, 8))[0]
            header += 8
        if kind == b"uuid":
            header += 16
        end = pos + length
        if length < header or end > size:
            raise Unsupported("Unsupported or truncated MP4 box")
        if pos == 0 and (kind != b"ftyp" or length < header + 8):
            raise Unsupported("Expected a leading MP4 file-type box")
        if kind in (b"moof", b"mfra", b"sidx"):
            raise Unsupported("Fragmented or indexed MP4 is outside this profile")
        if kind == b"moov":
            if saw_movie:
                raise Unsupported("Duplicate MP4 movie box")
            saw_movie = True
        if kind == b"mdat":
            saw_audio = True
        if kind == b"uuid" and _read_at(stream, pos + header - 16, 16) == BOX_ID:
            if payload is not None:
                raise Unsupported("Duplicate XM5 MP4 box")
            payload = _checked_payload(stream, pos + header, length - header)
            box_start, box_end = pos, end
        pos = end
    if not saw_movie or not saw_audio:
        raise Unsupported("Expected MP4 movie and media-data boxes")
    return Mp4Layout(payload, box_start, box_end)


def _syncsafe(value: bytes) -> int:
    if any(byte & 0x80 for byte in value):
        raise Unsupported("Invalid ID3 syncsafe size")
    return (value[0] << 21) | (value[1] << 14) | (value[2] << 7) | value[3]


def _pack_syncsafe(value: int) -> bytes:
    if value >= 1 << 28:
        raise Unsupported("ID3 tag is too large")
    return bytes((value >> shift) & 0x7F for shift in (21, 14, 7, 0))


def _frame_header(stream: BinaryIO, offset: int, size: int) -> None:
    a, b, c, _ = _read_at(stream, offset, 4)
    version = (b >> 3) & 3
    bitrate_index, rate_index = c >> 4, (c >> 2) & 3
    if (a != 255 or b & 0xE0 != 0xE0 or b & 6 != 2 or version == 1
            or bitrate_index in (0, 15) or rate_index == 3):
        raise Unsupported("Expected an MPEG Layer III audio frame")
    rates = (44100, 48000, 32000)
    sample_rate = rates[rate_index] >> {3: 0, 2: 1, 0: 2}[version]
    bitrates = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
                if version == 3 else
                (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
    frame_length = (144000 if version == 3 else 72000) * bitrates[bitrate_index] // sample_rate
    if offset + frame_length + ((c >> 1) & 1) > size:
        raise Unsupported("Truncated first MPEG frame")


def _zero_padding(stream: BinaryIO, start: int, end: int) -> None:
    while start < end:
        block = _read_at(stream, start, min(BUFFER_BYTES, end - start))
        if any(block):
            raise Unsupported("ID3 tag padding contains nonzero bytes")
        start += len(block)


def _mp3_layout(stream: BinaryIO, size: int) -> Mp3Layout:
    if _read_at(stream, 0, 3) != b"ID3":
        _frame_header(stream, 0, size)
        return Mp3Layout(3, 0, None, 0, 0)
    header = _read_at(stream, 0, 10)
    version, revision, flags = header[3:6]
    if version not in (3, 4) or revision or flags:
        raise Unsupported("Expected plain ID3v2.3 or ID3v2.4 without tag-level flags")
    tag_size = _syncsafe(header[6:10])
    tag_end = 10 + tag_size
    if tag_size > MAX_TAG or tag_end > size:
        raise Unsupported("ID3 tag exceeds the accepted limit")
    _frame_header(stream, tag_end, size)
    payload = None
    pos = 10
    while pos < tag_end:
        if _read_at(stream, pos, 1) == b"\0":
            _zero_padding(stream, pos, tag_end)
            break
        if pos + 10 > tag_end:
            raise Unsupported("Truncated ID3 frame header")
        header = _read_at(stream, pos, 10)
        name = header[:4]
        if not all(48 <= byte <= 57 or 65 <= byte <= 90 for byte in name):
            raise Unsupported("Invalid ID3 frame identifier")
        frame_size = _syncsafe(header[4:8]) if version == 4 else int.from_bytes(header[4:8], "big")
        start = pos + 10
        end = start + frame_size
        if frame_size == 0 or end > tag_end:
            raise Unsupported("ID3 frame exceeds the tag")
        if name in OFFSET_FRAMES:
            raise Unsupported("ID3 chapter, seek-index or linked-tag offsets require a separate writer")
        if name == b"PRIV":
            if header[9]:
                raise Unsupported("Transformed ID3 private frame")
            if frame_size >= len(OWNER) and _read_at(stream, start, len(OWNER)) == OWNER:
                if payload is not None:
                    raise Unsupported("Duplicate XM5 private frame")
                payload = _checked_payload(stream, start + len(OWNER), frame_size - len(OWNER))
                frame_start, frame_end = pos, end
        pos = end
    if payload is None:
        frame_start = frame_end = pos
    return Mp3Layout(version, tag_end, payload, frame_start, frame_end)


def _scan(stream: BinaryIO, suffix: str, size: int) -> ChunkLayout | Mp3Layout | Mp4Layout:
    if suffix == ".wav":
        return _chunk_layout(stream, size, False)
    if suffix in (".aif", ".aiff"):
        return _chunk_layout(stream, size, True)
    if suffix == ".mp3":
        return _mp3_layout(stream, size)
    if suffix == ".m4a":
        return _mp4_layout(stream, size)
    raise Unsupported("Supported card formats are WAV, AIFF, MP3 and M4A")


def locate(path: Path) -> PayloadSpan | None:
    """Return a validated payload span, including XM5I, or None if absent."""
    path = Path(path)
    with path.open("rb") as stream:
        return _scan(stream, path.suffix.lower(), os.fstat(stream.fileno()).st_size).payload


def read_payload(path: Path) -> bytes | None:
    """Read the bounded XM5 payload without decoding audio."""
    path = Path(path)
    with path.open("rb") as stream:
        span = _scan(stream, path.suffix.lower(), os.fstat(stream.fileno()).st_size).payload
        return None if span is None else _read_at(stream, span.offset, span.size)


def _edit(layout: ChunkLayout | Mp3Layout | Mp4Layout, payload: bytes, size: int) -> Edit:
    if isinstance(layout, ChunkLayout):
        signature, form, kind, endian = ((b"FORM", b"AIFF", b"APPL", ">")
                                         if layout.aiff else (b"RIFF", b"WAVE", MARKER, "<"))
        record = kind + struct.pack(endian + "I", len(payload)) + payload
        if len(payload) & 1:
            record += b"\0"
        if layout.chunk_end != size and len(record) != layout.chunk_end - layout.chunk_start:
            raise Unsupported("Resizing a nonfinal XM5 chunk could move existing audio offsets")
        updated = size - 8 + len(record) - (layout.chunk_end - layout.chunk_start)
        if updated > 0xFFFFFFFF:
            raise Unsupported("Output exceeds classic chunked form size")
        return Edit(signature + struct.pack(endian + "I", updated) + form,
                    layout.chunk_start, layout.chunk_end, record)
    if isinstance(layout, Mp4Layout):
        record = struct.pack(">I4s", 24 + len(payload), b"uuid") + BOX_ID + payload
        if layout.box_end != size and len(record) != layout.box_end - layout.box_start:
            raise Unsupported("Resizing a nonfinal XM5 box could move existing MP4 offsets")
        return Edit(b"", layout.box_start, layout.box_end, record)
    data = OWNER + payload
    size_bytes = _pack_syncsafe(len(data)) if layout.version == 4 else struct.pack(">I", len(data))
    record = b"PRIV" + size_bytes + b"\0\0" + data
    updated = max(0, layout.tag_end - 10) + len(record) - (layout.frame_end - layout.frame_start)
    if updated > MAX_TAG:
        raise Unsupported("Output ID3 tag exceeds the accepted limit")
    header = b"ID3" + bytes((layout.version, 0, 0)) + _pack_syncsafe(updated)
    return Edit(header, layout.frame_start, layout.frame_end, record)


def _copy(source: BinaryIO, target: BinaryIO, start: int, end: int) -> None:
    while start < end:
        chunk = _read_at(source, start, min(BUFFER_BYTES, end - start))
        target.write(chunk)
        start += len(chunk)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _target(card_root: Path, relative_path: str) -> Path:
    parts = relative_path.split("/")
    if (len(parts) < 3 or parts[:2] != ["xm5", "music"]
            or any(part in ("", ".", "..") or any(char in part for char in "\\:\0") for part in parts)):
        raise ValueError("Expected a card-relative file path under xm5/music")
    root = Path(card_root).resolve(strict=True)
    candidate = root.joinpath(*parts)
    if candidate.resolve(strict=True) != candidate or not stat.S_ISREG(candidate.lstat().st_mode):
        raise ValueError("Expected a regular, unlinked card copy")
    return candidate


def write(card_root: Path, relative_path: str, payload: bytes) -> PayloadSpan:
    """Replace one XM5 record on an exclusively owned card copy."""
    if not isinstance(payload, bytes) or not payload.startswith(MARKER) or len(payload) > MAX_PAYLOAD:
        raise ValueError("Expected a bounded bytes payload starting with XM5I")
    target = _target(card_root, relative_path)
    suffix = target.suffix.lower()
    temporary = None
    try:
        with target.open("rb") as source:
            original = os.fstat(source.fileno())
            size = original.st_size
            layout = _scan(source, suffix, size)
            old = layout.payload
            if old is not None and _read_at(source, old.offset, old.size) == payload:
                return old
            edit = _edit(layout, payload, size)
            prefix_end = 0 if isinstance(layout, Mp4Layout) else len(edit.header)
            if isinstance(layout, Mp3Layout) and not layout.tag_end:
                prefix_end = 0
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".xm5-", suffix=suffix,
                                             dir=target.parent, delete=False) as dest:
                temporary = Path(dest.name)
                dest.write(edit.header)
                _copy(source, dest, prefix_end, edit.start)
                dest.write(edit.record)
                _copy(source, dest, edit.stop, size)
                dest.flush()
                os.fsync(dest.fileno())
        with temporary.open("rb") as result:
            span = _scan(result, suffix, os.fstat(result.fileno()).st_size).payload
            if span is None or _read_at(result, span.offset, span.size) != payload:
                raise OSError("XM5 payload readback failed")
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
        if _target(card_root, relative_path) != target:
            raise OSError("Card copy changed during write")
        with target.open("rb") as current:
            if _identity(os.fstat(current.fileno())) != _identity(original):
                raise OSError("Card copy changed during write")
        os.replace(temporary, target)
        return span
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
