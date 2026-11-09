"""SQLite store for the library index, managed metadata overrides, notes, playlists and presets.

Source music is never written. Overrides, notes and playlist entries are keyed by absolute source
path, so a rescan (which may renumber track rows) does not lose them. Every connection is
short-lived or thread-local; scans run on their own connection (WAL mode).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tracks(
    path TEXT PRIMARY KEY,
    rel TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    title TEXT, artist TEXT, album TEXT, albumartist TEXT,
    tracknumber INTEGER, discnumber INTEGER, date TEXT, genre TEXT, comment TEXT,
    codec TEXT, sample_rate INTEGER, bits INTEGER, channels INTEGER, bitrate INTEGER,
    duration REAL, has_embedded_art INTEGER DEFAULT 0,
    error TEXT, scanned_at REAL
);
CREATE INDEX IF NOT EXISTS tracks_album ON tracks(albumartist, album);
CREATE TABLE IF NOT EXISTS overrides(path TEXT, field TEXT, value TEXT, PRIMARY KEY(path, field));
CREATE TABLE IF NOT EXISTS notes(path TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS playlists(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, created REAL, modified REAL);
CREATE TABLE IF NOT EXISTS playlist_items(playlist_id INTEGER NOT NULL, position INTEGER NOT NULL, path TEXT NOT NULL,
    PRIMARY KEY(playlist_id, position));
CREATE TABLE IF NOT EXISTS presets(name TEXT PRIMARY KEY, data TEXT NOT NULL, modified REAL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""

# Fields a user may override. Values are strings; empty string means "clear this tag on export".
EDITABLE_FIELDS = ("title", "artist", "album", "albumartist", "tracknumber", "discnumber", "date", "genre", "comment")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- settings
    def setting(self, key: str, default=None):
        with self.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES(?, ?)", (key, json.dumps(value)))

    # ---- tracks
    def tracks(self) -> list[dict]:
        with self.connect() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM tracks")]
            ov: dict[str, dict] = {}
            for r in c.execute("SELECT path, field, value FROM overrides"):
                ov.setdefault(r["path"], {})[r["field"]] = r["value"]
            notes = {r["path"]: r["text"] for r in c.execute("SELECT path, text FROM notes")}
        for row in rows:
            row["overrides"] = ov.get(row["path"], {})
            row["note"] = notes.get(row["path"], "")
        return rows

    def track_count(self) -> int:
        with self.connect() as c:
            return c.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    # ---- overrides / notes
    def set_overrides(self, paths: list[str], changes: dict[str, str | None]) -> None:
        """``changes``: field -> new value, or None to drop the override (revert to the file's tag)."""
        bad = set(changes) - set(EDITABLE_FIELDS)
        if bad:
            raise ValueError(f"not editable: {sorted(bad)}")
        with self.connect() as c:
            for p in paths:
                for f, v in changes.items():
                    if v is None:
                        c.execute("DELETE FROM overrides WHERE path=? AND field=?", (p, f))
                    else:
                        c.execute("INSERT OR REPLACE INTO overrides VALUES(?, ?, ?)", (p, f, v))

    def set_note(self, paths: list[str], text: str) -> None:
        with self.connect() as c:
            for p in paths:
                if text.strip():
                    c.execute("INSERT OR REPLACE INTO notes VALUES(?, ?)", (p, text))
                else:
                    c.execute("DELETE FROM notes WHERE path=?", (p,))

    # ---- playlists
    def playlists(self) -> list[dict]:
        with self.connect() as c:
            return [dict(r) | {"count": r["n"]} for r in c.execute(
                "SELECT p.id, p.name, p.modified, COUNT(i.path) AS n FROM playlists p "
                "LEFT JOIN playlist_items i ON i.playlist_id=p.id GROUP BY p.id ORDER BY p.name COLLATE NOCASE")]

    def create_playlist(self, name: str) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Playlist name is empty")
        now = time.time()
        with self.connect() as c:
            if c.execute("SELECT 1 FROM playlists WHERE name=?", (name,)).fetchone():
                raise ValueError(f"A playlist named '{name}' already exists")
            return c.execute("INSERT INTO playlists(name, created, modified) VALUES(?, ?, ?)", (name, now, now)).lastrowid

    def rename_playlist(self, pid: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("Playlist name is empty")
        with self.connect() as c:
            if c.execute("SELECT 1 FROM playlists WHERE name=? AND id<>?", (name, pid)).fetchone():
                raise ValueError(f"A playlist named '{name}' already exists")
            c.execute("UPDATE playlists SET name=?, modified=? WHERE id=?", (name, time.time(), pid))

    def delete_playlist(self, pid: int) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
            c.execute("DELETE FROM playlists WHERE id=?", (pid,))

    def playlist_paths(self, pid: int) -> list[str]:
        with self.connect() as c:
            return [r[0] for r in c.execute("SELECT path FROM playlist_items WHERE playlist_id=? ORDER BY position", (pid,))]

    def set_playlist_paths(self, pid: int, paths: list[str]) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
            c.executemany("INSERT INTO playlist_items VALUES(?, ?, ?)", [(pid, i, p) for i, p in enumerate(paths)])
            c.execute("UPDATE playlists SET modified=? WHERE id=?", (time.time(), pid))

    def playlist_name(self, pid: int) -> str:
        with self.connect() as c:
            row = c.execute("SELECT name FROM playlists WHERE id=?", (pid,)).fetchone()
        if not row:
            raise KeyError(pid)
        return row[0]

    # ---- PEQ presets (JSON from peq.Preset.to_dict)
    def presets(self) -> list[dict]:
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT data FROM presets ORDER BY name COLLATE NOCASE")]

    def save_preset(self, data: dict, replace: bool = True) -> None:
        with self.connect() as c:
            if not replace and c.execute("SELECT 1 FROM presets WHERE name=?", (data["name"],)).fetchone():
                raise ValueError(f"A preset named '{data['name']}' already exists")
            c.execute("INSERT OR REPLACE INTO presets VALUES(?, ?, ?)", (data["name"], json.dumps(data), time.time()))

    def delete_preset(self, name: str) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM presets WHERE name=?", (name,))
