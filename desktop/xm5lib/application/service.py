from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL);
INSERT OR IGNORE INTO state VALUES(1,0);
CREATE TABLE IF NOT EXISTS resources(kind TEXT,id TEXT,revision INTEGER NOT NULL,
 data TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS locations(path TEXT PRIMARY KEY,track_id TEXT NOT NULL,
 present INTEGER NOT NULL,root TEXT,sha256 TEXT);
CREATE INDEX IF NOT EXISTS location_tracks ON locations(track_id);
CREATE TABLE IF NOT EXISTS card_references(path TEXT PRIMARY KEY,track_id TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_imports(path TEXT PRIMARY KEY,sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,command TEXT NOT NULL,
 fingerprint TEXT NOT NULL,idempotency_key TEXT UNIQUE,response TEXT NOT NULL,
 changes TEXT NOT NULL,undoable INTEGER NOT NULL,undone_by TEXT,created REAL NOT NULL);
INSERT OR IGNORE INTO resources VALUES('favorites','main',0,'{"track_ids":[]}',0);
PRAGMA user_version=1;
"""
QUERIES = ("tracks.search", "tracks.all", "tracks.get", "playlists.list", "playlists.get",
           "favorites.list", "presets.list", "settings.get", "receipts.get")
COMMANDS = ("library.scan", "library.refresh", "library.relink_root", "library.import_legacy", "library.relocate_files", "library.intake_card", "playlists.create",
            "playlists.rename",
            "playlists.replace", "playlists.delete", "playlists.cover", "favorites.set", "favorites.replace", "tracks.annotate",
            "presets.save", "presets.delete", "undo")
GUARDED = {"playlists.rename", "playlists.replace", "playlists.delete", "playlists.cover", "tracks.annotate", "library.relocate_files", "favorites.replace"}
EDITABLE = {"title", "artist", "album", "albumartist", "tracknumber", "discnumber", "date", "genre", "comment"}
MAX_COVER_BYTES = 32 * 1024 * 1024
MAX_COVER_PIXELS = 40_000_000
MAX_COVER_DIMENSION = 16_384


class DomainError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code, self.message, self.details = code, message, details or {}
        super().__init__(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _path(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value):
        raise DomainError("INVALID_ARGUMENT", "A nonempty path is required")
    try:
        return os.path.normcase(str(Path(value).expanduser().resolve()))
    except (OSError, ValueError, RuntimeError) as exc:
        raise DomainError("INVALID_ARGUMENT", "Path cannot be resolved", {"reason": str(exc)}) from exc


def _uid():
    return str(uuid.uuid4())


def _text(args, key, *, empty=False):
    value = args.get(key)
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise DomainError("INVALID_ARGUMENT", f"{key} must be a nonempty string")
    return value if empty else value.strip()


def _list(args, key):
    value = args.get(key)
    if not isinstance(value, list):
        raise DomainError("INVALID_ARGUMENT", f"{key} must be a list")
    return value


class Application:
    """Each call uses its own connection. SQLite BEGIN IMMEDIATE serializes all writers.

    Playlist/track edits require a target revision. Scan and import collect immutable
    input snapshots before beginning the write transaction. No module import starts work.
    """
    def __init__(self, home: Path):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "application.sqlite3"
        self._closed = False
        self._write_lock = threading.RLock()
        with self._connection() as c:
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise DomainError("SCHEMA_VERSION", "Unsupported application database version", {"version": version})
            c.executescript(SCHEMA)

    @contextmanager
    def _connection(self):
        if self._closed:
            raise DomainError("CLOSED", "Application is closed")
        c = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
        try:
            yield c
        finally:
            c.close()

    def close(self):
        self._closed = True

    def capabilities(self):
        return {"api_version": 1, "schema_version": 1, "queries": list(QUERIES),
                "commands": list(COMMANDS), "revision_required": sorted(GUARDED),
                "idempotency": "persistent request fingerprint; receipt replay before revision check",
                "undo": "conditional touched-resource revisions; scan/import are not undoable",
                "cover_limits": {"bytes": MAX_COVER_BYTES, "pixels": MAX_COVER_PIXELS,
                                 "dimension": MAX_COVER_DIMENSION},
                "source_writes": False}

    @staticmethod
    def _state(c):
        return c.execute("SELECT revision FROM state WHERE id=1").fetchone()[0]

    @staticmethod
    def _row(c, kind, key, required=True):
        row = c.execute("SELECT * FROM resources WHERE kind=? AND id=?", (kind, key)).fetchone()
        if required and (row is None or row["deleted"]):
            raise DomainError("NOT_FOUND", f"{kind} not found", {"id": key})
        return dict(row) if row else None

    def _put(self, c, kind, key, data, changes, deleted=False):
        before = self._row(c, kind, key, False)
        encoded = _json(data)
        if before and before["data"] == encoded and before["deleted"] == int(deleted):
            return before["revision"]
        rev = (before["revision"] if before else 0) + 1
        c.execute("INSERT INTO resources VALUES(?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET "
                  "revision=excluded.revision,data=excluded.data,deleted=excluded.deleted",
                  (kind, key, rev, encoded, int(deleted)))
        token = (kind, key)
        if token not in changes:
            changes[token] = {"before": before}
        changes[token]["after"] = self._row(c, kind, key, False)
        return rev

    def _track(self, c, key):
        row = self._row(c, "track", key)
        data = json.loads(row["data"])
        locations = [dict(r) for r in c.execute("SELECT path,present,root,sha256 FROM locations "
                                               "WHERE track_id=? ORDER BY present DESC,path", (key,))]
        for location in locations:
            location["present"] = bool(location["present"])
        reference = data.get("card_reference")
        path = locations[0]["path"] if locations else "card://" + reference["path"] if reference else None
        return data.get("metadata", {}) | {"id": key, "revision": row["revision"], "path": path,
                "locations": locations, "unresolved": not any(x["present"] for x in locations),
                "card_reference": reference,
                "note": data.get("note", ""), "overrides": data.get("overrides", {})}

    def _cover_view(self, value):
        if not value:
            return value
        result = dict(value)
        managed = result.get("managed_path")
        if managed is None:
            old = Path(result.get("path", ""))
            # Existing content-addressed originals can be rebound after a profile
            # copy without altering historical resources or undo receipts.
            if (old.parent.name == "artwork" and old.parent.parent.name == "originals"
                    and old.name.split(".", 1)[0] == result.get("sha256")):
                managed = "originals/artwork/" + old.name
        if managed is not None:
            from pathlib import PurePosixPath
            if not isinstance(managed, str):
                raise DomainError("COVER_STORAGE", "Invalid managed artwork reference")
            rel = PurePosixPath(managed)
            if (rel.is_absolute() or ".." in rel.parts
                    or "\\" in managed or len(rel.parts) != 3
                    or rel.parts[:2] != ("originals", "artwork")):
                raise DomainError("COVER_STORAGE", "Invalid managed artwork reference")
            result["managed_path"] = rel.as_posix()
            result["path"] = str(self.home.joinpath(*rel.parts))
        return result

    def _playlist(self, c, key):
        row = self._row(c, "playlist", key)
        data = json.loads(row["data"])
        items = []
        for item in data["items"]:
            track = self._track(c, item["track_id"])
            items.append(item | {"path": track["path"], "unresolved": track["unresolved"]})
        return {"id": key, "revision": row["revision"], "name": data["name"], "items": items,
                "count": len(items), "track_ids": [i["track_id"] for i in items],
                "tracks": [i["path"] for i in items], "cover": self._cover_view(data.get("cover"))}

    def query(self, name, args=None):
        args = {} if args is None else args
        if name not in QUERIES:
            raise DomainError("UNKNOWN_QUERY", "Unknown query", {"name": name})
        if not isinstance(args, dict):
            raise DomainError("INVALID_ARGUMENT", "arguments must be an object")
        with self._connection() as c:
            c.execute("BEGIN")
            state = self._state(c)
            if name in ("tracks.search", "tracks.all"):
                text = args.get("text", args.get("query", args.get("q", "")))
                if not isinstance(text, str):
                    raise DomainError("INVALID_ARGUMENT", "search text must be a string")
                if name == "tracks.search" and not text:
                    # Frontend addition: an unfiltered page reads only its own rows (same order as below),
                    # so paging through a large library is linear instead of quadratic.
                    limit, offset = args.get("limit", 100), args.get("offset", 0)
                    if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
                        raise DomainError("INVALID_ARGUMENT", "limit must be1..1000 and offset>=0")
                    total = c.execute("SELECT COUNT(*) FROM resources WHERE kind='track' AND deleted=0").fetchone()[0]
                    rows = [self._track(c, r[0]) for r in c.execute(
                        "SELECT id FROM resources WHERE kind='track' AND deleted=0 ORDER BY id LIMIT ? OFFSET ?",
                        (limit, offset))]
                    return {"tracks": rows, "total": total, "revision": state}
                rows = [self._track(c, r[0]) for r in c.execute(
                    "SELECT id FROM resources WHERE kind='track' AND deleted=0 ORDER BY id")]
                if text:
                    needle = text.casefold()
                    rows = [r for r in rows if needle in " ".join(str(r.get(k) or "") for k in
                            ("title", "artist", "album", "albumartist", "path", "note")).casefold()]
                total = len(rows)
                if name == "tracks.search":
                    limit, offset = args.get("limit", 100), args.get("offset", 0)
                    if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
                        raise DomainError("INVALID_ARGUMENT", "limit must be1..1000 and offset>=0")
                    rows = rows[offset:offset + limit]
                return {"tracks": rows, "total": total, "revision": state}
            if name == "tracks.get":
                track = self._track(c, _text(args, "id"))
                return {"track": track, "revision": track["revision"], "state_revision": state}
            if name == "playlists.list":
                rows = [self._playlist(c, r[0]) for r in c.execute(
                    "SELECT id FROM resources WHERE kind='playlist' AND deleted=0 ORDER BY id")]
                return {"playlists": sorted(rows, key=lambda p: (p["name"].casefold(), p["id"])), "revision": state}
            if name == "playlists.get":
                row = self._playlist(c, _text(args, "id"))
                return {"playlist": row, "revision": row["revision"], "state_revision": state}
            if name == "favorites.list":
                row = self._row(c, "favorites", "main")
                ids = json.loads(row["data"])["track_ids"]
                return {"track_ids": ids, "favorites": [self._track(c, i) for i in ids],
                        "revision": row["revision"], "state_revision": state}
            if name == "presets.list":
                return {"presets": [json.loads(r[0]) for r in c.execute(
                    "SELECT data FROM resources WHERE kind='preset' AND deleted=0 ORDER BY id")], "revision": state}
            if name == "settings.get":
                key = _text(args, "key")
                row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                return {"key": key, "value": json.loads(row[0]) if row else None, "revision": state}
            row = c.execute("SELECT response FROM receipts WHERE id=?", (_text(args, "id"),)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Receipt not found")
            return json.loads(row[0])

    @staticmethod
    def _replay(c, key, fingerprint):
        if key is None:
            return None
        row = c.execute("SELECT fingerprint,response FROM receipts WHERE idempotency_key=?", (key,)).fetchone()
        if row:
            if row[0] != fingerprint:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Key already belongs to a different request")
            return json.loads(row[1]) | {"replayed": True}
        return None

    def execute(self, name, args=None, expected_revision=None, idempotency_key=None,cancel=None,progress=None):
        args = {} if args is None else args
        if name not in COMMANDS:
            raise DomainError("UNKNOWN_COMMAND", "Unknown command", {"name": name})
        if not isinstance(args, dict):
            raise DomainError("INVALID_ARGUMENT", "arguments must be an object")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise DomainError("INVALID_ARGUMENT", "expected_revision must be a nonnegative integer")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256):
            raise DomainError("INVALID_ARGUMENT", "idempotency_key must contain1..256 characters")
        try:
            fingerprint = hashlib.sha256(_json([name, args, expected_revision]).encode()).hexdigest()
        except (TypeError, ValueError) as exc:
            raise DomainError("INVALID_ARGUMENT", "arguments must contain finite JSON values") from exc
        with self._connection() as c:
            replay = self._replay(c, idempotency_key, fingerprint)
            if replay:
                return replay
        if name in GUARDED and expected_revision is None:
            raise DomainError("REVISION_REQUIRED", "Read the resource and supply its revision before editing")
        if name == "library.intake_card" and (args.get("favorites") or args.get("id")) and expected_revision is None:
            raise DomainError("REVISION_REQUIRED", "Read the target before importing card references")
        prepared = None
        if name == "library.scan":
            prepared = self._prepare_scan(args,cancel,progress)
        elif name == "library.relink_root":
            prepared = self._prepare_relink(args,cancel)
        elif name == "library.refresh":
            prepared = self._prepare_refresh(args)
        elif name == "library.relocate_files":
            from .file_relocations import prepare_relocations
            prepared = prepare_relocations(self, args)
        elif name == "library.import_legacy":
            prepared = self._prepare_legacy(args)
        elif name == "playlists.cover":
            prepared = self._prepare_cover(args)
        if name=='library.scan' and cancel is not None and cancel.is_set():
            raise DomainError('CANCELLED','Scan cancelled before publication')
        try:
            with self._write_lock, self._connection() as c:
                c.execute("BEGIN IMMEDIATE")
                replay = self._replay(c, idempotency_key, fingerprint)
                if replay:
                    c.rollback()
                    return replay
                actual = self._target_revision(c, name, args)
                if expected_revision is not None and actual != expected_revision:
                    raise DomainError("REVISION_CONFLICT", "Resource changed since it was read",
                                      {"expected": expected_revision, "actual": actual})
                changes = {}
                receipt_id = _uid()
                result, revision = self._dispatch(c, name, args, changes, prepared, receipt_id)
                state = self._state(c) + 1
                c.execute("UPDATE state SET revision=? WHERE id=1", (state,))
                undoable = bool(changes) and name not in {"library.scan", "library.refresh", "library.relink_root",
                                                          "library.import_legacy", "library.relocate_files", "undo"}
                response = {"receipt_id": receipt_id, "command": name, "revision": revision,
                            "state_revision": state, "result": result, "undoable": undoable, "replayed": False}
                c.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,NULL,?)", (receipt_id, name, fingerprint,
                          idempotency_key, _json(response), _json(list(changes.values())), int(undoable), time.time()))
                c.commit()
                return response
        except sqlite3.OperationalError as exc:
            raise DomainError("STORAGE_BUSY" if "locked" in str(exc).lower() else "STORAGE_ERROR", str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            raise DomainError("STORAGE_ERROR", str(exc)) from exc

    def _target_revision(self, c, name, args):
        if name == "library.intake_card":
            if args.get("favorites"):
                return self._row(c, "favorites", "main")["revision"]
            if args.get("id"):
                return self._row(c, "playlist", _text(args, "id"))["revision"]
        if name.startswith("playlists.") and name != "playlists.create":
            return self._row(c, "playlist", _text(args, "id"))["revision"]
        if name == "tracks.annotate":
            return self._row(c, "track", _text(args, "id"))["revision"]
        if name in ("favorites.set", "favorites.replace"):
            return self._row(c, "favorites", "main")["revision"]
        return self._state(c)

    def _name_available(self, c, name, excluding=None):
        for row in c.execute("SELECT id,data FROM resources WHERE kind='playlist' AND deleted=0"):
            if row[0] != excluding and json.loads(row[1])["name"] == name:
                raise DomainError("NAME_CONFLICT", "Playlist name already exists", {"name": name})

    def _dispatch(self, c, name, args, changes, prepared, receipt_id):
        if name == "library.intake_card":
            # Cataloguing a card path does not pull or identify audio. These small
            # unresolved rows survive undo; only the playlist/favorite edit is undone.
            from pathlib import PurePosixPath
            ids = []
            for entry in _list(args, "entries"):
                if not isinstance(entry, dict):
                    raise DomainError("INVALID_ARGUMENT", "Card entries must be objects")
                if "track_id" in entry:
                    tid = _text(entry, "track_id")
                    self._row(c, "track", tid)
                else:
                    path = _text(entry, "card_path", empty=True)
                    rel = PurePosixPath(path)
                    if (rel.is_absolute() or not rel.parts or any(p in (".", "..") for p in rel.parts)
                            or rel.as_posix() != path or any(ch in path for ch in "\\:\r\n\0")):
                        raise DomainError("INVALID_ARGUMENT", "Expected a canonical relative card path")
                    known = c.execute("SELECT track_id FROM card_references WHERE path=?", (path,)).fetchone()
                    if known:
                        tid = known[0]
                        self._row(c, "track", tid)
                    else:
                        tid = _uid()
                        value = {"metadata": {"title": rel.stem}, "card_reference": {"path": path}}
                        c.execute("INSERT INTO resources VALUES('track',?,1,?,0)", (tid, _json(value)))
                        c.execute("INSERT INTO card_references VALUES(?,?)", (path, tid))
                ids.append(tid)
            if args.get("favorites"):
                keep = args.get("preserve_track_ids", [])
                if not isinstance(keep, list):
                    raise DomainError("INVALID_ARGUMENT", "preserve_track_ids must be a list")
                return self._dispatch(c, "favorites.replace", {"track_ids": keep + ids}, changes, None, receipt_id)
            target = {"name": _text(args, "name"), "track_ids": ids}
            if args.get("id"):
                target["id"] = args["id"]
            return self._dispatch(c, "playlists.replace" if args.get("id") else "playlists.create",
                                  target, changes, None, receipt_id)
        if name == "library.scan":
            return self._apply_scan(c, prepared, changes), self._state(c) + 1
        if name == "library.relink_root":
            return self._apply_relink(c, prepared, changes), self._state(c) + 1
        if name == "library.refresh":
            return self._apply_refresh(c, prepared, changes), self._state(c) + 1
        if name == "library.relocate_files":
            from .file_relocations import apply_relocations
            return apply_relocations(self, c, prepared, changes), self._state(c) + 1
        if name == "library.import_legacy":
            return self._apply_legacy(c, prepared, changes), self._state(c) + 1
        if name == "playlists.create":
            title = _text(args, "name")
            self._name_available(c, title)
            ids=args.get('track_ids',[])
            if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids):
                raise DomainError('INVALID_ARGUMENT','track_ids must be a list of IDs')
            for track_id in ids:self._row(c,'track',track_id)
            key = _uid()
            revision = self._put(c, "playlist", key, {"name": title, "items": [
                {'occurrence_id':_uid(),'track_id':track_id} for track_id in ids]}, changes)
            return self._playlist(c, key), revision
        if name.startswith("playlists."):
            key = _text(args, "id")
            data = json.loads(self._row(c, "playlist", key)["data"])
            if name == "playlists.rename":
                title = _text(args, "name")
                self._name_available(c, title, key)
                data["name"] = title
            elif name == "playlists.cover":
                data["cover"] = prepared
            elif name == "playlists.replace":
                if "name" in args:
                    title = _text(args, "name")
                    self._name_available(c, title, key)
                    data["name"] = title
                if "items" in args:
                    items = _list(args, "items")
                    if any(not isinstance(i, dict) or not isinstance(i.get("track_id"), str) for i in items):
                        raise DomainError("INVALID_ARGUMENT", "items require track_id")
                    previous = {i["occurrence_id"]: i["track_id"] for i in data["items"]}
                    output = []
                    for item in items:
                        if item.get("occurrence_id") is not None and not isinstance(item["occurrence_id"], str):
                            raise DomainError("INVALID_ARGUMENT", "occurrence_id must be a string")
                        oid = item.get("occurrence_id") or _uid()
                        if oid in previous and previous[oid] != item["track_id"]:
                            raise DomainError("INVALID_ARGUMENT", "An occurrence cannot change track identity")
                        if item.get("occurrence_id") and oid not in previous:
                            raise DomainError("INVALID_ARGUMENT", "Unknown occurrence_id")
                        output.append({"occurrence_id": oid, "track_id": item["track_id"]})
                else:
                    ids = _list(args, "track_ids")
                    queues = {}
                    for item in data["items"]:
                        queues.setdefault(item["track_id"], []).append(item["occurrence_id"])
                    output = [{"track_id": tid, "occurrence_id": queues[tid].pop(0) if queues.get(tid) else _uid()}
                              for tid in ids if isinstance(tid, str)]
                    if len(output) != len(ids):
                        raise DomainError("INVALID_ARGUMENT", "track_ids must be strings")
                if len({i["occurrence_id"] for i in output}) != len(output):
                    raise DomainError("INVALID_ARGUMENT", "Duplicate occurrence_id")
                for item in output:
                    self._row(c, "track", item["track_id"])
                data["items"] = output
            revision = self._put(c, "playlist", key, data, changes, name == "playlists.delete")
            return ({"id": key, "deleted": True} if name == "playlists.delete" else self._playlist(c, key)), revision
        if name == "favorites.replace":
            ids = _list(args, "track_ids")
            for tid in ids:
                if not isinstance(tid, str):
                    raise DomainError("INVALID_ARGUMENT", "track_ids must be strings")
                self._row(c, "track", tid)
            value = list(dict.fromkeys(ids))
            revision = self._put(c, "favorites", "main", {"track_ids": value}, changes)
            return {"track_ids": value}, revision
        if name == "favorites.set":
            if type(args.get("favorite")) is not bool:
                raise DomainError("INVALID_ARGUMENT", "favorite must be boolean")
            ids = _list(args, "track_ids")
            for tid in ids:
                if not isinstance(tid, str):
                    raise DomainError("INVALID_ARGUMENT", "track_ids must be strings")
                self._row(c, "track", tid)
            current = json.loads(self._row(c, "favorites", "main")["data"])["track_ids"]
            selected = set(ids)
            value = list(dict.fromkeys(current + ids)) if args["favorite"] else [x for x in current if x not in selected]
            revision = self._put(c, "favorites", "main", {"track_ids": value}, changes)
            return {"track_ids": value}, revision
        if name == "tracks.annotate":
            key = _text(args, "id")
            data = json.loads(self._row(c, "track", key)["data"])
            if "note" in args:
                data["note"] = _text(args, "note", empty=True)
            if "overrides" in args:
                overrides = args["overrides"]
                if not isinstance(overrides, dict) or set(overrides) - EDITABLE or any(
                        v is not None and not isinstance(v, str) for v in overrides.values()):
                    raise DomainError("INVALID_ARGUMENT", "Unsupported metadata override")
                for field, value in overrides.items():
                    if value is None:
                        data.setdefault("overrides", {}).pop(field, None)
                    else:
                        data.setdefault("overrides", {})[field] = value
            revision = self._put(c, "track", key, data, changes)
            return self._track(c, key), revision
        if name == "presets.save":
            data = args.get("preset")
            if not isinstance(data, dict):
                raise DomainError("INVALID_ARGUMENT", "preset must be an object")
            key = _text(data, "name")
            data = data | {"name": key}
            revision = self._put(c, "preset", key, data, changes)
            return data, revision
        if name == "presets.delete":
            key = _text(args, "name")
            data = json.loads(self._row(c, "preset", key)["data"])
            revision = self._put(c, "preset", key, data, changes, True)
            return {"name": key, "deleted": True}, revision
        return self._undo(c, args, changes, receipt_id), self._state(c) + 1

    def _prepare_cover(self, args):
        """Validate a bounded image and retain its exact bytes before the DB mutation.

        Unreferenced copies after a conflict/failure are deliberately retained. Neither
        clearing nor undoing an association deletes user images or managed originals.
        """
        if "path" not in args:
            raise DomainError("INVALID_ARGUMENT", "path is required; use null to clear the cover")
        if args["path"] is None:
            return None
        source = Path(_path(args["path"]))
        try:
            source = source.resolve(strict=True)
            before = source.stat()
            if not stat.S_ISREG(before.st_mode):
                raise DomainError("INVALID_COVER", "Cover source must be a regular image file")
            if before.st_size > MAX_COVER_BYTES:
                raise DomainError("INVALID_COVER", "Cover exceeds the32MiB input limit")
            with source.open("rb") as stream:
                blob = stream.read(MAX_COVER_BYTES + 1)
            if not blob or len(blob) > MAX_COVER_BYTES:
                raise DomainError("INVALID_COVER", "Cover must contain1byte..32MiB of image bytes")
        except OSError as exc:
            raise DomainError("INVALID_COVER", "Cover source cannot be read") from exc
        from PIL import Image
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(blob)) as image:
                    width, height = image.size
                    fmt = image.format
                    if (width < 1 or height < 1 or width > MAX_COVER_DIMENSION or height > MAX_COVER_DIMENSION
                            or width * height > MAX_COVER_PIXELS or not fmt or not fmt.isalnum()):
                        raise DomainError("INVALID_COVER", "Cover dimensions or format exceed supported bounds")
                    image.verify()
                # verify() alone does not fully decode JPEG pixel data.
                with Image.open(io.BytesIO(blob)) as image:
                    image.load()
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise DomainError("INVALID_COVER", "Cover is not a complete supported image") from exc
        digest = hashlib.sha256(blob).hexdigest()
        folder = self.home / "originals" / "artwork"
        for part in (folder, folder.parent):
            if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
                raise DomainError("COVER_STORAGE", "Original image storage cannot contain links")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DomainError("COVER_STORAGE", "Cannot create original image storage") from exc
        if not folder.resolve().is_relative_to(self.home):
            raise DomainError("COVER_STORAGE", "Original image storage escaped application home")
        target = folder / f"{digest}.{fmt.lower()}"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if target.is_symlink() or (getattr(target.lstat(), "st_file_attributes", 0) & 0x400):
                raise DomainError("COVER_STORAGE", "Stored original cannot be a link")
        except OSError as exc:
            raise DomainError("COVER_STORAGE", "Cannot create immutable original image copy") from exc
        else:
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(blob)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise DomainError("COVER_STORAGE", "Cannot persist original image bytes") from exc
        try:
            if target.stat().st_size != len(blob):
                raise DomainError("COVER_STORAGE", "Stored original size does not match its identity")
            with target.open("rb") as stream:
                stored_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            if stored_hash != digest:
                raise DomainError("COVER_STORAGE", "Stored original hash does not match its identity")
            with source.open("rb") as stream:
                after_blob = stream.read(MAX_COVER_BYTES + 1)
            if (len(after_blob) != len(blob) or hashlib.sha256(after_blob).hexdigest() != digest
                    or source.stat().st_size != before.st_size):
                raise DomainError("SOURCE_CHANGED", "Cover source changed while it was copied")
        except OSError as exc:
            raise DomainError("COVER_STORAGE", "Cannot verify original image copy and source") from exc
        return {"path": target.relative_to(self.home).as_posix(), "managed_path": target.relative_to(self.home).as_posix(), "original_name":source.name,"sha256": digest, "bytes": len(blob), "format": fmt,
                "width": width, "height": height, "source_path": str(source)}

    def _undo(self, c, args, changes, receipt_id):
        key = _text(args, "receipt_id")
        receipt = c.execute("SELECT * FROM receipts WHERE id=?", (key,)).fetchone()
        if not receipt:
            raise DomainError("NOT_FOUND", "Receipt not found")
        if not receipt["undoable"] or receipt["undone_by"]:
            raise DomainError("UNDO_UNAVAILABLE", "Receipt is not undoable or has already been undone")
        recorded = json.loads(receipt["changes"])
        for change in recorded:
            after = change["after"]
            if self._row(c, after["kind"], after["id"], False) != after:
                raise DomainError("UNDO_CONFLICT", "A touched resource changed after this command", {"id": after["id"]})
            before = change["before"]
            if before and before["kind"] == "playlist" and not before["deleted"]:
                try:
                    self._name_available(c, json.loads(before["data"])["name"], before["id"])
                except DomainError as exc:
                    raise DomainError("UNDO_CONFLICT", "A restored playlist name is now occupied") from exc
        for change in recorded:
            before, after = change["before"], change["after"]
            self._put(c, after["kind"], after["id"], json.loads(before["data"] if before else after["data"]),
                      changes, bool(before["deleted"]) if before else True)
        c.execute("UPDATE receipts SET undone_by=? WHERE id=?", (receipt_id, key))
        return {"undone_receipt_id": key}

    def _ensure_track(self, c, path, changes, metadata=None, present=None, root=None, digest=None):
        path = _path(path)
        location = c.execute("SELECT track_id FROM locations WHERE path=?", (path,)).fetchone()
        key = location[0] if location else _uid()
        row = self._row(c, "track", key, False)
        data = json.loads(row["data"]) if row else {"metadata": {}, "note": "", "overrides": {}}
        if metadata is not None:
            data["metadata"] = metadata
        self._put(c, "track", key, data, changes)
        if present is None:
            present = Path(path).is_file()
        c.execute("INSERT INTO locations VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                  "present=excluded.present,root=COALESCE(excluded.root,locations.root),"
                  "sha256=COALESCE(excluded.sha256,locations.sha256)", (path, key, int(present), root, digest))
        return key

    def _known_files(self):
        """Path -> (size, mtime_ns, sha256, metadata) from the last scan, for unchanged-file reuse."""
        known = {}
        try:
            with self._connection() as c:
                for path, digest, data in c.execute(
                        "SELECT l.path, l.sha256, r.data FROM locations l JOIN resources r "
                        "ON r.kind='track' AND r.id=l.track_id WHERE l.sha256 IS NOT NULL AND r.deleted=0"):
                    meta = json.loads(data).get("metadata") or {}
                    if isinstance(meta.get("size"), int) and isinstance(meta.get("mtime_ns"), int) and not meta.get("error"):
                        known[path] = (meta["size"], meta["mtime_ns"], digest, meta)
        except (sqlite3.DatabaseError, ValueError, TypeError):
            return {}
        return known

    def _prepare_scan(self, args,cancel=None,progress=None):
        """Read every audio file under the roots (read-only). A file whose path, size and modification time
        match the previous scan keeps its recorded SHA-256 and tags instead of being read again (frontend
        addition: a rescan of an unchanged library no longer re-hashes every file)."""
        from ..core import tags
        roots = list(dict.fromkeys(_path(p) for p in _list(args, "roots")))
        if not roots:
            raise DomainError("INVALID_ARGUMENT", "At least one root is required")
        known = self._known_files()
        records, errors, complete, seen = [], [], [], set()
        stamp = [0.0]

        def report(i, total, path):
            now = time.monotonic()
            if progress is not None and (now - stamp[0] > 0.3 or i == total):
                stamp[0] = now
                progress({"phase": "scan", "current": i, "total": total, "path": path})

        for root in roots:
            if not Path(root).is_dir():
                raise DomainError("INVALID_ROOT", "Scan root is not an accessible directory", {"root": root})
        for root in roots:
            root_errors = []
            listing = []
            for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: root_errors.append(str(e))):
                dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink()
                                 and not getattr((Path(directory)/d).lstat(),'st_file_attributes',0)&0x400)
                for filename in sorted(files):
                    if cancel is not None and cancel.is_set():
                        raise DomainError('CANCELLED','Scan cancelled before publication')
                    if Path(filename).suffix.lower() in tags.AUDIO_EXTENSIONS:
                        listing.append(Path(directory) / filename)
            for i, original in enumerate(listing, 1):
                if cancel is not None and cancel.is_set():
                    raise DomainError('CANCELLED','Scan cancelled before publication')
                path = _path(original)
                report(i, len(listing), str(original))
                if path in seen:
                    continue
                if not Path(path).is_relative_to(Path(root)):
                    root_errors.append(f"Symlink outside scan root: {original}")
                    continue
                try:
                    before = original.stat()
                    previous = known.get(path)
                    if previous and previous[:2] == (before.st_size, before.st_mtime_ns):
                        digest = previous[2]
                        metadata = dict(previous[3]) | {"rel": original.relative_to(root).as_posix()}
                    else:
                        hasher=hashlib.sha256()
                        with original.open("rb") as stream:
                            while True:
                                if cancel is not None and cancel.is_set():
                                    raise DomainError('CANCELLED','Scan cancelled before publication')
                                chunk=stream.read(1<<20)
                                if not chunk:break
                                hasher.update(chunk)
                        digest=hasher.hexdigest()
                        metadata = {"title": original.stem, "rel": original.relative_to(root).as_posix(),
                                    "size": before.st_size, "mtime_ns": before.st_mtime_ns, "error": None}
                        try:
                            metadata.update(tags.read(original))
                            metadata["title"] = metadata.get("title") or original.stem
                        except Exception as exc:
                            metadata["error"] = f"{type(exc).__name__}: {exc}"
                            errors.append({"path": path, "error": metadata["error"]})
                    after = original.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise DomainError("SOURCE_CHANGED", "Source changed while scanning", {"path": path})
                    records.append({"path": path, "root": root, "metadata": metadata, "digest": digest})
                    seen.add(path)
                except OSError as exc:
                    root_errors.append(str(exc))
            if not root_errors and Path(root).is_dir():
                complete.append(root)
            errors.extend({"path": root, "error": e} for e in root_errors)
        return {"records": records, "complete": complete, "errors": errors}

    def _prepare_refresh(self, args):
        """Re-read known files after their tags were edited (frontend addition: the window's tag editor
        writes the file, then asks for exactly those files to be read again). The track keeps its ID and
        playlists/favourites stay attached, as a rescan of the same path would do; read-only here."""
        from ..core import tags
        paths = list(dict.fromkeys(_path(p) for p in _list(args, "paths")))
        if not paths or len(paths) > 5000:
            raise DomainError("INVALID_ARGUMENT", "paths must list 1..5000 files")
        records = []
        with self._connection() as c:
            for path in paths:
                row = c.execute("SELECT l.track_id, l.root, r.data FROM locations l JOIN resources r ON r.kind='track' "
                                "AND r.id=l.track_id WHERE l.path=? AND r.deleted=0", (path,)).fetchone()
                if not row:
                    raise DomainError("NOT_FOUND", "The file is not in the library", {"path": path})
                records.append((path, row[0], row[1], (json.loads(row[2]).get("metadata") or {}).get("rel")))
        out = []
        for path, track_id, root, rel in records:
            original = Path(path)
            try:
                before = original.stat()
                hasher = hashlib.sha256()
                with original.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1 << 20), b""):
                        hasher.update(chunk)
                metadata = {"title": original.stem, "rel": rel or (original.relative_to(root).as_posix() if root else original.name),
                            "size": before.st_size, "mtime_ns": before.st_mtime_ns, "error": None}
                try:
                    metadata.update(tags.read(original))
                    metadata["title"] = metadata.get("title") or original.stem
                except Exception as exc:
                    metadata["error"] = f"{type(exc).__name__}: {exc}"
                after = original.stat()
            except OSError as exc:
                raise DomainError("SOURCE_UNREADABLE", "The file cannot be read", {"path": path, "reason": str(exc)}) from exc
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise DomainError("SOURCE_CHANGED", "Source changed while reading", {"path": path})
            out.append({"path": path, "track_id": track_id, "metadata": metadata, "digest": hasher.hexdigest()})
        return out

    def _apply_refresh(self, c, prepared, changes):
        tracks = []
        for record in prepared:
            row = c.execute("SELECT track_id FROM locations WHERE path=?", (record["path"],)).fetchone()
            if not row or row[0] != record["track_id"]:
                raise DomainError("SOURCE_CHANGED", "The library changed while reading", {"path": record["path"]})
            data = json.loads(self._row(c, "track", row[0])["data"])
            data["metadata"] = record["metadata"]
            self._put(c, "track", row[0], data, changes)
            c.execute("UPDATE locations SET present=1, sha256=? WHERE path=?", (record["digest"], record["path"]))
            tracks.append(self._track(c, row[0]))
        return {"tracks": tracks}

    def _apply_scan(self, c, prepared, changes):
        seen = {r["path"] for r in prepared["records"]}
        added = relocated = 0
        for record in prepared["records"]:
            path = record["path"]
            existing = c.execute("SELECT track_id FROM locations WHERE path=?", (path,)).fetchone()
            if not existing:
                candidates = c.execute("SELECT DISTINCT track_id FROM locations WHERE sha256=?", (record["digest"],)).fetchall()
                candidates = [r[0] for r in candidates if not any(
                    p[0] in seen or Path(p[0]).is_file() for p in c.execute("SELECT path FROM locations WHERE track_id=?", (r[0],)))]
                if len(candidates) == 1:
                    c.execute("INSERT INTO locations VALUES(?,?,0,?,?)", (path, candidates[0], record["root"], record["digest"]))
                    # The old root may no longer be included in this scan. Its
                    # proven-missing aliases must not outrank the new location.
                    for old in c.execute("SELECT path FROM locations WHERE track_id=?", (candidates[0],)).fetchall():
                        if old[0] != path and not Path(old[0]).is_file():
                            c.execute("UPDATE locations SET present=0 WHERE path=?", (old[0],))
                    relocated += 1
                else:
                    added += 1
            self._ensure_track(c, path, changes, record["metadata"], True, record["root"], record["digest"])
        missing = 0
        for row in c.execute("SELECT path FROM locations WHERE present=1").fetchall():
            if row[0] not in seen and any(Path(row[0]).is_relative_to(Path(root)) for root in prepared["complete"]):
                c.execute("UPDATE locations SET present=0 WHERE path=?", (row[0],))
                missing += 1
        return {"found": len(seen), "added": added, "relocated": relocated, "missing": missing, "errors": prepared["errors"]}

    def _prepare_relink(self, args, cancel=None):
        old, new = _path(args.get("old_root")), _path(args.get("new_root"))
        if old == new or not Path(new).is_dir():
            raise DomainError("INVALID_ARGUMENT", "Choose a distinct existing replacement directory")
        with self._connection() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM locations WHERE root=?", (old,))]
        if not rows:
            raise DomainError("NOT_FOUND", "Original root has no registered tracks")
        targets = []
        for row in rows:
            if cancel is not None and cancel.is_set():
                raise DomainError("CANCELLED", "Relink cancelled before publication")
            relative = Path(row["path"]).relative_to(old)
            original = Path(new) / relative
            if original.is_symlink() or any(q.is_symlink() for q in original.parents if q != Path(new).parent and q.is_relative_to(new)):
                raise DomainError("PATH_NOT_ALLOWED", "Replacement tracks cannot traverse links")
            if not original.is_file():
                targets.append(row | {"new_path": None})
                continue
            before = original.stat()
            with original.open("rb") as stream:
                got = hashlib.file_digest(stream, "sha256").hexdigest()
            after = original.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise DomainError("SOURCE_CHANGED", "Replacement track changed during inspection")
            if not row.get("sha256") or got != row["sha256"]:
                raise DomainError("RELINK_CONFLICT", "Replacement track differs from registered bytes", {"path": str(original)})
            targets.append(row | {"new_path": _path(original), "verified_size": before.st_size,
                                  "verified_mtime_ns": before.st_mtime_ns})
        return {"old_root": old, "new_root": new, "targets": targets}

    def _apply_relink(self, c, prepared, changes):
        # Verify the entire metadata transaction before publishing any location.
        for row in prepared["targets"]:
            target = row["new_path"]
            prior = c.execute("SELECT track_id FROM locations WHERE path=?", (row["path"],)).fetchone()
            if prior is None or prior[0] != row["track_id"]:
                raise DomainError("REVISION_CONFLICT", "Source membership changed during relink")
            if target:
                current = c.execute("SELECT track_id FROM locations WHERE path=?", (target,)).fetchone()
                if current and current[0] != row["track_id"]:
                    raise DomainError("RELINK_CONFLICT", "Destination already belongs to a different track")
                st = Path(target).stat()
                if (st.st_size, st.st_mtime_ns) != (row["verified_size"], row["verified_mtime_ns"]):
                    raise DomainError("SOURCE_CHANGED", "Replacement changed before relink commit")
        for row in prepared["targets"]:
            c.execute("UPDATE locations SET present=0 WHERE path=?", (row["path"],))
            if row["new_path"]:
                c.execute("INSERT INTO locations VALUES(?,?,1,?,?) ON CONFLICT(path) DO UPDATE SET present=1,root=excluded.root,sha256=excluded.sha256",
                          (row["new_path"], row["track_id"], prepared["new_root"], row["sha256"]))
        return {"old_root": prepared["old_root"], "new_root": prepared["new_root"],
                "relinked": sum(bool(r["new_path"]) for r in prepared["targets"]),
                "unresolved": sum(not r["new_path"] for r in prepared["targets"])}

    def _prepare_legacy(self, args):
        path = Path(_path(args.get("path")))
        if path == self.path or not path.is_file():
            raise DomainError("INVALID_LEGACY", "Select an existing legacy database distinct from application.sqlite3")
        # Copy source bytes read-only, including WAL if present. SQLite recovery and backup
        # operate only on the private copy; opening the source could update its SHM file.
        files = [path] + ([Path(str(path) + "-wal")] if Path(str(path) + "-wal").exists() else [])
        blobs = {str(p): p.read_bytes() for p in files}
        digest = hashlib.sha256(b"".join(blobs.values())).hexdigest()
        stage = self.home / ("legacy-snapshot-" + _uid() + ".sqlite3")
        snapshot = sqlite3.connect(":memory:")
        snapshot.row_factory = sqlite3.Row
        try:
            stage.write_bytes(blobs[str(path)])
            if len(files) > 1:
                Path(str(stage) + "-wal").write_bytes(blobs[str(files[1])])
            if any(p.read_bytes() != blobs[str(p)] for p in files) or (Path(str(path) + "-wal").exists() != (len(files) > 1)):
                raise DomainError("LEGACY_CHANGED", "Close the legacy application before importing its database")
            source = sqlite3.connect(stage)
            try:
                source.backup(snapshot)
            finally:
                source.close()
            tables = {r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"tracks", "playlists", "playlist_items"} <= tables:
                raise DomainError("INVALID_LEGACY", "Database lacks expected legacy library tables")
            output = {table: [dict(r) for r in snapshot.execute('SELECT * FROM "' + table + '"')]
                      for table in ("tracks", "playlists", "playlist_items", "settings", "notes", "overrides", "presets") if table in tables}
            playlist_ids = {p["id"] for p in output["playlists"]}
            if any(item["playlist_id"] not in playlist_ids for item in output["playlist_items"]):
                raise DomainError("INVALID_LEGACY", "Legacy playlist members contain an orphan playlist_id")
            return {"path": str(path), "sha256": digest, "tables": output}
        except sqlite3.DatabaseError as exc:
            raise DomainError("INVALID_LEGACY", "Cannot read legacy database", {"reason": str(exc)}) from exc
        finally:
            snapshot.close()
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(stage) + suffix).unlink(missing_ok=True)

    def _apply_legacy(self, c, prepared, changes):
        previous = c.execute("SELECT sha256 FROM legacy_imports WHERE path=?", (prepared["path"],)).fetchone()
        if previous:
            if previous[0] != prepared["sha256"]:
                raise DomainError("IMPORT_CONFLICT", "This legacy source changed after its one-time import")
            return {"already_imported": True, "path": prepared["path"]}
        tables, tracks = prepared["tables"], {}
        def track(path, metadata=None):
            canonical = _path(path)
            if canonical not in tracks:
                tracks[canonical] = self._ensure_track(c, canonical, changes, metadata)
            return tracks[canonical]
        for row in tables.get("tracks", []):
            track(row["path"], {k: v for k, v in row.items() if k != "path"})
        for row in tables.get("notes", []):
            key = track(row["path"])
            data = json.loads(self._row(c, "track", key)["data"])
            data["note"] = row["text"]
            self._put(c, "track", key, data, changes)
        for row in tables.get("overrides", []):
            key = track(row["path"])
            data = json.loads(self._row(c, "track", key)["data"])
            data.setdefault("overrides", {})[row["field"]] = row["value"]
            self._put(c, "track", key, data, changes)
        count = 0
        for row in tables.get("playlists", []):
            self._name_available(c, row["name"])
            items = sorted((i for i in tables.get("playlist_items", []) if i["playlist_id"] == row["id"]), key=lambda i: i["position"])
            self._put(c, "playlist", _uid(), {"name": row["name"], "items": [
                {"occurrence_id": _uid(), "track_id": track(i["path"])} for i in items],
                "legacy": {"source": prepared["path"], "id": row["id"], "created": row.get("created"), "modified": row.get("modified")}}, changes)
            count += 1
        for row in tables.get("settings", []):
            try:
                value = json.loads(row["value"])
            except (ValueError, TypeError):
                value = row["value"]
            if c.execute("SELECT 1 FROM settings WHERE key=?", (row["key"],)).fetchone():
                raise DomainError("IMPORT_CONFLICT", "An imported setting would replace existing state", {"key": row["key"]})
            c.execute("INSERT INTO settings VALUES(?,?)", (row["key"], _json(value)))
            if row["key"] == "favorites" and isinstance(value, list):
                ids = json.loads(self._row(c, "favorites", "main")["data"])["track_ids"]
                self._put(c, "favorites", "main", {"track_ids": list(dict.fromkeys(ids + [track(p) for p in value]))}, changes)
        for row in tables.get("presets", []):
            try:
                data = json.loads(row["data"])
            except (ValueError, TypeError):
                data = {"legacy_data": row["data"]}
            if not isinstance(data, dict):
                data = {"legacy_data": data}
            if self._row(c, "preset", row["name"], False):
                raise DomainError("IMPORT_CONFLICT", "An imported preset would replace existing state", {"name": row["name"]})
            self._put(c, "preset", row["name"], data | {"name": row["name"]}, changes)
        c.execute("INSERT INTO legacy_imports VALUES(?,?)", (prepared["path"], prepared["sha256"]))
        return {"path": prepared["path"], "tracks": len(tracks), "playlists": count,
                "presets": len(tables.get("presets", [])), "already_imported": False}
