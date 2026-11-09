"""Player mode's controller: the firmware's window manager, keys, knob, screen and settings.

What the firmware does (read off ``demo`` V2.43, see docs/XM5-UI-BEHAVIOUR.md):

* keys (``on_wm_keyup_fun``): ⏮ previous, ⏯ play/pause, ⏭ next -- with the screen off they still work
  unless the key lock is on and set to lock the keys; with the screen turned 180° ⏮/⏭ swap. Fn runs the
  custom function: back to home (default), rotate the screen, key lock on/off, lock the screen (= the
  power key), the next gain, or add the song to My Fav. No list, no song: "无歌曲列表".
* knob: the volume, with the volume ring; locked like the keys when the screen is off;
* touch (``on_wm_ts*``): pulling down from the status bar opens the drop-down panel (it follows the
  finger), tapping the status bar right of x=150 opens the volume ring; any touch restarts the screen
  timer;
* the screen goes off after the time in 显示 › 熄屏时间 (30 s by default); the power key -- on the player's
  side, here in the case menu -- turns it on again (or shows the lock screen when 屏保 is on); a long press
  asks to shut down. Tapping the dark screen also wakes it here (the panel has no side key).

Settings use the firmware's own config names and defaults (``release/assets/default/raw/config.ini``) and
are kept in the settings file under ``device_config``; none of them reaches the audio path -- this is the
same player as the normal window (only 播放模式 is the player's own play order).
"""
from __future__ import annotations

import ctypes
import sys
import time

from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtGui import QPixmap

from .. import i18n
from ..i18n import tr
from . import awtk
from .awtk import make
from .screen import Layer, RowSource, SIZE

ROW = 120

# the stock config.ini (PLAYSET / SYSSET), plus the keys the firmware adds later
DEFAULTS = {
    "MAXVOLUME": 100, "MEMORYPLAY": 2, "BOOTVOLFLAG": 0, "BOOTVOLUME": 30, "DSDMODE": 0, "USBVOLMODE": 1,
    "GAPLESS": 1, "EQFLAG": 0, "EQMODE": 1, "REPLAYGAIN": 0, "FILTER": 3, "GAIN": 0, "BALANCE": 0,
    "JUMPFOLDER": 1, "USBDAC_CHARGE": 0, "FOLDERCOVER": 0,
    "AUTOSCAN": 0, "WIFI": 0, "BLUETOOTH": 0, "BTCODING": 0, "SYNCLINK": 0, "DLNA": 0, "AIRPLAY": 0, "USBMODE": 2,
    "OUTPUTWAY": 0, "CARMODE": 0, "COVERMODE": 0, "AUTOSYNCTIME": 1, "TIMEZONE": 22, "TIME24H": 1, "STANDBYMODE": 1,
    "SAVEPOWERMODE": 0, "LYRICMODE": 0, "LIGHTTIME": 1, "LIGHTNESS": 6, "SCREENSAVER": 0, "SSAVERTIMESHOW": 1,
    "DOUBLEWAKE": 0, "LOCKFLAG": 0, "LOCKMODE": 3, "THEME": 1, "CUSTOMFUNKEY": 0, "QUALITY": 3, "CDRIPPERSWITCH": 1,
    "ROTATEMODE": 0, "SLEEPFLAG": 0, "SLEEPTIME": 30, "STANDBYDEEP": 1, "SAVERTIME": 2,
    "HOMEMENU": None, "SORTMODE": 0, "ARTISTMODE": 0, "ALBUMTYPE": 0, "EQUSER": [6] * 10, "PLAYSPEED": 0,
}
LIGHTTIME = (15, 30, 60, 120, 300, 1800, 0)              # 显示 › 熄屏时间, seconds (0 = 常亮)
CUSTOM_FUNCTIONS = ("customfun_backhome", "customfun_rotate", "customfun_keylock", "customfun_lockscreen",
                    "customfun_gain", "customfun_fav")
GAINS = ("gain_low", "gain_middle", "gain_high")
KNOB_STEP = 6                                            # volume per notch (the case knob's)


