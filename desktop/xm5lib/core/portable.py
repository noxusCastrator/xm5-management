"""Portable locations and the single, versioned application settings writer.

Source registration is metadata only. It never edits, moves or removes source files.
All relative paths are relative to the application root, never the process cwd.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "xm5.application-config"
VERSION = 1


class ConfigError(RuntimeError):
    """A visible settings read/write, version, lock or revision error."""


@dataclass(frozen=True)
class PortablePaths:
    root: Path
    mode: str = "portable"
    initial_language: str | None = None

    @classmethod
    def discover(cls, app_root: Path | None = None):
        anchor = Path(app_root or Path(__file__).resolve().parents[2]).resolve()
        descriptor = anchor / "distribution.json"
        distribution = {}
        if descriptor.exists():
            try:
                distribution = json.loads(descriptor.read_text(encoding="utf-8"))
                if (not isinstance(distribution, dict) or distribution.get("schema") != "xm5.distribution.v1"
                        or distribution.get("mode") not in {"portable", "installed"}
                        or distribution.get("initial_language") not in {None, "en", "zh"}):
                    raise ValueError("unsupported distribution descriptor")
            except (OSError, ValueError) as exc:
                raise ConfigError(f"Cannot read distribution mode {descriptor}: {exc}") from exc
        mode = distribution.get("mode", "portable")
        override = os.environ.get("XM5LIB_HOME")
        if override:
            root = Path(override).expanduser()
        elif mode == "installed":
            base = os.environ.get("LOCALAPPDATA")
            if not base and os.name == "nt":
                from . import winenv
                base = winenv.known_folder("LocalAppData") or str(Path.home() / "AppData" / "Local")
            if not base or not Path(base).is_absolute():
                raise ConfigError("Installed mode requires an absolute Windows LOCALAPPDATA location")
            root = Path(base) / "XM5Studio"
        else:
            root = anchor
        return cls((root if root.is_absolute() else anchor / root).resolve(), mode,
                   distribution.get("initial_language"))

    @property
    def config(self): return self.root / "config.json"
    @property
    def data(self): return self.root / "data"
    @property
    def backend(self):
        override = os.environ.get("XM5LIB_BACKEND_HOME")
        if not override:
            return self.data / "backend"
        path = Path(override).expanduser()
        return (path if path.is_absolute() else self.root / path).resolve()
    @property
    def cache(self): return self.root / "cache"
    @property
    def artwork(self): return self.cache / "artwork"
    @property
    def runtime(self): return self.root / "runtime"

    def ensure(self):
        for path in (self.data, self.backend, self.artwork, self.runtime, self.runtime / "import-staging"):
            path.mkdir(parents=True, exist_ok=True)
        return self


def _empty():
    return {"schema": SCHEMA, "version": VERSION, "revision": 0,
            "music_sources": [], "peq_sources": [], "ui": {}, "lyrics": {"auto_search": False}}


class ConfigStore:
    """Read-modify-replace under a per-file process lock and cross-process lock.

    Each update rereads the file, preserving unknown fields and concurrent writers'
    changes. A previous valid revision is retained in config.json.bak. Invalid input
    is an error; it is never replaced with an empty profile. A stale lock requires
    explicit inspection/removal rather than risking a second writer.
    """
    _locks: dict[str, threading.RLock] = {}
    _guard = threading.Lock()

    def __init__(self, path: Path, app_root: Path | None = None, legacy: Path | None = None):
        self.path = Path(path).resolve()
        self.root = Path(app_root or self.path.parent).resolve()
        with self._guard:
            self._lock = self._locks.setdefault(os.path.normcase(str(self.path)), threading.RLock())
        self.seeded = False
        with self._writing():
            if self.path.exists():
                raw = self._read_json(self.path)
                if raw.get("schema") != SCHEMA:
                    if "schema" in raw:
                        raise ConfigError(f"Unsupported configuration schema in {self.path}")
                    self._commit(self._migrate(raw))
                    self.seeded = True
                else:
                    self._validate(raw)
            else:
                raw = self._read_json(Path(legacy)) if legacy is not None and Path(legacy).exists() else None
                self._commit(self._migrate(raw) if raw is not None else _empty())
                self.seeded = raw is not None

    @staticmethod
    def _read_json(path):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
            return value
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Cannot read configuration {path}: {exc}") from exc

    @staticmethod
    def _validate(value):
        if value.get("schema") != SCHEMA or value.get("version") != VERSION:
            raise ConfigError("Unsupported configuration schema/version; explicit migration is required")
        if not isinstance(value.get("revision"), int) or not isinstance(value.get("ui"), dict):
            raise ConfigError("Invalid configuration revision or UI preferences")
        if not isinstance(value.get("lyrics"), dict):
            raise ConfigError("Invalid lyrics preferences")
        for kind in ("music", "peq"):
            sources = value.get(kind + "_sources")
            if not isinstance(sources, list) or any(not isinstance(s, dict) or not s.get("id") or
                                                  not isinstance(s.get("path"), str) for s in sources):
                raise ConfigError(f"Invalid {kind} source records")
            if len({s["id"] for s in sources}) != len(sources):
                raise ConfigError(f"Duplicate {kind} source IDs")

    @contextmanager
    def _writing(self):
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(f"Cannot prepare configuration directory {self.path.parent}: {exc}") from exc
            lock = self.path.with_suffix(self.path.suffix + ".lock")
            deadline = time.monotonic() + 5
            while True:
                try:
                    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    break
                except FileExistsError:
                    if time.monotonic() >= deadline:
                        raise ConfigError(f"Configuration is locked: {lock}")
                    time.sleep(.025)
                except OSError as exc:
                    raise ConfigError(f"Cannot lock configuration {self.path}: {exc}") from exc
            try:
                try:
                    os.write(fd, str(os.getpid()).encode("ascii"))
                except OSError as exc:
                    raise ConfigError(f"Cannot write configuration lock {lock}: {exc}") from exc
                yield
            finally:
                os.close(fd)
                lock.unlink()

    @staticmethod
    def _replace(path, data):
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as f:
                name = f.name
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, path)
            name = None
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def _commit(self, value):
        self._validate(value)
        payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            if self.path.exists():
                self._replace(self.path.with_suffix(self.path.suffix + ".bak"), self.path.read_bytes())
            self._replace(self.path, payload)
        except OSError as exc:
            raise ConfigError(f"Cannot save configuration {self.path}: {exc}") from exc

    def snapshot(self):
        with self._lock:
            value = self._read_json(self.path)
            self._validate(value)
            return value

    def update(self, change, expected_revision: int | None = None):
        with self._writing():
            value = self.snapshot()
            if expected_revision is not None and value["revision"] != expected_revision:
                raise ConfigError("Configuration revision changed; reload before saving")
            change(value)
            value["revision"] += 1
            self._commit(value)
            return copy.deepcopy(value)

    def encode_path(self, path):
        target = Path(path).expanduser()
        target = (self.root / target).resolve() if not target.is_absolute() else target.resolve()
        try:
            return target.relative_to(self.root).as_posix()
        except ValueError:
            return str(target)

    def resolve_path(self, path):
        target = Path(path)
        return str(target if target.is_absolute() else self.root / target)

    def _source(self, path):
        encoded = self.encode_path(path)
        return {"id": str(uuid.uuid4()), "path": encoded,
                "label": Path(path).name or str(path), "enabled": True}

    def _migrate(self, old):
        value = _empty()
        value["ui"] = copy.deepcopy(old)
        for kind, key, single in (("music", "music_roots", "music_root"), ("peq", "peq_roots", "peace_dir")):
            roots = old.get(key)
            if roots is None:
                roots = [old[single]] if old.get(single) else []
            if not isinstance(roots, list):
                raise ConfigError(f"Invalid legacy {key}")
            value[kind + "_sources"] = [self._source(p) for p in dict.fromkeys(roots) if p]
            for k in (key, single):
                value["ui"].pop(k, None)
        return value

    def sources(self, kind: str, include_disabled=False):
        if kind not in ("music", "peq"):
            raise ValueError(kind)
        return [dict(s, path=self.resolve_path(s["path"])) for s in self.snapshot()[kind + "_sources"]
                if include_disabled or s.get("enabled", True)]

    def source_roots(self, kind):
        return [s["path"] for s in self.sources(kind)]

    def register_source(self, kind, path):
        source = self._source(path)
        def add(value):
            records = value[kind + "_sources"]
            match = next((s for s in records if self.encode_path(s["path"]) == source["path"]), None)
            if match:
                match["enabled"] = True
                source["id"] = match["id"]
            else:
                records.append(source)
        self.update(add)
        return source["id"]

    def relink_source(self, kind, source_id, path):
        encoded = self.encode_path(path)
        def relink(value):
            records = value[kind + "_sources"]
            if any(s["id"] != source_id and s.get("enabled", True) and self.encode_path(s["path"]) == encoded for s in records):
                raise ConfigError("This folder is already registered")
            source = next((s for s in records if s["id"] == source_id), None)
            if source is None:
                raise ConfigError(f"Unknown source ID: {source_id}")
            source["path"], source["enabled"] = encoded, True
        self.update(relink)

    def remove_source(self, kind, source_id):
        def remove(value):
            source = next((s for s in value[kind + "_sources"] if s["id"] == source_id), None)
            if source is None:
                raise ConfigError(f"Unknown source ID: {source_id}")
            source["enabled"] = False
        self.update(remove)

    def get(self, key, default=None):
        if key in ("music_roots", "peq_roots"):
            return self.source_roots(key.split("_")[0])
        if key in ("music_sources", "peq_sources"):
            return self.sources(key.split("_")[0])
        if key in ("music_root", "peace_dir"):
            roots = self.source_roots("music" if key == "music_root" else "peq")
            return roots[0] if roots else default
        value = self.snapshot()
        result = value["lyrics"].get("auto_search", False) if key == "lyrics.auto_search" else value["ui"].get(key, default)
        return default if result is None else result

    def set(self, key, result):
        if key in ("music_root", "peace_dir"):
            key, result = ("music_roots" if key == "music_root" else "peq_roots"), ([result] if result else [])
        def change(value):
            if key in ("music_roots", "peq_roots"):
                kind = key.split("_")[0]
                records = value[kind + "_sources"]
                wanted = [self.encode_path(p) for p in dict.fromkeys(result) if p]
                for s in records:
                    s["enabled"] = self.encode_path(s["path"]) in wanted
                known = {self.encode_path(s["path"]) for s in records}
                records.extend(self._source(p) for p in wanted if p not in known)
            elif key == "lyrics.auto_search":
                value.setdefault("lyrics", {})["auto_search"] = bool(result)
            else:
                value["ui"][key] = copy.deepcopy(result)
        self.update(change)


def _stream_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _profile_files(source):
    """Inventory only durable profile files; never copy discovery or credentials."""
    files = {}
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if "runtime" in rel.parts:
            continue
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ConfigError(f"Profile contains a link/reparse point: {path}")
        if not path.is_file() or path.name.endswith("-shm") or path.name in ("connection.json", "owner.lock", "token"):
            continue
        files[rel.as_posix()] = {"size": path.stat().st_size, "sha256": _stream_hash(path)}
    return files


@contextmanager
def _inactive_profile(source):
    """Hold the existing owner lock without creating/writing source files."""
    handles = []
    try:
        for runtime in _profile_runtimes(source):
            for part in (runtime, *runtime.parents):
                if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
                    raise ConfigError(f"Invalid profile runtime path: {part}")
            if (runtime / "connection.json").exists():
                raise ConfigError(f"Stop the old backend before migrating {source}; discovery is still present")
            target = runtime / "owner.lock"
            if not target.exists():
                continue
            handle = open(target, "rb")
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                raise ConfigError(f"Stop the old backend before migrating {source}; its owner lock is held") from exc
            handles.append(handle)
        yield
    finally:
        for handle in handles:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            handle.close()


def _profile_runtimes(source):
    paths = [source / "runtime"]             # v18 and explicit arbitrary profiles
    locations = PortablePaths.discover()
    if source == locations.backend:
        paths.append(locations.runtime / "backend")
    if source.name == "backend" and source.parent.name == "data":
        paths.append(source.parent.parent / "runtime/backend")
    return list(dict.fromkeys(paths))


def copy_profile(source: Path, target: Path) -> dict:
    """Explicit, copy-only profile migration with staged SQLite recovery.

    Original databases (including WAL and SHM) are never opened by SQLite. The
    source is inventoried before and after copying. SQLite backup runs only on
    staged copies so committed WAL rows are retained without changing originals.
    An existing destination, live owner or source drift refuses publication.
    """
    source, target = Path(source), Path(target)
    for root in (source, target):
        for part in (root.absolute(), *root.absolute().parents):
            if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
                raise ConfigError(f"Profile paths must not contain links: {part}")
    source, target = source.resolve(), target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ConfigError("Profile migration source and destination must not overlap")
    if not (source / "application.sqlite3").is_file():
        raise ConfigError(f"Not a recognized backend profile: {source}")
    if target.exists():
        raise ConfigError(f"Migration destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / ("." + target.name + ".migration.lock")
    stage = None
    descriptor = None
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with _inactive_profile(source):
            before = _profile_files(source)
            stage = Path(tempfile.mkdtemp(prefix="." + target.name + ".migration-", dir=target.parent))
            for rel, evidence in before.items():
                out = stage / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / rel, out)
                if out.stat().st_size != evidence["size"] or _stream_hash(out) != evidence["sha256"]:
                    raise ConfigError(f"Profile changed while copying: {rel}")
            for database in sorted(stage.rglob("*.sqlite3")):
                recovered = database.with_name(database.name + ".recovered")
                src = sqlite3.connect(database)
                dst = sqlite3.connect(recovered)
                try:
                    if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ConfigError(f"Copied database is corrupt: {database.name}")
                    src.backup(dst)
                    dst.execute("PRAGMA journal_mode=DELETE")
                    dst.commit()
                finally:
                    src.close()
                    dst.close()
                os.replace(recovered, database)
                for suffix in ("-wal", "-shm"):
                    Path(str(database) + suffix).unlink(missing_ok=True)
            if _profile_files(source) != before:
                raise ConfigError(f"Source profile changed during migration: {source}")
            receipt = {"schema": "xm5.profile-copy", "version": 1, "source": str(source), "files": before}
            history = stage / "migration-history"
            history.mkdir(exist_ok=True)
            (history / (uuid.uuid4().hex + ".json")).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if target.exists() or any((r / "connection.json").exists() for r in _profile_runtimes(source)):
                raise ConfigError("Profile destination conflict or active source; migration was not published")
            os.rename(stage, target)
            stage = None
            return receipt
    except (OSError, sqlite3.Error) as exc:
        raise ConfigError(f"Cannot copy backend profile {source} to {target}: {exc}") from exc
    finally:
        if stage is not None:
            resolved_stage = stage.resolve()
            if not resolved_stage.is_relative_to(target.parent) or not stage.name.startswith("." + target.name + ".migration-"):
                raise ConfigError("Refusing cleanup outside the migration staging directory")
            shutil.rmtree(resolved_stage)
        if descriptor is not None:
            os.close(descriptor)
            lock.unlink()


def migrate_previous_profile(locations: PortablePaths) -> dict | None:
    """Fresh portable launches do not discover or copy another installed profile.

    Explicit migration can call copy_profile(source, destination) after the user
    selects the source. Existing callers retain a harmless compatibility hook.
    """
    return None
