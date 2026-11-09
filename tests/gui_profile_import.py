"""Optional import widgets and prelaunch ordering, with disposable inputs only."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from xm5lib.core.portable import ConfigError, PortablePaths
from xm5lib.ui import i18n, profile_import


class ProfileImportWidgets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        i18n.set_language("en")
        self.temp = tempfile.TemporaryDirectory(prefix="xm5-import-widgets-")
        self.root = Path(self.temp.name)
        self.target = self.root / "Installed profile"
        self.dialog = None
        self.gate = None

    def tearDown(self):
        if self.gate is not None:
            self.gate.set()
        if self.dialog is not None:
            self.wait_for(lambda: self.dialog.task is None)
            self.dialog.close()
            self.dialog.deleteLater()
            self.app.processEvents()
        self.temp.cleanup()
        i18n.set_language("zh")

    def wait_for(self, predicate):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail("Timed out waiting for import widget")

    def open_dialog(self, importer=lambda *_: None):
        self.dialog = profile_import.ProfileImportDialog(self.target, importer=importer)
        self.dialog.show()
        return self.dialog

    def test_continue_fresh_is_explicit_and_does_not_create_profile(self):
        dialog = self.open_dialog()
        self.assertTrue(dialog.fresh.isDefault())
        self.assertFalse(dialog.copy.isEnabled())
        QTest.mouseClick(dialog.fresh, Qt.LeftButton)
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertFalse(self.target.exists())

    def test_cancel_leaves_destination_absent(self):
        dialog = self.open_dialog()
        QTest.mouseClick(dialog.cancel, Qt.LeftButton)
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertFalse(self.target.exists())

    def test_import_runs_off_gui_thread_and_cannot_close_while_copying(self):
        self.gate = threading.Event()
        calls = []

        def importer(source, destination):
            calls.append((source, destination, threading.get_ident()))
            if not self.gate.wait(5):
                raise RuntimeError("Synthetic import timed out")
            destination.mkdir()

        dialog = self.open_dialog(importer)
        dialog.source.setText(str(self.root / "Selected portable"))
        QTest.mouseClick(dialog.copy, Qt.LeftButton)
        self.wait_for(lambda: bool(calls))
        ticked = []
        QTimer.singleShot(0, lambda: ticked.append(True))
        self.wait_for(lambda: ticked)
        self.assertNotEqual(calls[0][2], threading.get_ident())
        self.assertFalse(dialog.fresh.isEnabled())
        self.assertFalse(dialog.cancel.isEnabled())
        dialog.reject()
        dialog.close()
        self.assertTrue(dialog.isVisible())
        self.gate.set()
        self.wait_for(lambda: dialog.result() == QDialog.Accepted)
        self.assertTrue(self.target.is_dir())

    def test_failure_stays_visible_until_explicit_fresh_or_cancel(self):
        def failed(*_):
            raise ConfigError("Synthetic active portable profile")

        dialog = self.open_dialog(failed)
        dialog.source.setText(str(self.root / "Selected portable"))
        dialog.copy.click()
        self.wait_for(lambda: dialog.task is None)
        self.assertTrue(dialog.isVisible())
        self.assertIn("Synthetic active portable profile", dialog.status.text())
        self.assertIn("not created", dialog.status.text())
        self.assertFalse(self.target.exists())
        self.assertTrue(dialog.fresh.isEnabled())
        dialog.fresh.click()
        self.assertEqual(dialog.result(), QDialog.Accepted)

    def test_only_selected_browse_path_is_used(self):
        dialog = self.open_dialog()
        source = str(self.root / "Explicitly selected portable")
        with patch.object(profile_import.QFileDialog, "getExistingDirectory", return_value=source):
            dialog.browse.click()
        self.assertEqual(dialog.source.text(), source)
        self.assertTrue(dialog.copy.isEnabled())

    def test_chinese_labels(self):
        i18n.set_language("zh")
        dialog = self.open_dialog()
        self.assertEqual(dialog.copy.text(), "导入资料")
        self.assertEqual(dialog.fresh.text(), "使用全新资料")
        self.assertIn("便携版应用", dialog.status.text())

    def test_portable_and_existing_installed_profiles_do_not_prompt(self):
        with patch.object(profile_import, "ProfileImportDialog") as dialog:
            self.assertTrue(profile_import.choose_installed_profile(PortablePaths(self.target)))
            self.target.mkdir()
            (self.target / "config.json").write_text("synthetic existing profile", encoding="utf-8")
            self.assertTrue(profile_import.choose_installed_profile(PortablePaths(self.target, "installed")))
            dialog.assert_not_called()

    def test_main_acquires_activity_guard_and_prompts_before_build(self):
        from xm5lib.ui import app as entry
        paths = PortablePaths(self.target, "installed", "en")
        order = []
        with patch("xm5lib.core.installation.acquire_program_activity", side_effect=lambda: order.append("mutex")), \
                patch.object(entry.PortablePaths, "discover", return_value=paths), \
                patch.object(profile_import, "choose_installed_profile", side_effect=lambda _: order.append("prompt") or False), \
                patch.object(entry, "build") as build:
            self.assertEqual(entry.main(["--no-autoscan"]), 0)
            build.assert_not_called()
        self.assertEqual(order, ["mutex", "prompt"])
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
