#!/usr/bin/env python3
"""Render the original XM5 Studio SVG to PNG and a multi-resolution Windows ICO."""
from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path
import struct
import sys


ROOT = Path(__file__).resolve().parents[1]
# The portable interpreter is isolated and may belong to a different checkout.
sys.path.insert(0, str(ROOT / "desktop"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402


ASSETS = ROOT / "desktop" / "xm5lib" / "ui" / "assets" / "studio"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4


def render_svg(source: Path, size: int) -> Image.Image:
    """Render each target independently, then filter its antialiased edges."""
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise ValueError(f"Invalid SVG: {source}")
    side = size * SUPERSAMPLE
    surface = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    surface.fill(0)
    painter = QPainter(surface)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, side, side))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("Cannot open the SVG render buffer")
    if not surface.save(buffer, "PNG"):
        raise RuntimeError("Cannot encode the SVG render")
    buffer.close()
    with Image.open(io.BytesIO(bytes(data))) as image:
        result = image.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    # Lanczos can leave a 1-2/255 alpha halo outside a tiny rounded corner.
    result.putalpha(result.getchannel("A").point([0, 0, 0, *range(3, 256)]))
    return result


def png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=9, optimize=False)
    return output.getvalue()


def ico_bytes(images: dict[int, Image.Image]) -> bytes:
    """Write PNG-backed, 32-bit RGBA frames supported by modern Windows."""
    payloads = [png_bytes(images[size]) for size in ICON_SIZES]
    offset = 6 + 16 * len(ICON_SIZES)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(ICON_SIZES)))
    for size, payload in zip(ICON_SIZES, payloads, strict=True):
        side = size if size < 256 else 0
        directory.extend(struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(payload), offset))
        offset += len(payload)
    return bytes(directory) + b"".join(payloads)


def validate_image(image: Image.Image, size: int) -> None:
    if image.mode != "RGBA" or image.size != (size, size):
        raise ValueError(f"Expected an RGBA {size} x {size} image")
    if image.getchannel("A").getextrema() != (0, 255):
        raise ValueError(f"Missing transparent or opaque pixels at {size}px")
    for corner in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
        if image.getpixel(corner)[3] != 0:
            raise ValueError(f"Nontransparent outer corner at {size}px")
    if image.getpixel((size // 2, size // 2))[3] != 255:
        raise ValueError(f"Unexpected transparent tile centre at {size}px")


def validate_ico(data: bytes, images: dict[int, Image.Image]) -> None:
    with Image.open(io.BytesIO(data)) as icon:
        if icon.format != "ICO" or icon.ico.sizes() != {(size, size) for size in ICON_SIZES}:
            raise ValueError("ICO frame dimensions do not match the Windows size set")
        for size in ICON_SIZES:
            frame = icon.ico.getimage((size, size)).convert("RGBA")
            validate_image(frame, size)
            if frame.tobytes() != images[size].tobytes():
                raise ValueError(f"ICO frame pixels changed at {size}px")


def preview_image(images: dict[int, Image.Image]) -> Image.Image:
    """Show the actual exports, including small sizes on light and dark surfaces."""
    preview = Image.new("RGB", (1200, 900), "#e9e7e1")
    draw = ImageDraw.Draw(preview)
    title = ImageFont.load_default(size=28)
    label = ImageFont.load_default(size=17)
    draw.text((32, 24), "XM5 Studio - original application icon", font=title, fill="#242528")
    preview.paste(images[1024].resize((512, 512), Image.Resampling.LANCZOS), (32, 82), images[1024].resize((512, 512), Image.Resampling.LANCZOS))
    draw.text((32, 604), "Vector master / 1024 px PNG", font=label, fill="#414246")
    preview.paste(images[256], (700, 134), images[256])
    draw.text((700, 412), "256 px export", font=label, fill="#414246")
    draw.text((620, 478), "ICO frames at their actual pixel sizes", font=label, fill="#414246")
    for y, background, foreground in ((536, "#faf9f6", "#242528"), (714, "#151618", "#d9d9d6")):
        draw.rounded_rectangle((600, y - 16, 1168, y + 154), radius=12, fill=background)
        x = 620
        for size in ICON_SIZES[:-1]:
            preview.paste(images[size], (x, y + (128 - size) // 2), images[size])
            draw.text((x, y + 132), str(size), font=label, fill=foreground)
            x += size + 28
    # A nearest-neighbour view reveals the 16px silhouette without inventing detail.
    pixel_view = images[16].resize((128, 128), Image.Resampling.NEAREST)
    preview.paste(pixel_view, (72, 708), pixel_view)
    draw.text((220, 752), "16 px enlarged 8x", font=label, fill="#414246")
    return preview


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ASSETS / "app.svg")
    parser.add_argument("--output-dir", type=Path, default=ASSETS)
    parser.add_argument("--check", action="store_true", help="Compare tracked exports without writing files")
    parser.add_argument("--preview", type=Path, help="Write an optional, untracked visual QA contact sheet")
    args = parser.parse_args()
    if args.check and args.preview:
        parser.error("--check does not write files; omit --preview")
    application = QGuiApplication.instance() or QGuiApplication(["render_studio_icon"])
    images = {size: render_svg(args.source, size) for size in (*ICON_SIZES, 1024)}
    for size, image in images.items():
        validate_image(image, size)
    exports = {
        "app.png": png_bytes(images[1024]),
        "app-256.png": png_bytes(images[256]),
        "app.ico": ico_bytes(images),
    }
    validate_ico(exports["app.ico"], images)
    if not args.check:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, data in exports.items():
        target = args.output_dir / name
        if args.check:
            if not target.is_file() or target.read_bytes() != data:
                raise ValueError(f"Export is missing or differs from the SVG render: {target}")
        else:
            target.write_bytes(data)
        print(f"{hashlib.sha256(data).hexdigest()}  {target}")
    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        preview_image(images).save(args.preview, format="PNG")
        print(f"Preview: {args.preview}")
    print("Verified RGBA transparency and ICO frames: " + ", ".join(map(str, ICON_SIZES)))
    # Keep the application alive until all Qt render objects have been released.
    _ = application
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
