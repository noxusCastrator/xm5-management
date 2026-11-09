"""Standalone, standard-library-only verification/restoration for XM5 card formats v1 and v2.

This exact file is also distributed as RESTORE_LIBRARY.py. Importing it has no side effects.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import unicodedata
import uuid

SCHEMA = "xm5.card-library"
VERSION = 2
META = "Struct"
MANIFEST = META + "/manifest.json"
CHECKSUM = META + "/manifest.sha256"
INCOMPLETE = META + "/INCOMPLETE"


class RestoreError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


def safe_relative(value):
    """A portable exact relative path; no traversal, drive, alias or device name."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise RestoreError("unsafe_path", "Expected a portable relative path", {"path": str(value)})
    p = PurePosixPath(value)
    if p.is_absolute() or str(p) != value:
        raise RestoreError("unsafe_path", "Non-canonical relative path", {"path": value})
    for part in p.parts:
        stem = part.split(".", 1)[0].upper()
        if (part in (".", "..") or part[-1:] in (".", " ") or any(ord(c) < 32 for c in part)
                or any(c in '<>"|?*' for c in part)
                or stem in {"CON", "PRN", "AUX", "NUL"}
                or stem in {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}):
            raise RestoreError("unsafe_path", "Unsafe portable filename", {"path": value})
    return value


def path_key(value):
    return unicodedata.normalize("NFC", safe_relative(value)).casefold()


def no_links(path):
    p = Path(os.path.abspath(path))
    for part in (p, *p.parents):
        try:
            reparse = bool(getattr(part.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        except FileNotFoundError:
            reparse = False
        if reparse or part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise RestoreError("symlink_refused", "Symlinks/junctions are not accepted", {"path": str(part)})
    return p


def checked_path(root, relative):
    return no_links(Path(root).joinpath(*PurePosixPath(safe_relative(relative)).parts))


def file_digest(path):
    no_links(path)
    if not stat.S_ISREG(Path(path).lstat().st_mode):
        raise RestoreError("not_regular", "Expected a regular file", {"path": str(path)})
    h = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise RestoreError("not_regular", "Expected a regular file", {"path": str(path)})
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
            size += len(block)
    return size, h.hexdigest()


def _windows_create(parent, name, *, directory, create=False, exclusive=False, desired_access=None):
    """NtCreateFile relative to a pinned directory, never traversing a reparse."""
    import ctypes
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]
    class ObjectAttributes(ctypes.Structure):
        _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                    ("ObjectName", ctypes.POINTER(UnicodeString)), ("Attributes", wintypes.ULONG),
                    ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]
    class StatusUnion(ctypes.Union):
        _fields_ = [("Status", wintypes.LONG), ("Pointer", ctypes.c_void_p)]
    class IoStatus(ctypes.Structure):
        _fields_ = [("Result", StatusUnion), ("Information", ctypes.c_size_t)]

    native = ctypes.WinDLL("ntdll")
    call = native.NtCreateFile
    call.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG, ctypes.POINTER(ObjectAttributes),
                     ctypes.POINTER(IoStatus), ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG,
                     wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
    call.restype = wintypes.LONG
    text = ctypes.create_unicode_buffer(name)
    encoded = name.encode("utf-16-le")
    string = UnicodeString(len(encoded), len(encoded) + 2, ctypes.cast(text, ctypes.c_void_p))
    attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), parent, ctypes.pointer(string), 0x40, None, None)
    result, status = wintypes.HANDLE(), IoStatus()
    # FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE for directories;
    # GENERIC_WRITE | FILE_READ_ATTRIBUTES | SYNCHRONIZE for a new regular file.
    access = 0x001000A0 if directory else 0x40100080
    if desired_access is not None:
        access = desired_access
    disposition = 2 if exclusive else 3 if create else 1  # CREATE / OPEN_IF / OPEN
    options = 0x00200000 | 0x20 | (1 if directory else 0x40)  # OPEN_REPARSE_POINT, synchronous, dir/non-dir
    code = call(ctypes.byref(result), access, ctypes.byref(attributes), ctypes.byref(status), None,
                0x10 if directory else 0x80, 3, disposition, options, None, 0)
    if code < 0:
        convert = native.RtlNtStatusToDosError
        convert.argtypes, convert.restype = [wintypes.LONG], wintypes.ULONG
        raise ctypes.WinError(convert(code))
    try:
        _windows_reject_reparse(result.value)
    except BaseException:
        _windows_close(result.value)
        raise
    return result.value


