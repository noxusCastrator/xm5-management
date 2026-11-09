"""Optional foobar2000 Default UI theme (.fth) matching the XM5 Library palette.

Mechanism: foobar2000 v2 *Preferences > Display > Default User Interface > Import theme* reads .fth
files. We do not author the binary format from scratch: the layout and colour records are taken
from theme templates shipped with the user's own foobar2000 installation
(``<install>/themes/*.fth``), only the colour values of known colour records are replaced, and the
result is round-trip validated. Nothing is imported automatically; the current ``theme.fth`` of the
profile is copied (read-only) as a backup next to the output so the user can re-import it.

Provenance: the derived file contains data from foobar2000's bundled templates (vendor-owned); it is
generated on the user's machine and is not redistributed with this app.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import time
from pathlib import Path

HEADER = bytes.fromhex("98baa0add436164a8e14415f73b4cb21")
COLOUR_CHUNK = bytes.fromhex("8cee8c598d8a8d48aa95ca5bda02622d")
# Colour record GUIDs, identified by comparing the bundled Black/White/Dark Grey Orange templates.
TEXT = bytes.fromhex("e78bd35d8aff6f41882da48e318785b2")
BACKGROUND = bytes.fromhex("c140fc16ba1c8543933be9327f6e351f")
HIGHLIGHT = bytes.fromhex("4280f9d26a3e3a42b8666501fea97593")
SELECTION = bytes.fromhex("6ba3e1eb0a7e9a468ec5cf03129040b5")

PALETTE = {TEXT: (229, 231, 235), BACKGROUND: (23, 25, 28), HIGHLIGHT: (214, 187, 141), SELECTION: (58, 63, 71)}
COLOUR_TEMPLATE = "Dark Grey Orange.fth"
LAYOUT_TEMPLATE = "Album art in playlist.fth"


def parse(data: bytes) -> list[tuple[bytes, bytes]]:
    """[(chunk guid, chunk body)]; body = 16-byte inner id + ``size`` bytes, as observed in v2 files."""
    if data[:16] != HEADER or len(data) < 20:
        raise ValueError("not a foobar2000 Default UI theme")
    count = struct.unpack_from("<I", data, 16)[0]
    pos, out = 20, []
    for _ in range(count):
        if pos + 20 > len(data):
            raise ValueError("truncated theme")
        guid = data[pos:pos + 16]
        size = struct.unpack_from("<I", data, pos + 16)[0]
        body = data[pos + 20:pos + 20 + 16 + size]
        if len(body) != 16 + size:
            raise ValueError("truncated theme chunk")
        out.append((guid, body))
        pos += 20 + 16 + size
    if pos != len(data):
        raise ValueError("unexpected trailing data")
    return out


def pack(chunks: list[tuple[bytes, bytes]]) -> bytes:
    return HEADER + struct.pack("<I", len(chunks)) + b"".join(
        g + struct.pack("<I", len(b) - 16) + b for g, b in chunks)


def colour_records(body: bytes) -> list[tuple[bytes, int]]:
    count = struct.unpack_from("<I", body, 16)[0]
    if len(body) != 20 + count * 20:
        raise ValueError("unexpected colour chunk layout")
    return [(body[20 + i * 20:36 + i * 20], struct.unpack_from("<I", body, 36 + i * 20)[0]) for i in range(count)]


def recolour(body: bytes, palette: dict[bytes, tuple[int, int, int]]) -> bytes:
    records = colour_records(body)
    if {g for g, _ in records} != set(palette):
        raise ValueError("colour records differ from the known template; no guessed patch applied")
    out = bytearray(body)
    for i, (guid, _) in enumerate(records):
        r, g, b = palette[guid]
        struct.pack_into("<I", out, 36 + i * 20, r | g << 8 | b << 16)
    return bytes(out)


def build(install_dir: Path, profile_dir: Path, output_parent: Path) -> Path:
    """Write XM5-Graphite.fth + backup + PROVENANCE.json into a new timestamped folder."""
    themes = Path(install_dir) / "themes"
    colours, layout = themes / COLOUR_TEMPLATE, themes / LAYOUT_TEMPLATE
    for p in (colours, layout):
        if not p.is_file():
            raise FileNotFoundError(f"foobar2000 template not found: {p}")
    cchunks = parse(colours.read_bytes())
    if len(cchunks) != 1 or cchunks[0][0] != COLOUR_CHUNK:
        raise ValueError("colour template structure changed; not patched")
    new_colour = (COLOUR_CHUNK, recolour(cchunks[0][1], PALETTE))
    merged = [c for c in parse(layout.read_bytes()) if c[0] != COLOUR_CHUNK] + [new_colour]
    data = pack(merged)
    if pack(parse(data)) != data:
        raise AssertionError("theme round-trip failed")
    out = Path(output_parent) / time.strftime("foobar-theme-%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=False)
    target = out / "XM5-Graphite.fth"
    target.write_bytes(data)
    current = Path(profile_dir) / "theme.fth"
    backup = None
    if current.is_file():
        backup = out / "your-previous-theme.fth"
        shutil.copyfile(current, backup)  # read-only copy of the profile file
    (out / "PROVENANCE.json").write_text(json.dumps({
        "format": "foobar2000 Default UI theme (.fth), import via Preferences > Display > Default User Interface",
        "applied_automatically": False,
        "templates": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (colours, layout)},
        "output_sha256": hashlib.sha256(data).hexdigest(),
        "backup_of_current_theme": str(backup) if backup else None,
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest() if backup else None,
        "palette_rgb": {"text": PALETTE[TEXT], "background": PALETTE[BACKGROUND],
                        "highlight": PALETTE[HIGHLIGHT], "selection": PALETTE[SELECTION]},
        "licence": "Derived locally from templates bundled with your foobar2000 installation (vendor-owned). "
                   "Do not redistribute the .fth; XM5 Library ships only the generator.",
    }, indent=2) + "\n", encoding="utf-8")
    (out / "HOW-TO-IMPORT.txt").write_text(
        "1. In foobar2000 open File > Preferences > Display > Default User Interface.\n"
        "2. Click 'Import theme...' and choose XM5-Graphite.fth.\n"
        "3. To go back, import your-previous-theme.fth the same way.\n"
        "Nothing was changed in your foobar2000 profile by XM5 Library.\n", encoding="utf-8")
    return target


def describe(path: Path) -> dict:
    chunks = parse(Path(path).read_bytes())
    info = {"chunks": len(chunks)}
    for g, b in chunks:
        if g == COLOUR_CHUNK:
            info["colours"] = {r.hex(): f"#{v & 0xFF:02x}{v >> 8 & 0xFF:02x}{v >> 16 & 0xFF:02x}"
                               for r, v in colour_records(b)}
    return info
