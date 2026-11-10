"""Public storage regressions. All mutable state lives in disposable directories.

Run with the approved desktop interpreter, -B, from any working directory.
No hardware, Qt window, network, encoder, personal music or production edit.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO / "desktop"))

from xm5lib.core.inline_card import delta, export_card, library, relative_playlists
from xm5lib.automation.engine import Engine
from xm5lib.automation.inline_library_actions import InlineLibraryActions
from xm5lib.application import DomainError


class PowerCut(BaseException):
    """Escape ordinary exception handlers like process termination."""


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(root):
    return {p.relative_to(root).as_posix(): {"bytes": p.stat().st_size, "sha256": digest(p)}
            for p in root.rglob("*") if p.is_file()}


def wav(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack("<64h", *([value] * 64)))


class Fixture:
    def __init__(self, root):
        self.root = root
        self.music = root / "Music"
        self.music.mkdir()
        self.card = root / "synthetic-card"
        self.card.mkdir()
        self.source = self.music / "ordinary.txt"
        self.source.write_bytes(b"original ordinary file\n")
        self.original = self.source.read_bytes()
        self.options = library.gain_options(enabled=False)
        library.execute(self.plan())
        self.relative = "xm5/music/Music/ordinary.txt"
        self.active = self.card / self.relative
        self.before = snapshot(self.card / "xm5")

    def plan(self):
        return library.make_plan(self.card, [self.music], [], options=self.options,
                                 existing="update" if (self.card / "xm5").exists() else "refuse")

    def changed_plan(self):
        self.source.write_bytes(b"replacement ordinary bytes\n")
        plan = self.plan()
        delta.annotate(plan)
        return plan

    def interrupted(self):
        plan = self.changed_plan()
        original = delta._move

        def cut_after_backup(root, source, destination):
            result = original(root, source, destination)
            if source == self.relative:
                raise PowerCut()
            return result

        with patch.object(delta, "_move", cut_after_backup):
            try:
                library.execute(plan)
            except PowerCut:
                pass
        return delta.transactions(self.card)[0]["transaction_id"]


class StorageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="unit-")
        self.addCleanup(self.directory.cleanup)
        self.fixture = Fixture(Path(self.directory.name))

    def test_export_and_delta_preserve_source_bytes(self):
        f = self.fixture
        plan = f.changed_plan()
        expected = snapshot(f.music)
        result = library.execute(plan)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(snapshot(f.music), expected)
        self.assertEqual(f.active.read_bytes(), f.source.read_bytes())

    def test_source_changed_after_plan_is_rejected_without_card_changes(self):
        f = self.fixture
        plan = f.changed_plan()
        before = snapshot(f.card / "xm5")
        f.source.write_bytes(b"user changes source after plan\n")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            library.execute(plan)
        self.assertEqual(snapshot(f.card / "xm5"), before)
        self.assertEqual(f.source.read_bytes(), b"user changes source after plan\n")

    def test_changed_owned_file_is_preserved(self):
        f = self.fixture
        plan = f.changed_plan()
        f.active.write_bytes(b"user card edit\n")
        with self.assertRaisesRegex(ValueError, "changed|preserved"):
            library.execute(plan)
        self.assertEqual(f.active.read_bytes(), b"user card edit\n")

    def test_unowned_file_is_never_claimed_or_removed(self):
        f = self.fixture
        user = f.card / "xm5/music/card-only.txt"
        user.write_bytes(b"user-added file\n")
        library.execute(f.changed_plan())
        self.assertEqual(user.read_bytes(), b"user-added file\n")
        self.assertNotIn("xm5/music/card-only.txt", library.load(f.card)[1]["owned"])

    def test_copy_interruption_keeps_previous_active_bytes(self):
        f = self.fixture
        plan = f.changed_plan()
        original = export_card._copy_file

        def cut_after_copy(*args, **kwargs):
            original(*args, **kwargs)
            raise PowerCut()

        with patch.object(export_card, "_copy_file", cut_after_copy):
            with self.assertRaises(PowerCut):
                library.execute(plan)
        self.assertEqual(snapshot(f.card / "xm5"), f.before)
        self.assertFalse((f.card / delta.POINTER).exists())

    def test_resume_does_not_need_source_and_replay_is_noop(self):
        f = self.fixture
        tx = f.interrupted()
        f.source.unlink()
        result = delta.recover(f.card, tx, action="resume")
        self.assertEqual(result["status"], "complete")
        before = snapshot(f.card)
        replay = delta.recover(f.card, tx, action="resume")
        self.assertEqual(replay["status"], "complete")
        self.assertEqual(snapshot(f.card), before)

    def test_rollback_does_not_need_source_and_replay_is_noop(self):
        f = self.fixture
        tx = f.interrupted()
        f.source.unlink()
        result = delta.recover(f.card, tx, action="rollback")
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(snapshot(f.card / "xm5"), f.before)
        before = snapshot(f.card)
        delta.recover(f.card, tx, action="rollback")
        self.assertEqual(snapshot(f.card), before)

    def test_external_active_edit_blocks_resume_and_rollback(self):
        f = self.fixture
        tx = f.interrupted()
        f.active.write_bytes(b"user recovery edit\n")
        for action in ("resume", "rollback"):
            result = delta.recover(f.card, tx, action=action)
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(f.active.read_bytes(), b"user recovery edit\n")

    def test_mutable_journal_cannot_replace_frozen_operations(self):
        f = self.fixture
        tx = f.interrupted()
        path = f.card / (".xm5-library-" + tx) / "journal.json"
        journal = json.loads(path.read_text("utf-8"))
        journal["operations"] = []
        path.write_text(json.dumps(journal), encoding="utf-8")
        before = snapshot(f.card)
        with self.assertRaisesRegex(ValueError, "Unexpected mutable"):
            delta.recover(f.card, tx, action="resume")
        self.assertEqual(snapshot(f.card), before)

    def test_success_retains_exact_replaced_preimage(self):
        f = self.fixture
        result = library.execute(f.changed_plan())
        backup = Path(result["backup_root"])
        self.assertEqual((backup / f.relative).read_bytes(), f.original)
        self.assertTrue(backup.exists())
        self.assertEqual(delta.transactions(f.card)[0]["backup_root"], str(backup))

    def test_corrupt_backup_blocks_rollback_without_removing_active_file(self):
        f = self.fixture
        plan = f.changed_plan()
        original = delta._move

        def cut_after_new_publish(root, source, destination):
            result = original(root, source, destination)
            if destination == f.relative:
                raise PowerCut()
            return result

        with patch.object(delta, "_move", cut_after_new_publish):
            with self.assertRaises(PowerCut):
                library.execute(plan)
        tx = delta.transactions(f.card)[0]["transaction_id"]
        backup = f.card / (".xm5-before-" + tx) / f.relative
        backup.write_bytes(b"external change to retained backup\n")
        active_before = f.active.read_bytes()
        result = delta.recover(f.card, tx, action="rollback")
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(f.active.exists(), "Blocked rollback must preserve the only active verified version")
        self.assertEqual(f.active.read_bytes(), active_before)


class PlaylistIndependentEditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="playlist-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.music = self.root / "Music"
        for name, value in (("first.wav", 1), ("second.wav", 2)):
            wav(self.music / name, value)
        self.card = self.root / "synthetic-card"
        self.card.mkdir()
        self.engine = Engine(self.root / "profile", music_roots=[self.music], export_roots=[self.card])
        self.addCleanup(self.engine.close)
        app = self.engine.application
        app.execute("library.scan", {"roots": [str(self.music)]})
        tracks = sorted(app.query("tracks.all")["tracks"], key=lambda x: x["path"])
        self.ids = [x["id"] for x in tracks]
        self.playlist = app.execute("playlists.create", {"name": "Initial", "track_ids": self.ids})["result"]
        plan = library.make_plan(self.card, [self.music], [], options=library.gain_options(enabled=False),
                                 selected_playlists=[self.playlist], track_paths={x["id"]: x["path"] for x in tracks})
        # Actual preparation uses WAV's deterministic container writer; no media process.
        library.execute(plan)
        manifest = library.load(self.card)[1]
        self.m3u = self.card / manifest["playlists"][0]["path"]
        self.card_paths = [manifest["track_paths"][tid] for tid in self.ids]
        self.actions = InlineLibraryActions(self.engine)

    def dispatch(self, name, extra=None):
        args = {"root": str(self.card), "m3u": str(self.m3u)}
        args.update(extra or {})
        return self.actions.dispatch(name, args, None, None,
                                     revision=self.current()["revision"] if name == "cards.import" else None)

    def edit_card(self, tracks, name):
        relative_playlists.write_playlist(self.card, self.m3u.relative_to(self.card).as_posix(), tracks)
        raw = self.m3u.read_text("utf-8")
        self.m3u.write_text(raw.replace("#EXTM3U\n", "#EXTM3U\n#PLAYLIST:" + name + "\n", 1),
                            encoding="utf-8", newline="\n")

    def current(self):
        return self.engine.application.query("playlists.get", {"id": self.playlist["id"]})["playlist"]

    def test_both_sides_track_edits_require_explicit_choice(self):
        self.engine.application.execute("playlists.replace", {"id": self.playlist["id"], "track_ids": [self.ids[0]]},
                                        expected_revision=self.current()["revision"])
        self.edit_card([self.card_paths[1]], "Initial")
        proposal = self.dispatch("cards.reconcile")
        self.assertEqual(proposal["status"], "conflict")
        before = self.current()
        with self.assertRaises(DomainError):
            self.dispatch("cards.import", {"input_sha256": proposal["input_sha256"]})
        self.assertEqual(self.current(), before)

    def test_pc_name_and_device_order_do_not_silently_lose_pc_name(self):
        self.engine.application.execute("playlists.rename", {"id": self.playlist["id"], "name": "PC edited name"},
                                        expected_revision=self.current()["revision"])
        self.edit_card(list(reversed(self.card_paths)), "Initial")
        proposal = self.dispatch("cards.reconcile")
        if proposal["conflict"]:
            return  # An explicit conflict is also a safe implementation.
        self.dispatch("cards.import", {"input_sha256": proposal["input_sha256"]})
        self.assertEqual(self.current()["name"], "PC edited name")
        self.assertEqual(self.current()["track_ids"], list(reversed(self.ids)))

    def test_pc_order_and_device_name_do_not_silently_lose_pc_order(self):
        self.engine.application.execute("playlists.replace", {"id": self.playlist["id"], "track_ids": list(reversed(self.ids))},
                                        expected_revision=self.current()["revision"])
        self.edit_card(self.card_paths, "Device edited name")
        proposal = self.dispatch("cards.reconcile")
        if proposal["conflict"]:
            return
        self.dispatch("cards.import", {"input_sha256": proposal["input_sha256"]})
        self.assertEqual(self.current()["track_ids"], list(reversed(self.ids)))
        self.assertEqual(self.current()["name"], "Device edited name")
