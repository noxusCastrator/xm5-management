"""Optional native Windows GUI/backend/media/export acceptance with synthetic files.

Run with the installed runtime: ``desktop\\XM5Library.cmd script
tools/run_desktop_tests.py --gui``. No card, device, user profile or audio output
is used. Set XM5LIB_SMOKE_ARTIFACTS to retain window PNGs and a JSON receipt.
"""
from __future__ import annotations

import gc
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
import uuid
import wave


ROOT = Path(__file__).resolve().parents[1]


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def make_wav(path: Path, frequency: int) -> None:
    """Three seconds of distinct, quiet PCM; playback always selects mpv NULL."""
    frames = bytearray()
    for sample in range(3 * 44100):
        value = int(4000 * math.sin(2 * math.pi * frequency * sample / 44100))
        frames.extend(struct.pack("<hh", value, value))
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        output.writeframes(frames)


def process_is_running(pid: int) -> bool:
    """Inspect only a PID returned by this test's owned mpv session."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # PID no longer exists.
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258  # WAIT_TIMEOUT: the process has not exited.
    finally:
        kernel.CloseHandle(handle)


@unittest.skipUnless(sys.platform == "win32", "Native smoke requires Windows x64")
class NativeDesktopSmoke(unittest.TestCase):
    def setUp(self):
        required = ("python/python.exe", "mpv/mpv.exe", "ffmpeg/ffmpeg.exe", "runtime-manifest.json")
        if any(not (ROOT / "desktop/vendor" / name).is_file() for name in required):
            self.skipTest("Run python tools/setup_desktop.py before --gui")
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("Run --gui with desktop/XM5Library.cmd script")
        if QApplication.instance() is not None:
            self.skipTest("Smoke requires its own QApplication")

        self.temp = tempfile.TemporaryDirectory(prefix="xm5-native-smoke-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile"
        self.music = self.root / "Synthetic Music \u97f3\u4e50"
        self.music.mkdir()
        self.eq = self.root / "Synthetic EQ"
        self.eq.mkdir()
        self.destination = self.root / "Local Export"
        self.destination.mkdir()
        self.first = self.music / "first.wav"
        self.second = self.music / "second.wav"
        make_wav(self.first, 247)
        make_wav(self.second, 523)
        (self.music / "first.lrc").write_text("[00:00.00]Synthetic smoke lyric\n", encoding="utf-8")
        from PIL import Image
        Image.new("RGB", (48, 48), (36, 108, 156)).save(self.music / "cover.jpg")
        self.apo = "Preamp: -3 dB\nFilter 1: ON PK Fc 1000 Hz Gain -2 dB Q 1\n"
        (self.eq / "SmokePreset.txt").write_text(self.apo, encoding="utf-8")
        self.originals = {path: file_hash(path) for directory in (self.music, self.eq)
                          for path in directory.rglob("*") if path.is_file()}
        self.original_env = {key: value for key, value in os.environ.items()
                             if key.startswith("XM5LIB_") or key == "QT_QPA_PLATFORM"}
        artifact_value = os.environ.get("XM5LIB_SMOKE_ARTIFACTS")
        self.artifacts = Path(artifact_value).resolve() if artifact_value else None
        self.addCleanup(self.restore_environment)
        for key in self.original_env:
            os.environ.pop(key, None)
        os.environ.update(XM5LIB_HOME=str(self.profile),
                          XM5LIB_BACKEND_HOME=str(self.profile / "data/backend"),
                          XM5LIB_ALLOW_NULL_PLAYBACK="1", XM5LIB_MPV_AO="null")
        os.environ["QT_QPA_PLATFORM"] = "windows"
        from xm5lib.core.portable import ConfigStore
        config = ConfigStore(self.profile / "config.json", app_root=self.profile)
        config.set("language", "en")
        config.set("export_roots", [str(self.destination)])
        config.register_source("peq", self.eq)
        self.receipt = {"source": str(ROOT), "profile": "temporary synthetic profile",
                        "backend_pids": [], "screenshots": [], "passed": False}
        self.processes = []
        self.app = self.win = None
        self.addCleanup(self.close_gui)
        from xm5lib.ui.app import build
        self.app, self.win = build(autoscan=False)
        self.assertEqual(self.app.platformName(), "windows")
        self.win.show()
        self.wait_for(lambda: self.win.backend.ready and self.win.state.is_loaded, "GUI backend ready")
        self.remember_backend()
        self.assertTrue(self.win.isVisible())
        self.assertTrue(self.win.backend.owned)
        self.capture("01-ready")

    def restore_environment(self):
        for key in list(os.environ):
            if key.startswith("XM5LIB_") or key == "QT_QPA_PLATFORM":
                os.environ.pop(key, None)
        os.environ.update(self.original_env)

    def remember_backend(self):
        process = self.win.backend.proc
        if process is not None and process not in self.processes:
            self.processes.append(process)
            self.receipt["backend_pids"].append(process.pid)

    def wait_for(self, predicate, description, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        reason = self.win.backend.reason if self.win is not None else ""
        self.fail(f"Timed out waiting for {description}: {reason}")

    def query(self, name, arguments=None):
        response = self.win.backend.client.query(name, arguments)
        self.assertTrue(response["ok"], response.get("error"))
        return response["result"]

    def command(self, name, arguments=None, revision=None):
        response = self.win.backend.client.command(name, arguments,
            expected_revision=revision, idempotency_key="native-smoke-" + uuid.uuid4().hex)
        self.assertTrue(response["ok"], response.get("error"))
        result = response["result"]
        if "job_id" not in result:
            return result
        completed = {}
        def finished():
            completed.update(self.query("jobs.get", {"id": result["job_id"]}))
            return completed.get("status") in {"succeeded", "failed", "cancelled", "interrupted"}
        self.wait_for(finished, name + " job")
        self.assertEqual(completed["status"], "succeeded", completed.get("error"))
        return completed["result"]

    def capture(self, name):
        if self.artifacts is None:
            return
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.app.processEvents()
        target = self.artifacts / (name + ".png")
        self.assertTrue(self.win.grab().save(str(target)))
        self.receipt["screenshots"].append({"file": target.name, "sha256": file_hash(target)})

    def close_gui(self):
        if self.win is not None:
            self.remember_backend()
            try:
                page = self.win.pages["export"]
                if page.running():
                    page.cancel()
                    self.wait_for(lambda: not page.running(), "cancelled export", timeout=15)
                self.win.close()
            finally:
                if getattr(self.win, "gui_control", None) is not None:
                    self.win.gui_control.close()
                self.win.console.stop()
                self.win.player.shutdown()
                from xm5lib.ui.app import dispose
                dispose(self.app, self.win)
                self.win = None
                self.app.shutdown()
                self.app = None
                gc.collect()
        for process in self.processes:
            self.assertIsNotNone(process.poll(), "Owned backend survived GUI shutdown")
        self.receipt["backend_processes_exited"] = True
        if self.artifacts is not None:
            self.artifacts.mkdir(parents=True, exist_ok=True)
            (self.artifacts / "NATIVE-GUI-SMOKE.json").write_text(
                json.dumps(self.receipt, ensure_ascii=True, indent=2) + "\n", encoding="utf-8", newline="\n")

    def test_scan_playlist_favorites_peq_null_playback_and_verified_export(self):
        self.assertTrue(self.win.add_music_folder(str(self.music)))
        self.wait_for(lambda: self.win.backend.ready and not self.win.state.busy
                      and len(self.win.state.tracks) == 2, "registered music scan")
        self.remember_backend()
        tracks = self.query("tracks.search")["tracks"]
        self.assertEqual(len(tracks), 2)
        by_name = {Path(track["path"]).name: track["id"] for track in tracks}
        first, second = by_name["first.wav"], by_name["second.wav"]
        self.assertNotEqual(first, second)
        order = [second, first, second]
        created = self.command("playlists.create", {"name": "Smoke order", "track_ids": order})["result"]
        saved = self.query("playlists.get", {"id": created["id"]})["playlist"]
        self.assertEqual(saved["track_ids"], order)
        self.assertEqual(len({row["occurrence_id"] for row in saved["items"]}), 3)
        favorite_before = self.query("favorites.list")
        self.command("favorites.replace", {"track_ids": [first, first]}, favorite_before["revision"])
        self.assertEqual(self.query("favorites.list")["track_ids"], [first])
        self.command("presets.import", {"name": "SmokePreset", "text": self.apo})
        self.assertEqual([row["name"] for row in self.query("presets.list")["presets"]], ["SmokePreset"])
        self.receipt["playlist_order"] = order
        self.receipt["favorite_ids"] = [first]

        connected = self.command("playback.connect", {"test_null": True})
        self.assertTrue(connected["test_null"])
        try:
            self.command("playback.command", {"action": "play_tracks", "arguments": {"track_ids": [first]}})
            self.command("playback.command", {"action": "volume.set", "arguments": {"value": 37}})
            self.command("playback.command", {"action": "peq.set", "arguments": {"preset_name": "SmokePreset"}})
            playback = {}
            def progressed():
                playback.update(self.query("playback.status"))
                return (playback.get("position_ms") or 0) > 100 and playback["peq"]["configured"]
            self.wait_for(progressed, "silent mpv playback progress", timeout=12)
            self.assertTrue(playback["test_null"])
            self.assertEqual(playback["volume"], 37)
            self.assertEqual(playback["peq"]["sample_rate"], 44100)
            self.receipt["null_playback"] = {key: playback[key] for key in
                ("test_null", "position_ms", "duration_ms", "volume", "peq", "pid")}
        finally:
            self.command("playback.disconnect")
        self.assertFalse(process_is_running(connected["pid"]), "Owned NULL mpv survived disconnect")
        self.receipt["null_playback_process_exited"] = True

        self.win.state.load_all()
        self.wait_for(lambda: self.win.state.playlist(created["id"]) is not None
                      and self.win.state.preset("SmokePreset") is not None, "GUI library refresh")
        self.win.open_playlist(created["id"])
        self.capture("02-playlist")
        self.win.show_page("export")
        page = self.win.pages["export"]
        page.refresh_sources()
        page.parent_edit.setText(str(self.destination))
        from PySide6.QtCore import Qt
        for widget, identities in ((page.music_sources, None), (page.peq_sources, None),
                                   (page.lists, {created["id"]}), (page.presets, {"SmokePreset"})):
            for index in range(widget.count()):
                item = widget.item(index)
                item.setCheckState(Qt.Checked if identities is None or item.data(Qt.UserRole) in identities
                                   else Qt.Unchecked)
        page.auto_lyrics.setChecked(False)
        page.preview_btn.click()
        self.wait_for(lambda: page.plan is not None or (page.preview_btn.isEnabled()
                      and bool(page.details.toPlainText())), "GUI export preview")
        self.remember_backend()
        self.assertIsNotNone(page.plan, page.details.toPlainText())
        self.assertTrue(page.run_btn.isEnabled(), page.summary.text())
        self.assertEqual(page.plan["plan"]["summary"]["tracks"], 2)
        self.capture("03-export-preview")
        page.run_btn.click()
        self.wait_for(lambda: page.result_root is not None and not page.running(), "GUI export completion")
        self.assertEqual(Path(page.result_root), self.destination / "xm5")
        verification = self.command("cards.verify", {"root": str(self.destination)})
        self.assertTrue(verification["ok"], verification)
        container = self.destination / "xm5"
        for name in ("music", "EQ", "Struct"):
            self.assertTrue((container / name).is_dir(), name)
        manifest = json.loads((container / "Struct/library.json").read_text(encoding="utf-8"))
        playlist = next(row for row in manifest["playlists"] if row["id"] == created["id"])
        from xm5lib.core.inline_card.relative_playlists import read_playlist, FAVORITES
        exported_order = [row.card_path.as_posix() for row in read_playlist(self.destination, playlist["path"])]
        self.assertEqual(exported_order, [manifest["track_paths"][identity] for identity in order])
        self.assertEqual([row.card_path.as_posix() for row in read_playlist(self.destination, FAVORITES)],
                         [manifest["track_paths"][first]])
        self.assertTrue((container / "Struct/peq/SmokePreset.txt").is_file())
        self.assertFalse((container / "Struct/INCOMPLETE").exists())
        self.assertEqual({path: file_hash(path) for path in self.originals}, self.originals)
        self.capture("04-export-complete")
        self.receipt.update(passed=True, verification=verification, source_files_unchanged=True,
                            exported_playlist_entries=len(exported_order), qt_platform=self.app.platformName())


if __name__ == "__main__":
    unittest.main(verbosity=2)
