"""Album art discovery and resizing. Sources are only read; results go to the cache or export copies."""
from __future__ import annotations

import io
import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

import mutagen
from PIL import Image, ImageOps

from . import tags
from .native_art_index import NATIVE_SIDES

FOLDER_NAMES = ("cover", "folder", "front", "albumart", "album")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
MAX_PIXELS = 40_000_000  # refuse decompression bombs
PREVIEW_SIDE = 80
SCREENSAVER_SIDE = 150
MEDIUM_SIDE = 160
PLAYBACK_SIDE = 330
EXPORT_QUALITIES = {PREVIEW_SIDE: 85, SCREENSAVER_SIDE: 85, MEDIUM_SIDE: 85, PLAYBACK_SIDE: 88}


@dataclass(frozen=True)
class ExportArt:
    preview: bytes
    playback: bytes
    medium: bytes
    screensaver: bytes
    native_bitmaps: dict[int, bytes]


def export_art(data: bytes) -> ExportArt:
    """Decode once for four compatibility JPEGs and the nine native UI BMP sizes.

    Keep the whole image, with black padding for a non-square or smaller source.
    These bytes are for a new card export only; never replace the library image.
    """
    with Image.open(io.BytesIO(data)) as original:
        if original.width * original.height > MAX_PIXELS:
            raise ValueError(f"image too large ({original.width}x{original.height})")
        oriented = ImageOps.exif_transpose(original)
        rgba = oriented.convert("RGBA")
        image = Image.new("RGB", rgba.size, "black")
        image.paste(rgba, mask=rgba.getchannel("A"))
        outputs, native_bitmaps = {}, {}
        for side in NATIVE_SIDES:
            resized = image.copy()
            resized.thumbnail((side, side), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (side, side), "black")
            canvas.paste(resized, ((side - resized.width) // 2, (side - resized.height) // 2))
            pixels = canvas.convert("RGBA").tobytes("raw", "BGRA")
            native_bitmaps[side] = (struct.pack("<2sIHHI", b"BM", 64 + len(pixels), 0, 0, 64)
                                    + struct.pack("<IiiHHIIiiII", 40, side, -side, 1, 32, 0,
                                                  len(pixels), 2835, 2835, 0, 0)
                                    + b"\0" * 10 + pixels)
            if side in EXPORT_QUALITIES:
                out = io.BytesIO()
                canvas.save(out, "JPEG", quality=EXPORT_QUALITIES[side], optimize=True, progressive=False)
                outputs[side] = out.getvalue()
    return ExportArt(preview=outputs[PREVIEW_SIDE], medium=outputs[MEDIUM_SIDE],
                     playback=outputs[PLAYBACK_SIDE], screensaver=outputs[SCREENSAVER_SIDE],
                     native_bitmaps=native_bitmaps)


def stock_preview_keys(basename: str) -> tuple[str, ...]:
    """Existing folder-view s_ cache names, not a new device storage format.

    Stock hashes UTF-8 basenames and has a special combining-dakuten fold.
    Ambiguous handakuten and truncated names are refused, not guessed.
    Collision handling across different artworks belongs to the exporter.
    """
    raw = basename.encode("utf-8")
    if b"\xe3\x82\x9a" in raw:
        raise ValueError("combining handakuten cache rule is unverified")
    folded = bytearray(raw)
    marker = b"\xe3\x82\x99"
    index = 0
    while (index := folded.find(marker, index)) >= 0:
        if index == 0:
            raise ValueError("leading combining dakuten cache rule is unverified")
        folded[index - 1] += 1
        del folded[index:index + 3]
    if max(len(raw), len(folded)) >= 128:
        raise ValueError("basename exceeds the stock 127-byte cache-key limit")
    return tuple(dict.fromkeys("s_" + hashlib.md5(value).hexdigest() + ".jpg"
                               for value in (bytes(folded), raw)))


def folder_image(folder: Path) -> Path | None:
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT]
    except OSError:
        return None
    by_stem = {p.stem.casefold(): p for p in files}
    for name in FOLDER_NAMES:
        if name in by_stem:
            return by_stem[name]
    return sorted(files, key=lambda p: p.name.casefold())[0] if len(files) == 1 else None


def source_art(track_paths: list[str]) -> tuple[bytes | None, str]:
    """Best cover for an album: folder image first, else first embedded picture. Returns (bytes, origin)."""
    if track_paths:
        img = folder_image(Path(track_paths[0]).parent)
        if img is not None:
            try:
                return img.read_bytes(), str(img)
            except OSError:
                pass
    for p in track_paths[:4]:
        try:
            audio = mutagen.File(p)
            data = tags.embedded_picture(audio) if audio is not None else None
        except Exception:
            data = None
        if data:
            return data, f"embedded:{p}"
    return None, ""


def resize_jpeg(data: bytes, max_side: int, quality: int = 88) -> bytes:
    """Shrink (never enlarge) to fit ``max_side`` and encode as baseline JPEG."""
    with Image.open(io.BytesIO(data)) as im:
        if im.width * im.height > MAX_PIXELS:
            raise ValueError(f"image too large ({im.width}x{im.height})")
        im = im.convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
        return out.getvalue()


def cached_thumbnail(cache_dir: Path, cover_id: str, track_paths: list[str], size: int = 220) -> Path | None:
    """Thumbnail in the app cache; generated once. Returns None when the album has no art."""
    target = cache_dir / f"{cover_id}-{size}.jpg"
    missing = cache_dir / f"{cover_id}-{size}.none"
    if target.exists():
        return target
    if missing.exists():
        return None
    data, _ = source_art(track_paths)
    if not data:
        missing.write_bytes(b"")
        return None
    try:
        thumb = resize_jpeg(data, size, quality=85)
    except Exception:
        missing.write_bytes(b"")
        return None
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(thumb)
    tmp.replace(target)
    return target
