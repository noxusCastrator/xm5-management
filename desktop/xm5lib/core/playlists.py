"""Ordinary M3U/M3U8 reading and writing (local files only)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse


@dataclass
class ImportResult:
    name: str
    paths: list[str] = field(default_factory=list)        # resolved, in the library, in order
    missing: list[str] = field(default_factory=list)      # file does not exist
    outside: list[str] = field(default_factory=list)      # exists, but not in the indexed library
    unsupported: list[str] = field(default_factory=list)  # URLs / streams
    encoding: str = "utf-8"


def _decode(data: bytes, suffix: str) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        if suffix == ".m3u8":
            raise ValueError("M3U8 file is not valid UTF-8")
        return data.decode("mbcs" if os.name == "nt" else "latin-1"), "ansi"


def norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def read_m3u(path: Path, library_paths: list[str]) -> ImportResult:
    path = Path(path)
    text, enc = _decode(path.read_bytes(), path.suffix.lower())
    lib = {norm(p): p for p in library_paths}
    res = ImportResult(path.stem, encoding=enc)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            u = urlparse(line)
            if u.scheme != "file":
                res.unsupported.append(line)
                continue
            line = unquote(u.path.lstrip("/")) if u.netloc == "" else f"//{u.netloc}{unquote(u.path)}"
        p = Path(line)
        if not p.is_absolute():
            p = path.parent / p
        key = norm(str(p))
        if key in lib:
            res.paths.append(lib[key])
        elif p.is_file():
            res.outside.append(str(p))
        else:
            res.missing.append(line)
    return res


def m3u8_text(entries: list[tuple[str, float | None, str]]) -> str:
    """``entries``: (path-as-written, duration seconds, display title). LF line ends, UTF-8 text."""
    out = ["#EXTM3U"]
    for ref, dur, title in entries:
        out.append(f"#EXTINF:{int(round(dur)) if dur else -1},{title}")
        out.append(ref)
    return "\n".join(out) + "\n"


def write_m3u8(target: Path, tracks: list[dict], relative: bool) -> None:
    target = Path(target)
    entries = []
    for t in tracks:
        ref = t["path"]
        if relative:
            try:
                ref = os.path.relpath(ref, target.parent)
            except ValueError:  # different drive: keep absolute
                pass
        entries.append((ref, t.get("duration"), display_title(t)))
    target.write_text(m3u8_text(entries), encoding="utf-8", newline="\n")


def display_title(t: dict) -> str:
    artist = t.get("artist") or t.get("albumartist") or ""
    title = t.get("title") or Path(t["path"]).stem
    return f"{artist} - {title}" if artist else title
