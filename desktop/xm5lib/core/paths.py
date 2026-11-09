"""Locations of persistent state, independent of cwd and of where the app is installed."""
from __future__ import annotations

import os
from pathlib import Path

from . import winenv

DEFAULT_MUSIC_ROOT = Path("D:/music/main")
DEFAULT_FOOBAR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "foobar2000" / "foobar2000.exe"
DEFAULT_PEACE_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "EqualizerAPO" / "config"
FOOBAR_PROFILE = Path(os.environ.get("APPDATA", "")) / "foobar2000-v2"


def app_home() -> Path:
    """Per-user state folder.

    ``XM5LIB_HOME`` overrides it (tests, portable use). Otherwise LocalAppData\\XM5Library.
    A Low-integrity process cannot write LocalAppData; it gets LocalAppDataLow instead so the UI
    can still open and explain the problem, and ``is_fallback_home`` reports that.
    """
    override = os.environ.get("XM5LIB_HOME")
    if override:
        return Path(override)
    il = winenv.integrity_level()
    if il is not None and il < winenv.MEDIUM:
        base = winenv.known_folder("LocalAppDataLow") or str(Path.home() / "AppData" / "LocalLow")
        return Path(base) / "XM5Library"
    base = winenv.known_folder("LocalAppData") or os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "XM5Library"


def is_fallback_home(home: Path) -> bool:
    return "LocalLow" in home.parts


def ensure_home(home: Path | None = None) -> Path:
    home = home or app_home()
    for sub in ("", "covers", "handoff", "backups", "logs"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home
