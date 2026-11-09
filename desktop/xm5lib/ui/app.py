"""GUI entry point using application-root config/data/cache/runtime locations.

XM5LIB_HOME overrides the portable root for tests. Language is loaded before page
modules so the existing translated labels remain coherent when the window builds.
"""
from __future__ import annotations

import argparse
import gc
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QProcess, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QMessageBox

from .. import APP_NAME
from ..core.portable import ConfigError, PortablePaths, migrate_previous_profile
from . import i18n, style
from .prefs import Prefs

APP = Path(__file__).resolve().parents[2]



class QuietWheelStyle(QProxyStyle):
    """Fusion, except that a drop-down never changes its value under the mouse wheel (the tag panel is
    mostly drop-downs on a page that scrolls)."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ComboBox_AllowWheelScrolling:
            return 0
        return super().styleHint(hint, option, widget, returnData)

def gui_home() -> Path:
    return PortablePaths.discover(APP).data


def build(argv=None, autoscan: bool = False):
    """The window plus its backend: the backend is started (or attached to) in the background."""
    app = QApplication.instance() or QApplication([sys.argv[0]] + list(argv or []))
    app.setApplicationName(APP_NAME)
    icon = APP / "xm5lib" / "ui" / "assets" / "studio" / "app.ico"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    app.setStyle(QuietWheelStyle("Fusion"))           # drop-downs ignore the mouse wheel: the page scrolls
    app.setStyleSheet(style.QSS)
    locations = PortablePaths.discover(APP)
    from ..core.installation import acquire_program_activity
    acquire_program_activity()
    migrate_previous_profile(locations)
    locations.ensure()
    home = locations.data
    legacy = home / "gui-settings.json"
    # Earlier isolated test/portable profiles used the override root itself.
    if not legacy.exists() and os.environ.get("XM5LIB_HOME"):
        legacy = locations.root / "gui-settings.json"
    fresh = not locations.config.exists() and not legacy.exists()
    prefs = Prefs(locations.config, seed=legacy, app_root=locations.root)
    if fresh and locations.initial_language:
        prefs.set("language", locations.initial_language)
    i18n.set_language(prefs.get("language") or "zh")
    from .main_window import MainWindow                 # after the language is known
    from .service import Backend, backend_home
    from .state import LibraryState
    backend = Backend(backend_home(), locations.runtime / "logs")
    state = LibraryState(backend, locations.runtime / "import-staging", roots=lambda: list(prefs.get("music_roots") or []),
                         ignore_artist=lambda: bool(prefs.get("albums_ignore_artist")))
    win = MainWindow(home, prefs, backend, state)
    if icon.is_file():
        win.setWindowIcon(QIcon(str(icon)))
    # These service objects have GUI-thread affinity and share the window's
    # lifetime. Python callback cycles must not decide their C++ delete order.
    backend.setParent(win)
    state.setParent(win)
    backend._bridge.setParent(backend)
    from .lyrics_dialog import install_lyrics
    install_lyrics(win)
    prefs.on_error = win.error
    win.autoscan = autoscan
    win.first_run_pending = fresh
    return app, win


def dispose(app, win) -> None:
    """Drain owned workers and delete widgets while QApplication is still alive.

    A closed QWidget is hidden, not deleted. Letting cyclic Python wrappers
    collect after QApplication teardown can access already-freed Qt objects.
    In-flight HTTP replies also must not reach pages during their deletion.
    """
    backend = win.backend
    backend.shutdown()
    for lane in backend._lanes.values():
        if lane is not threading.current_thread():
            lane.join(timeout=12)
    win.state.blockSignals(True)
    backend.blockSignals(True)
    bridge = backend._bridge
    bridge.blockSignals(True)
    QCoreApplication.removePostedEvents(bridge, QEvent.MetaCall)
    win.deleteLater()
    QCoreApplication.sendPostedEvents(win, QEvent.DeferredDelete)


def restart() -> bool:
    """Start the program again the way it was started (after the language changed)."""
    ok = QProcess.startDetached(sys.executable, ["-B", str(Path(sys.argv[0]).resolve())] + sys.argv[1:])
    return bool(ok[0] if isinstance(ok, tuple) else ok)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument("--no-autoscan", action="store_true", help="do not scan on first start")
    args, _ = ap.parse_known_args(argv)
    try:
        app, win = build(autoscan=not args.no_autoscan)
    except (ConfigError, OSError) as exc:
        app = QApplication.instance() or QApplication([sys.argv[0]])
        QMessageBox.critical(None, APP_NAME, str(exc))
        return 1
    win.show()
    if win.first_run_pending:
        from .first_run import show_first_run
        QTimer.singleShot(0, lambda: show_first_run(win))
    try:
        return app.exec()
    finally:
        dispose(app, win)
        del win
        # PySide's supported shutdown hook clears qApp and destroys the native
        # singleton before CPython's final cyclic-GC/module teardown begins.
        app.shutdown()
        gc.collect()
