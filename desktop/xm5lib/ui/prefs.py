"""Flat GUI preference adapter for the application-root config.json.

Library tracks, playlists, favorites, annotations and original artwork stay in the
portable backend database/profile. AppData inputs require explicit migration.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from ..core.portable import ConfigError, ConfigStore

GUI_KEYS = ("music_root", "peace_dir", "skin_theme", "player_volume", "player_mode", "player_device",
            "player_exclusive", "case_grooves", "export_parent")


def legacy_settings(db: Path) -> dict:
    """The old GUI's settings table, read from a temporary copy (never opens the original)."""
    db = Path(db)
    if not db.is_file():
        return {}
    with tempfile.TemporaryDirectory(prefix="xm5-legacy-") as tmp:
        copy = Path(tmp) / "legacy.sqlite"
        try:
            shutil.copyfile(db, copy)
            wal = Path(str(db) + "-wal")
            if wal.is_file():
                shutil.copyfile(wal, Path(str(copy) + "-wal"))
            con = sqlite3.connect(copy)
            try:
                rows = con.execute("SELECT key, value FROM settings").fetchall()
            finally:
                con.close()
        except (OSError, sqlite3.DatabaseError):
            return {}
    out = {}
    for key, value in rows:
        try:
            out[key] = json.loads(value)
        except (TypeError, ValueError):
            out[key] = value
    return out


class Prefs:
    """The flat page API backed by the single serialized ConfigStore writer.

    ``seed`` and ``legacy_db`` are explicit migration inputs. The normal entry point
    only supplies the previous application-local GUI file; AppData is never probed.
    """
    def __init__(self, path: Path, legacy_db: Path | None = None, seed: Path | None = None,
                 app_root: Path | None = None):
        self.path = Path(path)
        self.store = ConfigStore(path, app_root=app_root, legacy=seed)
        self.on_error = None
        self.seeded = self.store.seeded
        if legacy_db is not None and not seed and not self.store.get("legacy_db_imported", False):
            old = legacy_settings(legacy_db)
            for key in GUI_KEYS:
                if old.get(key) not in (None, ""):
                    self.store.set(key, old[key])
            self.store.set("legacy_db_imported", True)

    @property
    def data(self):
        return self.store.snapshot()

    def get(self, key: str, default=None):
        return self._call(self.store.get, key, default)

    def set(self, key: str, value) -> None:
        self._call(self.store.set, key, value)

    def register_source(self, kind, path):
        return self._call(self.store.register_source, kind, path)

    def relink_source(self, kind, source_id, path):
        return self._call(self.store.relink_source, kind, source_id, path)

    def remove_source(self, kind, source_id):
        return self._call(self.store.remove_source, kind, source_id)

    def _call(self, operation, *args):
        try:
            return operation(*args)
        except ConfigError as exc:
            if self.on_error is not None:
                self.on_error(str(exc))
            raise


__all__ = ["Prefs", "legacy_settings", "GUI_KEYS"]
