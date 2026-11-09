"""Resolve shipped executables without PATH, registry, installed apps or downloads."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

APP_ROOT = Path(__file__).resolve().parents[2]
_TOOLS = {"mpv": "vendor/mpv/mpv.exe", "ffmpeg": "vendor/ffmpeg/ffmpeg.exe"}
_verified: dict[tuple, Path] = {}
_lock = threading.Lock()


class BundledRuntimeError(RuntimeError):
    pass


def require_tool(name: str) -> Path:
    """Require an unchanged bundled executable. Failure remains visible to callers."""
    if name not in _TOOLS:
        raise ValueError(f"Unknown bundled tool: {name}")
    relative = _TOOLS[name]
    path = APP_ROOT / relative
    manifest = APP_ROOT / "vendor" / "runtime-manifest.json"
    try:
        stat, manifest_stat = path.stat(), manifest.stat()
        key = (relative, stat.st_size, stat.st_mtime_ns, manifest_stat.st_size, manifest_stat.st_mtime_ns)
        with _lock:
            if key in _verified:
                return _verified[key]
            expected = json.loads(manifest.read_text(encoding="utf-8"))["files"][relative]
            if stat.st_size != expected["size"]:
                raise BundledRuntimeError(f"Bundled {name} size mismatch; restore the portable package.")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != expected["sha256"]:
                raise BundledRuntimeError(f"Bundled {name} checksum mismatch; restore the portable package.")
            _verified[key] = path
            return path
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BundledRuntimeError(f"Bundled {name} is missing or invalid; restore the portable package: {exc}") from exc


def runtime_summary() -> dict:
    result = {"mode": "bundled-windows-x64", "app_root": str(APP_ROOT), "path_lookup": False}
    for name in _TOOLS:
        try:
            result[name] = {"available": True, "path": str(require_tool(name))}
        except BundledRuntimeError as exc:
            result[name] = {"available": False, "error": str(exc)}
    return result
