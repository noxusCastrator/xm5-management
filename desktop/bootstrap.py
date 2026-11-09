"""Launch only the application's bundled, isolated Windows Python runtime."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent


def _activate() -> None:
    sys.dont_write_bytecode = True
    expected = APP / "vendor" / "python"
    if Path(sys.executable).resolve().parent != expected.resolve():
        raise SystemExit("Start XM5Library.cmd; the bundled Python runtime is required.")
    if not sys.flags.isolated or not (sys.flags.no_site or sys.flags.no_user_site):
        raise SystemExit("The bundled runtime must use its isolated python314._pth configuration.")
    # The relative _pth also applies to backend children launched with -m.
    # No site.addsitedir, user site, environment PYTHONPATH or .venv is consulted.
    os.environ["PYTHONNOUSERSITE"] = "1"
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    from xm5lib.core.bundled_runtime import require_tool
    require_tool("mpv")
    require_tool("ffmpeg")


def check() -> int:
    import importlib.metadata
    import subprocess
    import PySide6.QtCore
    import PySide6.QtWidgets
    import mutagen
    import PIL.Image
    import fastapi
    import uvicorn
    import pydantic_core
    import sqlite3
    from xm5lib.core.bundled_runtime import require_tool
    versions = {name: importlib.metadata.version(name) for name in
                ("PySide6-Essentials", "shiboken6", "mutagen", "Pillow", "fastapi", "uvicorn", "pydantic")}
    for name in ("mpv", "ffmpeg"):
        completed = subprocess.run([str(require_tool(name)), "--version" if name == "mpv" else "-version"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=15, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        versions[name] = completed.stdout.decode("utf-8", "replace").splitlines()[0]
    print(json.dumps({"status": "ok", "executable": sys.executable, "isolated": sys.flags.isolated,
                      "no_site": sys.flags.no_site, "no_user_site": sys.flags.no_user_site,
                      "sys_path": sys.path, "versions": versions}, ensure_ascii=False))
    return 0


def main() -> int:
    _activate()
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        return check()
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        from xm5lib.cli import main as cli
        return cli(sys.argv[2:])
    if len(sys.argv) > 2 and sys.argv[1] == "script":
        import runpy
        sys.argv = sys.argv[2:]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return 0
    from xm5lib.ui.app import main as gui
    return gui(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