def _windows_close(handle):
    import ctypes
    from ctypes import wintypes
    call = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    call.argtypes, call.restype = [wintypes.HANDLE], wintypes.BOOL
    call(handle)


def _windows_reject_reparse(handle):
    import ctypes
    from ctypes import wintypes
    class AttributeTag(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]
    call = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    call.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    call.restype = wintypes.BOOL
    info = AttributeTag()
    if not call(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.FileAttributes & 0x400:
        raise RestoreError("symlink_refused", "Reparse points are not accepted by the pinned writer")


def _windows_anchor(anchor):
    import ctypes
    from ctypes import wintypes
    path = anchor if anchor.startswith("\\\\?\\") else "\\\\?\\UNC\\" + anchor[2:] if anchor.startswith("\\\\") else "\\\\?\\" + anchor
    call = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    call.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    call.restype = wintypes.HANDLE
    handle = call(path, 0x001000A0, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _windows_reject_reparse(handle)
    except BaseException:
        _windows_close(handle)
        raise
    return handle


@contextmanager
def _pinned_directory(root, relative="", *, create=False):
    """Pin each component from the filesystem anchor through an owned directory.

    Windows handles omit FILE_SHARE_DELETE and remain open until the caller is
    finished. POSIX walks use directory descriptors and O_NOFOLLOW at every hop.
    Only relative descendants of the already-existing root may be created.
    """
    root = Path(os.path.abspath(root))
    descendant = PurePosixPath(safe_relative(relative)).parts if relative else ()
    root_parts = root.parts[1:]
    handles = []
    windows = os.name == "nt"
    try:
        if windows:
            handles.append(_windows_anchor(root.anchor))
        else:
            handles.append(os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)))
        for index, name in enumerate((*root_parts, *descendant)):
            may_create = create and index >= len(root_parts)
            if windows:
                handle = _windows_create(handles[-1], name, directory=True, create=may_create)
            else:
                if may_create:
                    try:
                        os.mkdir(name, dir_fd=handles[-1])
                    except FileExistsError:
                        pass
                handle = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=handles[-1])
            handles.append(handle)
        yield handles[-1]
    finally:
        for handle in reversed(handles):
            _windows_close(handle) if windows else os.close(handle)


def _exclusive_leaf(parent, name):
    """Open a new regular file relative to the currently pinned parent."""
    if os.name == "nt":
        import msvcrt
        handle = _windows_create(parent, name, directory=False, exclusive=True)
        try:
            return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            _windows_close(handle)
            raise
    return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o666, dir_fd=parent)


@contextmanager
def exclusive_output(root, relative):
    """Create output exclusively using pinned parents, with no path-based reopen."""
    parts = PurePosixPath(safe_relative(relative)).parts
    with _pinned_directory(root, "/".join(parts[:-1]), create=True) as parent:
        descriptor = _exclusive_leaf(parent, parts[-1])
        with os.fdopen(descriptor, "wb") as stream:
            yield stream


def ensure_directory(root, relative):
    """Create an owned relative directory using the same pinned component walk."""
    with _pinned_directory(root, safe_relative(relative), create=True):
        pass


def make_new_directory(root, relative):
    """Create an exclusive owned directory while pinning every parent."""
    parts = PurePosixPath(safe_relative(relative)).parts
    with _pinned_directory(root, "/".join(parts[:-1]), create=True) as parent:
        if os.name == "nt":
            handle = _windows_create(parent, parts[-1], directory=True, exclusive=True)
            _windows_close(handle)
        else:
            os.mkdir(parts[-1], dir_fd=parent)


def replace_owned(root, source_relative, destination_relative):
    """Publish an owned metadata temporary within one pinned directory."""
    source = PurePosixPath(safe_relative(source_relative))
    destination = PurePosixPath(safe_relative(destination_relative))
    if source.parent != destination.parent:
        raise RestoreError("unsafe_path", "Owned metadata replacement must stay in one directory")
    parent_relative = source.parent.as_posix()
    with _pinned_directory(root, "" if parent_relative == "." else parent_relative) as parent:
        if os.name == "nt":
            _windows_publish(parent, source.name, destination.name)
        else:
            os.replace(source.name, destination.name, src_dir_fd=parent, dst_dir_fd=parent)


