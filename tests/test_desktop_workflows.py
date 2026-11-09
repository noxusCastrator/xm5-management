"""Public desktop service regressions with disposable synthetic input only.

The test runner must put the repository's ``desktop`` directory on sys.path.
These tests use the real Engine/Application and public configuration/playlist
interfaces. They do not start Qt, an HTTP server, media processes or audio output.
They are a focused public suite, not the historical 146-case acceptance ledger.
"""
from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import time
import unittest
import wave

from xm5lib.application import Application, DomainError
from xm5lib.automation.engine import Engine
from xm5lib.core.playlists import read_m3u, write_m3u8
from xm5lib.core.portable import ConfigError, ConfigStore


def make_wav(path: Path, sample: int = 0) -> bytes:
    """Create a short standard PCM WAV without any encoder or user music."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack("<64h", *([sample] * 64)))
    return path.read_bytes()


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="xm5-public-workflow-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.music = self.root / "Music Space \u97f3\u4e50"
        self.first = self.music / "first.wav"
        self.second = self.music / "second.wav"
        self.originals = {
            self.first: make_wav(self.first),
            self.second: make_wav(self.second, 1000),
        }

    def application(self, name="application"):
        app = Application(self.root / name)
        self.addCleanup(app.close)
        return app

    def scanned_application(self):
        app = self.application()
        receipt = app.execute("library.scan", {"roots": [str(self.music)]})
        self.assertEqual(receipt["result"]["errors"], [])
        tracks = app.query("tracks.all")["tracks"]
        by_name = {Path(track["path"]).name: track for track in tracks}
        return app, by_name["first.wav"]["id"], by_name["second.wav"]["id"]

    def engine(self):
        engine = Engine(self.root / "engine", music_roots=[self.music])
        self.addCleanup(engine.close)
        return engine

    def wait_job(self, engine, submitted):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = engine.query("jobs.get", {"id": submitted["job_id"]})
            if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return job
            time.sleep(0.01)
        self.fail("Desktop scan did not finish within 10 seconds")


class DesktopWorkflowTests(WorkflowFixture):
    def test_engine_scan_indexes_audio_without_changing_original_bytes(self):
        engine = self.engine()
        submitted = engine.command("library.scan", {"roots": [str(self.music)]}, None, "scan-local")
        job = self.wait_job(engine, submitted)
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(job["result"]["result"]["errors"], [])
        tracks = engine.query("tracks.all", {})["tracks"]
        self.assertEqual({Path(track["path"]).name for track in tracks}, {"first.wav", "second.wav"})
        self.assertTrue(all(not track["unresolved"] for track in tracks))
        for path, before in self.originals.items():
            self.assertEqual(path.read_bytes(), before)

    def test_engine_rejects_scan_outside_allowed_music_root(self):
        outside = self.root / "Outside"
        original = make_wav(outside / "Unapproved.wav")
        engine = self.engine()
        submitted = engine.command("library.scan", {"roots": [str(outside)]}, None, "scan-outside")
        job = self.wait_job(engine, submitted)
        self.assertEqual(job["status"], "failed", job)
        self.assertTrue(job["error"].startswith("PATH_NOT_ALLOWED:"), job)
        self.assertEqual(engine.query("tracks.all", {})["tracks"], [])
        self.assertEqual((outside / "Unapproved.wav").read_bytes(), original)

    def test_scan_of_missing_root_keeps_existing_library(self):
        app, first, second = self.scanned_application()
        before = app.query("tracks.all")
        with self.assertRaises(DomainError) as rejected:
            app.execute("library.scan", {"roots": [str(self.root / "Absent")]})
        self.assertEqual(rejected.exception.code, "INVALID_ROOT")
        after = app.query("tracks.all")
        self.assertEqual(after, before)

    def test_playlist_repeat_order_and_name_survive_restart(self):
        app, first, second = self.scanned_application()
        created = app.execute("playlists.create", {"name": "Evening", "track_ids": [second, first, second]})
        playlist = created["result"]
        occurrence_ids = [item["occurrence_id"] for item in playlist["items"]]
        self.assertEqual(len(set(occurrence_ids)), 3)
        app.execute("playlists.rename", {"id": playlist["id"], "name": "Evening revised"},
                    expected_revision=playlist["revision"])
        app.close()
        reopened = self.application()
        restored = reopened.query("playlists.get", {"id": playlist["id"]})["playlist"]
        self.assertEqual(restored["name"], "Evening revised")
        self.assertEqual(restored["track_ids"], [second, first, second])
        self.assertEqual([item["occurrence_id"] for item in restored["items"]], occurrence_ids)

    def test_stale_playlist_revision_cannot_overwrite_newer_name(self):
        app, first, second = self.scanned_application()
        playlist = app.execute("playlists.create", {"name": "Original", "track_ids": [first]})["result"]
        app.execute("playlists.rename", {"id": playlist["id"], "name": "Current"},
                    expected_revision=playlist["revision"])
        with self.assertRaises(DomainError) as rejected:
            app.execute("playlists.rename", {"id": playlist["id"], "name": "Stale overwrite"},
                        expected_revision=playlist["revision"])
        self.assertEqual(rejected.exception.code, "REVISION_CONFLICT")
        self.assertEqual(app.query("playlists.get", {"id": playlist["id"]})["playlist"]["name"], "Current")

    def test_creation_retry_after_restart_returns_receipt_without_duplicate(self):
        app, first, second = self.scanned_application()
        arguments = {"name": "One request", "track_ids": [first, second]}
        receipt = app.execute("playlists.create", arguments, idempotency_key="create-once")
        app.close()
        reopened = self.application()
        replay = reopened.execute("playlists.create", arguments, idempotency_key="create-once")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["receipt_id"], receipt["receipt_id"])
        self.assertEqual(len(reopened.query("playlists.list")["playlists"]), 1)
        with self.assertRaises(DomainError) as rejected:
            reopened.execute("playlists.create", {"name": "Changed request"}, idempotency_key="create-once")
        self.assertEqual(rejected.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_favorites_deduplicate_and_undo_restores_previous_selection(self):
        app, first, second = self.scanned_application()
        app.execute("favorites.set", {"track_ids": [first, first], "favorite": True})
        initial = app.query("favorites.list")
        self.assertEqual(initial["track_ids"], [first])
        changed = app.execute("favorites.replace", {"track_ids": [second, first, second]},
                              expected_revision=initial["revision"])
        self.assertEqual(app.query("favorites.list")["track_ids"], [second, first])
        app.execute("undo", {"receipt_id": changed["receipt_id"]})
        self.assertEqual(app.query("favorites.list")["track_ids"], [first])

    def test_card_only_references_keep_relative_paths_and_repeated_rows(self):
        app = self.application()
        relative = "xm5/music/Card additions/\u4e00\u9996\u6b4c.wav"
        created = app.execute("library.intake_card", {
            "name": "From player", "entries": [{"card_path": relative}, {"card_path": relative}]})["result"]
        self.assertEqual(created["count"], 2)
        self.assertEqual(created["track_ids"][0], created["track_ids"][1])
        self.assertNotEqual(created["items"][0]["occurrence_id"], created["items"][1]["occurrence_id"])
        app.close()
        reopened = self.application()
        playlist = reopened.query("playlists.get", {"id": created["id"]})["playlist"]
        track = reopened.query("tracks.get", {"id": playlist["track_ids"][0]})["track"]
        self.assertEqual(track["card_reference"], {"path": relative})
        self.assertTrue(track["unresolved"])
        self.assertEqual(track["locations"], [])
        self.assertEqual(playlist["track_ids"], created["track_ids"])

    def test_invalid_card_reference_rolls_back_whole_intake(self):
        app = self.application()
        for invalid in ("../escape.wav", "/absolute.wav", "xm5/../escape.wav", "C:/escape.wav", "xm5\\music\\escape.wav"):
            with self.subTest(path=invalid):
                with self.assertRaises(DomainError) as rejected:
                    app.execute("library.intake_card", {"name": "Reject", "entries": [
                        {"card_path": "xm5/music/Valid but unpublished.wav"}, {"card_path": invalid}]})
                self.assertEqual(rejected.exception.code, "INVALID_ARGUMENT")
                self.assertEqual(app.query("tracks.all")["tracks"], [])
                self.assertEqual(app.query("playlists.list")["playlists"], [])

    def test_relative_m3u_round_trip_keeps_repeats_and_reports_missing_entry(self):
        playlist = self.music / "Listen.m3u8"
        write_m3u8(playlist, [{"path": str(path), "title": path.stem} for path in
                             (self.second, self.first, self.second)], relative=True)
        text = playlist.read_text(encoding="utf-8")
        self.assertNotIn(str(self.music), text)
        with playlist.open("a", encoding="utf-8", newline="\n") as output:
            output.write("Missing from PC.wav\n")
        result = read_m3u(playlist, [str(self.first), str(self.second)])
        self.assertEqual(result.paths, [str(self.second), str(self.first), str(self.second)])
        self.assertEqual(result.missing, ["Missing from PC.wav"])
        self.assertEqual(result.outside, [])
        self.assertEqual(result.unsupported, [])


class ConfigWorkflowTests(WorkflowFixture):
    def test_settings_change_preserves_unknown_fields_and_source_bytes(self):
        path = self.root / "config.json"
        store = ConfigStore(path, app_root=self.root)
        store.update(lambda config: config.update({"future_extension": {"mode": "retain", "values": [1, 2]}}))
        source_id = store.register_source("music", self.music)
        store.set("language", "en")
        reopened = ConfigStore(path, app_root=self.root)
        self.assertEqual(reopened.snapshot()["future_extension"], {"mode": "retain", "values": [1, 2]})
        self.assertEqual(reopened.get("language"), "en")
        self.assertEqual(reopened.sources("music")[0]["id"], source_id)
        self.assertEqual(Path(reopened.source_roots("music")[0]).resolve(), self.music.resolve())
        for source, before in self.originals.items():
            self.assertEqual(source.read_bytes(), before)

    def test_malformed_existing_configuration_is_rejected_without_replacement(self):
        path = self.root / "config.json"
        malformed = b'{"schema": "xm5.application-config", "version": '
        path.write_bytes(malformed)
        with self.assertRaises(ConfigError):
            ConfigStore(path, app_root=self.root)
        self.assertEqual(path.read_bytes(), malformed)
        self.assertFalse(path.with_suffix(".json.bak").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
