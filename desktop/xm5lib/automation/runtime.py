"""Per-profile ownership and private discovery; no fixed port or global singleton."""
import ctypes
import json
import os
import re
import subprocess
import uuid
from pathlib import Path


def default_home():
    from ..core.portable import PortablePaths
    return PortablePaths.discover(Path(__file__).resolve().parents[2]).backend


def runtime_path(home=None):
    from ..core.portable import PortablePaths
    paths = PortablePaths.discover(Path(__file__).resolve().parents[2])
    home = Path(home or paths.backend).resolve()
    return paths.runtime / 'backend' if home == paths.backend.resolve() else home / 'runtime' 


def secure_directory(path):
    path = Path(path)
    no_links(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        if path.stat().st_uid != os.getuid():
            raise PermissionError('Runtime directory belongs to another user')
        path.chmod(0o700)
        return
    # Explicit protected DACL, not POSIX chmod pretending to protect Windows tokens.
    sid = re.search(rb'S-1-\d+(?:-\d+)+', subprocess.check_output(
        ['whoami', '/user', '/fo', 'csv', '/nh'], creationflags=subprocess.CREATE_NO_WINDOW)).group().decode('ascii')
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.c_void_p()
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                                                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.SetFileSecurityW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    sddl = f'D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)'
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(pointer), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not adv.SetFileSecurityW(str(path), 0x80000004, pointer):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(pointer)


def no_links(path):
    path=Path(path).absolute()
    for part in (path,*path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(),'st_file_attributes',0)&0x400):
            raise PermissionError('Runtime paths must not contain links or reparse points')


def check_private(path):
    path=Path(path);no_links(path)
    if os.name!='nt':
        info=path.stat()
        if info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise PermissionError('Discovery permissions are not private')
        return
    adv=ctypes.WinDLL('advapi32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    size=ctypes.c_uint32()
    adv.GetFileSecurityW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.POINTER(ctypes.c_uint32)]
    adv.GetFileSecurityW(str(path),5,None,0,ctypes.byref(size))
    data=ctypes.create_string_buffer(size.value)
    if not adv.GetFileSecurityW(str(path),5,data,size,ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer=ctypes.c_void_p()
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(data,1,5,ctypes.byref(pointer),None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        sddl=ctypes.wstring_at(pointer)
    finally:
        kernel.LocalFree(pointer)
    sid=re.search(rb'S-1-\d+(?:-\d+)+',subprocess.check_output(['whoami','/user','/fo','csv','/nh'],
                    creationflags=subprocess.CREATE_NO_WINDOW)).group().decode('ascii')
    owner=re.search(r'O:(.*?)(?=[GDS]:|$)',sddl)
    aces=re.findall(r'\(([^)]+)\)',sddl)
    if not owner or owner[1] not in (sid,'SY') or not aces or any(ace.split(';')[-1] not in (sid,'SY') for ace in aces):
        raise PermissionError('Discovery ACL or owner is not private')


class ProfileLock:
    def __init__(self, runtime):
        self.runtime = Path(runtime)
        secure_directory(self.runtime)
        target = self.runtime / 'owner.lock'
        if target.is_symlink():
            raise ValueError('Invalid lock file')
        self.file = open(target, 'a+b')
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                if not target.stat().st_size:
                    self.file.write(b'0'); self.file.flush(); self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('A backend already owns this profile') from None

    def close(self):
        if not self.file.closed:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()


def write_discovery(runtime, record):
    target = Path(runtime) / 'connection.json'
    if target.is_symlink():
        raise ValueError('Invalid discovery file')
    temp = target.with_name('connection.'+uuid.uuid4().hex+'.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(record, f); f.flush(); os.fsync(f.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def remove_discovery(runtime, instance):
    target = Path(runtime) / 'connection.json'
    try:
        if json.loads(target.read_text(encoding='utf-8')).get('instance_id') == instance:
            target.unlink()
    except (OSError, ValueError, TypeError, AttributeError):
        pass