def _windows_publish(parent, source_name, destination_name):
    """Rename an owned file handle relative to its still-pinned parent handle."""
    return _windows_rename(parent, source_name, parent, destination_name, directory=False, replace=True)


def _windows_rename(source_parent, source_name, destination_parent, destination_name, *, directory, replace):
    import ctypes
    from ctypes import wintypes
    class RenameInformation(ctypes.Structure):
        _fields_ = [("ReplaceIfExists", ctypes.c_ubyte), ("RootDirectory", wintypes.HANDLE),
                    ("FileNameLength", wintypes.ULONG), ("FileName", wintypes.WCHAR * 1)]
    encoded = destination_name.encode("utf-16-le")
    data = ctypes.create_string_buffer(RenameInformation.FileName.offset + len(encoded))
    info = ctypes.cast(data, ctypes.POINTER(RenameInformation)).contents
    info.ReplaceIfExists, info.RootDirectory, info.FileNameLength = int(replace), destination_parent, len(encoded)
    ctypes.memmove(ctypes.addressof(data) + RenameInformation.FileName.offset, encoded, len(encoded))
    # DELETE | READ_ATTRIBUTES | SYNCHRONIZE; OPEN_REPARSE_POINT still applies.
    source = _windows_create(source_parent, source_name, directory=directory,
                             desired_access=0x001100A0 if directory else 0x00110080)
    try:
        native = ctypes.WinDLL("ntdll")
        call = native.NtSetInformationFile
        call.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG, ctypes.c_int]
        call.restype = wintypes.LONG
        status = (ctypes.c_size_t * 2)()
        code = call(source, ctypes.byref(status), data, len(data), 10)
        if code < 0:
            convert = native.RtlNtStatusToDosError
            convert.argtypes, convert.restype = [wintypes.LONG], wintypes.ULONG
            raise ctypes.WinError(convert(code))
    finally:
        _windows_close(source)


def rename_owned_directory(source, destination):
    """Move an owned directory by pinned parents, atomically refusing collisions."""
    source, destination = Path(os.path.abspath(source)), Path(os.path.abspath(destination))
    safe_relative(source.name)
    safe_relative(destination.name)
    with _pinned_directory(source.parent) as old_parent, _pinned_directory(destination.parent) as new_parent:
        if os.name == "nt":
            _windows_rename(old_parent, source.name, new_parent, destination.name, directory=True, replace=False)
        else:
            import ctypes
            native = ctypes.CDLL(None, use_errno=True)
            call = getattr(native, "renameat2", None)
            if call is None:
                raise RestoreError("secure_rename_unavailable", "Exclusive directory rename is unavailable on this POSIX runtime")
            call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            call.restype = ctypes.c_int
            if call(old_parent, os.fsencode(source.name), new_parent, os.fsencode(destination.name), 1):
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(destination))


def unlink_owned(root, relative):
    """Remove one owned marker without reopening its parents by path."""
    path = PurePosixPath(safe_relative(relative))
    parent_relative = path.parent.as_posix()
    with _pinned_directory(root, "" if parent_relative == "." else parent_relative) as parent:
        if os.name == "nt":
            os.unlink(Path(root).joinpath(*path.parts))
        else:
            os.unlink(path.name, dir_fd=parent)


def new_destination(destination, sources):
    dest = no_links(destination)
    if os.path.lexists(dest):
        raise RestoreError("destination_exists", "Destination must be a new directory", {"path": str(dest)})
    for source in sources:
        source = no_links(source).resolve()
        if dest == source or dest.is_relative_to(source) or source.is_relative_to(dest):
            raise RestoreError("root_overlap", "Source and destination may not overlap", {"source": str(source), "destination": str(dest)})
    if not dest.parent.is_dir():
        raise RestoreError("missing_parent", "Destination parent must exist", {"path": str(dest.parent)})
    return dest