def battery() -> tuple[int, bool] | None:
    """(percent, on mains) from Windows, or None without a battery (or not Windows)."""
    if sys.platform != "win32":
        return None

    class SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    s = SPS()
    try:
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            return None
    except Exception:
        return None
    if s.BatteryFlag == 128 or s.BatteryLifePercent > 100:
        return None
    return int(s.BatteryLifePercent), s.ACLineStatus == 1


class _Job(QRunnable):
    def __init__(self, fn, done):
        super().__init__()
        self.fn, self.done = fn, done

    def run(self):
        try:
            out = self.fn()
        except Exception:
            out = None
        self.done.emit(out)


class Relay(QObject):
    ready = Signal(object)


_RELAYS: set = set()


def background(fn, then) -> None:
    """``fn`` on the thread pool, ``then(result)`` back on the GUI thread."""
    relay = Relay()

    def finish(out):
        _RELAYS.discard(relay)
        then(out)
    relay.ready.connect(finish)
    _RELAYS.add(relay)
    QThreadPool.globalInstance().start(_Job(fn, relay.ready))


# ====================================================================== the device
class Device(QObject):
    """Window manager of the firmware: status bar, a stack of pages, dialogs; keys, knob, screen state."""

    def __init__(self, win, frame):
        super().__init__(frame)
        self.win, self.frame, self.screen = win, frame, frame.screen
        self.lang = awtk.strings("en_US" if i18n.LANG == "en" else "zh_CN")
        self.screen.lang = self.lang
        self.screen.raw = self._touch
        self.active = False
        self.config = dict(DEFAULTS)
        self.config.update(win.prefs.get("device_config") or {})
        self.stack: list = []
        self.dialogs: list = []
        self.volume_popup = None
        self.panel = None                    # the drop-down panel while it is out
        self.overlay = None                  # a full-screen page over everything (lock screen, power off)
        self.powered = True
        self._toast_timer = QTimer(self, singleShot=True, interval=1600, timeout=self._end_toast)
        self._toast = None
        self._press_y = None
        self._pull = None                    # drop-down drag in progress: (start y, panel y0)
        self._idle = QTimer(self, singleShot=True, timeout=self._idle_out)
        self._sleep = QTimer(self, singleShot=True, timeout=self._sleep_out)     # 电源管理 › 睡眠关机
        self.edit = None                     # the focused text field (search, names)
        from .dialogs import StatusBar       # (late: these import this module)
        from .library import HomePage
        self.bar = StatusBar(self)
        self.home = HomePage(self)
        self.stack.append(self.home)
        frame.typer = self._typed                   # the computer's keyboard types into the focused field
        frame.editing = lambda: self.live_edit() is not None
        frame.keyReleased.connect(self.key)
        frame.keyPressed.connect(self._key_down)
        frame.knobTurned.connect(self.knob)
        p = win.player
        p.stateChanged.connect(lambda _s: self.changed("state"))
        p.trackChanged.connect(lambda _t: self.changed("track"))
        p.positionChanged.connect(lambda _ms: self.changed("position"))
        p.durationChanged.connect(lambda _ms: self.changed("position"))
        p.volumeChanged.connect(self._volume)
        p.modeChanged.connect(lambda _m: self.changed("mode"))
        p.queueChanged.connect(lambda: self.changed("queue"))
        st = win.state
        st.tracksChanged.connect(lambda: self.changed("library"))
        st.favoritesChanged.connect(lambda: self.changed("favorites"))
        st.playlistsChanged.connect(lambda: self.changed("playlists"))
        if hasattr(st, "busyChanged"):
            st.busyChanged.connect(lambda _k: self.changed("scan"))
        self.scan_progress = None
        if hasattr(st, "jobProgress"):
            st.jobProgress.connect(self._scan_progress)
        self._clock = QTimer(self, interval=10000, timeout=lambda: self.active and self.bar.tick())
        self._clock.start()
        self.ab = None                       # AB repeat: [a ms, b ms | None]
        p.positionChanged.connect(self._ab_check)
        self.apply_config()
        self.sync()

    def s(self, key: str, fallback: str | None = None) -> str:
        return self.lang.get(key) or fallback or key

    def _scan_progress(self, p) -> None:
        self.scan_progress = p if isinstance(p, dict) else None
        self.changed("scan")

    # ---- settings
    def cfg(self, key: str):
        return self.config.get(key, DEFAULTS.get(key))

    def set_cfg(self, key: str, value) -> None:
        self.config[key] = value
        self.win.prefs.set("device_config", {k: v for k, v in self.config.items() if v != DEFAULTS.get(k)})
        self.apply_config()

    def reset_config(self) -> None:
        """恢复出厂设置: the stock settings back (the library, playlists and favourites are not touched)."""
        self.config = dict(DEFAULTS)
        self.win.prefs.set("device_config", {})
        self.apply_config()
        self.apply_sleep()
        self.home.rebuild()

    def apply_sleep(self) -> None:
        if int(self.cfg("SLEEPFLAG") or 0) and self.powered:
            self._sleep.start(max(1, int(self.cfg("SLEEPTIME") or 30)) * 60 * 1000)
        else:
            self._sleep.stop()

    def _sleep_out(self) -> None:
        self.set_cfg("SLEEPFLAG", 0)
        if self.powered:
            self._power_off()

    def apply_config(self) -> None:
        self.frame.set_rotation(int(self.cfg("ROTATEMODE") or 0))
        level = max(0, min(10, int(self.cfg("LIGHTNESS"))))
        self.screen.shade = int((10 - level) * 9)
        self.screen.update()
        self.bar.update()
        self.reset_idle()
        if self.panel is not None:
            self.panel.fill()
        for p in self.stack[-1:]:
            p.changed("config")

    # ---- pages
    def sync(self) -> None:
        layers = [self.stack[-1].layer, self.bar.layer] + [d.layer for d in self.dialogs]
        if self.panel is not None:
            layers.append(self.panel.layer)
        if self.volume_popup is not None:
            layers.append(self.volume_popup.layer)
        if self.overlay is not None:
            layers.append(self.overlay.layer)
        self.screen.layers = layers
        self.screen.update()

    def open(self, page) -> None:
        self.close_dialogs()
        self.close_panel()
        if self.stack:
            self.stack[-1].hidden()
        self.stack.append(page)
        page.shown()
        self.sync()

    def replace(self, page) -> None:
        """Open ``page`` in place of the current one (the firmware's navigator_switch_to)."""
        if len(self.stack) > 1:
            self.stack.pop().hidden()
        self.open(page)

    def back(self) -> None:
        if self.overlay is not None:
            return
        if self.volume_popup is not None:
            self._hide_volume()
        if self._toast is not None:
            self._end_toast()
        if self.panel is not None:
            self.close_panel()
            return
        if self.dialogs:
            self.dialogs[-1].close()
            return
        if self.edit is not None and self.edit.keyboard_open():
            self.edit.close_keyboard()
            return
        page = self.stack[-1]
        if getattr(page, "back_key", None) and page.back_key():
            return
        if len(self.stack) > 1:
            self.stack.pop().hidden()
            self.stack[-1].shown()
            self.sync()

    def home_page(self) -> None:
        self.close_dialogs()
        self.close_panel()
        while len(self.stack) > 1:
            self.stack.pop().hidden()
        self.stack[-1].shown()
        self.sync()

    def find_page(self, cls):
        return next((p for p in reversed(self.stack) if isinstance(p, cls)), None)

    def playing(self) -> None:
        from .playing import PlayingPage
        if self.win.player.current is None:
            return
        page = self.find_page(PlayingPage)
        if page is not None:
            while self.stack[-1] is not page:
                self.stack.pop().hidden()
            self.close_dialogs()
            page.shown()
            self.sync()
            return
        self.open(PlayingPage(self))

    def show_dialog(self, d) -> None:
        self.dialogs.append(d)
        self.screen.relayout(d.layer)
        self.sync()

    def close_dialog(self, d) -> None:
        if d in self.dialogs:
            self.dialogs.remove(d)
            self.sync()

    def close_dialogs(self) -> None:
        self.dialogs.clear()

    def toast(self, text: str, ms: int | None = None) -> None:
        from .dialogs import MessageDialog
        if self._toast is not None:
            self.close_dialog(self._toast)
        d = self._toast = MessageDialog(self, text)
        self.show_dialog(d)
        self._toast_timer.start(ms or max(1600, 70 * len(text)))

    def stoast(self, key: str) -> None:
        self.toast(self.s(key))

    def _end_toast(self) -> None:
        if self._toast is not None:
            self.close_dialog(self._toast)
            self._toast = None

    def confirm(self, text: str, yes, no=None) -> None:
        from .dialogs import ConfirmDialog
        self.show_dialog(ConfirmDialog(self, text, yes, no))

    # ---- the drop-down panel (follows the finger)
    def open_panel(self, y: float = 720) -> None:
        from .dialogs import StatusPanel
        if self.panel is None:
            self.panel = StatusPanel(self)
            self.screen.relayout(self.panel.layer)
        self.panel.set_offset(y)
        self.sync()

    def close_panel(self) -> None:
        if self.panel is not None:
            self.panel = None
            self.sync()

    # ---- activity
    def activate(self, on: bool) -> None:
        self.active = on
        self.screen.active = on and self.frame.screen_on
        if on:
            self.screen.gifs_run = self.win.player.state == "playing"
            self.bar.update()
            for p in self.stack:
                p.changed("library")
                p.changed("track")
            self.sync()
            self.reset_idle()
        else:
            self._idle.stop()
            if self.volume_popup is not None:
                self._hide_volume()

    def changed(self, what: str) -> None:
        if what == "state":
            self.screen.gifs_run = self.win.player.state == "playing"
        if not self.active:
            return
        if what in ("state", "track", "position"):
            self.bar.update()
        page = self.stack[-1]
        page.changed(what)
        for below in self.stack[:-1]:
            below.pending.add(what)
        for d in list(self.dialogs):
            d.changed(what)
        if self.overlay is not None:
            self.overlay.changed(what)
        if what == "track":
            self.ab = None
        self.screen.update()

    def covers_arrived(self) -> None:
        if not self.active:
            return
        for p in self.stack:
            p.changed("covers")
        self.screen.update()

    # ---- screen power
    @property
    def screen_on(self) -> bool:
        return self.frame.screen_on

    def reset_idle(self) -> None:
        if not self.active or not self.powered:
            return
        i = int(self.cfg("LIGHTTIME"))
        secs = LIGHTTIME[i] if 0 <= i < len(LIGHTTIME) else 0
        page = self.stack[-1] if self.stack else None
        if getattr(page, "lyric_mode", None) and page.lyric_mode():
            secs = 0                                       # 歌词模式: the lyrics keep the screen on
        if secs and self.screen_on:
            self._idle.start(secs * 1000)
        else:
            self._idle.stop()

    def _idle_out(self) -> None:
        if self.active and self.overlay is None:
            self.set_screen(False)

    def set_screen(self, on: bool) -> None:
        self.frame.set_screen_on(on)
        self.screen.active = on and self.active
        if on:
            self.reset_idle()
            self.screen.update()
        else:
            self._idle.stop()
            self.close_panel()

    def power_key(self, long: bool = False) -> None:
        """The side key: short = screen on/off (the lock screen first when 屏保 is on), long = shut down."""
        from .dialogs import PowerOffPage, SaverPage
        if not self.powered:                  # off: the player needs a long press; here any press starts it
            self._boot()
            return
        if long:
            self.set_screen(True)
            self.confirm(self.s("msg_poweroff"), self._power_off)
            return
        if self.screen_on:
            self.set_screen(False)
            return
        self.set_screen(True)
        if int(self.cfg("SCREENSAVER")) and self.overlay is None:
            self.overlay = SaverPage(self)
            self.screen.relayout(self.overlay.layer)
            self.sync()

    def _power_off(self) -> None:
        from .dialogs import PowerOffPage
        self.win.player.pause()
        self.close_dialogs()
        self.overlay = PowerOffPage(self)
        self.screen.relayout(self.overlay.layer)
        self.sync()
        QTimer.singleShot(2500, self._dark)

    def _dark(self) -> None:
        self.powered = False
        self.overlay = None
        self._sleep.stop()
        self.set_screen(False)

    def _boot(self) -> None:
        """Power on: the ONIX logo, then home."""
        from .dialogs import BootPage
        self.powered = True
        if int(self.cfg("BOOTVOLFLAG") or 0):                # 开机音量
            self.win.player.set_volume(min(int(self.cfg("MAXVOLUME")), int(self.cfg("BOOTVOLUME"))))
        self.apply_sleep()
        self.overlay = BootPage(self)
        self.screen.relayout(self.overlay.layer)
        self.home_page()
        self.set_screen(True)
        self.sync()
        QTimer.singleShot(1800, self._booted)

    def _booted(self) -> None:
        self.overlay = None
        self.sync()
        self.reset_idle()

    def close_overlay(self) -> None:
        self.overlay = None
        self.sync()

    # ---- keys and knob
    def _key_down(self, name: str) -> None:
        if name == "screen":                     # a tap on the dark glass: as the side key (on, or start up)
            if not self.powered or not self.screen_on:
                self.power_key()
            return
        self.reset_idle()

    def _locked(self, what: str) -> bool:
        """Key lock applies with the screen off: LOCKMODE 1 locks the volume, 2 the keys, 3 both."""
        if self.screen_on or not int(self.cfg("LOCKFLAG")):
            return False
        mode = int(self.cfg("LOCKMODE"))
        return mode == 3 or (mode == 1 and what == "volume") or (mode == 2 and what == "keys")

    def key(self, name: str, long: bool = False) -> None:
        if not self.powered:
            return
        self.reset_idle()
        p = self.win.player
        if name in ("prev", "next"):
            if self._locked("keys"):
                return
            if int(self.cfg("ROTATEMODE")) == 2:
                name = "next" if name == "prev" else "prev"
            if not p.queue:
                if self.screen_on:
                    self.stoast("msg_notsonglist")
                return
            (p.previous if name == "prev" else p.next)()
        elif name == "play":
            if self._locked("keys"):
                return
            if p.current is None:
                if self.screen_on:
                    self.stoast("msg_notsonglist")
                return
            p.toggle()
        elif name == "fn":
            self.custom_function()

    def custom_function(self) -> None:
        mode = int(self.cfg("CUSTOMFUNKEY"))
        if mode == 0:
            if self.screen_on:
                self.home_page()
        elif mode == 1:
            self.set_cfg("ROTATEMODE", (int(self.cfg("ROTATEMODE")) + 1) % 4)
        elif mode == 2:
            self.set_cfg("LOCKFLAG", 0 if int(self.cfg("LOCKFLAG")) else 1)
        elif mode == 3:
            self.power_key()
        elif mode == 4:
            self.set_cfg("GAIN", (int(self.cfg("GAIN")) + 1) % 3)
            if self.screen_on:
                self.stoast(GAINS[int(self.cfg("GAIN"))])
        elif mode == 5:
            t = self.win.player.current
            if t is None:
                if self.screen_on:
                    self.stoast("msg_notsonglist")
                return
            self.win.favorites.set([t["path"]], True)
            if self.screen_on:
                self.stoast("msg_favsuccess")

    def knob(self, notches: int) -> None:
        if not self.powered or self._locked("volume"):
            return
        self.reset_idle()
        p = self.win.player
        limit = int(self.cfg("MAXVOLUME"))
        p.set_volume(max(0, min(limit, p.volume + notches * KNOB_STEP)))

    def _volume(self, v: int) -> None:
        if not self.active:
            return
        self.bar.update()
        if self.panel is not None:
            self.panel.changed("volume")
        if not self.screen_on:
            return
        from .dialogs import VolumeDialog
        if self.volume_popup is None:
            self.volume_popup = VolumeDialog(self)
            self.screen.relayout(self.volume_popup.layer)
            self.sync()
        self.volume_popup.set(v)
        self.screen.update()

    def show_volume(self) -> None:
        self._volume(self.win.player.volume)

    def _hide_volume(self) -> None:
        self.volume_popup = None
        self.sync()

    # ---- AB repeat (the playing page's ⋯): seek back to A whenever the song passes B
    def _ab_check(self, ms: int) -> None:
        ab = self.ab
        if ab and ab[1] is not None and ms >= ab[1]:
            self.win.player.seek(ab[0])

    # ---- window-level touch (on_wm_tsdown / tsmove / tsup)
    def _touch(self, kind: str, pos: QPointF) -> bool:
        if not self.powered:
            return True
        self.reset_idle()
        x, y = pos.x(), pos.y()
        if self.overlay is not None:
            return self.overlay.touch(kind, pos)
        if kind == "down":
            self._press_y = y
            self._pull = None
            if self.panel is not None and self.panel.offset >= 720 - 1:
                self._pull = ("panel", y, self.panel.offset)
            return False
        if kind == "move":
            if self._pull is not None:
                kind_, y0, off0 = self._pull
                dy = y - y0
                if kind_ == "panel" and dy > -12 and kind_ != "pulling":
                    return False
                self.panel.set_offset(max(0.0, min(720.0, off0 + dy)))
                self._pull = ("pulling", y0, off0)
                self.sync()
                return True
            if self._press_y is not None and self._press_y < 51 and y - self._press_y > 12 \
                    and self.panel is None and not self.dialogs:
                self.open_panel(max(60.0, y))
                self._pull = ("pulling", self._press_y, 0.0)
                self.panel.set_offset(y)
                return True
            return False
        if kind == "up":
            if self._pull is not None and self._pull[0] == "pulling":
                off = self.panel.offset if self.panel is not None else 0
                self._pull = None
                if self.panel is not None:
                    if off > 260:
                        self.panel.set_offset(720)
                        self.sync()
                    else:
                        self.close_panel()
                return True
            self._pull = None
            py, self._press_y = self._press_y, None
            if py is not None and py < 50 and y < 50 and x >= 150 and self.panel is None:
                self.show_volume()
                return True
            if self.panel is None and not self.dialogs and self.stack:
                return bool(self.stack[-1].touch(kind, pos))
        return False

    # ---- text fields
    def focus_edit(self, edit) -> None:
        self.edit = edit

    def live_edit(self):
        """The focused field, if its page or dialog is the one on top."""
        e = self.edit
        if e is None or not self.screen_on or not self.powered:
            return None
        top = self.stack[-1] if self.stack else None
        return e if e.page is top or e.page in self.dialogs else None

    def type_text(self, text: str) -> bool:
        """Physical keyboard input for the focused field (search, names)."""
        e = self.live_edit()
        if e is None:
            return False
        e.insert(text)
        return True

    def key_backspace(self) -> bool:
        e = self.live_edit()
        if e is None:
            return False
        e.backspace()
        return True

    def key_enter(self) -> bool:
        e = self.live_edit()
        if e is None:
            return False
        e.submit()
        return True

    def _typed(self, e) -> bool:
        """A key from the computer's keyboard while player mode has the focus."""
        from PySide6.QtCore import Qt
        k = e.key()
        if k == Qt.Key_Backspace:
            return self.key_backspace()
        if k in (Qt.Key_Return, Qt.Key_Enter):
            return self.key_enter()
        text = e.text()
        if text and text.isprintable() and not (e.modifiers() & (Qt.ControlModifier | Qt.AltModifier)):
            return self.type_text(text)
        return False

    # ---- library helpers
    @property
    def view(self):
        return self.win.view

    def play(self, tracks: list[dict], start: int = 0) -> None:
        if not tracks:
            self.stoast("msg_notfile")
            return
        self.win.play_rows(list(tracks), start)

    def cover(self, album) -> QPixmap | None:
        if album is None:
            return None
        pix = self.win.cover_pixmap(album)
        if pix is None:
            self.win.request_covers([album])
        return pix

    def track_cover(self, t: dict) -> QPixmap | None:
        return self.cover(self.view.album_of(t)) if t else None


