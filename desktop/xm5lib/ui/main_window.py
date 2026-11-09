"""Main window: navigation, search, inspector, and the controller methods pages call."""
from __future__ import annotations

import collections
import os
import subprocess
import threading
from pathlib import Path

import time

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractScrollArea, QApplication, QDialog, QFrame, QScrollArea, QFileDialog, QHBoxLayout, QLabel,
                               QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..core import covers, library, paths, playlists, winenv
from ..core.portable import PortablePaths
from . import catalog, hardware, style, tagfile, widgets
from .export_page import ExportPage
from .console_live import LiveConsole
from .inspector import Inspector
from .library_pages import (AlbumsPage, ArtistsPage, FavoritesPage, FolderPage, GenrePage, ResolutionPage,
                            TracksPage, rounded)
from .models import NOW_PLAYING
from .peq_page import PeqPage
from .player import MODE_NAMES, Player, format_ms, mpv_exe
from .playlist_page import PlaylistPage, PlaylistsPage
from .prefs import Prefs
from .service import Backend, BackendConfig
from .settings_page import SettingsPage
from .state import LibraryState
from .widgets import NavDelegate, SeekBar, StatusPlay
from .workers import Jobs
from .i18n import tr

# The XM5 library menu, in firmware order (smart lists -- frequent/recent/recently added -- are not mirrored).
NAV = [(tr("文件夹"), "folders", "home_folder"), (tr("专辑"), "albums", "home_album"), (tr("歌手"), "artists", "home_songer"),
       (tr("风格"), "genres", "home_genre"), (tr("解析度"), "resolution", "home_hr"), (tr("最爱"), "favorites", "home_fav"),
       (tr("所有歌曲"), "tracks", "home_allmusic"), (tr("歌单"), "playlists", "home_playlist")]
TOOLS = [(tr("PEQ 预设"), "peq", "home_playset"), (tr("TF 卡导出"), "export", "home_cdripper"), (tr("系统设置"), "settings", "home_sysset")]
LIBRARY_KEYS = {"folders", "albums", "artists", "genres", "resolution", "favorites", "tracks", "playlists", "playlist"}


def scroller(w: QWidget, width: int | None = None, min_h: int | None = None) -> QScrollArea:
    """Wrap a panel so it scrolls instead of squashing when the window is short."""
    area = QScrollArea()
    area.setWidget(w)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    if width:
        area.setFixedWidth(width)
    if min_h:
        w.setMinimumHeight(min_h)
    w.setAttribute(Qt.WA_StyledBackground, True)
    return area


def nav_list(entries) -> QListWidget:
    lst = QListWidget()
    lst.setObjectName("nav")
    lst.setItemDelegate(NavDelegate(lst))
    lst.setMouseTracking(True)
    lst.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    for label, key, icon in entries:
        it = QListWidgetItem(label)
        it.setData(Qt.UserRole, key)
        it.setData(Qt.DecorationRole, QPixmap(style.icon_path(icon)))
        lst.addItem(it)
    lst.setFixedHeight(len(entries) * 38 + 4)
    return lst


def player_mode_icon() -> QIcon:
    """The title-bar key for player mode: the XM5 in outline (square screen, the knob on its top edge)."""
    icon = QIcon()
    for colour, mode in (("#cfcfcf", QIcon.Normal), (style.ACCENT, QIcon.Active)):
        pix = QPixmap(28, 28)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(colour), 2.2)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(5, 6, 18, 20), 3, 3)
        p.drawRect(QRectF(8.5, 10, 11, 11))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(colour))
        p.drawRoundedRect(QRectF(15, 1.5, 6, 4), 1.5, 1.5)
        p.end()
        icon.addPixmap(pix, mode)
    return icon


