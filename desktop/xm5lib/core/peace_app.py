"""Open the installed Peace application (it remains the desktop EQ; nothing is applied from here)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import winenv


def open_peace(config_dir: Path, spawn=subprocess.Popen) -> str:
    exe = Path(config_dir) / "Peace.exe"
    if not exe.is_file():
        raise FileNotFoundError(f"Peace.exe not found in {config_dir}")
    il = winenv.integrity_level()
    if il is not None and il < winenv.MEDIUM:
        raise PermissionError(f"XM5 Library runs at {winenv.level_name(il)} integrity; Peace started from here could "
                              "not save its configuration. Start XM5 Library with XM5Library.cmd.")
    proc = spawn([str(exe)], cwd=str(exe.parent), close_fds=True)
    return f"Peace started (pid {proc.pid})."
