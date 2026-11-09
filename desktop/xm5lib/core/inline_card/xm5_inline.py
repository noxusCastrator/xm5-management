"""XM5 native-FLAC information blocks and rebuildable card track index."""

import argparse
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import struct
import tempfile

from PIL import Image


APP_ID = b"XM5I"
APP_OFFSET = 42
SLOT_SIZES = {330: 98304, 160: 32768, 80: 12288}
SLOT_OFFSETS = {330: 4, 160: 98308, 80: 131076}
LEVELS_OFFSET = 4 + sum(SLOT_SIZES.values())
LYRICS_OFFSET = LEVELS_OFFSET + 8
MAX_LYRICS = 128 * 1024
LEVEL_ABSENT = -(1 << 31)
MAX_BLOCKS = 4096
INDEX_PATH = "xm5/Struct/tracks.tsv"
LEGACY_MARKERS = ("manifest.json", "manifest.sha256", "INCOMPLETE")


class InlineError(ValueError):
    """An input cannot safely be interpreted or modified."""


def check_cancel(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise InterruptedError("Inline card operation cancelled")


@dataclass(frozen=True)
class Block:
    offset: int
    kind: int
    size: int

    @property
    def body(self):
        return self.offset + 4


@dataclass(frozen=True)
class Layout:
    blocks: tuple[Block, ...]
    app: Block | None
    audio_offset: int
    file_size: int


def _jpeg(image, side, *, load=False):
    if not (image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9")):
        raise InlineError(f"Expected a {side}x{side} JPEG")
    try:
        with Image.open(BytesIO(image), formats=("JPEG",)) as picture:
            if picture.size != (side, side):
                raise InlineError(f"Expected {side}x{side}, got {picture.size}")
            if load:
                picture.load()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise InlineError(f"Invalid {side}x{side} JPEG: {exc}") from exc


def _level(value, label, minimum, maximum):
    if value is None:
        return LEVEL_ABSENT
    if not isinstance(value, int) or not minimum <= value <= maximum:
        raise InlineError(f"Invalid {label} milli-decibels: {value!r}")
    return value


def encode_body(images, peak_mdbfs=None, crest_mdb=None, lyrics=b""):
    """Build a complete APPLICATION body, including its XM5I ID."""
    if images.keys() - SLOT_SIZES.keys():
        raise InlineError("Images must use the 330, 160 or 80 pixel roles")
    if isinstance(lyrics, str):
        lyrics = lyrics.encode("utf-8")
    if len(lyrics) > MAX_LYRICS:
        raise InlineError("Lyrics exceed the 128 KiB limit")
    _check_utf8(lyrics)
    body = bytearray(LYRICS_OFFSET)
    body[:4] = APP_ID
    for side, image in images.items():
        if len(image) > SLOT_SIZES[side] - 4:
            raise InlineError(f"{side} pixel JPEG exceeds its fixed slot")
        _jpeg(image, side, load=True)
        offset = SLOT_OFFSETS[side]
        struct.pack_into("<I", body, offset, len(image))
        body[offset + 4:offset + 4 + len(image)] = image
    peak = _level(peak_mdbfs, "peak", LEVEL_ABSENT + 1, 0)
    crest = _level(crest_mdb, "peak-to-average", 0, (1 << 31) - 1)
    struct.pack_into("<ii", body, LEVELS_OFFSET, peak, crest)
    return bytes(body) + lyrics


def _check_utf8(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InlineError("Lyrics are not valid UTF-8") from exc


def _slot_length(raw, side):
    length = struct.unpack("<I", raw)[0]
    if length > SLOT_SIZES[side] - 4:
        raise InlineError(f"Invalid length for {side} pixel slot")
    return length


def _decode_levels(raw):
    peak, crest = struct.unpack("<ii", raw)
    if peak > 0 or (crest < 0 and crest != LEVEL_ABSENT):
        raise InlineError("Invalid XM5 level data")
    return (None if peak == LEVEL_ABSENT else peak,
            None if crest == LEVEL_ABSENT else crest)


def validate_body(body):
    """Verify all slots, including padding, and return summary metadata."""
    if not body.startswith(APP_ID) or not LYRICS_OFFSET <= len(body) <= LYRICS_OFFSET + MAX_LYRICS:
        raise InlineError("Invalid XM5 application body")
    sizes = {}
    for side, capacity in SLOT_SIZES.items():
        offset = SLOT_OFFSETS[side]
        length = _slot_length(body[offset:offset + 4], side)
        end = offset + 4 + length
        if length:
            _jpeg(body[offset + 4:end], side, load=True)
        if any(body[end:offset + capacity]):
            raise InlineError(f"Nonzero padding in {side} pixel slot")
        sizes[side] = length
    peak, crest = _decode_levels(body[LEVELS_OFFSET:LYRICS_OFFSET])
    lyrics = _check_utf8(body[LYRICS_OFFSET:])
    return {"image_lengths": sizes, "peak_mdbfs": peak, "crest_mdb": crest, "lyrics": lyrics}


def _read_exact(file, size):
    data = file.read(size)
    if len(data) != size:
        raise InlineError("Unexpected end of FLAC")
    return data


def _read_at(file, offset, size):
    file.seek(offset)
    return _read_exact(file, size)


def _streaminfo(file):
    header = _read_at(file, 0, 8)
    if header[:4] != b"fLaC" or header[4] & 127 or header[5:8] != b"\0\0\x22":
        raise InlineError("Expected native FLAC with a 34-byte STREAMINFO")
    return header[4]


def _block(file, offset, file_size):
    header = _read_at(file, offset, 4)
    block = Block(offset, header[0] & 127, int.from_bytes(header[1:], "big"))
    if block.kind == 127 or block.body + block.size > file_size:
        raise InlineError("Invalid FLAC metadata block")
    return block, bool(header[0] & 128)


def _has_app_id(file, block):
    if block.kind != 2:
        return False
    if block.size < len(APP_ID):
        raise InlineError("FLAC APPLICATION is missing its ID")
    return _read_at(file, block.body, len(APP_ID)) == APP_ID


def _check_app_size(block):
    if not LYRICS_OFFSET <= block.size <= LYRICS_OFFSET + MAX_LYRICS:
        raise InlineError("XM5 block length is outside its permitted range")


def _check_slot_headers(file, body_offset):
    for side, offset in SLOT_OFFSETS.items():
        _slot_length(_read_at(file, body_offset + offset, 4), side)
    _decode_levels(_read_at(file, body_offset + LEVELS_OFFSET, 8))


def _scan(file):
    file_size = os.fstat(file.fileno()).st_size
    _streaminfo(file)
    offset = 4
    blocks = []
    app = None
    for _ in range(MAX_BLOCKS):
        block, last = _block(file, offset, file_size)
        if blocks and block.kind == 0:
            raise InlineError("Duplicate STREAMINFO")
        blocks.append(block)
        if _has_app_id(file, block):
            if app is not None:
                raise InlineError("Duplicate XM5 application blocks")
            _check_app_size(block)
            app = block
        offset = block.body + block.size
        if last:
            sync = _read_at(file, offset, 2)
            if sync[0] != 255 or sync[1] not in (248, 249):
                raise InlineError("Missing FLAC audio frames")
            return Layout(tuple(blocks), app, offset, file_size)
    raise InlineError("Too many FLAC metadata blocks")


def _canonical_app(file):
    if _streaminfo(file) & 128:
        return None
    block, _ = _block(file, APP_OFFSET, os.fstat(file.fileno()).st_size)
    if _has_app_id(file, block):
        _check_app_size(block)
        return block
    return None


def read_slot(track, side, *, recover_moved=False):
    """Read one slot by fixed seek, or explicitly recover a relocated block."""
    if side not in SLOT_SIZES:
        raise InlineError("Image role must be 80, 160 or 330")
    with Path(track).open("rb") as file:
        app = _scan(file).app if recover_moved else _canonical_app(file)
        if app is None:
            return None
        offset = app.body + SLOT_OFFSETS[side]
        length = _slot_length(_read_at(file, offset, 4), side)
        if not length:
            return None
        data = _read_exact(file, length)
        _jpeg(data, side)
        return data


def inspect_track(track):
    """Find and fully validate the block, even after tag editors relocate it."""
    with Path(track).open("rb") as file:
        layout = _scan(file)
        if layout.app is None:
            return {"present": False}
        app = layout.app
        result = validate_body(_read_at(file, app.body, app.size))
        return {"present": True, "canonical": app.offset == APP_OFFSET,
                "offset": app.offset, "size": app.size, **result}


def _reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _relative_parts(relative):
    parts = relative.split("/")
    if ("\\" in relative or any(ord(char) < 32 or char == ":" for char in relative)
            or any(part in ("", ".", "..") for part in parts)):
        raise InlineError(f"Expected a safe card-relative path: {relative!r}")
    return parts


def _card_path(root, relative, *, allow_new=False):
    root = Path(root).absolute()
    if _reparse(root.lstat()) or not root.is_dir():
        raise InlineError("Card root must be a real directory")
    parts = _relative_parts(relative)
    path = root
    for index, part in enumerate(parts):
        path /= part
        try:
            info = path.lstat()
        except FileNotFoundError:
            if allow_new and index == len(parts) - 1:
                return path
            raise
        if _reparse(info):
            raise InlineError(f"Card path contains a link or reparse point: {path}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise InlineError(f"Expected a directory: {path}")
    return path


def _snapshot(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _copy_digest(source, offset, count, destination=None, cancel=None):
    source.seek(offset)
    digest = sha256()
    while count:
        check_cancel(cancel)
        data = _read_exact(source, min(count, 1024 * 1024))
        count -= len(data)
        digest.update(data)
        if destination is not None:
            destination.write(data)
    return digest.digest()


def _header(kind, size, last):
    return bytes((kind | (128 if last else 0),)) + size.to_bytes(3, "big")


def _original_blocks(layout):
    return [block for block in layout.blocks if block != layout.app]


def _emit(source, layout, body, output, cancel=None):
    original = _original_blocks(layout)
    output.write(b"fLaC")
    saved = []
    for index, block in enumerate(original):
        output.write(_header(block.kind, block.size, index != 0 and index == len(original) - 1))
        digest = _copy_digest(source, block.body, block.size, output, cancel)
        saved.append((block.kind, block.size, digest))
        if index == 0:
            output.write(_header(2, len(body), len(original) == 1))
            output.write(body)
    audio = _copy_digest(source, layout.audio_offset, layout.file_size - layout.audio_offset, output, cancel)
    return saved, audio


def _verify(path, body, expected, cancel=None):
    with path.open("rb") as file:
        layout = _scan(file)
        app = layout.app
        if app is None or app.offset != APP_OFFSET or _read_at(file, app.body, app.size) != body:
            raise InlineError("Temporary FLAC does not contain the expected XM5 block")
        metadata = [(block.kind, block.size, _copy_digest(file, block.body, block.size, cancel=cancel))
                    for block in _original_blocks(layout)]
        audio = _copy_digest(file, layout.audio_offset, layout.file_size - layout.audio_offset, cancel=cancel)
        if (metadata, audio) != expected:
            raise InlineError("Temporary FLAC changed original metadata or audio")


def _read_index(path):
    if not path.exists():
        return {}
    if not path.is_file():
        raise InlineError("Track index must be a regular file")
    result = {}
    try:
        with path.open(encoding="utf-8") as file:
            for line in file:
                status, separator, relative = line.rstrip("\r\n").partition("\t")
                if status not in ("0", "1") or not separator:
                    raise InlineError("Malformed track index. Rebuild it before updating")
                _relative_parts(relative)
                if relative in result:
                    raise InlineError("Duplicate track index entry. Rebuild it before updating")
                result[relative] = status == "1"
    except UnicodeDecodeError as exc:
        raise InlineError("Track index must be UTF-8. Rebuild it before updating") from exc
    return result


def check_legacy_card(root):
    """Keep standalone writes away from INFO02's manifest-owned card tree."""
    struct_path = _card_path(root, "xm5/Struct", allow_new=True)
    if struct_path.exists() and not struct_path.is_dir():
        raise InlineError("xm5/Struct must be a directory")
    if any(os.path.lexists(struct_path / name) for name in LEGACY_MARKERS):
        raise InlineError("Legacy INFO02 export requires its own transaction")


def _index_path(root):
    check_legacy_card(root)
    struct_path = _card_path(root, "xm5/Struct", allow_new=True)
    struct_path.mkdir(exist_ok=True)
    return _card_path(root, INDEX_PATH, allow_new=True)


def _write_index(path, records):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=".tracks-", delete=False) as file:
            temporary = Path(file.name)
            for relative in sorted(records):
                file.write(f"{int(records[relative])}\t{relative}\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh_index_record(root, relative, present):
    """Publish a verified presence hint for a card-relative track."""
    _card_path(root, relative)
    path = _index_path(root)
    records = _read_index(path)
    if records.get(relative) != present:
        records[relative] = present
        _write_index(path, records)


def organize_track(root, relative, body=None, *, publish_index=True, cancel=None, replace_body=False):
    """Refresh one index record and insert or reposition an XM5 block as needed."""
    check_cancel(cancel)
    track = _card_path(root, relative)
    info = track.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise InlineError("Track must be a regular file")
    check_legacy_card(root)
    index_path = _index_path(root) if publish_index else None
    records = _read_index(index_path) if publish_index else None
    temporary = None
    try:
        with track.open("rb") as source:
            if _snapshot(info) != _snapshot(os.fstat(source.fileno())):
                raise InlineError("Source changed while being opened")
            layout = _scan(source)
            if layout.app is None:
                status = "absent" if body is None else "inserted"
            else:
                if replace_body and body is not None:
                    status = "updated"
                else:
                    body = _read_at(source, layout.app.body, layout.app.size)
                    status = "present" if layout.app.offset == APP_OFFSET else "relocated"
            if body is not None:
                validate_body(body)
            if status in ("inserted", "relocated", "updated"):
                with tempfile.NamedTemporaryFile("wb", dir=track.parent, prefix=".xm5-",
                                                 delete=False) as output:
                    temporary = Path(output.name)
                    expected = _emit(source, layout, body, output, cancel)
                    output.flush()
                    os.fsync(output.fileno())
        if temporary is not None:
            _verify(temporary, body, expected, cancel)
            os.chmod(temporary, stat.S_IMODE(info.st_mode))
        latest = _card_path(root, relative).lstat()
        if _snapshot(info) != _snapshot(latest) or info.st_ctime_ns != latest.st_ctime_ns:
            raise InlineError("Source changed while being organized")
        check_cancel(cancel)
        if temporary is not None:
            os.replace(temporary, track)
        present = body is not None
        if records is not None and records.get(relative) != present:
            records[relative] = present
            _write_index(index_path, records)
        return status
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def rebuild_index(root, adapters=None, *, cancel=None):
    """Regenerate hints from FLAC and any additional container probes."""
    music = _card_path(root, "xm5/music")
    if not music.is_dir():
        raise InlineError("xm5/music must be a directory")
    root = Path(root).absolute()
    records = {}
    skipped = {}
    adapters = adapters or {}

    def scan_error(error):
        raise error

    for folder, dirs, names in os.walk(music, onerror=scan_error):
        check_cancel(cancel)
        dirs[:] = [name for name in dirs if not _reparse((Path(folder) / name).lstat())]
        for name in names:
            track = Path(folder) / name
            suffix = track.suffix.lower()
            if suffix != ".flac" and suffix not in adapters:
                continue
            relative = track.relative_to(root).as_posix()
            try:
                track = _card_path(root, relative)
                if not stat.S_ISREG(track.lstat().st_mode):
                    raise InlineError("Track must be a regular file")
                if suffix == ".flac":
                    with track.open("rb") as file:
                        app = _scan(file).app
                        if app is not None:
                            _check_slot_headers(file, app.body)
                        records[relative] = app is not None
                else:
                    records[relative] = adapters[suffix](track)
            except (ValueError, OSError) as exc:
                skipped[relative] = str(exc)
    check_cancel(cancel)
    _write_index(_index_path(root), records)
    return {"tracks": len(records), "prepared": sum(records.values()), "skipped": skipped}


def _bounded_read(path, maximum):
    with Path(path).open("rb") as file:
        data = file.read(maximum + 1)
    if len(data) > maximum:
        raise InlineError(f"{path} exceeds the {maximum}-byte limit")
    return data


def _parse_cover(arg):
    key, separator, path = arg.partition("=")
    if key not in ("330", "160", "80") or not separator or not path:
        raise argparse.ArgumentTypeError("Use 330=PATH, 160=PATH or 80=PATH")
    return int(key), path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("track", type=Path)
    image = commands.add_parser("read-image")
    image.add_argument("track", type=Path)
    image.add_argument("side", type=int, choices=SLOT_SIZES)
    image.add_argument("output", type=Path)
    image.add_argument("--recover-moved", action="store_true")
    organize = commands.add_parser("organize")
    organize.add_argument("card", type=Path)
    organize.add_argument("track", help="Card-relative path using forward slashes")
    organize.add_argument("--cover", action="append", type=_parse_cover, default=[])
    organize.add_argument("--peak-mdbfs", type=int)
    organize.add_argument("--crest-mdb", type=int)
    organize.add_argument("--lyrics", type=Path)
    rebuild = commands.add_parser("rebuild-index")
    rebuild.add_argument("card", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect_track(args.track)
        elif args.command == "read-image":
            data = read_slot(args.track, args.side, recover_moved=args.recover_moved)
            if data is None:
                return 1
            with args.output.open("xb") as file:
                file.write(data)
            result = {"bytes": len(data)}
        elif args.command == "organize":
            images = {side: _bounded_read(path, SLOT_SIZES[side] - 4) for side, path in args.cover}
            lyrics = _bounded_read(args.lyrics, MAX_LYRICS) if args.lyrics else b""
            body = None
            if images or args.lyrics or args.peak_mdbfs is not None or args.crest_mdb is not None:
                body = encode_body(images, args.peak_mdbfs, args.crest_mdb, lyrics)
            result = {"status": organize_track(args.card, args.track, body)}
        else:
            result = rebuild_index(args.card)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (InlineError, OSError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