# ====================================================================== pages
def awtk_rect(n: awtk.Node) -> QRectF:
    return QRectF(n.x, n.y, n.w, n.h)


class Page:
    """A firmware window below the status bar (``fullscreen`` ones cover it)."""
    ui = ""
    fullscreen = False

    def __init__(self, dev: Device, title: str = ""):
        self.dev, self.win = dev, dev.win
        self.root = awtk.load_ui(self.ui)
        self.root.raw = (0, 0, 720, 720 if self.fullscreen else 670)
        self.layer = Layer(self.root, 0, 0 if self.fullscreen else 50, name=self.ui)
        self.title = title
        self.gif = None
        self.title_node = None
        self.pending: set[str] = set()
        for n in self.root.walk():
            if n.name == "image_wait":               # the firmware's loading spinner: nothing loads slowly here
                n.visible = False
        self.build()
        self.relayout()

    def s(self, key, fallback=None):
        return self.dev.s(key, fallback)

    def relayout(self) -> None:
        awtk.layout(self.root, 720, self.root.raw[3])

    def build(self) -> None:
        pass

    def shown(self) -> None:
        for what in sorted(self.pending - {"position"}):
            self.changed(what)
        self.pending.clear()
        self.changed("track")

    def hidden(self) -> None:
        pass

    def changed(self, what: str) -> None:
        if what in ("track", "state") and self.gif is not None:
            self.gif.visible = self.win.player.current is not None

    def touch(self, kind, pos) -> bool:
        return False

    # ---- the firmware's navigation bar
    def navbar(self, title: str, buttons=(), title_w: float | None = None, parent=None) -> awtk.Node:
        """Return (0..105), the title, buttons right-aligned before the "now playing" GIF at 622 (each
        ``(style, callback)`` 88 wide), as the firmware's list pages build it."""
        bar = parent or self.root.find("navigationbar_view")
        bar.children = [c for c in bar.children if c.name not in ("btn_return", "scrlabel_title", "gif_returnplay")
                        and not c.name.startswith("navbtn")]
        ret = make("button", (0, 0, 105, 82), "btn_return", "navbar_btnreturn_style", parent=bar)
        ret.on_click = self.dev.back
        right = 622 - 88 * len(buttons)
        w = title_w if title_w is not None else right - 118
        self.title_node = make("hscroll_label", (118, 0, w, 82), "scrlabel_title", "msg_leftwhite36_style",
                               parent=bar, loop="true", text=title)
        self.nav_buttons = []
        for i, (style, fn) in enumerate(buttons):
            b = make("button", (right + 88 * i, 0, 88, 82), f"navbtn{i}", style, parent=bar)
            b.on_click = fn
            self.nav_buttons.append(b)
        self.gif = make("gif", (622, 0, 88, 82), "gif_returnplay", parent=bar, image="playing_gif")
        self.gif.on_click = self.dev.playing
        self.gif.visible = self.win.player.current is not None
        return bar

    def scroller(self, client: str, bar: str = "scroll_bar_m") -> awtk.Node:
        node = self.root[client]
        node.bar = node.parent.find(bar) if node.parent is not None else None
        return node