class MainWindow(QMainWindow):
    """The XM5-shaped window. Library data and playback belong to the integration backend (``backend`` /
    ``state``); this window shows them and turns clicks into backend commands."""

    def __init__(self, home: Path, prefs: Prefs, backend: Backend, state: LibraryState, player: Player | None = None):
        super().__init__()
        self.home, self.prefs, self.backend, self.state = home, prefs, backend, state
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1500, 920)
        self.jobs = Jobs(self)
        self.query = ""
        self.pixmaps: dict[str, QPixmap] = {}
        self.cover_queue: collections.deque = collections.deque()
        self.cover_lock = threading.Lock()
        self.queued_covers: set[str] = set()
        self.messages: list[str] = []   # every error shown, for diagnostics/tests
        self.quiet = False              # tests: record errors instead of modal boxes
        self.last_dialog = None         # tests: the dialog a quiet window opened instead of exec()
        self._restart = False

        self.favorites = state.favorites
        widgets.COLUMN_STORE = prefs            # each track list keeps its own columns in the settings file
        theme = prefs.get("skin_theme") or "titanium"
        self.theme = style.LEGACY_THEME.get(theme, theme)
        self.last_error = ""
        # the player: the backend's own mpv when it has one, otherwise the local engines; the wheel on the
        # case is its volume knob
        if player is None:
            from .backend_player import make_player
            player = make_player(self)
        self.player = player
        self._vol_save = QTimer(self, singleShot=True, interval=800,
                                timeout=lambda: self.prefs.set("player_volume", self.player.volume))

        # frameless: the window *is* the player
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowIcon(QPixmap(style.icon_path("home_allmusic")))
        self.setMinimumSize(1040, 700)
        self.case = hardware.Case()
        self._case_parts()
        screen = QWidget()
        screen.setObjectName("screen")
        sl = QVBoxLayout(screen)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(self._status_strip())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        rail = self._sidebar()
        body.addWidget(rail)
        middle = QWidget()
        middle.setObjectName("page")
        ml = QVBoxLayout(middle)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.hide()
        bw = QWidget()
        bl = QVBoxLayout(bw)
        bl.setContentsMargins(24, 10, 24, 0)
        bl.addWidget(self.banner)
        ml.addWidget(bw)
        self.stack = QStackedWidget()
        self.stack.setObjectName("pages")
        ml.addWidget(self.stack, 1)
        body.addWidget(middle, 1)
        self.inspector = Inspector()
        self.inspector.save_requested.connect(self._save_tags)
        self.inspector.message.connect(self.toast)
        self.inspector_area = scroller(self.inspector, 300)
        body.addWidget(self.inspector_area)
        sl.addLayout(body, 1)
        sl.addWidget(self._now_bar())
        self._wire_player()
        self.glass = hardware.Glass()
        gl = QVBoxLayout(self.glass)
        gl.setContentsMargins(14, 14, 14, 14)
        gl.setSpacing(0)
        frame = QWidget()
        frame.setObjectName("screenFrame")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(1, 1, 1, 1)
        # the glass shows the library window, or in player mode the firmware's own screens (device/)
        self.screens = QStackedWidget()
        self.screens.addWidget(screen)
        fl.addWidget(self.screens)
        self.player_mode = False
        self.device = None
        self.device_frame = None          # player mode: the whole XM5 (device/frame.py) instead of the case
        self._normal_state = None
        frame.setStyleSheet("QWidget#screenFrame { background: #000; border: 1px solid #1d1d1d; border-radius: 3px; }")
        gl.addWidget(frame, 1)
        self.glare = hardware.Glare(self.glass)
        self.case.set_grooves(1)                       # the grooves option was removed (v17)
        self.case.set_parts(self.wheel, self.plate, self.glass)
        self._update_min_height()
        self._apply_margins()
        self.central = QStackedWidget()                # the case, or in player mode the XM5 itself
        self.central.setAttribute(Qt.WA_TranslucentBackground, True)
        self.central.addWidget(self.case)
        self.setCentralWidget(self.central)
        self.case.setContextMenuPolicy(Qt.CustomContextMenu)
        self.case.customContextMenuRequested.connect(
            lambda pos: self._more_menu(self.case.mapToGlobal(pos)))

        self.pages = {"folders": FolderPage(self), "albums": AlbumsPage(self), "artists": ArtistsPage(self),
                      "genres": GenrePage(self), "resolution": ResolutionPage(self), "favorites": FavoritesPage(self),
                      "tracks": TracksPage(self), "playlists": PlaylistsPage(self), "playlist": PlaylistPage(self),
                      "peq": PeqPage(self),
                      "export": ExportPage(self), "settings": SettingsPage(self)}
        for p in self.pages.values():
            # QWidget subclasses paint their stylesheet background only with this attribute;
            # without it the previous page shows through.
            p.setAttribute(Qt.WA_StyledBackground, True)
        self.slots = {}
        for key, p in self.pages.items():
            slot = scroller(p, min_h=660) if key in ("peq", "export", "settings") else p
            self.slots[key] = slot
            self.stack.addWidget(slot)
        self.inspector.setAttribute(Qt.WA_StyledBackground, True)
        st = self.state
        st.tracksChanged.connect(self.reload_library)
        st.playlistsChanged.connect(self.reload_playlists)
        st.favoritesChanged.connect(self._favorites_refresh)
        st.tagsWritten.connect(self._tags_written)
        st.notice.connect(self.toast)
        st.problem.connect(self.error)
        st.busyChanged.connect(self._busy_changed)
        st.jobProgress.connect(self._job_progress)
        st.loaded.connect(self._library_loaded)
        self.backend.stateChanged.connect(self._backend_state)
        self.backend.unhandled.connect(lambda e: self.toast_error(tr("后端：{0}", e.message)))
        QShortcut(QKeySequence.Undo, self, activated=self.state.undo)
        self.statusBar().showMessage("Ready")
        self.statusBar().hide()
        QShortcut(QKeySequence.Find, self, activated=self.open_search)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self._escape)
        QShortcut(QKeySequence(Qt.Key_F5), self, activated=self.start_scan)
        QShortcut(QKeySequence(Qt.Key_Backspace), self, activated=self._back)
        self._clock = QTimer(self, interval=15000, timeout=self._tick_clock)
        self._clock.start()
        self._toast_timer = QTimer(self, singleShot=True, interval=7000, timeout=self._tick_clock)
        self._environment_banner()
        self.apply_theme(self.theme)
        self.reload_library()
        self.reload_playlists()
        self.show_page("albums")
        self.pages["settings"].refresh()
        self.console = LiveConsole(self)              # the 指令 tab: GPT's script language, live on this window
        if self.player.engine == "backend":
            self.pages["settings"].tone.clicked.disconnect()
            self.pages["settings"].tone.clicked.connect(lambda: self.console.run("test_output()"))
        self.inspector.console_page.attach(self.console, self.prefs)
        self._tick_clock()
        self.start_backend()
        from ..control.session import GuiSession
        try:
            self.gui_control = GuiSession(self)
        except (OSError, ValueError, RuntimeError) as exc:
            self.gui_control = None
            self.toast_error(tr("本地窗口指令服务无法启动：{0}", str(exc)))
        if prefs.get("device_view") is True:          # the window comes back the way it was left
            QTimer.singleShot(0, lambda: self.toggle_player_mode(True))

    # ------------------------------------------------------------------ layout
    def _sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("rail")
        side.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(0, 4, 0, 8)
        lay.setSpacing(0)

        def section(title):
            l = QLabel(title)
            l.setObjectName("sectionTitle")
            lay.addWidget(l)

        section(tr("曲库  LIBRARY"))
        self.nav = nav_list(NAV)
        self.nav.currentItemChanged.connect(lambda it, _: it and self._goto(it.data(Qt.UserRole), self.nav))
        self.nav.itemClicked.connect(lambda it: self._to_top(it.data(Qt.UserRole)))
        lay.addWidget(self.nav)
        lay.addSpacing(6)
        section(tr("工具  TOOLS"))
        self.tools = nav_list(TOOLS)
        self.tools.currentItemChanged.connect(self._tool_selected)
        lay.addWidget(self.tools)
        lay.addStretch(1)
        area = scroller(side, 208)
        area.setObjectName("railArea")
        area.setMinimumHeight(0)
        return area

    def _tool_selected(self, it, previous) -> None:
        if it is None:
            return
        key = it.data(Qt.UserRole)
        if key == "rescan":                       # an action, like the player's 更新曲库, not a page
            self.tools.blockSignals(True)
            self.tools.setCurrentItem(previous) if previous is not None else self.tools.setCurrentRow(-1)
            if previous is None:
                self.tools.clearSelection()
            self.tools.blockSignals(False)
            self.start_scan()
            return
        self._goto(key, self.tools)

    def _case_parts(self) -> None:
        """What sits on the metal: the engraving and, flush with the top edge, the language wheel."""
        self.plate = hardware.Engraving()
        self.plate.clicked.connect(self.toggle_theme)        # click TITANIUM / ALUMINIUM
        self.wheel = hardware.XM5Knob()      # the volume wheel of the built-in player
        self.wheel.rolled.connect(lambda steps: self.player.step_volume(-steps))
        self.wheel.setToolTip(tr("音量滚轮：向上滚动加大，向下减小"))

        # kept for callers/tests that look for them
        self.count = QLabel("")
        self.scan_bar = QProgressBar()
        self.scan_bar.hide()
        self.scan_btn = QPushButton("Rescan library")
        self.scan_btn.setObjectName("rescan")
        self.scan_btn.clicked.connect(self.start_scan)
        self.scan_btn.hide()

    def _status_strip(self) -> QWidget:
        """The firmware status bar: state on the left, clock in the middle, counts on the right."""
        bar = QWidget()
        bar.setObjectName("statusStrip")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setFixedHeight(34)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)
        self.st_left = StatusPlay()           # firmware style: play state, speaker, volume number
        self.st_left.set_volume(self.player.volume)
        self.st_left.stepped.connect(self.player.step_volume)
        self.st_clock = QLabel("")
        self.st_clock.setAlignment(Qt.AlignCenter)
        self.st_right = QLabel("")
        self.st_right.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText(tr("搜索 标题 / 艺术家 / 专辑 / 风格 / 格式"))
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(24)
        self.search.hide()
        self._search_timer = QTimer(self, singleShot=True, interval=180, timeout=self._apply_search)
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        self.search_icon = QPushButton("⌕")
        self.search_icon.setObjectName("searchIcon")
        self.search_icon.setToolTip(tr("搜索 (Ctrl+F)"))
        self.search_icon.setCursor(Qt.PointingHandCursor)
        self.search_icon.clicked.connect(lambda: self.close_search(force=True) if self.search.isVisible()
                                         else self.open_search())
        lay.addWidget(self.st_left, 1)
        lay.addWidget(self.st_clock, 2)
        lay.addWidget(self.search, 2)
        right = QHBoxLayout()
        right.setSpacing(6)
        right.addStretch(1)
        right.addWidget(self.st_right)
        right.addWidget(self.search_icon)
        right.addSpacing(10)
        self.win_buttons = []
        # the middle key is not maximise: the window turns into the player itself (double-click the metal
        # above the screen still maximises the normal window)
        for glyph, tip, fn in (("—", tr("最小化"), self.showMinimized),
                               ("", tr("播放器模式"), lambda: self.toggle_player_mode(True)),
                               ("✕", tr("关闭"), self.close)):
            b = QPushButton(glyph)
            b.setObjectName("winClose" if glyph == "✕" else "winButton")
            if not glyph:
                b.setObjectName("winPlayerMode")
                b.setIcon(player_mode_icon())
                b.setIconSize(QSize(14, 14))
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(26, 22)
            b.clicked.connect(fn)
            self.win_buttons.append(b)
            right.addWidget(b)
        lay.addLayout(right, 1)
        return bar

    # ------------------------------------------------------------------ player mode
    def toggle_player_mode(self, on: bool | None = None) -> None:
        """Player mode (the title bar's middle key): the window becomes a whole XM5 -- body, glass, the four
        keys, the knob -- at one of the fixed scales (1x: the screen's own 720 x 720 pixels); its screen runs the
        firmware's pages. The same player keeps playing. The hidden x at the top right leaves it; dragging the
        bottom-right corner picks another scale."""
        on = (not self.player_mode) if on is None else bool(on)
        if on == self.player_mode:
            return
        if on:
            from . import device
            if not device.available():
                self.toast_error(tr("缺少原厂界面资源（assets/xm5stock），无法进入播放器模式"))
                return
            first = self.device is None
            if first:
                self.device_frame, self.device = device.create(self, self.central, scale=self._device_scale())
                f = self.device_frame
                f.exitRequested.connect(lambda: self.toggle_player_mode(False))
                f.sizeWanted.connect(lambda _s: self._fit_player_mode(keep_corner=True))
                f.scaleChanged.connect(lambda sc: self.prefs.set("device_scale", sc))
                f.menuRequested.connect(self._more_menu)
                self.central.addWidget(f)
            self.close_search(force=True)
            maxed = self.isMaximized() or self.isFullScreen()
            self._normal_state = (QRect(self.normalGeometry() if maxed else self.geometry()), maxed)
            if maxed:
                self.showNormal()
            self.player_mode = True
            self.device_frame.set_theme(self.theme)
            if not first:
                self.device_frame.set_scale(self._device_scale())
            self.central.setCurrentWidget(self.device_frame)
            self.device.activate(True)
            self._fit_player_mode()
            self.device_frame.setFocus()
            if not self.prefs.get("device_view_hint"):
                self.prefs.set("device_view_hint", True)
                self.device.toast(tr("右上角的隐形 × 退出播放器模式；拖动右下角缩放"), 3500)
        else:
            self.player_mode = False
            self.device.activate(False)
            self.central.setCurrentWidget(self.case)
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.setMinimumWidth(1040)
            self._update_min_height()
            geom, maxed = self._normal_state or (None, False)
            if geom is not None and geom.isValid():
                self.setGeometry(geom)
            else:
                self.resize(1500, 920)
            if maxed:
                self.showMaximized()
        self.prefs.set("device_view", self.player_mode)        # ("player_mode" is the play order)

    def _device_scale(self) -> float:
        """The saved scale if the whole player fits on this monitor, else the biggest one that does."""
        from .device.body import SCALES, Geometry
        mon = self.screen() or QApplication.primaryScreen()
        dpr = mon.devicePixelRatio() if mon is not None else 1.0
        avail = mon.availableGeometry() if mon is not None else QRect(0, 0, 1600, 1000)
        fits = [s for s in SCALES if Geometry(s).size[1] <= avail.height() * dpr
                and Geometry(s).size[0] <= avail.width() * dpr] or [SCALES[0]]
        want = self.prefs.get("device_scale")
        if isinstance(want, (int, float)) and want in fits:
            return float(want)
        return max(s for s in fits if s <= 1.0) if any(s <= 1.0 for s in fits) else fits[0]

    def player_mode_size(self) -> QSize:
        return self.device_frame.logical_size() if self.device_frame is not None else QSize(0, 0)

    def _fit_player_mode(self, keep_corner: bool = False) -> None:
        """The window takes the player's size: centred where the window was, or (resizing from the
        bottom-right corner) with the top-left corner kept; always inside the monitor."""
        if not self.player_mode or self.device_frame is None:
            return
        size = self.device_frame.logical_size()
        old = self.geometry()
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.setFixedSize(size)
        g = QRect(old.topLeft(), size)
        if not keep_corner:
            g.moveCenter(old.center())
        mon = self.screen() or QApplication.primaryScreen()
        if mon is not None:
            a = mon.availableGeometry()
            g.moveLeft(max(a.left(), min(g.left(), a.right() - g.width() + 1)))
            g.moveTop(max(a.top(), min(g.top(), a.bottom() - g.height() + 1)))
        self.move(g.topLeft())

    def _escape(self) -> None:
        if self.player_mode:
            self.device.back()
        else:
            self.close_search()

    def open_search(self) -> None:
        if self.player_mode:
            return
        self.st_clock.hide()
        self.search.show()
        self.search.setFocus()
        self.search.selectAll()

    def close_search(self, force: bool = False) -> None:
        if not self.search.isVisible():
            return
        if self.search.text() and not force:
            self.search.clear()
            return
        self.search.clear()
        self.search.hide()
        self.st_clock.show()

    def _now_bar(self) -> QWidget:
        """The firmware's mini player: a progress line on top, then cover, title, transport, time, play mode,
        favourite and playlist. Shows the playing song; with nothing loaded, the current selection."""
        bar = QWidget()
        bar.setObjectName("nowBar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setFixedHeight(78)
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.seek_bar = SeekBar()
        self.seek_bar.seek.connect(self.player.seek)
        outer.addWidget(self.seek_bar)
        lay = QHBoxLayout()
        lay.setContentsMargins(16, 2, 18, 8)
        lay.setSpacing(10)
        outer.addLayout(lay, 1)
        self.now_cover = QLabel()
        self.now_cover.setFixedSize(54, 54)
        lay.addWidget(self.now_cover)
        lay.addSpacing(4)
        txt = QVBoxLayout()
        txt.setSpacing(2)
        txt.addStretch(1)
        self.now_title = QLabel("")
        self.now_title.setObjectName("nowTitle")
        self.now_sub = QLabel("")
        self.now_sub.setObjectName("nowSub")
        txt.addWidget(self.now_title)
        txt.addWidget(self.now_sub)
        txt.addStretch(1)
        lay.addLayout(txt, 1)

        def button(name, icon, size, box, tip):
            b = QPushButton()
            b.setObjectName(name)
            b.setFlat(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setIcon(QIcon(style.icon_path(icon)))
            b.setIconSize(size)
            b.setFixedSize(box, box)
            b.setStyleSheet("QPushButton { background: transparent; border: none; }"
                            "QPushButton:hover { background: #151515; border-radius: %dpx; }" % (box // 2))
            return b

        self.now_prev = button("nowPrev", "prev", QSize(22, 27), 42, tr("上一首 (Ctrl+←)"))
        self.now_play = button("nowPlay", "playing_stop", QSize(27, 37), 50, tr("播放 (空格)"))
        self.now_next = button("nowNext", "next", QSize(22, 27), 42, tr("下一首 (Ctrl+→)"))
        for b in (self.now_prev, self.now_play, self.now_next):
            lay.addWidget(b)
        self.now_time = QLabel("")
        self.now_time.setObjectName("nowSub")
        self.now_time.setMinimumWidth(96)
        self.now_time.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.now_time)
        self.now_mode = button("nowMode", "btn_listplay", QSize(30, 30), 42, "")
        self.now_fav = button("nowFav", "btn_fav", QSize(30, 30), 42, tr("最爱"))
        self.now_list = button("nowList", "botton_playlists", QSize(32, 32), 42, tr("加入歌单"))
        for b in (self.now_mode, self.now_fav, self.now_list):
            lay.addWidget(b)
        self.now_prev.clicked.connect(self.player.previous)
        self.now_next.clicked.connect(self.player.next)
        self.now_play.clicked.connect(self._play_button)
        self.now_mode.clicked.connect(lambda: self.toast(MODE_NAMES[self.player.cycle_mode()]))
        self.now_fav.clicked.connect(lambda: self.toggle_favorite(self._now_targets()))
        self.now_list.clicked.connect(lambda: self.add_to_playlist_menu(
            self._now_targets(), self.now_list.mapToGlobal(self.now_list.rect().topLeft())))
        self.selection: list[str] = []
        self._mode_icon(self.player.mode)
        self._show_now([])
        return bar

    # ------------------------------------------------------------------ player
    def _wire_player(self) -> None:
        p = self.player
        p.stateChanged.connect(self._player_state)
        p.trackChanged.connect(self._player_track)
        p.positionChanged.connect(self._player_time)
        p.durationChanged.connect(lambda ms: (self.seek_bar.set_duration(ms), self._player_time()))
        p.volumeChanged.connect(self._player_volume)
        p.modeChanged.connect(self._mode_icon)
        p.queueChanged.connect(lambda: self._show_now(self.selection))
        p.failed.connect(self.toast_error)
        p.engineChanged.connect(self._engine_changed)
        p.trackChanged.connect(self._record_play)
        for keys, fn in (("Space", p.toggle), ("Ctrl+Right", p.next), ("Ctrl+Left", p.previous),
                         ("Ctrl+Up", lambda: p.step_volume(2)), ("Ctrl+Down", lambda: p.step_volume(-2))):
            QShortcut(QKeySequence(keys), self, activated=fn)
        for key, fn in ((Qt.Key_MediaTogglePlayPause, p.toggle), (Qt.Key_MediaPlay, p.toggle),
                        (Qt.Key_MediaPause, p.pause), (Qt.Key_MediaStop, p.stop), (Qt.Key_MediaNext, p.next),
                        (Qt.Key_MediaPrevious, p.previous)):
            QShortcut(QKeySequence(key), self, activated=fn)
        if not p.available:
            self.now_play.setToolTip(tr("内置播放器不可用：{0}", p.reason))

    def _record_play(self, track) -> None:
        """最近播放 / 经常播放 (player mode's home): the last 100 songs started, and how often each was."""
        path = (track or {}).get("path")
        if not path:
            return
        h = dict(self.prefs.get("device_history") or {})
        h["recent"] = ([path] + [p for p in h.get("recent", []) if p != path])[:100]
        counts = dict(h.get("counts") or {})
        counts[path] = int(counts.get(path, 0)) + 1
        if len(counts) > 300:
            counts = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:300])
        h["counts"] = counts
        try:
            self.prefs.set("device_history", h)
        except Exception:                                   # a full disk must not stop the music
            pass

    def _engine_changed(self) -> None:
        self.now_play.setToolTip(tr("播放 (空格)") if self.player.available else tr("内置播放器不可用：{0}", self.player.reason))
        if "settings" in getattr(self, "pages", {}):
            self.pages["settings"].refresh_player()

    def _player_state(self, state: str) -> None:
        self.st_left.set_state(state)
        playing = state == "playing"
        self.now_play.setIcon(QIcon(style.icon_path("playing_playing" if playing else "playing_stop")))
        self.now_play.setToolTip(tr("暂停 (空格)") if playing else tr("播放 (空格)"))

    def _player_track(self, track) -> None:
        NOW_PLAYING["path"] = track["path"] if track else None
        for page in self.pages.values() if hasattr(self, "pages") else ():
            for m in [getattr(page, n, None) for n in ("model", "tracks_model")]:
                if hasattr(m, "refresh_playing"):
                    m.refresh_playing()
        self._show_now(self.selection)

    def _player_time(self, *_):
        p = self.player
        self.seek_bar.set_position(p.position)
        self.now_time.setText(f"{format_ms(p.position)} / {format_ms(p.duration)}" if p.current else "")

    def _player_volume(self, v: int) -> None:
        self.st_left.set_volume(v)
        self._vol_save.start()

    def _mode_icon(self, mode: str) -> None:
        icon = {"order": "btn_listplay", "repeat_all": "btn_repeatall", "repeat_one": "btn_repeattrack",
                "shuffle": "btn_shuffle"}[mode]
        self.now_mode.setIcon(QIcon(style.icon_path(icon)))
        self.now_mode.setToolTip(tr("播放模式：{0}（点击切换）", MODE_NAMES[mode]))
        self.prefs.set("player_mode", mode)

    def _play_button(self) -> None:
        if self.player.current is not None:
            self.player.toggle()
        elif self.selection:
            self.play_paths(self.selection)

    def _now_targets(self) -> list[str]:
        cur = self.player.current
        return [cur["path"]] if cur is not None else list(self.selection)

    def play_rows(self, rows: list[dict], start: int = 0) -> None:
        """Play ``rows`` as the queue from row ``start`` (rows missing from the library are skipped)."""
        start_t = rows[start] if 0 <= start < len(rows) else None
        tracks = [t for t in rows if t.get("path") in self.view.by_path]
        if not tracks:
            return
        i = next((k for k, t in enumerate(tracks) if t is start_t), 0)
        if not self._player_ready():
            return
        self.player.play_tracks(tracks, i)

    def _player_ready(self) -> bool:
        if self.player.available:
            return True
        self.error(tr("播放器没有加载起来，无法播放。\n\n{0}", self.player.reason))
        return False

    def choose_output(self, engine) -> str | None:
        """First play with the backend: the user names the output (never a silent default speaker)."""
        if self.quiet:
            return os.environ.get("XM5LIB_TEST_OUTPUT") or None
        from .backend_player import OutputChooser
        dlg = OutputChooser(self, self.backend, engine.device_id)
        ok = dlg.exec() == QDialog.Accepted
        if ok and "settings" in getattr(self, "pages", {}):
            QTimer.singleShot(0, self.pages["settings"].refresh_player)
        return dlg.choice if ok else None

    def play_paths(self, paths_: list[str]) -> None:
        self.play_rows([self.view.by_path[p] for p in paths_ if p in self.view.by_path], 0)

    def enqueue_paths(self, paths_: list[str]) -> None:
        tracks = [self.view.by_path[p] for p in paths_ if p in self.view.by_path]
        if tracks and self._player_ready():
            self.player.enqueue(tracks)
            self.toast(tr("已加入播放队列：{0} 首", len(tracks)))

    def play_next_paths(self, paths_: list[str]) -> None:
        tracks = [self.view.by_path[p] for p in paths_ if p in self.view.by_path]
        if tracks and self._player_ready():
            self.player.play_next(tracks)
            self.toast(tr("下一首播放：{0} 首", len(tracks)))

    # ------------------------------------------------------------------ case state
    def apply_theme(self, theme: str, save: bool = False) -> None:
        theme = style.LEGACY_THEME.get(theme, theme)
        self.theme = theme if theme in style.METAL else "titanium"
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(style.qss(self.theme))
        for w in (self.case, self.plate, self.wheel, self.glass):
            w.set_theme(self.theme)
        if getattr(self, "device_frame", None) is not None:
            self.device_frame.set_theme(self.theme)
        if "settings" in getattr(self, "pages", {}):
            self.pages["settings"].show_theme(self.theme)
        if save:
            self.prefs.set("skin_theme", self.theme)

    def toggle_theme(self) -> None:
        self.apply_theme("titanium" if self.theme == "aluminum" else "aluminum", save=True)

    def set_grooves(self, n: int) -> None:
        self.case.set_grooves(n)
        self.prefs.set("case_grooves", self.case.grooves)
        self._update_min_height()
        self.glare.setGeometry(self.glass.rect())

    def _update_min_height(self) -> None:
        self.setMinimumHeight(round(self.case.min_body_height() + 2 * self.case.margin))

    def _apply_margins(self) -> None:
        m = 0 if self.isMaximized() or self.isFullScreen() else 12
        self.case.margin = m
        self.case._place()
        self.case.update()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == e.Type.WindowStateChange and hasattr(self, "case"):
            self._apply_margins()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "glare"):
            self.glare.setGeometry(self.glass.rect())
            self.glare.raise_()

    def _lcd(self, big: str | None = None) -> None:
        """Counts in the status bar; during a scan, its progress."""
        if big is None:
            big = tr("{0} 首 · {1} 张", len(self.view.tracks), len(self.view.albums))
            self.st_right.setStyleSheet("")
        else:
            self.st_right.setStyleSheet(f"color: {style.ACCENT};")
        self.st_right.setText(big)

    def _tick_clock(self) -> None:
        self.st_clock.setText(time.strftime("%H:%M"))
        self.st_clock.setStyleSheet("")

    def _wheel_scroll(self, dy: int) -> None:
        areas = [a for a in self.current_page().findChildren(QAbstractScrollArea)
                 if a.isVisible() and a.verticalScrollBar().maximum() > 0]
        if not areas:
            return
        area = max(areas, key=lambda a: a.width() * a.height())
        sb = area.verticalScrollBar()
        sb.setValue(sb.value() + dy)

    def _more_menu(self, pos=None) -> None:
        menu = QMenu(self)
        if self.player_mode:                          # on the player's metal: the player and the window
            from .device.body import SCALES
            d, f = self.device, self.device_frame
            menu.addAction(tr("电源键（亮屏 / 熄屏）"), lambda: d.power_key(False))
            menu.addAction(tr("长按电源键（关机 / 开机）"), lambda: d.power_key(True))
            sizes = menu.addMenu(tr("播放器大小"))
            for sc in SCALES:
                a = sizes.addAction(f"{sc:g}x" + ("  (720 px)" if sc == 1.0 else ""),
                                    lambda sc=sc: (f.set_scale(sc), self.prefs.set("device_scale", sc)))
                a.setCheckable(True)
                a.setChecked(sc == f.s)
            menu.addAction(tr("换成钛版外壳") if self.theme == "aluminum" else tr("换成铝版外壳"),
                           lambda: self.apply_theme("titanium" if self.theme == "aluminum" else "aluminum", save=True))
            menu.addSeparator()
            menu.addAction(tr("退出播放器模式"), lambda: self.toggle_player_mode(False))
            menu.addAction(tr("最小化"), self.showMinimized)
            menu.addAction(tr("关闭"), self.close)
            menu.exec(pos if pos is not None else self.cursor().pos())
            return
        menu.addAction(tr("更新曲库  (F5)"), self.start_scan)
        menu.addAction(tr("搜索  (Ctrl+F)"), self.open_search)
        menu.addAction(tr("导入 M3U/M3U8 歌单…"), lambda: self.import_m3u())
        menu.addAction(tr("重新定位音乐来源…"), self.choose_relink_music)
        menu.addSeparator()
        menu.addAction(tr("PEQ 预设"), lambda: self.show_page("peq"))
        menu.addAction(tr("TF 卡导出"), lambda: self.show_page("export"))
        menu.addAction(tr("设置"), lambda: self.show_page("settings"))
        menu.addSeparator()
        menu.addAction(tr("换成钛版外壳") if self.theme == "aluminum" else tr("换成铝版外壳"),
                       lambda: self.apply_theme("titanium" if self.theme == "aluminum" else "aluminum", save=True))
        menu.addAction(tr("打开数据目录"), lambda: subprocess.Popen(["explorer.exe", os.path.normpath(str(self.home))]))
        if self.last_error:
            menu.addSeparator()
            menu.addAction(tr("清除错误提示"), self._clear_error)
        menu.addSeparator()
        menu.addAction(tr("播放器模式"), lambda: self.toggle_player_mode(True))
        menu.addAction(tr("最小化"), self.showMinimized)
        menu.addAction(tr("还原") if self.isMaximized() else tr("最大化"),
                       lambda: self.showNormal() if self.isMaximized() else self.showMaximized())
        menu.addAction(tr("关闭"), self.close)
        menu.exec(pos if pos is not None else self.cursor().pos())

    def _clear_error(self) -> None:
        self.last_error = ""
        self._tick_clock()

    def _back(self) -> None:
        if self.player_mode:
            self.device.back()
            return
        w = self.current_page()
        if w is self.pages["albums"] and self.pages["albums"].stack.currentIndex() == 1:
            self.pages["albums"].stack.setCurrentIndex(0)
        elif w is self.pages["folders"]:
            self.pages["folders"].go_up()
        elif w is self.pages["playlist"]:
            self.show_playlists()

    def _to_top(self, key: str) -> None:
        """Clicking the category you are in goes back to its first level (album grid, all playlists)."""
        w = self.current_page()
        if key == "albums" and w is self.pages["albums"]:
            self.pages["albums"].stack.setCurrentIndex(0)
        elif key == "playlists" and w is self.pages["playlist"]:
            self.show_playlists()

    def _show_now(self, paths_: list[str]) -> None:
        cur = self.player.current
        if cur is not None:
            paths_ = [cur["path"]]
        tracks = [self.view.by_path.get(p) or (cur if cur and cur["path"] == p else None) for p in paths_] \
            if hasattr(self, "view") else []
        tracks = [t for t in tracks if t]
        for b in (self.now_fav, self.now_play, self.now_list):
            b.setEnabled(bool(tracks))
        for b in (self.now_prev, self.now_next):
            b.setEnabled(len(self.player.queue) > 0)
        self._player_time()
        if not tracks:
            self.now_title.setText("XM5 Music Manager")
            self.now_sub.setText(tr("选择歌曲或专辑"))
            self.now_cover.setPixmap(QPixmap(style.icon_path("default_cover")).scaled(
                54, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.now_fav.setIcon(QIcon(style.icon_path("btn_fav")))
            return
        t0 = tracks[0]
        albums = {self.view.album_key(t) for t in tracks}
        if len(tracks) == 1:
            title = t0.get("title") or Path(t0["path"]).stem
            sub = " · ".join(x for x in (t0.get("artist") or t0.get("albumartist"), t0.get("album"),
                                         library.format_rate(t0)) if x)
        elif len(albums) == 1:
            title = t0.get("album") or Path(t0["path"]).parent.name
            one = self.view.album_of(t0)
            sub = tr("{0} · {1} 首 · {2}", one.artist if one else library.album_artist(t0), len(tracks), library.format_duration(sum(t.get('duration') or 0 for t in tracks)))
        else:
            title = tr("已选 {0} 首", len(tracks))
            sub = tr("{0} 张专辑 · {1}", len(albums), library.format_duration(sum(t.get('duration') or 0 for t in tracks)))
        self.now_title.setText(title)
        self.now_sub.setText(sub)
        alb = self.view.album_by_key.get(self.view.album_key(t0))
        pix = self.cover_pixmap(alb) if alb is not None else None
        self.now_cover.setPixmap(rounded(pix, 54, 6) if pix is not None else
                                 QPixmap(style.icon_path("default_cover")).scaled(54, 54, Qt.KeepAspectRatio,
                                                                                  Qt.SmoothTransformation))
        on = all(self.favorites.is_fav(t["path"]) for t in tracks)
        self.now_fav.setIcon(QIcon(style.icon_path("btn_faved" if on else "btn_fav")))

    # ------------------------------------------------------------------ favourites
    def set_favorite(self, paths_: list[str], on: bool) -> None:
        if not paths_:
            return
        self.favorites.set(paths_, on)
        self.toast(tr("已加入最爱：{0} 首", len(paths_)) if on else tr("已移出最爱：{0} 首", len(paths_)))

    def toggle_favorite(self, paths_: list[str]) -> None:
        if not paths_:
            return
        on = self.favorites.toggle(paths_)
        self.toast(tr("已加入最爱：{0} 首", len(paths_)) if on else tr("已移出最爱：{0} 首", len(paths_)))

    def _favorites_refresh(self) -> None:
        for page in self.pages.values():
            for m in [getattr(page, n, None) for n in ("model", "tracks_model", "results_model")]:
                if hasattr(m, "refresh_favs"):
                    m.refresh_favs()
        self.pages["favorites"].refresh()
        if hasattr(self.pages["albums"], "_fav_state") and self.pages["albums"].album is not None:
            self.pages["albums"]._fav_state()
        self._show_now(self.selection)

    def _environment_banner(self) -> None:
        il = winenv.integrity_level()
        msgs = []
        if il is not None and il < winenv.MEDIUM:
            msgs.append(tr("这个窗口以 {0} 完整性级别运行，部分本地文件或音频资源可能无法访问。请关掉它，用 XM5Library.cmd 启动。", winenv.level_name(il)))
        if paths.is_fallback_home(self.home):
            msgs.append(tr("程序数据暂存在低完整性位置：{0}", self.home))
        if self.backend.state == "failed":
            msgs.append(tr("后端服务没有启动：曲库、歌单和播放暂时不可用。{0}（日志在 runtime\\logs 文件夹里）", self.backend.reason.splitlines()[0] if self.backend.reason else ''))
        self.banner.setText("\n".join(msgs))
        self.banner.setVisible(bool(msgs))

    def _goto(self, key: str, source) -> None:
        for other in (self.nav, self.tools):
            if other is not source:
                other.blockSignals(True)
                other.clearSelection()
                other.setCurrentRow(-1)
                other.blockSignals(False)
        self.stack.setCurrentWidget(self.slots[key])
        self.inspector_area.setVisible(key in LIBRARY_KEYS or self.inspector.pages.currentIndex() == 1)   # 指令 stays
        if key == "favorites":
            self.pages["favorites"].refresh()
        elif key == "playlists":
            self.pages["playlists"].refresh()
        if key == "export":
            self.pages["export"].refresh_sources()
        elif key == "settings":
            self.pages["settings"].refresh()
        elif key == "peq":
            self.pages["peq"].reload()

    def current_page(self) -> QWidget:
        slot = self.stack.currentWidget()
        return slot.widget() if isinstance(slot, QScrollArea) else slot

    def show_page(self, key: str) -> None:
        for lst in (self.nav, self.tools):
            for i in range(lst.count()):
                if lst.item(i).data(Qt.UserRole) == key:
                    if lst.currentRow() == i:                 # already highlighted (e.g. 歌单 while one is open)
                        self._goto(key, lst)
                    else:
                        lst.setCurrentRow(i)
                    return

    # ------------------------------------------------------------------ data
    @property
    def view(self) -> library.LibraryView:
        return self.state.view

    def set_albums_ignore_artist(self, on: bool) -> None:
        """系统设置 › 专辑无视歌手: regroup the albums (nothing is written to the files or the backend)."""
        self.prefs.set("albums_ignore_artist", bool(on))
        self.state.regroup()

    def settings_value(self, key):
        return self.prefs.get(key)

    def set_setting(self, key, value):
        self.prefs.set(key, value)

    def reload_library(self) -> None:
        self._apply_search()
        if self.current_page() is self.pages["playlist"]:
            self.pages["playlist"].refresh()
        if self.selection:
            self.inspector.set_tracks([self.view.by_path[p] for p in self.selection if p in self.view.by_path])

    def _apply_search(self) -> None:
        self.query = self.search.text().strip()
        albums = self.view.search_albums(self.query)
        tracks = self.view.search_tracks(self.query)
        n_err = sum(1 for t in self.view.tracks if t.get("error"))
        q = tr(" · 搜索“{0}”", self.query) if self.query else ""
        suffix = tr(" · {0} 首无法读取", n_err) if n_err else ""
        self.pages["albums"].set_albums(albums, tr("{0} 张{1}", len(albums), q))
        self.pages["tracks"].set_tracks(tracks, tr("{0} 首{1}{2}", len(tracks), q, suffix))
        self.pages["artists"].set_artists([(a, n) for a, n in self.view.artists()
                                           if not self.query or self.view.search_albums(self.query, a)])
        shown = set(t["path"] for t in tracks)
        self.pages["genres"].set_groups(catalog.genre_tracks(tracks))
        self.pages["resolution"].set_groups([(r, [t for t in ts if t["path"] in shown])
                                             for r, ts in catalog.resolutions(self.view)
                                             if any(t["path"] in shown for t in ts)])
        self.pages["folders"].set_root(catalog.folder_tree(self.view if not self.query else _Filtered(self.view, tracks),
                                                           self.music_roots()))
        self.pages["favorites"].refresh()
        self.count.setText(f"{len(self.view.tracks)} tracks · {len(self.view.albums)} albums{suffix}")
        self._lcd()
        self.request_covers(albums)

    def set_selection_paths(self, paths_: list[str]) -> None:
        self.selection = list(paths_)
        self.inspector.set_tracks([self.view.by_path[p] for p in paths_ if p in self.view.by_path])
        self._show_now(self.selection)

    def _save_tags(self, paths_: list[str], changes: dict, cover, note) -> None:
        """The tag panel's 保存: fields and cover go into the files; the note stays in the library."""
        if note is not None:
            self.state.annotate(paths_, None, note, ok=self.inspector.reload)
        if not changes and cover is tagfile.KEEP:
            return
        if not self.backend.ready:
            self.error(tr("后端服务没有运行，暂时不能写标签。"))
            self.inspector.reload()
            return
        n = len(paths_)
        self.toast(tr("正在写入 {0} 首…", n))

        def done(written, failed):
            if written:
                self.toast(tr("已写入 {0} 首的标签（Ctrl+Z 撤销）", len(written)))
        self.state.write_tags([(p, changes, cover) for p in paths_], tr("修改 {0} 首的标签", n), ok=done)

    def _tags_written(self, paths_: list[str], cover_changed: bool) -> None:
        if set(paths_) & {t["path"] for t in self.inspector.tracks}:
            self.inspector.reload()
        if cover_changed:
            self.forget_covers([self.view.by_path[p] for p in paths_ if p in self.view.by_path])

    def forget_covers(self, tracks: list[dict]) -> None:
        """Embedded pictures changed: drop the cached thumbnails of their albums and draw them again."""
        albums = {a.cover_id: a for a in (self.view.album_of(t) for t in tracks) if a is not None}
        cache = PortablePaths.discover().artwork
        with self.cover_lock:
            for cid in albums:
                self.pixmaps.pop(cid, None)
                self.pages["albums"].model.pixmaps.pop(cid, None)
                self.queued_covers.discard(cid)
                for f in cache.glob(f"{cid}-*"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
        self.reload_library()
        self.request_covers(list(albums.values()))

    # ------------------------------------------------------------------ backend
    def music_roots(self, existing: bool = False) -> list[str]:
        roots = [r for r in (self.prefs.get("music_roots") or []) if r]
        return [r for r in roots if Path(r).is_dir()] if existing else roots

    def peq_roots(self, existing: bool = False) -> list[str]:
        roots = self.prefs.get("peq_roots") or []
        return [r for r in roots if Path(r).is_dir()] if existing else roots

    def source_records(self, kind: str) -> list[dict]:
        return self.prefs.get(kind + "_sources")

    def choose_relink_music(self) -> None:
        roots = self.music_roots()
        if not roots:
            return
        old, ok = QInputDialog.getItem(self, tr("重新定位音乐来源"), tr("原来的文件夹"), roots, 0, False)
        if ok:
            replacement = QFileDialog.getExistingDirectory(self, tr("选择现在的音乐文件夹位置"), str(Path(old).parent))
            if replacement:
                self.relink_music_folder(old, replacement)

    def relink_music_folder(self, folder: str, replacement: str) -> bool:
        """Explicit relocation preserves source, track and playlist identities."""
        source = next((s for s in self.source_records("music") if Path(s["path"]) == Path(folder)), None)
        if source is None or not Path(replacement).is_dir():
            return False
        target = Path(replacement).resolve()
        for other in self.music_roots():
            if Path(other) == Path(folder):
                continue
            candidate = Path(other).resolve()
            if target == candidate or target.is_relative_to(candidate) or candidate.is_relative_to(target):
                self.error(tr("这个位置与另一个音乐来源重叠，请选择独立的文件夹。"))
                return False
        self.prefs.relink_source("music", source["id"], target)
        def failed(error):
            self.prefs.relink_source("music", source["id"], folder)
            self.state.reindex()
            self.restart_backend()
            self.error(tr("来源没有完成重新定位：{0}", error))
        def finished(job):
            if job.get("status") != "succeeded":
                failed(job.get("error") or job.get("status"))
                return
            self.state.load_all()
            self.state.scan([str(target)])
        def relink():
            self.backend.command("library.relink_root", {"old_root": folder, "new_root": str(target)}, lane="jobs",
                                 ok=lambda r: self.backend.watch(r["job_id"], done=finished, failed=finished),
                                 err=failed)
        self.state.reindex()
        self.restart_backend(relink)
        return True

    def backend_config(self) -> BackendConfig:
        exports = list(self.prefs.get("export_roots") or []) + [self.prefs.get("export_parent") or ""]
        mpv = mpv_exe()
        return BackendConfig(music_roots=self.music_roots(existing=True),
                             export_roots=[e for e in dict.fromkeys(exports) if e and Path(e).is_dir()],
                             import_roots=[str(self.home), str(PortablePaths.discover().runtime / "import-staging")] + self.peq_roots(existing=True),
                             mpv=str(mpv) if mpv else None,
                             allow_null_playback=os.environ.get("XM5LIB_ALLOW_NULL_PLAYBACK") == "1")

    def start_backend(self) -> None:
        self._when_ready: list = []
        self.backend.start(self.backend_config())

    def restart_backend(self, then=None) -> None:
        """New folders for the backend (music, export, card) take effect only when it starts again."""
        if then is not None:
            self._when_ready.append(then)
        self._lcd(tr("正在重启后端…"))
        self.backend.restart(self.backend_config())

    def ensure_roots(self, export=(), then=None) -> None:
        """Run ``then`` once the backend may use ``export`` folders (restarting it to add them if needed)."""
        have = [Path(r) for r in self.backend.config.export_roots]
        missing = [str(r) for r in export if r and not any(Path(r) == h or h in Path(r).parents for h in have)]
        if not missing and self.backend.ready:
            if then:
                then()
            return
        known = list(self.prefs.get("export_roots") or [])
        self.prefs.set("export_roots", list(dict.fromkeys(known + missing))[-12:])
        self.restart_backend(then)

    def _backend_state(self, st: str) -> None:
        self._environment_banner()
        if st == "ready":
            self._lcd(tr("读取曲库…"))
            self.state.load_all()
            waiting, self._when_ready = list(getattr(self, "_when_ready", [])), []
            for fn in waiting:
                fn()
        elif st == "starting":
            self._lcd(tr("后端启动中…"))
        elif st == "failed":
            self._lcd(tr("后端未运行"))
            self.error(tr("后端服务没有启动，曲库、歌单和播放暂时不可用。\n\n{0}\n\n日志：{1}", self.backend.reason, self.backend.log_path))
        if hasattr(self, "pages"):
            self.pages["settings"].refresh_library()

    def _library_loaded(self) -> None:
        self._lcd()
        if getattr(self, "autoscan", False) and not self.state.tracks and self.music_roots(existing=True) \
                and not getattr(self, "_first_scan", False):
            self._first_scan = True
            self.start_scan()

    # ------------------------------------------------------------------ music folders (the 文件夹 page manages them)
    def add_music_folder(self, folder: str) -> bool:
        folder = str(Path(folder))
        roots = self.music_roots()
        new = Path(folder).resolve()
        for r in roots:
            old = Path(r).resolve()
            if new == old:
                self.toast(tr("「{0}」已经在曲库里了", folder))
                return False
            if new.is_relative_to(old) or old.is_relative_to(new):
                self.error(tr("「{0}」和曲库里的「{1}」互相包含。\n曲库文件夹不能重叠（导出到 TF 卡时每个文件夹是一个独立的根目录）。", folder, r))
                return False
        self.prefs.set("music_roots", roots + [folder])
        self.state.reindex()
        self.restart_backend(lambda: self.state.scan([folder]))
        self.toast(tr("已添加音乐文件夹：{0}，正在扫描…", folder))
        return True

    def remove_music_folder(self, folder: str) -> None:
        roots = [r for r in self.music_roots() if r != folder]
        self.prefs.set("music_roots", roots)
        self.state.reindex()
        self.restart_backend()
        self.toast(tr("已从曲库移除：{0}（文件没有被删除）", folder))

    def scan_folder(self, folder: str) -> None:
        self.start_scan([folder])

    # ------------------------------------------------------------------ scanning
    def start_scan(self, roots: list[str] | None = None) -> None:
        """更新曲库: scan the music folders (all of them, or the ones given); F5 again cancels."""
        if self.state.busy == "scan":
            self.state.cancel_job()
            self._lcd(tr("正在取消扫描…"))
            return
        if self.state.busy:
            self.toast(tr("正在处理另一项任务，稍后再扫描。"))
            return
        if not self.backend.ready:
            self.error(tr("后端服务没有运行，不能扫描。\n\n{0}", self.backend.describe()))
            return
        roots = [r for r in (roots or self.music_roots()) if Path(r).is_dir()]
        if not roots:
            missing = self.music_roots()
            self.error(tr("找不到这些音乐文件夹：\n  ") + "\n  ".join(missing) if missing else
                       tr("还没有音乐文件夹。请在「文件夹」页面添加。"))
            return
        have = [Path(r) for r in self.backend.config.music_roots]
        if any(Path(r) not in have for r in roots):
            self.restart_backend(lambda: self.state.scan(roots))
            return
        self.state.scan(roots)

    def _job_progress(self, p) -> None:
        if self.state.busy == "scan" and isinstance(p, dict) and p.get("total"):
            self._lcd(tr("扫描 {0}/{1}  (F5 取消)", p.get('current', 0), p['total']))

    def _busy_changed(self, kind: str) -> None:
        self._lcd({"scan": tr("扫描中…  (F5 取消)"), "import": tr("导入旧数据中…")}.get(kind))

    # ------------------------------------------------------------------ covers
    def request_covers(self, albums) -> None:
        with self.cover_lock:
            for a in albums:
                if a.cover_id not in self.pixmaps and a.cover_id not in self.queued_covers:
                    self.queued_covers.add(a.cover_id)
                    self.cover_queue.append((a.cover_id, [t["path"] for t in a.tracks]))
        if self.cover_queue and not self.jobs.busy("covers"):
            cache = PortablePaths.discover().artwork

            def work(job):
                while True:
                    with self.cover_lock:
                        if not self.cover_queue or job.cancel.is_set():
                            return None
                        cid, tracks = self.cover_queue.popleft()
                    path = covers.cached_thumbnail(cache, cid, tracks)
                    job.emit_item((cid, str(path) if path else None))

            self.jobs.start("covers", work, on_item=self._cover_ready, on_done=lambda _: self._covers_done())

    def _covers_done(self):
        if self.cover_queue:
            QTimer.singleShot(0, lambda: self.request_covers([]))

    def _cover_ready(self, item) -> None:
        cid, path = item
        pix = QPixmap(path) if path else QPixmap()
        self.pixmaps[cid] = pix
        if not pix.isNull():
            if self.device is not None:
                self.device.covers_arrived()
            self.pages["albums"].model.set_cover(cid, pix)
            alb = self.pages["albums"].album
            if alb is not None and alb.cover_id == cid and self.pages["albums"].stack.currentIndex() == 1:
                self.pages["albums"].set_cover(pix)
            if self.current_page() is self.pages["playlist"]:
                self.pages["playlist"].refresh_cover()
            elif self.current_page() is self.pages["playlists"]:
                self.pages["playlists"].covers_arrived()
            now = self._now_targets()
            if now:
                t0 = self.view.by_path.get(now[0])
                alb = self.view.album_of(t0) if t0 is not None else None
                if alb is not None and alb.cover_id == cid:
                    self._show_now(self.selection)

    def cover_pixmap(self, album):
        pix = self.pixmaps.get(album.cover_id)
        return pix if pix is not None and not pix.isNull() else None

    def open_album(self, album) -> None:
        self.show_page("albums")
        self.pages["albums"].show_album(album)

    # ------------------------------------------------------------------ playlists
    def reload_playlists(self) -> None:
        if not hasattr(self, "pages"):
            return
        self.pages["playlists"].refresh()
        page = self.pages["playlist"]
        if self.current_page() is page and page.pid is None:
            self.show_playlists()

    def show_playlists(self) -> None:
        self.show_page("playlists")

    def open_playlist(self, pid: str) -> None:
        """A playlist opens inside 歌单 (the sidebar keeps 歌单 highlighted)."""
        if self.state.playlist(pid) is None:
            return
        row = next(i for i in range(self.nav.count()) if self.nav.item(i).data(Qt.UserRole) == "playlists")
        self.nav.blockSignals(True)
        self.nav.setCurrentRow(row)
        self.nav.blockSignals(False)
        self._goto("playlist", self.nav)
        self.pages["playlist"].load(pid)

    select_playlist = open_playlist             # older name (tests, harness)

    def create_playlist(self, name: str | None = None, paths_: list[str] | None = None, edit: bool = True,
                        open_it: bool = True) -> None:
        """Like a streaming app: the playlist exists at once (default name), opens, and offers 编辑详情."""
        name = name or self.state.unique_name()

        def made(pid):
            self.reload_playlists()
            if open_it:
                self.select_playlist(pid)
                if edit:
                    QTimer.singleShot(0, self.pages["playlist"].edit_details)
            self.toast(tr("已新建歌单「{0}」", name) + (tr("：{0} 首", len(paths_)) if paths_ else ""))
        if not self.backend.ready:
            self.error(tr("后端服务没有运行，暂时不能新建歌单。"))
            return
        self.state.create_playlist(name, paths_ or [], ok=made)

    def add_to_playlist(self, pid: str, paths_: list[str]) -> None:
        n = self.state.append(pid, paths_)
        pl = self.state.playlist(pid)
        if n and pl:
            self.toast(tr("已加入「{0}」：{1} 首", pl['name'], n))

    def rename_playlist(self, pid: str) -> None:
        self.select_playlist(pid)
        self.pages["playlist"].edit_details()

    def ask(self, title: str, text: str, yes: str = tr("确定"), no: str = tr("取消")) -> bool:
        box = QMessageBox(QMessageBox.Question, title, text, parent=self)
        ok = box.addButton(yes, QMessageBox.AcceptRole)
        box.addButton(no, QMessageBox.RejectRole)
        box.setDefaultButton(ok)
        box.exec()
        return box.clickedButton() is ok

    def delete_playlist(self, pid: str) -> None:
        pl = self.state.playlist(pid)
        if pl is None:
            return
        name = pl["name"]
        if not self.quiet and not self.ask(tr("删除歌单"), tr("删除歌单「{0}」？\n\n音乐文件不受影响；删掉后可以按 Ctrl+Z 撤销。", name),
                                           tr("删除"), tr("取消")):
            return
        page = self.pages["playlist"]
        if page.pid == pid:
            page.pid = None
            self.show_playlists()
        self.state.delete_playlist(pid, ok=lambda _r: self.toast(tr("已删除歌单「{0}」（Ctrl+Z 撤销）", name)))

    def import_m3u(self, pid: str | None = None, path: str | None = None) -> None:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, tr("导入歌单"), "", tr("歌单 (*.m3u *.m3u8)"))
            if not path:
                return
        try:
            res = playlists.read_m3u(Path(path), [t["path"] for t in self.view.tracks])
        except (OSError, ValueError) as exc:
            self.error(tr("读不了 {0}：{1}", path, exc))
            return
        problems = res.missing + res.outside + res.unsupported
        if problems:
            msg = (tr("在曲库里找到 {0} 首。不能导入的：\n", len(res.paths))
                   + (tr("  {0} 个文件不存在\n", len(res.missing)) if res.missing else "")
                   + (tr("  {0} 个文件不在音乐文件夹里\n", len(res.outside)) if res.outside else "")
                   + (tr("  {0} 个网络流/链接\n", len(res.unsupported)) if res.unsupported else "")
                   + "\n".join("  " + p for p in problems[:12]))
            if self.quiet:
                self.messages.append(msg)
            elif not self.ask(tr("导入歌单"), msg + tr("\n\n只导入找到的这些？"), tr("导入"), tr("取消")):
                return
        if pid is None:
            names = {p["name"] for p in self.state.playlist_list()}
            name, n = res.name, 2
            while name in names:
                name = f"{res.name} ({n})"
                n += 1
            self.create_playlist(name, res.paths, edit=False)
        else:
            self.add_to_playlist(pid, res.paths)

    def export_m3u(self, pid: str, path: str | None = None) -> None:
        pl = self.state.playlist(pid)
        if pl is None:
            return
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, tr("导出歌单"), pl["name"] + ".m3u8", "M3U8 (*.m3u8)")
            if not path:
                return
        rows, missing = self.state.playlist_rows(pid)
        tracks = [t for t in rows if t["path"] not in missing]
        playlists.write_m3u8(Path(path), tracks, relative=False)
        self.toast(tr("已导出 {0} 首到 {1}", len(tracks), path))

    def add_to_playlist_menu(self, paths_: list[str], pos) -> None:
        menu = QMenu(self)
        self._fill_playlist_menu(menu, paths_)
        menu.exec(pos)

    def _fill_playlist_menu(self, menu: QMenu, paths_: list[str]) -> None:
        lists = self.state.playlist_list()
        for pl in lists:
            menu.addAction(pl["name"], lambda pid=pl["id"]: self.add_to_playlist(pid, paths_))
        if lists:
            menu.addSeparator()
        menu.addAction(tr("＋ 新建歌单并加入"), lambda: self._new_from(paths_))

    def _new_from(self, paths_: list[str]) -> None:
        """Like a streaming app's "Add to new playlist": named after the song or album, no page switch."""
        tracks = [self.view.by_path[p] for p in paths_ if p in self.view.by_path]
        albums = {self.view.album_key(t) for t in tracks}
        base = (tracks[0].get("title") if len(tracks) == 1 else
                tracks[0].get("album") if len(albums) == 1 and tracks[0].get("album") else None) if tracks else None
        base = "".join(ch for ch in (base or "") if ch not in '\\/:*?"<>|').strip()[:60]
        name = self.state.unique_name(base, numbered=False) if base else self.state.unique_name()
        self.create_playlist(name, paths_, edit=False, open_it=False)

    def track_menu(self, paths_: list[str], pos, extra=None, explorer: bool = True) -> None:
        if not paths_:
            return
        menu = QMenu(self)
        menu.addAction(tr("▶  播放"), lambda: self.play_paths(paths_))
        menu.addAction(tr("下一首播放"), lambda: self.play_next_paths(paths_))
        menu.addAction(tr("加入播放队列"), lambda: self.enqueue_paths(paths_))
        sub = menu.addMenu(tr("加入歌单"))
        self._fill_playlist_menu(sub, paths_)
        on = all(self.favorites.is_fav(p) for p in paths_)
        menu.addAction(tr("♥  移出最爱") if on else tr("♡  加入最爱"), lambda: self.set_favorite(paths_, not on))
        if extra:
            menu.addSeparator()
            for label, fn in extra:
                menu.addAction(label, fn)
        if explorer:
            menu.addSeparator()
            menu.addAction(tr("在资源管理器中显示"), lambda: subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(paths_[0])]))
        menu.exec(pos)

    # ------------------------------------------------------------------ external apps
    def open_peace(self) -> None:
        self.show_page("peq")
        self.pages["peq"].name.setFocus()

    # ------------------------------------------------------------------ feedback
    def toast(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 12000)
        line = msg.splitlines()[0] if msg else ""
        self.st_clock.setText(line if len(line) < 90 else line[:88] + "…")
        self.st_clock.setStyleSheet(f"color: {style.ACCENT};")
        self.st_clock.setToolTip(msg)
        self._toast_timer.start()

    def toast_error(self, msg: str) -> None:
        """A problem worth seeing but not worth a dialog (e.g. a file the player cannot decode)."""
        self.messages.append(msg)
        self.toast(msg)
        self.st_clock.setStyleSheet(f"color: {style.DANGER};")

    def error(self, msg: str) -> None:
        self.messages.append(msg)
        self.statusBar().showMessage(msg.splitlines()[0], 15000)
        self.last_error = msg
        self.st_clock.setText(msg.splitlines()[0][:88])
        self.st_clock.setStyleSheet(f"color: {style.DANGER};")
        self._toast_timer.start()
        if not self.quiet:
            QMessageBox.warning(self, APP_NAME, msg)

    def closeEvent(self, event):
        export = self.pages.get("export") if hasattr(self, "pages") else None
        if export is not None and export.running():
            if not self.ask(APP_NAME, tr("TF 卡导出还在进行。取消导出并退出吗？导出文件夹会保持「未完成」标记，不会被当成可用的卡。"),
                            tr("取消导出并退出"), tr("继续导出")):
                event.ignore()
                return
            export.cancel()
        if getattr(self, "gui_control", None) is not None:
            self.gui_control.close()
        self.console.stop()
        self.prefs.set("player_volume", self.player.volume)
        self.player.shutdown()
        for name in list(self.jobs.running):
            self.jobs.cancel(name)
        self.jobs.pool.waitForDone(10000)
        self.backend.shutdown()
        event.accept()
        if self._restart:                                    # language change: start again, now on our own
            from .app import restart
            restart()

    def restart_app(self) -> None:
        self._restart = True
        self.close()


class _Filtered:
    """Minimal LibraryView stand-in holding only the search hits (for the folder tree)."""

    def __init__(self, view, tracks):
        self.tracks = tracks