def detect_layout(root):
    """Detect exact metadata paths without migrating or writing either format."""
    root = no_links(root)
    found = []
    for version, meta in ((2, "Struct"), (1, ".xm5")):
        if checked_path(root, meta + "/manifest.json").is_file() or checked_path(root, meta + "/INCOMPLETE").exists():
            found.append({"version": version, "meta": meta, "manifest": meta + "/manifest.json",
                          "checksum": meta + "/manifest.sha256", "incomplete": meta + "/INCOMPLETE",
                          "playlists": "Struct/playlists" if version == 2 else "_explaylist_data",
                          "inbox": "Struct/inbox" if version == 2 else "XM5_INBOX",
                          "art": "Struct/covers/native" if version == 2 else ".xm5/art/native",
                          "art_index": "Struct/indexes/art-index.bin" if version == 2 else ".xm5/art-index.bin",
                          "art_ready": "Struct/indexes/art.ready" if version == 2 else ".xm5/art.ready",
                          "art_tsv": "Struct/indexes/art-index.tsv" if version == 2 else ".xm5/art-index.tsv",
                          "restore_tool": "Struct/recovery/RESTORE_LIBRARY.py" if version == 2 else "RESTORE_LIBRARY.py"})
    if len(found) != 1:
        raise RestoreError("ambiguous_layout" if found else "unowned_destination",
                           "Expected exactly one recognized XM5 metadata layout", {"path": str(root)})
    if found[0]["version"] == 2 and root.name != "xm5":
        raise RestoreError("container_name", "Version 2 container must be named exactly xm5")
    return found[0]


def playlist_files(root):
    """Read only native working playlists from the detected layout."""
    layout = detect_layout(root)
    start = checked_path(root, layout["playlists"])
    if not start.exists():
        return []
    _, files = _snapshot_card_tree(root, layout["playlists"])
    return [checked_path(root, item["path"]) for item in files
            if PurePosixPath(item["path"]).suffix.lower() in {".m3u", ".m3u8"}]


def load_manifest(root):
    root = no_links(root)
    layout = detect_layout(root)
    if checked_path(root, layout["incomplete"]).exists():
        raise RestoreError("incomplete_export", "Export has not committed")
    p = checked_path(root, layout["manifest"])
    if p.stat().st_size > 64 * 1024 * 1024:
        raise RestoreError("manifest_too_large", "Manifest exceeds 64 MiB")
    data = p.read_bytes()
    expected = checked_path(root, layout["checksum"]).read_text(encoding="ascii").strip()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RestoreError("manifest_checksum", "Manifest checksum mismatch")
    m = json.loads(data.decode("utf-8"))
    if not isinstance(m, dict):
        raise RestoreError("invalid_manifest", "Expected a manifest object")
    if m.get("schema") != SCHEMA or m.get("version") != layout["version"] or m.get("status") != "complete":
        raise RestoreError("unsupported_format", "Unsupported or incomplete card manifest")
    if not isinstance(m.get("files"), list) or not isinstance(m.get("roots"), list) or not isinstance(m.get("build_id"), str):
        raise RestoreError("invalid_manifest", "Missing file/root records")
    seen, restore_seen = set(), set()
    for entry in m["files"]:
        key = path_key(entry["path"])
        if key in seen:
            raise RestoreError("path_collision", "Duplicate/aliased manifest path", {"path": entry["path"]})
        seen.add(key)
        if not isinstance(entry.get("size"), int) or entry["size"] < 0 or len(entry.get("sha256", "")) != 64:
            raise RestoreError("invalid_manifest", "Invalid file checksum/size")
        if entry.get("restore_path") is not None:
            key = path_key(entry["restore_path"])
            if key in restore_seen:
                raise RestoreError("restore_collision", "Duplicate/aliased restoration path")
            restore_seen.add(key)
    for entry in m.get("directories", []) + m.get("original_directories", []):
        safe_relative(entry["path"])
        if entry.get("restore_path") is not None:
            safe_relative(entry["restore_path"])
    return m


