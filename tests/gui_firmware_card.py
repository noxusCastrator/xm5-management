"""Qt update-card acceptance with a fake service and temporary card directories.

Run with the bundled Python. No volume enumeration or device access occurs.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from xm5lib.ui import i18n
from xm5lib.ui.firmware_card_dialog import FirmwareCardDialog, blocked_reason, text


class FakeFirmwareService:
    """Only this fake service may mutate its owned temporary directory."""

    def __init__(self, card: Path):
        self.card = card
        self.payload = b"synthetic update-card fixture\n"
        self.sha256 = hashlib.sha256(self.payload).hexdigest()
        self.info = {"available": True, "app_version": "0.2.2-fixture", "version": "V2.43-LIB01-fixture",
                     "sha256": self.sha256, "size": len(self.payload), "error": ""}
        self.calls = []
        self.plan_error = ""
        self.execute_error = ""
        self.plan_gate = None
        self.execute_gate = None

    def _record(self, name, **args):
        self.calls.append({"method": name, "thread": threading.get_ident(), **args})

    def firmware_info(self):
        self._record("firmware_info")
        return dict(self.info)

    def list_targets(self):
        self._record("list_targets")
        return [{"id": "fixture-volume", "root": str(self.card), "label": "Sandbox card",
                 "capacity": 32_000_000_000, "free": 24_000_000_000, "filesystem": "FAT32"}]

    def plan(self, target_id):
        self._record("plan", target_id=target_id)
        if self.plan_gate is not None and not self.plan_gate.wait(5):
            raise RuntimeError("Test plan gate timed out")
        if self.plan_error:
            raise ValueError(self.plan_error)
        return {"token": "fixture-plan-token", "target_id": target_id, "root": str(self.card),
                "label": "Sandbox card", "capacity": 32_000_000_000,
                "existing_update": (self.card / "update.bin").exists(), "existing_size": 3,
                "firmware_version": self.info["version"], "firmware_sha256": self.sha256}

    def execute(self, token, *, confirmed=False, progress=None):
        self._record("execute", token=token, confirmed=confirmed)
        if ((self.card / "update.bin").exists() and not confirmed) or token != "fixture-plan-token":
            raise ValueError("Confirmation and exact plan token required")
        if self.execute_gate is not None and not self.execute_gate.wait(5):
            raise RuntimeError("Test execute gate timed out")
        if progress:
            progress({"phase": "copy", "completed": 0, "total": 1})
        if self.execute_error:
            raise OSError(self.execute_error)
        if progress:
            progress({"phase": "copy", "completed": 0, "total": len(self.payload)})
        (self.card / "update.bin").write_bytes(self.payload)
        if progress:
            progress({"phase": "verify", "completed": len(self.payload), "total": len(self.payload)})
        return {"root": str(self.card), "sha256": self.sha256, "size": len(self.payload), "status": "complete"}


class Controller(QWidget):
    def __init__(self):
        super().__init__()
        self.state = SimpleNamespace(busy="", job=None)
        self.player = SimpleNamespace(state="stopped")
        self.export_running = False
        self.pages = {"export": SimpleNamespace(running=lambda: self.export_running)}
        self.shown_pages = []

    def show_page(self, page):
        self.shown_pages.append(page)


class FirmwareCardWidgets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        i18n.set_language("en")
        self.temp = tempfile.TemporaryDirectory(prefix="xm5-firmware-widgets-")
        self.card = Path(self.temp.name) / "Sandbox card"
        self.card.mkdir()
        self.sentinel = self.card / "keep until confirmed.txt"
        self.sentinel.write_bytes(b"back up this fixture")
        (self.card / "update.bin").write_bytes(b"old")
        self.service = FakeFirmwareService(self.card)
        self.ctl = Controller()
        self.ctl.show()
        self.dialog = None
        self.async_errors = []

    def tearDown(self):
        for gate in (self.service.plan_gate, self.service.execute_gate):
            if gate is not None:
                gate.set()
        if self.dialog is not None:
            if self.dialog._confirmation is not None:
                self.dialog._confirmation.reject()
            self.wait_for(lambda: not self.dialog.running())
            self.dialog.close()
            self.dialog.deleteLater()
        self.ctl.close()
        self.ctl.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()
        i18n.set_language("zh")

    def wait_for(self, predicate, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if self.async_errors:
                raise self.async_errors.pop(0)
            if predicate():
                return
            QTest.qWait(10)
        self.fail("Timed out waiting for Qt firmware operation")

    def open_dialog(self):
        self.dialog = FirmwareCardDialog(self.ctl, service=self.service)
        self.dialog.show()
        self.wait_for(lambda: self.dialog._info and not self.dialog.running())
        return self.dialog

    def confirm_with(self, action):
        def poll():
            confirmation = self.dialog._confirmation
            if confirmation is None:
                QTimer.singleShot(10, poll)
                return
            try:
                action(confirmation)
            except BaseException as exc:
                self.async_errors.append(exc)
                confirmation.reject()
        QTimer.singleShot(10, poll)

    def start_plan(self):
        self.dialog.targets.setCurrentIndex(1)
        QTest.mouseClick(self.dialog.create_button, Qt.LeftButton)

    def executed(self):
        return [c for c in self.service.calls if c["method"] == "execute"]

    def test_no_default_drive_and_selection_does_not_plan_or_write(self):
        dialog = self.open_dialog()
        self.assertIsNone(dialog.targets.currentData())
        self.assertFalse(dialog.create_button.isEnabled())
        self.assertEqual(dialog.app_version.text(), "0.2.2-fixture")
        self.assertEqual(dialog.firmware_version.text(), "V2.43-LIB01-fixture")
        self.assertIn("Sandbox card", dialog.targets.itemText(1))
        self.assertIn("32.00 GB", dialog.targets.itemText(1))
        dialog.targets.setCurrentIndex(1)
        self.app.processEvents()
        self.assertTrue(dialog.create_button.isEnabled())
        self.assertIn("24.00 GB", dialog.drive_info.text())
        self.assertFalse(any(c["method"] == "plan" for c in self.service.calls))
        self.assertFalse(self.executed())
        self.assertTrue(self.sentinel.exists())

    def test_cancel_is_default_and_keeps_card_unchanged(self):
        self.open_dialog()

        def cancel(box):
            self.assertEqual(len(box.buttons.buttons()), 2)
            self.assertTrue(box.cancel.isDefault())
            self.assertFalse(box.confirm.isDefault())
            self.assertIn("Replace it?", box.warning.text())
            self.assertIn(str(self.card), box.warning.text())
            self.assertIn("Other files will be preserved", box.warning.text())
            self.assertIn('href="export"', box.backup.text())
            QTest.mouseClick(box.cancel, Qt.LeftButton)

        self.confirm_with(cancel)
        self.start_plan()
        self.wait_for(lambda: "Cancelled." in self.dialog.status.text())
        self.assertFalse(self.executed())
        self.assertEqual(self.sentinel.read_bytes(), b"back up this fixture")

    def test_confirm_calls_worker_service_and_shows_copied_hash(self):
        self.open_dialog()
        self.confirm_with(lambda box: QTest.mouseClick(box.confirm, Qt.LeftButton))
        self.start_plan()
        self.wait_for(lambda: self.dialog._complete)
        self.assertEqual(self.executed()[0]["token"], "fixture-plan-token")
        self.assertTrue(self.executed()[0]["confirmed"])
        self.assertEqual({p.name for p in self.card.iterdir()}, {"update.bin", self.sentinel.name})
        self.assertEqual(hashlib.sha256((self.card / "update.bin").read_bytes()).hexdigest(), self.service.sha256)
        self.assertIn(self.service.sha256, self.dialog.status.text())
        self.assertIn("official XM5 update procedure", self.dialog.status.text())
        self.assertFalse(self.dialog.create_button.isEnabled())
        self.assertTrue(all(c["thread"] != threading.get_ident() for c in self.service.calls))

    def test_missing_update_copies_without_replacement_dialog(self):
        (self.card / "update.bin").unlink()
        self.open_dialog()
        self.start_plan()
        self.wait_for(lambda: self.dialog._complete)
        self.assertIsNone(self.dialog._confirmation)
        self.assertFalse(self.executed()[0]["confirmed"])
        self.assertEqual(self.sentinel.read_bytes(), b"back up this fixture")

    def test_enter_uses_cancel_and_confirmation_rejects_second_operation(self):
        self.open_dialog()

        def cancel(box):
            self.assertFalse(self.dialog.create_button.isEnabled())
            self.dialog.create_card()
            self.dialog.refresh_targets()
            QTest.keyClick(box, Qt.Key_Return)

        self.confirm_with(cancel)
        self.start_plan()
        self.wait_for(lambda: "Cancelled." in self.dialog.status.text())
        self.assertEqual(len([c for c in self.service.calls if c["method"] == "plan"]), 1)
        self.assertFalse(self.executed())
        self.assertTrue(self.sentinel.exists())

    def test_firmware_missing_disables_execution_and_shows_reason(self):
        self.service.info.update(available=False, error="Fixture package missing")
        self.open_dialog()
        self.dialog.targets.setCurrentIndex(1)
        self.assertFalse(self.dialog.create_button.isEnabled())
        self.assertIn("Fixture package missing", self.dialog.status.text())
        self.dialog.create_card()
        self.assertFalse(self.executed())
        self.assertFalse(any(c["method"] == "plan" for c in self.service.calls))

    def test_active_scan_export_and_playback_block_plan(self):
        self.open_dialog()
        self.dialog.targets.setCurrentIndex(1)
        for obj, attr, value in ((self.ctl.state, "busy", "scan"), (self.ctl.state, "job", object()),
                                 (self.ctl, "export_running", True), (self.ctl.player, "state", "playing"),
                                 (self.ctl.player, "state", "paused")):
            previous = getattr(obj, attr)
            setattr(obj, attr, value)
            self.dialog._update_controls()
            self.assertFalse(self.dialog.create_button.isEnabled())
            self.dialog.create_card()
            self.assertEqual(self.dialog.status.text(), blocked_reason(self.ctl))
            setattr(obj, attr, previous)
        self.assertFalse(any(c["method"] == "plan" for c in self.service.calls))
        self.assertTrue(self.sentinel.exists())

    def test_export_started_after_plan_cannot_execute(self):
        self.open_dialog()

        def changed(box):
            self.ctl.export_running = True
            box.accept()  # Even a programmatic acceptance must recheck ownership.

        self.confirm_with(changed)
        self.start_plan()
        self.wait_for(lambda: "TF card job" in self.dialog.status.text())
        self.assertFalse(self.executed())
        self.assertTrue(self.sentinel.exists())

    def test_backup_link_opens_export_without_writing(self):
        self.open_dialog()
        self.confirm_with(lambda box: box.backup.linkActivated.emit("export"))
        self.start_plan()
        self.wait_for(lambda: self.ctl.shown_pages == ["export"])
        self.assertFalse(self.dialog.isVisible())
        self.assertFalse(self.executed())
        self.assertTrue(self.sentinel.exists())

    def test_planning_keeps_event_loop_alive_and_rejects_duplicate_operation(self):
        self.service.plan_gate = threading.Event()
        self.open_dialog()
        self.start_plan()
        self.wait_for(lambda: any(c["method"] == "plan" for c in self.service.calls))
        ticked = []
        QTimer.singleShot(0, lambda: ticked.append(True))
        self.wait_for(lambda: ticked)
        self.assertFalse(self.dialog.refresh_button.isEnabled())
        self.dialog.create_card()
        self.dialog.refresh_targets()
        self.assertEqual(len([c for c in self.service.calls if c["method"] == "plan"]), 1)
        self.confirm_with(lambda box: box.reject())
        self.service.plan_gate.set()
        self.wait_for(lambda: "Cancelled." in self.dialog.status.text())

    def test_writing_blocks_dialog_and_parent_close_until_complete(self):
        self.service.execute_gate = threading.Event()
        self.open_dialog()
        self.confirm_with(lambda box: box.accept())
        self.start_plan()
        self.wait_for(lambda: bool(self.executed()))
        self.assertFalse(self.dialog.close_button.isEnabled())
        self.dialog.reject()
        self.dialog.close()
        self.ctl.close()
        self.assertTrue(self.dialog.isVisible())
        self.assertTrue(self.ctl.isVisible())
        self.service.execute_gate.set()
        self.wait_for(lambda: self.dialog._complete)
        self.assertTrue(self.dialog.close_button.isEnabled())
        self.dialog.close()
        self.assertFalse(self.dialog.isVisible())

    def test_plan_failure_never_calls_execute(self):
        self.service.plan_error = "Fixture volume changed"
        self.open_dialog()
        self.start_plan()
        self.wait_for(lambda: "Fixture volume changed" in self.dialog.status.text())
        self.assertIn("has not started", self.dialog.status.text())
        self.assertFalse(self.executed())
        self.assertTrue(self.sentinel.exists())

    def test_write_failure_reports_error_without_claiming_drive_cleared(self):
        self.service.execute_error = "Fixture write failed"
        self.open_dialog()
        self.confirm_with(lambda box: box.accept())
        self.start_plan()
        self.wait_for(lambda: "Other files are not cleared" in self.dialog.status.text())
        self.assertIn("Fixture write failed", self.dialog.status.text())
        self.assertFalse(self.dialog._complete)
        self.assertNotIn("rollback", self.dialog.status.text().lower())

    def test_chinese_dialog_and_warning_use_local_translation(self):
        i18n.set_language("zh")
        self.open_dialog()
        self.assertEqual(self.dialog.windowTitle(), "制作更新盘")
        self.assertEqual(text("section"), "播放器固件")

        def cancel(box):
            self.assertIn("是否替换", box.warning.text())
            self.assertIn("其他文件将保留", box.warning.text())
            self.assertEqual(box.cancel.text(), "取消")
            box.reject()

        self.confirm_with(cancel)
        self.start_plan()
        self.wait_for(lambda: "已取消" in self.dialog.status.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
