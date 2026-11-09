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
    """Compatibility entry using the same profile resolver as GUI and clients."""
    from .portable import PortablePaths
    return PortablePaths.discover().data


def is_fallback_home(home: Path) -> bool:
    return "LocalLow" in home.parts


def ensure_home(home: Path | None = None) -> Path:
    home = home or app_home()
    for sub in ("", "covers", "handoff", "backups", "logs"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home