def verify_export(root):
    problems = []
    modified_return_files = []
    checked = 0
    try:
        m = load_manifest(root)
        for entry in m["files"]:
            try:
                size, digest = file_digest(checked_path(root, entry["path"]))
                if size != entry["size"] or digest != entry["sha256"]:
                    if (entry.get("kind") == "generated_playlist"
                            and entry["path"].startswith(detect_layout(root)["playlists"] + "/")
                            and PurePosixPath(entry["path"]).suffix.lower() in {".m3u", ".m3u8"}):
                        modified_return_files.append({"path": entry["path"], "expected_sha256": entry["sha256"], "observed_sha256": digest})
                    else:
                        problems.append({"code": "file_checksum", "path": entry["path"]})
                checked += 1
            except (OSError, RestoreError) as exc:
                problems.append({"code": getattr(exc, "code", "unreadable_file"), "path": entry["path"], "message": str(exc)})
        for entry in m.get("directories", []) + m.get("original_directories", []):
            p = checked_path(root, entry["path"])
            if not p.is_dir():
                problems.append({"code": "missing_directory", "path": entry["path"]})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "root": str(Path(root).absolute()), "checked": checked,
                "problems": [{"code": getattr(exc, "code", "invalid_manifest"), "message": str(exc)}]}
    return {"ok": not problems, "root": str(Path(root).absolute()), "checked": checked,
            "build_id": m["build_id"], "problems": problems, "modified_return_files": modified_return_files}


def _snapshot_card_tree(root, prefix):
    """Observe ordinary input bytes now; these hashes are not an original-PC baseline."""
    start = checked_path(root, prefix)
    if not os.path.lexists(start):
        return [], []
    if not start.is_dir():
        raise RestoreError("not_directory", "Expected a card additions directory", {"path": prefix})
    directories, files, seen = [], [], set()
    def register(relative):
        key = path_key(relative)
        if key in seen:
            raise RestoreError("path_collision", "Aliased card addition path", {"path": relative})
        seen.add(key)
    def walk_error(error):
        raise error
    for folder, names, leaves in os.walk(start, followlinks=False, onerror=walk_error):
        names.sort(key=lambda n: (n.casefold(), n))
        leaves.sort(key=lambda n: (n.casefold(), n))
        folder = no_links(folder)
        relative = folder.relative_to(root).as_posix()
        register(relative)
        directories.append(relative)
        for name in names:
            checked_path(root, (folder / name).relative_to(root).as_posix())
        for name in leaves:
            source = no_links(folder / name)
            relative = source.relative_to(root).as_posix()
            register(relative)
            size, digest = file_digest(source)
            files.append({"path": relative, "size": size, "sha256": digest})
    return directories, files


def _recovery_plan(root, manifest):
    entries = []
    directories = [d["restore_path"] for d in manifest.get("directories", []) + manifest.get("original_directories", []) if d.get("restore_path")]
    for record in manifest["files"]:
        if record.get("restore_path"):
            entries.append({"path": record["path"], "restore_path": record["restore_path"],
                            "size": record["size"], "sha256": record["sha256"],
                            "origin": "manifest_original", "integrity_basis": "manifest",
                            "original_source_known": True})
    tracked = {path_key(record["path"]): record for record in manifest["files"]}
    original_targets = [e["restore_path"] for e in entries] + directories
    layout = detect_layout(root)
    inbox_dirs, inbox_files = _snapshot_card_tree(root, layout["inbox"])
    playlist_dirs, playlist_files = _snapshot_card_tree(root, layout["playlists"])
    playlist_files_selected = [e for e in playlist_files if PurePosixPath(e["path"]).suffix.lower() in {".m3u", ".m3u8"}]
    additions_present = bool(inbox_files or len(inbox_dirs) > 1)
    for namespace, present in (("Card additions", additions_present), ("Card playlists", bool(playlist_files_selected))):
        if present and any(path_key(PurePosixPath(p).parts[0]) == path_key(namespace) for p in original_targets):
            raise RestoreError("restore_collision", "Recovery namespace overlaps original files", {"namespace": namespace})
    if additions_present:
        directories.extend("Card additions/" + d for d in inbox_dirs)
    for record in inbox_files:
        baseline = tracked.get(path_key(record["path"]))
        if baseline and (baseline["size"], baseline["sha256"]) != (record["size"], record["sha256"]):
            raise RestoreError("file_checksum", "Tracked inbox file changed", {"path": record["path"]})
        entries.append({**record, "restore_path": "Card additions/" + record["path"],
                        "origin": "card_addition", "integrity_basis": "manifest" if baseline else "observed_at_recovery",
                        "original_source_known": False, "baseline_sha256": baseline["sha256"] if baseline else None})
    for record in playlist_files_selected:
        baseline = tracked.get(path_key(record["path"]))
        relative = PurePosixPath(record["path"]).relative_to(layout["playlists"]).as_posix()
        entries.append({**record, "restore_path": "Card playlists/" + relative,
                        "origin": "card_playlist", "integrity_basis": "observed_at_recovery",
                        "original_source_known": False, "baseline_sha256": baseline["sha256"] if baseline else None,
                        "modified_since_export": record["sha256"] != baseline["sha256"] if baseline else None})
    # Preflight every destination, including parent aliases and reserved report/marker paths.
    file_keys = {path_key("RESTORE_INCOMPLETE"), path_key("RECOVERY_REPORT.json")}
    directory_keys = set()
    directory_spelling = {}
    def add_directory(relative):
        parts = PurePosixPath(safe_relative(relative)).parts
        for index in range(1, len(parts) + 1):
            spelling = "/".join(parts[:index])
            key = path_key(spelling)
            if key in file_keys:
                raise RestoreError("restore_collision", "File/directory recovery collision", {"path": relative})
            if key in directory_spelling and directory_spelling[key] != spelling:
                raise RestoreError("restore_collision", "Aliased recovery directory", {"path": relative})
            directory_spelling[key] = spelling
            directory_keys.add(key)
    for directory in directories:
        add_directory(directory)
    for entry in entries:
        relative = safe_relative(entry["restore_path"])
        key = path_key(relative)
        if key in file_keys or key in directory_keys:
            raise RestoreError("restore_collision", "Duplicate/aliased recovery file", {"path": relative})
        parent = PurePosixPath(relative).parent.as_posix()
        if parent != ".":
            add_directory(parent)
        file_keys.add(key)
    return entries, directories, [e["path"] for e in playlist_files if e not in playlist_files_selected]


