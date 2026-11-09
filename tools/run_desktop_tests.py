"""Run the public desktop regression suite from an ordinary source checkout."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
for directory in (ROOT, ROOT / "desktop", ROOT / "tools"):
    sys.path.insert(0, str(directory))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="also run the native Qt/backend/export smoke test")
    args = parser.parse_args()
    suite = unittest.TestLoader().discover(str(ROOT / "tests"), pattern="test_*.py")
    if args.gui:
        sys.path.insert(0, str(ROOT / "tests"))
        for module in ("gui_desktop_smoke", "gui_firmware_card", "gui_profile_import"):
            suite.addTests(unittest.TestLoader().loadTestsFromName(module))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
