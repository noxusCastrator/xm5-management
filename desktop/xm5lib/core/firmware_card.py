"""Pinned firmware and explicit, one-use update-card preparation.

The Windows adapter only admits removable media and USB-backed drive roots.
Tests inject a volume provider backed by owned temporary directories. This is
file copying, not clearing, formatting, partitioning, ejecting or flashing.
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
import tarfile
import threading
import time
import uuid


class FirmwareCardError(RuntimeError):
    pass


UPGRADE_MARGIN = 8 * 1024 * 1024
# V2.43 check_updatefw_exist compares getDiskMemoryInfo's rounded GiB free
# value with 0.10. Retain a conservative 100 MiB floor plus our margin.
STOCK_MIN_FREE = 100 * 1024 * 1024
RECOVERY_NAMES = ('firmware_v20.info', 'recovery-update')


def _payload_sizes(binary):
    """Inspect the verified uncompressed OTA without extracting any member."""
    expected = {'firmware_v20.info', 'recovery-update/xImage', 'recovery-update/rootfs.squashfs'}
    sizes = {}
    try:
        with tarfile.open(binary, 'r:') as archive:
            for member in archive:
                name = member.name.rstrip('/')
                if member.isdir() and name == 'recovery-update' and name not in sizes:
                    sizes[name] = 0
                elif member.isfile() and name in expected and name not in sizes and member.size > 0:
                    sizes[name] = member.size
                else:
                    raise FirmwareCardError('Unexpected bundled firmware archive member: ' + member.name)
        if not expected <= sizes.keys():
            raise FirmwareCardError('Bundled firmware is missing upgrade payloads.')
    except tarfile.TarError as error:
        raise FirmwareCardError('Bundled firmware is not a valid uncompressed upgrade archive.') from error
    return [sizes[name] for name in sorted(expected)]


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
        sectors, sector_bytes, available, total = (w.DWORD() for _ in range(4))
        if not kernel.GetDiskFreeSpaceW(ctypes.c_wchar_p(root), ctypes.byref(sectors), ctypes.byref(sector_bytes),
                                        ctypes.byref(available), ctypes.byref(total)):
            raise FirmwareCardError('Cannot establish the filesystem allocation unit.')
        identity = f'{guid.value}|{serial.value:08x}'
        return {'id': hashlib.sha256((root.upper()+identity).encode()).hexdigest(), 'identity': identity,
                'root': root, 'label': label.value or root, 'capacity': usage.total, 'free': usage.free,
                'filesystem': filesystem.value, 'kind': kind, 'read_only': bool(flags.value & 0x80000),
                'allocation_unit': sectors.value * sector_bytes.value}

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
        return {**data, 'payload_sizes': _payload_sizes(binary)}, binary

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
    def _destination(path):
        """Inspect only update.bin; unrelated files are never traversed."""
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        if _linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
            raise FirmwareCardError('update.bin must be an ordinary regular file, not a link or directory.')
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        digest = _hash(path)
        after = path.lstat()
        if _linked(after) or signature(before) != signature(after):
            raise FirmwareCardError('update.bin changed while checking it. Preview again.')
        return {'identity': signature(after), 'sha256': digest, 'size': after.st_size}

    @staticmethod
    def _no_recovery_conflict(root):
        for name in RECOVERY_NAMES:
            path = root / name
            try:
                path.lstat()  # Includes broken links; nothing at this path is ours to remove.
            except FileNotFoundError:
                continue
            raise FirmwareCardError(f'Upgrade path already exists: {path}. Move it elsewhere yourself, then retry.')

    @staticmethod
    def _space(row, firmware, destination):
        unit = row.get('allocation_unit', 128 * 1024)
        if type(unit) is not int or unit <= 0:
            raise FirmwareCardError('Invalid filesystem allocation unit.')
        allocated = lambda n: ((n + unit - 1) // unit) * unit
        copy = allocated(firmware['size'])
        previous = allocated(destination['size']) if destination else 0
        # Three extracted files, a recovery directory, and an explicit margin.
        reserve = max(sum(allocated(n) for n in firmware['payload_sizes']) + unit,
                      STOCK_MIN_FREE) + UPGRADE_MARGIN
        required = max(copy, copy - previous + reserve)
        return required, reserve, previous

    @classmethod
    def _check_space(cls, row, firmware, destination, *, copied=False):
        required, reserve, previous = cls._space(row, firmware, destination)
        needed = max(0, reserve - previous) if copied else required
        if row['free'] < needed:
            raise FirmwareCardError(f'Not enough free space for copying and player extraction: '
                                    f'need {needed} bytes free, have {row["free"]}.')
        return required, reserve

    def plan(self, target_id: str):
        firmware, _ = self._firmware()
        row, root = self._target(target_id)
        with _directory(root):
            self._no_recovery_conflict(root)
            destination = self._destination(root / 'update.bin')
            required, reserve = self._check_space(row, firmware, destination)
        token = uuid.uuid4().hex
        self._plans = {key: value for key, value in self._plans.items() if time.monotonic()-value['created'] < 600}
        self._plans[token] = {'target': row, 'destination': destination, 'firmware': firmware, 'created': time.monotonic()}
        return {'token': token, 'target_id': target_id, 'root': str(root), 'label': row['label'],
                'capacity': row['capacity'], 'existing_update': destination is not None,
                'existing_size': destination['size'] if destination else 0,
                'required_free': required, 'upgrade_reserve': reserve,
                'firmware_version': firmware['version'], 'firmware_sha256': firmware['sha256']}

    def execute(self, token: str, *, confirmed: bool = False, progress=None):
        if not self._lock.acquire(blocking=False):
            raise FirmwareCardError('Another update-card operation is running.')
        temporary = None
        temporary_identity = None
        published = False
        try:
            plan = self._plans.get(token)
            if plan is None or time.monotonic()-plan['created'] >= 600:
                raise FirmwareCardError('This confirmation expired or was already used. Preview again.')
            if plan['destination'] is not None and confirmed is not True:
                raise FirmwareCardError('Explicit replacement confirmation is required; no files were changed.')
            self._plans.pop(token)
            firmware, source = self._firmware()
            if firmware != plan['firmware']:
                raise FirmwareCardError('Bundled firmware changed. Preview again.')
            row, root = self._target(plan['target']['id'])
            if row['identity'] != plan['target']['identity'] or str(root) != str(Path(plan['target']['root']).absolute()):
                raise FirmwareCardError('The volume changed. Preview again.')
            self._check_space(row, firmware, plan['destination'])
            def check():
                current, _ = self._target(row['id'])
                if current['identity'] != row['identity']:
                    raise FirmwareCardError('The drive was removed or replaced.')
            def notify(phase):
                if progress:
                    progress({'phase': phase, 'completed': 0, 'total': 0})
            with source.open('rb') as original, _directory(root):
                check()
                self._no_recovery_conflict(root)
                destination = root / 'update.bin'
                if self._destination(destination) != plan['destination']:
                    raise FirmwareCardError('update.bin changed after preview. Preview and confirm again.')
                notify('copy')
                temporary = root / ('.xm5-update-' + uuid.uuid4().hex + '.tmp')
                with temporary.open('xb') as output:
                    info = os.fstat(output.fileno())
                    temporary_identity = (info.st_dev, info.st_ino)
                    while block := original.read(1024 * 1024):
                        check()
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                notify('verify')
                if _hash(temporary) != firmware['sha256']:
                    raise FirmwareCardError('Copied firmware failed verification.')
                check()
                if self._destination(destination) != plan['destination']:
                    raise FirmwareCardError('update.bin changed during copying. Preview and confirm again.')
                self._no_recovery_conflict(root)
                current, _ = self._target(row['id'])
                if current['identity'] != row['identity']:
                    raise FirmwareCardError('The drive was removed or replaced.')
                self._check_space(current, firmware, plan['destination'], copied=True)
                if plan['destination'] is not None:
                    os.replace(temporary, destination)
                elif os.name == 'nt':
                    # Windows rename fails if a same-name file appeared meanwhile.
                    os.rename(temporary, destination)
                else:
                    # The production adapter is Windows-only. Keep fixture publication
                    # no-clobber on POSIX too, rather than using overwriting rename.
                    os.link(temporary, destination)
                    temporary.unlink()
                published = True
                notify('complete')
            return {'root': str(root), 'sha256': firmware['sha256'], 'size': firmware['size'], 'status': 'complete'}
        except Exception as error:
            suffix = (' Firmware was copied; completion reporting failed.' if published else
                      ' Firmware was not replaced. Other files were left unchanged.')
            raise FirmwareCardError(str(error)+suffix) from error
        finally:
            if temporary is not None and temporary_identity is not None and not published:
                try:
                    check()
                    info = temporary.lstat()
                    if not _linked(info) and (info.st_dev, info.st_ino) == temporary_identity:
                        temporary.unlink()
                except (OSError, FirmwareCardError):
                    pass  # A disconnected volume may retain only our partial copy.
            self._lock.release()
