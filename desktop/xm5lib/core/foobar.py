"""Hand a selection to the installed foobar2000, and say precisely what happened.

Background (verification/diag/DIAGNOSIS.md): a foobar2000 started by a Low-integrity process inherits
Low integrity and cannot write its Medium-labelled profile, failing with "Cannot access configuration
folder: Access denied". So before spawning we check our own integrity and any running instance, and
after spawning we classify the outcome instead of assuming success:

* ``blocked``    – preflight found a condition that would make foobar fail; nothing was started
* ``spawn_failed`` – CreateProcess failed (missing exe, OS error)
* ``app_error``  – foobar is showing its error dialog (text included)
* ``forwarded``  – the new process handed the command to an already running instance and exited
* ``started``    – a new foobar instance is running with its main window
* ``pending``    – still starting when the observation window ended
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import playlists, winenv

ERROR_TITLE = "foobar2000 encountered an error"
EXE_NAME = "foobar2000.exe"


@dataclass
class Preflight:
    exe: Path | None
    own_integrity: int | None
    instances: list[winenv.ProcessInfo] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


@dataclass
class Outcome:
    status: str
    message: str
    pid: int | None = None
    exit_code: int | None = None
    playlist: str | None = None
    detail: dict = field(default_factory=dict)


def locate(configured: str | None = None) -> Path | None:
    candidates = [configured] if configured else []
    candidates += [str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "foobar2000" / EXE_NAME),
                   str(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "foobar2000" / EXE_NAME)]
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\foobar2000.exe") as k:
                    candidates.append(winreg.QueryValue(k, None))
            except OSError:
                pass
    except ImportError:
        pass
    for c in candidates:
        if c and Path(c.strip('"')).is_file():
            return Path(c.strip('"'))
    return None


def error_dialogs(pid: int) -> list[str]:
    """Texts of visible foobar error dialogs owned by ``pid``."""
    out = []
    for w in winenv.windows_of(pid, with_children=True):
        if w.visible and w.title == ERROR_TITLE:
            msgs = [t for t in w.texts if len(t) > 3] or [w.title]
            out.append(max(msgs, key=len))
    return out


def preflight(configured: str | None = None) -> Preflight:
    exe = locate(configured)
    own = winenv.integrity_level()
    pf = Preflight(exe, own, winenv.processes(EXE_NAME))
    if exe is None:
        pf.blockers.append("foobar2000.exe was not found. Set its location in Settings.")
    if own is not None and own < winenv.MEDIUM:
        pf.blockers.append(
            f"XM5 Library is running at {winenv.level_name(own)} integrity. A foobar2000 started from here would "
            "inherit it and fail with 'Cannot access configuration folder: Access denied'. Start XM5 Library with "
            "XM5Library.cmd (it uses the base Python interpreter), not with the .venv interpreter or from a "
            "sandboxed tool.")
    for p in pf.instances:
        errs = error_dialogs(p.pid)
        if errs:
            pf.blockers.append(f"foobar2000 (pid {p.pid}, {winenv.level_name(p.integrity)} integrity) is showing an "
                               f"error: '{errs[0]}'. Close that dialog/instance first.")
        elif p.integrity is not None and own is not None and p.integrity != own:
            pf.blockers.append(f"A foobar2000 instance (pid {p.pid}) runs at {winenv.level_name(p.integrity)} integrity, "
                               f"this app at {winenv.level_name(own)}. Commands would be forwarded to it unreliably; "
                               "close it or restart it normally.")
        elif p.integrity is None:
            pf.warnings.append(f"Could not inspect foobar2000 pid {p.pid}; the command will be forwarded to it.")
    return pf


def write_handoff(home: Path, tracks: list[dict]) -> Path:
    """UTF-8 M3U8 of absolute paths in the app data folder (no command-line length limit)."""
    folder = home / "handoff"
    folder.mkdir(parents=True, exist_ok=True)
    for old in sorted(folder.glob("selection-*.m3u8"))[:-20]:  # keep the last few for diagnosis
        try:
            old.unlink()
        except OSError:
            pass
    target = folder / time.strftime("selection-%Y%m%d-%H%M%S.m3u8")
    n = 1
    while target.exists():
        target = folder / time.strftime(f"selection-%Y%m%d-%H%M%S-{n}.m3u8")
        n += 1
    playlists.write_m3u8(target, tracks, relative=False)
    return target


def command(exe: Path, playlist: Path, mode: str) -> list[str]:
    """``add`` appends to foobar's active playlist; ``open`` lets foobar open/play it (its default action)."""
    if mode == "add":
        return [str(exe), "/add", str(playlist)]
    if mode == "open":
        return [str(exe), str(playlist)]
    raise ValueError(mode)


def send(home: Path, tracks: list[dict], mode: str = "add", configured: str | None = None,
         observe_seconds: float = 6.0, spawn=subprocess.Popen) -> Outcome:
    if not tracks:
        return Outcome("blocked", "Nothing selected.")
    pf = preflight(configured)
    if not pf.ok:
        return Outcome("blocked", " ".join(pf.blockers), detail={"preflight": pf.blockers})
    playlist = write_handoff(home, tracks)
    before = {p.pid for p in pf.instances}
    args = command(pf.exe, playlist, mode)
    try:
        proc = spawn(args, cwd=str(pf.exe.parent), close_fds=True,
                     creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except OSError as exc:
        return Outcome("spawn_failed", f"Could not start foobar2000: {exc}", playlist=str(playlist),
                       detail={"args": args})
    return observe(proc, before, playlist, args, observe_seconds)


def observe(proc, before: set[int], playlist: Path, args: list[str], seconds: float) -> Outcome:
    deadline = time.monotonic() + seconds
    detail = {"args": args, "existing_instances": sorted(before)}
    while True:
        code = proc.poll()
        errs = error_dialogs(proc.pid) if code is None else []
        for pid in before:
            errs += error_dialogs(pid)
        if errs:
            return Outcome("app_error", f"foobar2000 reported: {errs[0]}", proc.pid, code, str(playlist), detail)
        if code is not None:
            if before:
                status = "forwarded" if code == 0 else "app_error"
                msg = (f"Sent {Path(playlist).name} to the running foobar2000 (pid {', '.join(map(str, sorted(before)))})."
                       if code == 0 else f"foobar2000 exited with code {code} while forwarding.")
                return Outcome(status, msg, proc.pid, code, str(playlist), detail)
            return Outcome("app_error", f"foobar2000 exited immediately with code {code}.", proc.pid, code,
                           str(playlist), detail)
        titles = [w.title for w in winenv.windows_of(proc.pid) if w.visible and w.title]
        if titles and time.monotonic() > deadline - seconds / 2:
            detail["windows"] = titles
            return Outcome("started", f"foobar2000 started (pid {proc.pid}).", proc.pid, None, str(playlist), detail)
        if time.monotonic() > deadline:
            detail["windows"] = titles
            return Outcome("pending", f"foobar2000 (pid {proc.pid}) is still starting; no error seen so far.",
                           proc.pid, None, str(playlist), detail)
        time.sleep(0.25)
