"""Install the approved Windows runtime beside the live desktop source."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.request
import zipfile

REPO = Path(__file__).resolve().parents[1]
ARCHIVE_NAME = "XM5Library-Windows-x64-0.2.0.zip"
ARCHIVE_ROOT = "XM5Library-0.2.0"
RELEASE_URL = (
    "https://github.com/noxusCastrator/xm5-management/releases/download/"
    "v0.2.0/" + ARCHIVE_NAME
)


@dataclass(frozen=True)
class RuntimeLock:
    archive_sha256: str
    archive_size: int
    manifest_sha256: str
    installed_manifest_sha256: str = ""


APPROVED = RuntimeLock(
    "e3303599d251199373297feb631a9930f246dfe902bf2151daaa2440d0143a29",
    121859733,
    "b1ea56500314408d9c8580bd6a0624af5089e4533bd8162dd176da00ab4c3ee3",
    "6e972f894e7a3410a80f67b91db8d6553cf27ea65a304e20db95d63f4dc16290",
)
MANIFEST = "vendor/runtime-manifest.json"
REPARSE_POINT = 0x400
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_RESERVED.update(prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³")


class SetupError(ValueError):
    """A dependency input or destination did not pass validation."""


def _ordinary(path: Path, *, directory: bool = False) -> None:
    details = path.lstat()
    valid_type = stat.S_ISDIR(details.st_mode) if directory else stat.S_ISREG(details.st_mode)
    if not valid_type or getattr(details, "st_file_attributes", 0) & REPARSE_POINT:
        raise SetupError(f"Refuse link, reparse point or nonordinary path: {path}")


def _no_links(path: Path) -> None:
    """Check ancestors before opening or creating a path, including junctions."""
    for parent in reversed(path.parents):
        if os.path.lexists(parent):
            _ordinary(parent, directory=True)
    if os.path.lexists(path):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & REPARSE_POINT:
            raise SetupError(f"Refuse link or reparse point: {path}")


def _within(root: Path, path: Path) -> None:
    if not path.is_absolute() or not path.is_relative_to(root) or ".." in path.parts:
        raise SetupError(f"Path leaves the repository: {path}")
    _no_links(path)


def _mkdir(root: Path, path: Path) -> None:
    _within(root, path)
    for part in reversed((path, *path.parents)):
        if not part.is_relative_to(root):
            continue
        if not os.path.lexists(part):
            part.mkdir()
        _ordinary(part, directory=True)


def _sha(path: Path) -> str:
    _no_links(path)
    _ordinary(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SetupError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(data: bytes) -> dict:
    value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise SetupError("Expected a JSON object")
    return value


def release_lock(repo: Path) -> RuntimeLock:
    path = repo / "release.json"
    _no_links(path)
    _ordinary(path)
    release = _json(path.read_bytes())
    if "development_runtime" in release:
        asset = release["development_runtime"]
        if (not isinstance(asset, dict) or asset.get("asset_filename") != ARCHIVE_NAME
                or asset.get("tag") != "v0.2.0" or asset.get("url") != RELEASE_URL):
            raise SetupError("development_runtime does not match the approved 0.2.0 desktop archive")
    else:
        assets = release.get("assets", {})
        if not isinstance(assets, dict):
            raise SetupError("release.json assets must be an object")
        asset = assets.get(ARCHIVE_NAME, {})
    if (not isinstance(asset, dict) or asset.get("sha256") != APPROVED.archive_sha256
            or asset.get("size") != APPROVED.archive_size):
        raise SetupError("release.json does not match the approved 0.2.0 desktop archive")
    return APPROVED


def _parts(name: str, *, directory: bool = False) -> tuple[str, ...]:
    if not isinstance(name, str) or not name or "\\" in name:
        raise SetupError(f"Unsafe archive/manifest path: {name!r}")
    if directory and name.endswith("/"):
        name = name[:-1]
    parts = tuple(name.split("/"))
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                or part.split(".", 1)[0].upper() in _RESERVED):
            raise SetupError(f"Unsafe archive/manifest path: {name!r}")
    return parts


def _manifest(data: bytes, expected_sha256: str) -> dict[str, dict]:
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise SetupError("Runtime manifest SHA-256 mismatch")
    manifest = _json(data)
    rows = manifest.get("files")
    if manifest.get("schema") != "xm5.bundled-runtime.v1" or not isinstance(rows, dict) or not rows:
        raise SetupError("Unknown or empty runtime manifest")
    folded = set()
    for name, record in rows.items():
        parts = _parts(name)
        if len(parts) < 2 or parts[0] != "vendor" or name == MANIFEST:
            raise SetupError(f"Runtime manifest path is outside its payload: {name}")
        if name.casefold() in folded:
            raise SetupError(f"Case-colliding runtime manifest path: {name}")
        folded.add(name.casefold())
        if (not isinstance(record, dict) or type(record.get("size")) is not int
                or record["size"] < 0 or not isinstance(record.get("sha256"), str)
                or not _DIGEST.fullmatch(record["sha256"])):
            raise SetupError(f"Invalid runtime manifest record: {name}")
    return rows


def is_bytecode_path(name: str) -> bool:
    """Identify derived interpreter caches, which are never runtime inputs."""
    parts = name.replace("\\", "/").casefold().split("/")
    return "__pycache__" in parts or parts[-1].endswith((".pyc", ".pyo"))


def normalized_manifest(data: bytes, lock: RuntimeLock = APPROVED) -> bytes:
    """Authenticate release metadata, then remove only generated bytecode rows."""
    rows = _manifest(data, lock.manifest_sha256)
    manifest = _json(data)
    manifest["files"] = {name: record for name, record in rows.items() if not is_bytecode_path(name)}
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def verify_archive(archive: Path, lock: RuntimeLock) -> None:
    _no_links(archive)
    _ordinary(archive)
    if archive.stat().st_size != lock.archive_size or _sha(archive) != lock.archive_sha256:
        raise SetupError(f"Archive size/SHA-256 mismatch: {archive}")


def _entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    payload = {}
    seen = {}
    infos = archive.infolist()
    if len(infos) > 20000 or sum(info.file_size for info in infos) > 2 * 1024**3:
        raise SetupError("Archive exceeds the runtime extraction limits")
    for info in infos:
        # ZipInfo.filename can truncate an embedded NUL; inspect the original too.
        if info.orig_filename != info.filename:
            raise SetupError("Archive path contains a NUL")
        parts = _parts(info.filename, directory=info.is_dir())
        kind = stat.S_IFMT(info.external_attr >> 16)
        if (kind not in (0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG)
                or info.external_attr & REPARSE_POINT or info.flag_bits & 1):
            raise SetupError(f"Refuse special, linked or encrypted ZIP member: {info.filename}")
        if parts[0] != ARCHIVE_ROOT:
            raise SetupError(f"Unexpected archive root: {info.filename}")
        key = "/".join(parts).casefold()
        if key in seen:
            raise SetupError(f"Duplicate/case-colliding ZIP member: {info.filename}")
        seen[key] = info.is_dir()
        if len(parts) >= 3 and parts[1] == "vendor" and not info.is_dir():
            payload["/".join(parts[1:])] = info
    for key in seen:
        parents = key.split("/")[:-1]
        for length in range(1, len(parents) + 1):
            parent = "/".join(parents[:length])
            if parent in seen and not seen[parent]:
                raise SetupError(f"ZIP file is also a parent directory: {parent}")
    return payload


def verify_vendor(app: Path, lock: RuntimeLock) -> int:
    vendor = app / "vendor"
    _no_links(vendor)
    _ordinary(vendor, directory=True)
    manifest = app / MANIFEST
    _no_links(manifest)
    _ordinary(manifest)
    if manifest.stat().st_size > 8 * 1024**2:
        raise SetupError("Runtime manifest is too large")
    rows = _manifest(manifest.read_bytes(), lock.installed_manifest_sha256)
    found = set()
    for current, directories, files in os.walk(vendor, followlinks=False):
        for name in directories:
            _ordinary(Path(current) / name, directory=True)
        for name in files:
            path = Path(current) / name
            _ordinary(path)
            relative = path.relative_to(app).as_posix()
            _parts(relative)
            if not is_bytecode_path(relative):
                found.add(relative)
    if found != set(rows) | {MANIFEST}:
        raise SetupError("Existing vendor has missing or extra files; review it manually before setup")
    for name, record in rows.items():
        path = app / name
        if path.stat().st_size != record["size"] or _sha(path) != record["sha256"]:
            raise SetupError(f"Runtime file size/SHA-256 mismatch: {name}")
    return len(rows)


def _remove_stage(repo: Path, stage: Path) -> None:
    # Only remove this invocation's new, ordinary private staging directory.
    _within(repo, stage)
    if not stage.resolve().is_relative_to((repo / ".local" / "dev-setup").resolve()):
        raise SetupError(f"Refuse staging cleanup outside dev-setup: {stage}")
    for current, directories, files in os.walk(stage, followlinks=False):
        _ordinary(Path(current), directory=True)
        for name in directories:
            _ordinary(Path(current) / name, directory=True)
        for name in files:
            _ordinary(Path(current) / name)
    shutil.rmtree(stage)


def install_archive(repo: Path, archive: Path, lock: RuntimeLock) -> str:
    """Validate a locked archive and atomically install only its vendor tree."""
    app = repo / "desktop"
    _no_links(app)
    _ordinary(app, directory=True)
    verify_archive(archive, lock)
    with zipfile.ZipFile(archive) as zipped:
        entries = _entries(zipped)
        if MANIFEST not in entries or entries[MANIFEST].file_size > 8 * 1024**2:
            raise SetupError("Missing or oversized runtime manifest")
        raw_manifest = zipped.read(entries[MANIFEST])
        raw_rows = _manifest(raw_manifest, lock.manifest_sha256)
        installed_manifest = normalized_manifest(raw_manifest, lock)
        rows = _manifest(installed_manifest, lock.installed_manifest_sha256)
        if set(entries) != set(raw_rows) | {MANIFEST}:
            raise SetupError("ZIP vendor files do not match the runtime manifest")
        for name, record in raw_rows.items():
            if entries[name].file_size != record["size"]:
                raise SetupError(f"ZIP/runtime manifest size mismatch: {name}")
        vendor = app / "vendor"
        if os.path.lexists(vendor):
            verify_vendor(app, lock)
            return "Existing desktop/vendor verified; no files changed."
        work = repo / ".local" / "dev-setup"
        _mkdir(repo, work)
        stage = Path(tempfile.mkdtemp(prefix="runtime-", dir=work))
        try:
            for name, info in entries.items():
                if name == MANIFEST:
                    destination = stage / MANIFEST
                    _mkdir(repo, destination.parent)
                    destination.write_bytes(installed_manifest)
                    continue
                destination = stage / name
                cached = is_bytecode_path(name)
                if not cached:
                    _mkdir(repo, destination.parent)
                digest = hashlib.sha256()
                size = 0
                with zipped.open(info) as source:
                    output = None if cached else destination.open("xb")
                    try:
                        while block := source.read(1024 * 1024):
                            size += len(block)
                            if size > info.file_size:
                                raise SetupError(f"ZIP member exceeded its declared size: {name}")
                            digest.update(block)
                            if output is not None:
                                output.write(block)
                    finally:
                        if output is not None:
                            output.close()
                if size != raw_rows[name]["size"] or digest.hexdigest() != raw_rows[name]["sha256"]:
                    raise SetupError(f"Runtime file size/SHA-256 mismatch: {name}")
            verify_vendor(stage, lock)
            _within(repo, vendor)
            if os.path.lexists(vendor):
                raise SetupError("desktop/vendor appeared during setup; leaving it unchanged")
            # On Windows rename refuses an existing destination, including an empty directory.
            os.rename(stage / "vendor", vendor)
        finally:
            _remove_stage(repo, stage)
    return "Installed approved runtime in desktop/vendor. Run desktop\\XM5Library.cmd."


def download_archive(repo: Path, lock: RuntimeLock) -> Path:
    work = repo / ".local" / "dev-setup"
    _mkdir(repo, work)
    cache = work / ARCHIVE_NAME
    if os.path.lexists(cache):
        verify_archive(cache, lock)
        return cache
    descriptor, temporary = tempfile.mkstemp(prefix="download-", suffix=".part", dir=work)
    temporary = Path(temporary)
    try:
        deadline = time.monotonic() + 180
        request = urllib.request.Request(RELEASE_URL, headers={"User-Agent": "XM5-desktop-source-setup/0.2.0"})
        with os.fdopen(descriptor, "wb") as output:
            with urllib.request.urlopen(request, timeout=20) as response:
                if not response.geturl().startswith("https://"):
                    raise SetupError("Download redirected outside HTTPS")
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > lock.archive_size or time.monotonic() > deadline:
                        raise SetupError("Download exceeded its size or 180-second time limit")
                    output.write(block)
        verify_archive(temporary, lock)
        _within(repo, cache)
        if os.path.lexists(cache):
            raise SetupError("Archive cache appeared during download; leaving it unchanged")
        os.rename(temporary, cache)
        return cache
    finally:
        if temporary.exists():
            _within(repo, temporary)
            _ordinary(temporary)
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--archive", type=Path, help="Use the exact approved release ZIP offline")
    options.add_argument("--check-only", action="store_true", help="Verify installed vendor without downloading or writing")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        parser.error("Python 3.12 or newer is required")
    if os.name != "nt":
        parser.error("Desktop runtime setup supports Windows x64 only")
    try:
        lock = release_lock(REPO)
        if args.check_only or (args.archive is None and os.path.lexists(REPO / "desktop" / "vendor")):
            count = verify_vendor(REPO / "desktop", lock)
            print(f"Approved desktop runtime verified ({count} files); no files changed.")
        else:
            archive = args.archive.absolute() if args.archive else download_archive(REPO, lock)
            print(install_archive(REPO, archive, lock))
        return 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError, http.client.HTTPException) as error:
        print(f"Desktop setup failed: {error}", file=sys.stderr)
        print("No existing vendor is replaced. Review it manually or use a fresh source checkout.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
