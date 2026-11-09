"""Windows process environment inspection (read-only).

Used for read-only diagnostics. The portable app uses its bundled Python and
media tools; it does not need installed music applications. Nothing here changes
tokens, ACLs or other processes.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass, field

IS_WINDOWS = sys.platform == "win32"

# Mandatory integrity RIDs.
UNTRUSTED, LOW, MEDIUM, MEDIUM_PLUS, HIGH, SYSTEM = 0x0, 0x1000, 0x2000, 0x2100, 0x3000, 0x4000

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25
_TH32CS_SNAPPROCESS = 0x2


def level_name(rid: int | None) -> str:
    if rid is None:
        return "unknown"
    if rid < LOW:
        return "Untrusted"
    if rid < MEDIUM:
        return "Low"
    if rid < HIGH:
        return "Medium"
    if rid < SYSTEM:
        return "High"
    return "System"


if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)

    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
    _adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    _adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    _adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    _adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _u32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _u32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.IsWindowVisible.argtypes = [wintypes.HWND]


def _token_integrity(token) -> int | None:
    size = wintypes.DWORD()
    _adv.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not _adv.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, buf, size, ctypes.byref(size)):
        return None
    sid = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_MANDATORY_LABEL.Label.Sid
    count = _adv.GetSidSubAuthorityCount(sid).contents.value
    return int(_adv.GetSidSubAuthority(sid, count - 1).contents.value)


def integrity_level(pid: int | None = None) -> int | None:
    """Mandatory integrity RID of ``pid`` (default: this process); None if unknown."""
    if not IS_WINDOWS:
        return None
    if pid is None:
        handle, close = _k32.GetCurrentProcess(), False
    else:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        close = True
        if not handle:
            return None
    try:
        token = wintypes.HANDLE()
        if not _adv.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            return _token_integrity(token)
        finally:
            _k32.CloseHandle(token)
    finally:
        if close:
            _k32.CloseHandle(handle)


@dataclass
class ProcessInfo:
    pid: int
    parent: int
    name: str
    integrity: int | None = None


def processes(name: str | None = None) -> list[ProcessInfo]:
    """Running processes, optionally filtered by executable name (case-insensitive)."""
    if not IS_WINDOWS:
        return []
    snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            exe = entry.szExeFile
            if name is None or exe.lower() == name.lower():
                out.append(ProcessInfo(entry.th32ProcessID, entry.th32ParentProcessID, exe))
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    for p in out:
        p.integrity = integrity_level(p.pid)
    return out


@dataclass
class WindowInfo:
    title: str
    visible: bool
    texts: list[str] = field(default_factory=list)


def windows_of(pid: int, with_children: bool = False) -> list[WindowInfo]:
    """Top-level windows of ``pid``; optionally the text of their child controls (dialog text)."""
    if not IS_WINDOWS:
        return []
    found: list[tuple[int, WindowInfo]] = []

    def text(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        _u32.GetWindowTextW(hwnd, buf, 1024)
        return buf.value

    @_WNDENUMPROC
    def top(hwnd, _):
        owner = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            found.append((hwnd, WindowInfo(text(hwnd), bool(_u32.IsWindowVisible(hwnd)))))
        return True

    _u32.EnumWindows(top, 0)
    if with_children:
        for hwnd, info in found:
            if not info.visible:
                continue

            @_WNDENUMPROC
            def child(h, _, info=info):
                t = text(h)
                if t:
                    info.texts.append(t)
                return True

            _u32.EnumChildWindows(hwnd, child, 0)
    return [w for _, w in found]


def known_folder(name: str) -> str | None:
    """Resolve LocalAppData / RoamingAppData / LocalAppDataLow via SHGetKnownFolderPath."""
    if not IS_WINDOWS:
        return None
    guids = {
        "LocalAppData": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
        "RoamingAppData": "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",
        "LocalAppDataLow": "{A520A1A4-1780-4FF6-BD18-167343C5AF16}",
    }

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    g = GUID()
    ctypes.oledll.ole32.CLSIDFromString(guids[name], ctypes.byref(g))
    out = ctypes.c_wchar_p()
    shell = ctypes.WinDLL("shell32")
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                           ctypes.POINTER(ctypes.c_wchar_p)]
    if shell.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return out.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def environment_summary() -> dict:
    il = integrity_level()
    return {"pid": os.getpid(), "integrity_rid": il, "integrity": level_name(il),
            "python": sys.version.split()[0], "executable": sys.executable}
