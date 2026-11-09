"""Installed and portable paths, using synthetic profiles and no live sessions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
from xm5lib.core.portable import ConfigError, ConfigStore, PortablePaths


class DistributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xm5-mode-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.app = self.base / "program 空间"
        self.app.mkdir()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "local appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        for key in ("XM5LIB_HOME", "XM5LIB_BACKEND_HOME"):
            os.environ.pop(key, None)

    def descriptor(self, **extra):
        data = {"schema": "xm5.distribution.v1", "mode": "installed"} | extra
        (self.app / "distribution.json").write_text(json.dumps(data), encoding="utf-8")

    def test_source_checkout_and_explicit_portable_remain_adjacent(self):
        self.assertEqual(PortablePaths.discover(self.app).root, self.app)
        self.descriptor(mode="portable")
        self.assertEqual(PortablePaths.discover(self.app).root, self.app)
        self.assertFalse((self.app / "config.json").exists())

    def test_installed_uses_separate_localappdata_after_program_move(self):
        self.descriptor(initial_language="en")
        before = PortablePaths.discover(self.app)
        moved = self.base / "moved program"
        self.app.rename(moved)
        after = PortablePaths.discover(moved)
        self.assertEqual(before, after)
        self.assertEqual(after.root, self.base / "local appdata/XM5Studio")
        self.assertEqual(after.backend, after.root / "data/backend")
        self.assertEqual(after.initial_language, "en")
        self.assertFalse(after.root.exists())

    def test_environment_overrides_keep_script_semantics(self):
        self.descriptor()
        os.environ["XM5LIB_HOME"] = "selected profile"
        os.environ["XM5LIB_BACKEND_HOME"] = "custom backend"
        paths = PortablePaths.discover(self.app)
        self.assertEqual(paths.root, self.app / "selected profile")
        self.assertEqual(paths.backend, paths.root / "custom backend")

    def test_bad_mode_never_silently_uses_an_adjacent_profile(self):
        for extra in ({"mode": "other"}, {"schema": "future"}, {"initial_language": "bad"}):
            with self.subTest(extra=extra):
                self.descriptor(**extra)
                with self.assertRaises(ConfigError):
                    PortablePaths.discover(self.app)
        self.assertFalse((self.app / "config.json").exists())

    def test_corrupt_descriptor_refuses(self):
        (self.app / "distribution.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ConfigError):
            PortablePaths.discover(self.app)

    def test_installed_refuses_relative_appdata(self):
        self.descriptor()
        os.environ["LOCALAPPDATA"] = "relative"
        with self.assertRaises(ConfigError):
            PortablePaths.discover(self.app)

    def test_portable_and_installed_configs_are_independent(self):
        portable = ConfigStore(self.app / "config.json")
        portable.set("language", "zh")
        self.descriptor(initial_language="en")
        installed = PortablePaths.discover(self.app)
        ConfigStore(installed.config).set("language", "en")
        self.assertEqual(portable.get("language"), "zh")
        self.assertEqual(ConfigStore(installed.config).get("language"), "en")

    def test_backend_logs_and_cli_discovery_share_the_profile(self):
        from xm5lib.automation import runtime
        from xm5lib.control.client import GuiClient
        from xm5lib.core import paths as legacy
        os.environ["XM5LIB_HOME"] = str(self.base / "explicit script profile")
        paths = PortablePaths.discover(self.app)
        self.assertEqual(runtime.default_home(), paths.backend)
        self.assertEqual(runtime.runtime_path(), paths.runtime / "backend")
        self.assertEqual(legacy.app_home(), paths.data)
        with patch("xm5lib.automation.runtime.check_private") as check:
            with self.assertRaisesRegex(RuntimeError, "NO_OPEN_GUI"):
                GuiClient()
        self.assertEqual(check.call_args.args[0], paths.runtime / "gui-control/session.json")
        self.assertFalse(paths.root.exists())


if __name__ == "__main__":
    unittest.main()
