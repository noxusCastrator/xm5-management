"""Explicit copy-only import of a selected, closed portable application profile."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .portable import ConfigError, ConfigStore, copy_profile


def _no_links(path: Path) -> None:
    for part in (path.absolute(), *path.absolute().parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ConfigError(f"Profile import paths must not contain links: {part}")


def _config_bytes(source: Path) -> bytes:
    config = source / "config.json"
    _no_links(config)
    if not config.is_file() or config.stat().st_size > 16 * 1024 * 1024:
        raise ConfigError(f"Not a supported portable configuration: {config}")
    if (source / "config.json.lock").exists():
        raise ConfigError("Close the portable application first; its configuration lock is present")
    if any((source / rel).exists() for rel in ("runtime/gui-control/session.json", "runtime/backend/connection.json")):
        raise ConfigError("Close the portable application first; its session is still present")
    return config.read_bytes()


def _rebased_config(payload: bytes, source: Path) -> dict:
    try:
        value = json.loads(payload.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        ConfigStore._validate(value)  # Never instantiate the settings writer on the source.
        for kind in ("music", "peq"):
            for record in value[kind + "_sources"]:
                original = Path(record["path"])
                if not str(record["path"]).strip() or (original.drive and not original.is_absolute()):
                    raise ValueError("source folder paths must be nonempty and unambiguous")
                if not original.is_absolute():
                    record["path"] = str((source / original).resolve())
        ConfigStore._validate(value)
        return value
    except (UnicodeError, ValueError, TypeError, RuntimeError) as exc:
        raise ConfigError(f"Invalid portable configuration: {exc}") from exc


def import_portable_profile(source: Path, destination: Path) -> dict:
    """Publish config and durable backend data into a previously absent root.

    The selected portable application stays intact. SQLite and WAL recovery are
    delegated to copy_profile, which only opens staged database copies. A failed
    import removes only its own newly created staging directory.
    """
    source, destination = Path(source).expanduser(), Path(destination).expanduser()
    _no_links(source)
    _no_links(destination)
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir():
        raise ConfigError(f"Portable application folder does not exist: {source}")
    if source == destination or source in destination.parents or destination in source.parents:
        raise ConfigError("Portable source and installed profile must not overlap")
    if destination.exists():
        raise ConfigError(f"Installed profile already exists; nothing was replaced: {destination}")

    stage = None
    stage_identity = None
    lock_fd = None
    lock = destination.parent / ("." + destination.name + ".import.lock")
    try:
        before = _config_bytes(source)
        config_sha256 = hashlib.sha256(before).hexdigest()
        config = _rebased_config(before, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _no_links(destination.parent)
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        if destination.exists():
            raise ConfigError(f"Installed profile appeared during import: {destination}")
        stage = Path(tempfile.mkdtemp(prefix="." + destination.name + ".import-", dir=destination.parent))
        stage_identity = (stage.stat().st_dev, stage.stat().st_ino)
        backend = stage / "data/backend"
        receipt = copy_profile(source / "data/backend", backend)
        # Current profiles keep cache outside backend. Do not publish a cache
        # directory from a selected older/custom profile either.
        cache = backend / "cache"
        if cache.exists():
            _no_links(cache)
            if cache.is_dir():
                shutil.rmtree(cache)
            else:
                cache.unlink()
        payload = (json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        with open(stage / "config.json", "xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        _no_links(source)
        if hashlib.sha256(_config_bytes(source)).hexdigest() != config_sha256:
            raise ConfigError("Portable configuration changed during import; nothing was published")
        _no_links(destination)
        if destination.exists():
            raise ConfigError(f"Installed profile appeared during import: {destination}")
        os.rename(stage, destination)
        stage = None
        return {"schema": "xm5.portable-profile-import.v1", "source": str(source),
                "destination": str(destination), "config_sha256": config_sha256,
                "backend": receipt}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot import portable profile: {exc}") from exc
    finally:
        try:
            if stage is not None:
                _no_links(stage)
                info = stage.stat()
                if (stage.parent != destination.parent or not stage.name.startswith("." + destination.name + ".import-")
                        or (info.st_dev, info.st_ino) != stage_identity):
                    raise ConfigError("Refusing to remove an unowned profile import staging directory")
                shutil.rmtree(stage)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
                lock.unlink()
