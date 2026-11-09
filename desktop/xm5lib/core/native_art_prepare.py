"""Exact native UI bitmap recipe without redundant JPEG encoding or cache IO."""
import io
import struct

from PIL import Image, ImageOps

from .native_art_index import NATIVE_SIDES


def prepare(data):
    with Image.open(io.BytesIO(data)) as original:
        if original.width * original.height > 40_000_000:
            raise ValueError("image exceeds forty million pixels")
        oriented = ImageOps.exif_transpose(original)
        rgba = oriented.convert("RGBA")
        image = Image.new("RGB", rgba.size, "black")
        image.paste(rgba, mask=rgba.getchannel("A"))
        result = {}
        for side in NATIVE_SIDES:
            resized = image.copy()
            resized.thumbnail((side, side), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (side, side), "black")
            canvas.paste(resized, ((side - resized.width) // 2, (side - resized.height) // 2))
            pixels = canvas.convert("RGBA").tobytes("raw", "BGRA")
            result[side] = (struct.pack("<2sIHHI", b"BM", 64 + len(pixels), 0, 0, 64)
                            + struct.pack("<IiiHHIIiiII", 40, side, -side, 1, 32, 0, len(pixels), 2835, 2835, 0, 0)
                            + b"\0" * 10 + pixels)
        return result