def restore_export(root, destination):
    root = no_links(root).resolve()
    report = verify_export(root)
    if not report["ok"]:
        raise RestoreError("invalid_export", "Refusing to restore an invalid export", report)
    m = load_manifest(root)
    entries, directories, ignored_playlist_files = _recovery_plan(root, m)
    dest = new_destination(destination, [root])
    make_new_directory(dest.parent, dest.name)
    marker = dest / "RESTORE_INCOMPLETE"
    with exclusive_output(dest, "RESTORE_INCOMPLETE") as stream:
        stream.write(b"Restoration did not finish. Do not merge this directory.\n")
    restored = []
    try:
        for directory in directories:
            ensure_directory(dest, directory)
        for entry in entries:
            src = checked_path(root, entry["path"])
            target = checked_path(dest, entry["restore_path"])
            with src.open("rb") as source, exclusive_output(dest, entry["restore_path"]) as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            if file_digest(target) != (entry["size"], entry["sha256"]):
                raise RestoreError("file_checksum", "File changed during restore", {"path": entry["path"]})
            restored.append(entry["restore_path"])
        result = {"ok": True, "destination": str(dest), "build_id": m["build_id"], "restored": restored,
                  "root_mapping": m["roots"], "peq_root_mapping": m.get("peq_roots", []),
                  "lyrics": m.get("lyrics", []), "favorites": m.get("favorites", []),
                  "preset_metadata": m.get("preset_metadata", {}), "source_files_modified": False, "full_card_backup": False,
                  "observed_at_utc": datetime.now(timezone.utc).isoformat(), "mappings": entries,
                  "card_additions": [e for e in entries if e["origin"] == "card_addition"],
                  "card_playlists": [e for e in entries if e["origin"] == "card_playlist"],
                  "ignored_playlist_files": ignored_playlist_files,
                  "unmanaged_policy": "Untracked files outside the detected inbox and native M3U/M3U8 playlists are left untouched and are not copied; this is not a full-card backup."}
        result["recovery_report"] = "RECOVERY_REPORT.json"
        with exclusive_output(dest, "RECOVERY_REPORT.json") as output:
            output.write((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        unlink_owned(dest, "RESTORE_INCOMPLETE")
    except Exception:
        # A fresh partial tree is retained for diagnosis, never mistaken for complete.
        raise
    return result


def _update_fingerprint(root):
    records = []
    def onerror(error):
        raise error
    for folder, directories, files in os.walk(no_links(root), followlinks=False, onerror=onerror):
        directories.sort(key=lambda x: (x.casefold(), x))
        files.sort(key=lambda x: (x.casefold(), x))
        folder = no_links(folder)
        for name in [*directories, *files]:
            no_links(folder / name)
        records.append(["directory", folder.relative_to(root).as_posix()])
        for name in files:
            path = folder / name
            records.append(["file", path.relative_to(root).as_posix(), *file_digest(path)])
    raw = json.dumps(records, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def recover_update(transaction):
    """Standard-library-only rollback of an explicitly selected owned update.

    No path stored in JSON authorizes a move: all paths are recomputed from the
    checked transaction name and parent. Unknown or changed roots are refused.
    """
    control = no_links(transaction)
    match = re.fullmatch(r"\.xm5-update-([0-9a-f]{32})", control.name)
    if not match:
        raise RestoreError("unowned_transaction", "Expected an owned update transaction directory")
    build_id = match.group(1)
    owner = json.loads(checked_path(control, "OWNER.json").read_text(encoding="utf-8"))
    if owner != {"schema": "xm5.update-owner", "version": 1, "build_id": build_id}:
        raise RestoreError("unowned_transaction", "Update ownership marker does not match")
    journal_path = checked_path(control, "transaction.json")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("schema") != "xm5.update-transaction" or journal.get("build_id") != build_id:
        raise RestoreError("unowned_transaction", "Update journal does not match ownership")
    active = no_links(control.parent / "xm5")
    backup_parent = no_links(control.parent / (".xm5-backup-" + build_id))
    backup = checked_path(backup_parent, "xm5")
    candidate = checked_path(control, "xm5")
    old_fp, new_fp = journal["original_fingerprint"], journal.get("candidate_fingerprint")
    if journal.get("status") == "complete":
        if not active.exists() or not verify_export(active)["ok"]:
            raise RestoreError("recovery_conflict", "Completed update's active export no longer verifies")
        return {"ok": True, "status": "complete", "active": True, "root": str(active), "backup_root": str(backup)}
    def rename(source, destination):
        rename_owned_directory(source, destination)
    before_move = journal.get("status") in {"preparing", "prepared", "moving_original"}
    if active.exists() and not backup.exists() and (before_move or _update_fingerprint(active) == old_fp):
        pass  # The original export never moved; preserve any later user drift too.
    elif backup.exists() and _update_fingerprint(backup) == old_fp:
        backup_owner = json.loads(checked_path(backup_parent, "OWNER.json").read_text(encoding="utf-8"))
        if backup_owner != {"schema": "xm5.backup-owner", "version": 1, "build_id": build_id}:
            raise RestoreError("unowned_transaction", "Backup ownership marker does not match")
        if active.exists():
            if not new_fp or _update_fingerprint(active) != new_fp or candidate.exists():
                raise RestoreError("recovery_conflict", "Active/candidate bytes changed; recovery requires review")
            rename(active, candidate)
        rename(backup, active)
        if _update_fingerprint(active) != old_fp:
            raise RestoreError("recovery_failed", "Original export did not verify after rollback")
    else:
        raise RestoreError("recovery_conflict", "Original export location or bytes changed; refusing recovery moves")
    journal.update(status="rolled_back", candidate_root=str(candidate))
    temporary_name = "transaction.json.tmp-" + uuid.uuid4().hex
    with exclusive_output(control, temporary_name) as stream:
        stream.write(json.dumps(journal, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    replace_owned(control, temporary_name, "transaction.json")
    return {"ok": True, "status": "rolled_back", "active": True, "root": str(active), "candidate_root": str(candidate)}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Verify or restore an XM5 card library to a NEW directory; Python standard library only")
    parser.add_argument("root", type=Path)
    parser.add_argument("destination", nargs="?", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--recover-update", action="store_true", help="Explicitly roll back the owned update transaction supplied as root")
    args = parser.parse_args(argv)
    try:
        result = recover_update(args.root) if args.recover_update else verify_export(args.root) if args.verify else restore_export(args.root, args.destination) if args.destination else None
        if result is None:
            parser.error("provide a new destination or --verify")
    except (OSError, ValueError) as exc:
        result = {"ok": False, "code": getattr(exc, "code", "restore_failed"), "message": str(exc), "details": getattr(exc, "details", {})}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
