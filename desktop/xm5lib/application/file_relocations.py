"""Application-owned acceptance of an explicit staged file relocation.

The script transaction moves verified ordinary files. This command publishes
the corresponding location mapping under the normal Application transaction.
It never discovers, guesses by equal hashes, renames, or writes source bytes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import stat

from .service import DomainError, _path
from ..core.restore_card import no_links


def prepare_relocations(application, args):
    moves = args.get("moves")
    if not isinstance(moves, list) or not 1 <= len(moves) <= 10_000:
        raise DomainError("INVALID_ARGUMENT", "moves must contain 1..10000 exact relocations")
    result, old_paths, new_paths, tracks = [], set(), set(), set()
    with application._connection() as connection:
        for move in moves:
            if not isinstance(move, dict) or set(move) != {"track_id", "old_path", "new_path", "sha256"}:
                raise DomainError("INVALID_ARGUMENT", "Relocations require exact track_id/old_path/new_path/sha256")
            old, new, tid = _path(move["old_path"]), _path(move["new_path"]), move["track_id"]
            if old == new or old in old_paths or new in new_paths or tid in tracks:
                raise DomainError("RELOCATION_CONFLICT", "Relocations require distinct paths and unique track IDs")
            old_paths.add(old); new_paths.add(new); tracks.add(tid)
            row = connection.execute("SELECT track_id,root,sha256 FROM locations WHERE path=? AND present=1", (old,)).fetchone()
            if row is None or row["track_id"] != tid or row["sha256"] != move["sha256"]:
                raise DomainError("REVISION_CONFLICT", "Original location/bytes no longer match the staged relocation")
            if not row["root"] or not Path(new).is_relative_to(Path(row["root"])):
                raise DomainError("SOURCE_SCOPE", "Relocation must remain inside the original registered source root")
            if connection.execute("SELECT 1 FROM locations WHERE path=? AND track_id<>?", (new,tid)).fetchone():
                raise DomainError("RELOCATION_CONFLICT", "New location belongs to another track")
            no_links(old); no_links(new)
            if os.path.lexists(old) or not Path(new).is_file() or Path(new).suffix.lower()!=Path(old).suffix.lower():
                raise DomainError("SOURCE_CHANGED", "Staged relocation requires absent original and present same-format new path")
            before=Path(new).stat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise DomainError("SOURCE_SCOPE", "Relocated source must be a single-link ordinary file")
            with Path(new).open("rb") as stream: actual=hashlib.file_digest(stream,"sha256").hexdigest()
            after=Path(new).stat()
            if actual!=move["sha256"] or (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
                raise DomainError("SOURCE_CHANGED", "Relocated source changed during exact-byte verification")
            application._row(connection,"track",tid)
            result.append({**move,"old_path":old,"new_path":new,"root":row["root"],"size":after.st_size,
                           "mtime_ns":after.st_mtime_ns,"inode":after.st_ino})
    if old_paths & new_paths:
        raise DomainError("RELOCATION_CONFLICT", "Swaps/cycles require a separate explicit transaction")
    return result


def apply_relocations(application, connection, prepared, changes):
    for move in prepared:
        row=connection.execute("SELECT track_id,root,sha256,present FROM locations WHERE path=?",(move["old_path"],)).fetchone()
        new=Path(move["new_path"]); no_links(new); no_links(move["old_path"])
        current=new.stat()
        if (row is None or row["track_id"]!=move["track_id"] or not row["present"] or row["sha256"]!=move["sha256"]
                or Path(move["old_path"]).exists() or current.st_nlink!=1
                or (current.st_size,current.st_mtime_ns,current.st_ino)!=(move["size"],move["mtime_ns"],move["inode"])):
            raise DomainError("SOURCE_CHANGED","Relocation changed before application commit")
        if connection.execute("SELECT 1 FROM locations WHERE path=? AND track_id<>?",(move["new_path"],move["track_id"])).fetchone():
            raise DomainError("RELOCATION_CONFLICT","New location was claimed during preparation")
    tracks=[]
    for move in prepared:
        connection.execute("UPDATE locations SET present=0 WHERE path=?",(move["old_path"],))
        connection.execute("INSERT INTO locations VALUES(?,?,1,?,?) ON CONFLICT(path) DO UPDATE SET present=1,root=excluded.root,sha256=excluded.sha256",
                           (move["new_path"],move["track_id"],move["root"],move["sha256"]))
        data=json.loads(application._row(connection,"track",move["track_id"])["data"])
        data.setdefault("metadata",{}).update(rel=Path(move["new_path"]).relative_to(move["root"]).as_posix(),size=move["size"],mtime_ns=move["mtime_ns"])
        application._put(connection,"track",move["track_id"],data,changes)
        tracks.append(application._track(connection,move["track_id"]))
    return {"tracks":tracks,"relocated":len(tracks)}
