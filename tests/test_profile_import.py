"""Copy-only portable import using synthetic applications; no personal profiles."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))

from xm5lib.application import Application
from xm5lib.core import profile_import
from xm5lib.core.portable import ConfigError, ConfigStore


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class PortableImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xm5-profile-import-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "Portable 音乐"
        self.source.mkdir()
        self.target = self.root / "Installed profile"
        music = self.source / "music"
        music.mkdir()
        song = music / "fixture.wav"
        with wave.open(str(song), "wb") as out:
            out.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            out.writeframes(struct.pack("<64h", *([400] * 64)))
        (self.source / "presets").mkdir()
        (self.source / "presets/fixture.txt").write_text("Preamp: -3 dB\n", encoding="utf-8")
        self.config = {"schema": "xm5.application-config", "version": 1, "revision": 7,
                       "music_sources": [{"id": "music-source", "path": "music", "enabled": True, "label": "Music"}],
                       "peq_sources": [{"id": "peq-source", "path": "presets", "enabled": False, "label": "EQ"}],
                       "ui": {"language": "en", "player_volume": 35}, "lyrics": {"auto_search": False},
                       "future_field": {"preserve": True}}
        self.write_config()
        app = Application(self.source / "data/backend")
        try:
            app.execute("library.scan", {"roots": [str(music)]})
            track = app.query("tracks.all")["tracks"][0]
            self.track_id = track["id"]
            self.playlist = app.execute("playlists.create", {"name": "Fixture listening", "track_ids": [self.track_id, self.track_id]})["result"]
            app.execute("favorites.set", {"track_ids": [self.track_id], "favorite": True})
            app.execute("tracks.annotate", {"id": self.track_id, "note": "Fixture note"}, expected_revision=track["revision"])
            app.execute("presets.save", {"preset": {"name": "Fixture EQ", "preamp": -3, "bands": []}})
        finally:
            app.close()
        for rel in ("runtime/backend/token", "cache/cover.jpg", "data/backend/cache/thumbnail.jpg",
                    "data/backend/token", "data/backend/owner.lock", "data/backend/connection.json"):
            path = self.source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic excluded artifact")

    def write_config(self):
        (self.source / "config.json").write_text(json.dumps(self.config, ensure_ascii=False) + "\n", encoding="utf-8")

    def assert_no_import_artifacts(self):
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob(".Installed profile.import-*")), [])
        self.assertFalse((self.root / ".Installed profile.import.lock").exists())

    def test_preserves_durable_resources_and_rebases_relative_sources_copy_only(self):
        before = hashes(self.source)
        with patch.object(ConfigStore, "__init__", side_effect=AssertionError("Source config must be read-only")):
            receipt = profile_import.import_portable_profile(self.source, self.target)
        self.assertEqual(hashes(self.source), before)
        self.assertEqual(receipt["config_sha256"], before["config.json"])
        config = ConfigStore(self.target / "config.json", app_root=self.target)
        self.assertEqual(config.source_roots("music"), [str(self.source / "music")])
        self.assertEqual(config.sources("peq", include_disabled=True)[0]["path"], str(self.source / "presets"))
        self.assertFalse(config.sources("peq", include_disabled=True)[0]["enabled"])
        self.assertEqual(config.snapshot()["future_field"], {"preserve": True})
        self.assertEqual(config.snapshot()["revision"], 7)
        app = Application(self.target / "data/backend")
        try:
            self.assertEqual(app.query("playlists.get", {"id": self.playlist["id"]})["playlist"]["track_ids"], [self.track_id, self.track_id])
            self.assertEqual(app.query("favorites.list")["track_ids"], [self.track_id])
            self.assertEqual(app.query("tracks.get", {"id": self.track_id})["track"]["note"], "Fixture note")
            self.assertEqual(app.query("presets.list")["presets"][0]["name"], "Fixture EQ")
        finally:
            app.close()
        for rel in ("runtime", "cache", "data/backend/cache", "data/backend/token", "data/backend/owner.lock", "data/backend/connection.json"):
            self.assertFalse((self.target / rel).exists(), rel)

    def test_absolute_source_paths_remain_absolute_and_unchanged(self):
        self.config["music_sources"][0]["path"] = str(self.root / "Elsewhere")
        self.write_config()
        profile_import.import_portable_profile(self.source, self.target)
        imported = json.loads((self.target / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(imported["music_sources"][0]["path"], self.config["music_sources"][0]["path"])

    def test_existing_destination_never_replaced_even_when_empty(self):
        for nonempty in (False, True):
            with self.subTest(nonempty=nonempty):
                self.target.mkdir(exist_ok=True)
                if nonempty:
                    (self.target / "config.json").write_text("existing", encoding="utf-8")
                before = hashes(self.target)
                with self.assertRaisesRegex(ConfigError, "already exists"):
                    profile_import.import_portable_profile(self.source, self.target)
                self.assertEqual(hashes(self.target), before)

    def test_file_destination_and_overlap_refused(self):
        self.target.write_text("unrelated", encoding="utf-8")
        with self.assertRaises(ConfigError):
            profile_import.import_portable_profile(self.source, self.target)
        self.assertEqual(self.target.read_text(), "unrelated")
        for target in (self.source, self.source / "nested", self.root):
            with self.subTest(target=target), self.assertRaisesRegex(ConfigError, "overlap"):
                profile_import.import_portable_profile(self.source, target)

    def test_active_backend_discovery_and_gui_or_config_lock_refused(self):
        before = hashes(self.source)
        for rel in ("runtime/backend/connection.json", "runtime/gui-control/session.json", "config.json.lock"):
            with self.subTest(rel=rel):
                active = self.source / rel
                active.parent.mkdir(parents=True, exist_ok=True)
                active.write_text("synthetic active owner", encoding="utf-8")
                with self.assertRaises(ConfigError):
                    profile_import.import_portable_profile(self.source, self.target)
                self.assert_no_import_artifacts()
                active.unlink()
        self.assertEqual(hashes(self.source), before)

    def test_malformed_or_unsupported_config_rejected_without_source_rewrite(self):
        for payload in (b"{", b"[]", b'{"schema":"future"}', b"\xff"):
            with self.subTest(payload=payload):
                (self.source / "config.json").write_bytes(payload)
                before = hashes(self.source)
                with self.assertRaises(ConfigError):
                    profile_import.import_portable_profile(self.source, self.target)
                self.assertEqual(hashes(self.source), before)
                self.assert_no_import_artifacts()

    def test_unrecognized_source_and_missing_backend_refused(self):
        for source in (self.root / "Absent", self.root):
            with self.subTest(source=source), self.assertRaises(ConfigError):
                profile_import.import_portable_profile(source, self.target)
        (self.source / "data/backend/application.sqlite3").unlink()
        with self.assertRaisesRegex(ConfigError, "recognized backend"):
            profile_import.import_portable_profile(self.source, self.target)
        self.assert_no_import_artifacts()

    def test_config_drift_after_backend_copy_prevents_publication(self):
        real_copy = profile_import.copy_profile

        def changed(source, target):
            result = real_copy(source, target)
            (self.source / "config.json").write_bytes((self.source / "config.json").read_bytes() + b" ")
            return result

        with patch.object(profile_import, "copy_profile", side_effect=changed):
            with self.assertRaisesRegex(ConfigError, "changed during import"):
                profile_import.import_portable_profile(self.source, self.target)
        self.assert_no_import_artifacts()

    def test_copy_failure_removes_only_owned_staging(self):
        sibling = self.root / ".Installed profile.import-unrelated"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("keep", encoding="utf-8")
        before = hashes(self.source)

        def fail(source, target):
            target.mkdir(parents=True)
            (target / "partial.bin").write_bytes(b"partial")
            raise ConfigError("Synthetic copy failure")

        with patch.object(profile_import, "copy_profile", side_effect=fail):
            with self.assertRaisesRegex(ConfigError, "Synthetic copy failure"):
                profile_import.import_portable_profile(self.source, self.target)
        self.assertEqual(hashes(self.source), before)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob(".Installed profile.import-*")), [sibling])
        self.assertEqual((sibling / "keep.txt").read_text(), "keep")

    def test_publish_failure_removes_stage_without_changing_source(self):
        before = hashes(self.source)
        real_rename = os.rename

        def fail_final(source, target):
            if Path(target) == self.target:
                raise OSError("Synthetic publish failure")
            return real_rename(source, target)

        with patch.object(profile_import.os, "rename", side_effect=fail_final):
            with self.assertRaisesRegex(ConfigError, "publish failure"):
                profile_import.import_portable_profile(self.source, self.target)
        self.assertEqual(hashes(self.source), before)
        self.assert_no_import_artifacts()

    def test_destination_appearing_during_copy_is_preserved(self):
        real_copy = profile_import.copy_profile

        def destination_appears(source, target):
            receipt = real_copy(source, target)
            self.target.mkdir()
            (self.target / "unrelated.txt").write_text("keep", encoding="utf-8")
            return receipt

        with patch.object(profile_import, "copy_profile", side_effect=destination_appears):
            with self.assertRaisesRegex(ConfigError, "appeared during import"):
                profile_import.import_portable_profile(self.source, self.target)
        self.assertEqual((self.target / "unrelated.txt").read_text(), "keep")
        self.assertEqual(list(self.root.glob(".Installed profile.import-*")), [])
        self.assertFalse((self.root / ".Installed profile.import.lock").exists())

    def test_wal_commits_recovered_from_staged_copy_without_modifying_originals(self):
        database = self.source / "data/backend/application.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE import_wal_fixture(value TEXT)")
            connection.execute("INSERT INTO import_wal_fixture VALUES('committed fixture')")
            connection.commit()
            before = hashes(self.source)
            profile_import.import_portable_profile(self.source, self.target)
            self.assertEqual(hashes(self.source), before)
            copied = sqlite3.connect(self.target / "data/backend/application.sqlite3")
            try:
                self.assertEqual(copied.execute("SELECT value FROM import_wal_fixture").fetchone()[0], "committed fixture")
            finally:
                copied.close()
        finally:
            connection.close()

    def test_source_or_destination_link_rejected(self):
        linked = self.root / "linked source"
        try:
            linked.symlink_to(self.source, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                self.skipTest(f"Symlink creation unavailable: {exc}")
            import _winapi
            _winapi.CreateJunction(str(self.source), str(linked))
        with self.assertRaisesRegex(ConfigError, "links"):
            profile_import.import_portable_profile(linked, self.target)
        with self.assertRaisesRegex(ConfigError, "links"):
            profile_import.import_portable_profile(self.source, linked / "new profile")
        self.assert_no_import_artifacts()

    @unittest.skipUnless(os.name == "nt", "Windows owner-lock acceptance")
    def test_held_backend_owner_lock_refuses_import(self):
        import msvcrt
        lock = self.source / "runtime/backend/owner.lock"
        lock.write_bytes(b"0")
        with open(lock, "r+b") as held:
            msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                with self.assertRaisesRegex(ConfigError, "owner lock is held"):
                    profile_import.import_portable_profile(self.source, self.target)
                self.assert_no_import_artifacts()
            finally:
                held.seek(0)
                msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)

    def test_closed_backend_owner_lock_can_be_read_without_copying_runtime(self):
        lock = self.source / "runtime/backend/owner.lock"
        lock.write_bytes(b"0")
        before = hashes(self.source)
        profile_import.import_portable_profile(self.source, self.target)
        self.assertEqual(hashes(self.source), before)
        self.assertFalse((self.target / "runtime").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