class Dialog:
    ui = ""
    passive = False

    def __init__(self, dev: Device):
        self.dev, self.win = dev, dev.win
        self.root = awtk.load_ui(self.ui, theme="default") if self.ui else make("dialog", (0, 0, 600, 360), "dialog")
        self.layer = Layer(self.root, 0, 0, modal=not self.passive, dim=0 if self.passive else 100,
                           outside=self.close, name=self.ui, passive=self.passive)
        self.build()
        awtk.layout(self.root, 720, 720)

    def s(self, key, fallback=None):
        return self.dev.s(key, fallback)

    def build(self) -> None:
        pass

    def close(self) -> None:
        self.dev.close_dialog(self)

    def changed(self, what: str) -> None:
        pass


# ====================================================================== rows (the firmware's table rows)
def row_base(w: float, h: float = ROW, style: str = "btn_tablerow_style") -> awtk.Node:
    return make("list_item", (0, 0, w, h), "row", style)


def label(parent, rect, text, size=32, color=(255, 255, 255, 255), align=2, name="label", kind="hscroll_label",
          **props) -> awtk.Node:
    n = make(kind, rect, name, parent=parent, text=text, only_focus="true", **props)
    n.inline["normal"] = {"font_size": size, "text_color": color, "text_align_h": align}
    return n


GREY = (170, 170, 170, 255)
GOLD = (202, 180, 135, 255)


def check_box(row: awtk.Node, on: bool) -> None:
    make("image", (30, 36, 48, 48), "img_choice", parent=row, draw_type="center",
         image="mulselected" if on else "unselect")


def right_arrow(row: awtk.Node, x: float = 642) -> awtk.Node:
    return make("button", (x, (row.h - 48) / 2, 48, 48), "btn_right", "btn_rightarrow_style", parent=row)


__all__ = ["Device", "Page", "Dialog", "row_base", "label", "check_box", "right_arrow", "background", "battery",
           "RowSource", "ROW", "GREY", "GOLD", "DEFAULTS", "awtk_rect", "SIZE"]
