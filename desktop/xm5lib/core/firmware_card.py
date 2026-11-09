"""Pinned firmware and explicit, one-use update-card preparation.

The Windows adapter only admits removable media and USB-backed drive roots.
Tests inject a volume provider backed by owned temporary directories. This is
file deletion/copying, not formatting, partitioning, ejecting or flashing.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid


class FirmwareCardError(RuntimeError):
    pass


def _hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


@contextmanager
def _directory(path):
    """Hold a Windows directory against rename/reparse replacement while walking."""
    if _linked(path.lstat()) or not path.is_dir():
        raise FirmwareCardError('A directory changed or is a link. Refresh the target.')
    handle = None
    if os.name == 'nt':
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        # Share read/write, but not delete: the directory cannot be renamed while held.
        handle = kernel.CreateFileW(str(path), 0x80, 3, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        class AttributeTag(ctypes.Structure):
            _fields_ = [('attributes', w.DWORD), ('tag', w.DWORD)]
        value = AttributeTag()
        kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(value), ctypes.sizeof(value)) or value.attributes & 0x400:
            kernel.CloseHandle(handle)
            raise FirmwareCardError('Refuse a reparse-point directory.')
    try:
        yield
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


class WindowsVolumes:
    def __init__(self):
        self._roots = {}

    @staticmethod
    def _usb_roots():
        # OS storage inventory only. No device commands, raw handles or writes.
        command = (
                   "$ErrorActionPreference='Stop'; @("
                   "Get-CimInstance Win32_DiskDrive | Where-Object InterfaceType -eq 'USB' | "
                   "Get-CimAssociatedInstance -Association Win32_DiskDriveToDiskPartition | "
                   "Get-CimAssociatedInstance -Association Win32_LogicalDiskToPartition | "
                   "Select-Object -ExpandProperty DeviceID -Unique) | ConvertTo-Json -Compress")
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                capture_output=True, timeout=20, check=True, creationflags=0x08000000)
            rows = json.loads(result.stdout.decode('utf-8-sig') or '[]')
            if isinstance(rows, str):
                rows = [rows]
            return {str(value).upper()+'\\' for value in rows if re.fullmatch(r'[A-Za-z]:', str(value))}
        except (OSError, ValueError, subprocess.SubprocessError):
            return set()  # Unverified fixed disks are never offered.

    @staticmethod
    def _read(root):
        if os.name != 'nt' or not re.fullmatch(r'[A-Za-z]:\\', root):
            raise FirmwareCardError('A Windows drive root is required.')
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetDriveTypeW.argtypes = [w.LPCWSTR]
        kind = kernel.GetDriveTypeW(root)
        label, filesystem, guid = (ctypes.create_unicode_buffer(n) for n in (261, 261, 1024))
        serial, maximum, flags = w.DWORD(), w.DWORD(), w.DWORD()
        if not kernel.GetVolumeInformationW(ctypes.c_wchar_p(root), label, len(label), ctypes.byref(serial),
                ctypes.byref(maximum), ctypes.byref(flags), filesystem, len(filesystem)):
            raise FirmwareCardError('The drive is unavailable or has no readable media.')
        if not kernel.GetVolumeNameForVolumeMountPointW(ctypes.c_wchar_p(root), guid, len(guid)):
            raise FirmwareCardError('Cannot establish the volume identity.')
        usage = shutil.disk_usage(root)
        identity = f'{guid.value}|{serial.value:08x}'
        return {'id': hashlib.sha256((root.upper()+identity).encode()).hexdigest(), 'identity': identity,
                'root': root, 'label': label.value or root, 'capacity': usage.total, 'free': usage.free,
                'filesystem': filesystem.value, 'kind': kind, 'read_only': bool(flags.value & 0x80000)}

    def list_targets(self):
        if os.name != 'nt':
            return []
        usb = self._usb_roots()
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        result, self._roots = [], {}
        for index in range(26):
            if not mask & (1 << index):
                continue
            root = chr(65+index)+':\\'
            try:
                kind = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
                if kind != 2 and not (kind == 3 and root in usb):
                    continue
                row = self._read(root)
                if row['kind'] != 2 and not (row['kind'] == 3 and root in usb):
                    continue
                self._roots[row['id']] = root
                result.append(row)
            except (OSError, FirmwareCardError):
                continue
        return result

    def inspect(self, target_id):
        root = self._roots.get(target_id)
        if root is None:
            raise FirmwareCardError('Refresh and select a removable or USB drive.')
        row = self._read(root)
        if row['id'] != target_id:
            raise FirmwareCardError('The selected drive changed. Refresh and confirm again.')
        return row


class FirmwareCardService:
    def __init__(self, app_root: Path, volume_provider=None):
        self.app_root = Path(app_root).resolve()
        self.provider = volume_provider if volume_provider is not None else WindowsVolumes()
        self._plans = {}
        self._lock = threading.Lock()

    def _firmware(self):
        folder = self.app_root / 'firmware'
        manifest, binary = folder / 'manifest.json', folder / 'update.bin'
        for path in (folder, manifest, binary):
            if _linked(path.lstat()):
                raise FirmwareCardError('Bundled firmware must not be a link.')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if (data.get('schema') != 'xm5.firmware-bundle.v1' or data.get('file') != 'update.bin'
                or not isinstance(data.get('version'), str) or data.get('size') != binary.stat().st_size
                or not re.fullmatch('[0-9a-f]{64}', data.get('sha256', '')) or _hash(binary) != data['sha256']):
            raise FirmwareCardError('Bundled firmware integrity check failed.')
        return data, binary

    def firmware_info(self):
        try:
            data, _ = self._firmware()
            return {**data, 'available': True, 'error': ''}
        except (OSError, ValueError, FirmwareCardError) as error:
            return {'available': False, 'version': '', 'sha256': '', 'size': 0, 'error': str(error)}

    def _target(self, target_id):
        row = self.provider.inspect(target_id)
        root = Path(row['root']).absolute()
        if _linked(root.lstat()) or root.resolve() != root:
            raise FirmwareCardError('The selected root contains a link or changed mount.')
        if row['filesystem'].upper() not in {'FAT', 'FAT32', 'EXFAT'}:
            raise FirmwareCardError('Use a FAT/FAT32/exFAT card; this operation does not format drives.')
        if row.get('read_only'):
            raise FirmwareCardError('The selected volume is write-protected.')
        from .portable import PortablePaths
        locations = PortablePaths.discover(self.app_root)
        protected = [self.app_root, locations.root.resolve(), locations.backend.resolve(),
                     locations.runtime.resolve(), locations.cache.resolve(), Path.home().resolve()]
        for name in ('SystemRoot', 'WINDIR', 'ProgramFiles', 'ProgramFiles(x86)'):
            if os.environ.get(name):
                protected.append(Path(os.environ[name]).resolve())
        if root.parent == root and os.name != 'nt':
            raise FirmwareCardError('The system filesystem root is not a target.')
        if any(path.is_relative_to(root) for path in protected):
            raise FirmwareCardError('Refuse the system, user-profile or running-application drive.')
        return row, root

    def list_targets(self):
        result = []
        for row in self.provider.list_targets():
            try:
                self._target(row['id'])
                result.append({key: row[key] for key in ('id', 'root', 'label', 'capacity', 'free', 'filesystem')})
            except (OSError, KeyError, FirmwareCardError):
                continue
        return result

    @staticmethod
    def _inventory(root):
        rows, total = [], 0
        def walk(folder):
            nonlocal total
            with _directory(folder):
                for path in sorted(folder.iterdir()):
                    info = path.lstat()
                    rows.append((path.relative_to(root).as_posix(), info.st_mode, info.st_size, info.st_mtime_ns,
                                 getattr(info, 'st_file_attributes', 0)))
                    if stat.S_ISDIR(info.st_mode) and not _linked(info):
                        walk(path)
                    else:
                        total += info.st_size
        walk(root)
        return hashlib.sha256(json.dumps(rows).encode()).hexdigest(), len(rows), total

    def plan(self, target_id: str):
        firmware, _ = self._firmware()
        row, root = self._target(target_id)
        if row['capacity'] < firmware['size'] + 1024 * 1024:
            raise FirmwareCardError('The selected drive is too small.')
        fingerprint, count, size = self._inventory(root)
        token = uuid.uuid4().hex
        self._plans = {key: value for key, value in self._plans.items() if time.monotonic()-value['created'] < 600}
        self._plans[token] = {'target': row, 'fingerprint': fingerprint, 'firmware': firmware, 'created': time.monotonic()}
        return {'token': token, 'target_id': target_id, 'root': str(root), 'label': row['label'],
                'capacity': row['capacity'], 'files_count': count, 'bytes_to_remove': size,
                'firmware_version': firmware['version'], 'firmware_sha256': firmware['sha256']}

    def execute(self, token: str, *, confirmed: bool = False, progress=None):
        if confirmed is not True:
            raise FirmwareCardError('Explicit confirmation is required; no files were changed.')
        if not self._lock.acquire(blocking=False):
            raise FirmwareCardError('Another update-card operation is running.')
        started = False
        try:
            plan = self._plans.pop(token, None)
            if plan is None or time.monotonic()-plan['created'] >= 600:
                raise FirmwareCardError('This confirmation expired or was already used. Preview again.')
            firmware, source = self._firmware()
            if firmware != plan['firmware']:
                raise FirmwareCardError('Bundled firmware changed. Preview again.')
            row, root = self._target(plan['target']['id'])
            if row['identity'] != plan['target']['identity'] or str(root) != str(Path(plan['target']['root']).absolute()):
                raise FirmwareCardError('The volume changed. Preview again.')
            if self._inventory(root)[0] != plan['fingerprint']:
                raise FirmwareCardError('Drive contents changed after preview. Preview and confirm again.')
            def check():
                current, _ = self._target(row['id'])
                if current['identity'] != row['identity']:
                    raise FirmwareCardError('The drive was removed or replaced.')
            completed = 0
            def notify(phase):
                if progress:
                    progress({'phase': phase, 'completed': completed, 'total': 0})
            def clear(folder):
                nonlocal completed, started
                with _directory(folder):
                    for path in sorted(folder.iterdir()):
                        check()
                        info = path.lstat()
                        if stat.S_ISDIR(info.st_mode) and not _linked(info):
                            clear(path)
                            check()
                            started = True
                            path.rmdir()
                        elif stat.S_ISDIR(info.st_mode):
                            started = True
                            path.rmdir()  # Remove a junction itself, never its contents.
                        else:
                            started = True
                            path.unlink()  # Unlink symlinks themselves, never their targets.
                        completed += 1
                        notify('clear')
            with source.open('rb') as original, _directory(root):
                clear(root)
                check()
                if any(root.iterdir()):
                    raise FirmwareCardError('New files appeared while clearing the drive.')
                notify('copy')
                temporary = root / 'update.bin.tmp'
                with temporary.open('xb') as output:
                    started = True
                    while block := original.read(1024 * 1024):
                        check()
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                notify('verify')
                if _hash(temporary) != firmware['sha256']:
                    raise FirmwareCardError('Copied firmware failed verification.')
                check()
                temporary.rename(root / 'update.bin')
                if {path.name for path in root.iterdir()} != {'update.bin'}:
                    raise FirmwareCardError('Other files appeared on the drive. Inspect it before use.')
                notify('complete')
            return {'root': str(root), 'sha256': firmware['sha256'], 'size': firmware['size'], 'status': 'complete'}
        except Exception as error:
            suffix = ' The drive may be partially cleared; no rollback is promised.' if started else ' No files were changed.'
            raise FirmwareCardError(str(error)+suffix) from error
        finally:
            self._lock.release()
