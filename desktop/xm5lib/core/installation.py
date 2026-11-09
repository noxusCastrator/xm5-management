"""Process-lifetime update guard shared by the GUI, backend and native launcher.

Setup never asks Restart Manager to close or terminate a process. A common named
mutex makes every profile visible, including explicit script profile overrides.
"""
from __future__ import annotations

import atexit
import os

APP_MUTEX = r"Global\XM5Studio.Application.167851CF-0701-4B99-8FE4-AC2EB066C7D8"
SETUP_MUTEX = r"Global\XM5Studio.Setup.167851CF-0701-4B99-8FE4-AC2EB066C7D8"
_handle = None


def acquire_program_activity():
    global _handle
    if os.name != "nt" or _handle is not None:
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def updating():
        guard = kernel.OpenMutexW(0x00100000, False, SETUP_MUTEX)
        if guard:
            kernel.CloseHandle(guard)
            return True
        if ctypes.get_last_error() != 2:
            raise OSError("Cannot inspect the XM5 Studio setup guard")
        return False

    if updating():
        raise OSError("XM5 Studio setup is running. Finish or close setup, then open the application.")
    handle = kernel.CreateMutexW(None, False, APP_MUTEX)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if updating():
        kernel.CloseHandle(handle)
        raise OSError("XM5 Studio setup started. Close setup before opening the application.")
    _handle = handle
    atexit.register(kernel.CloseHandle, handle)
