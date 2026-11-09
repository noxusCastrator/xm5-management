"""播放设置 (``playset_page``, 14 items) and 系统设置 (``sysset_page``, 15 items) with their pages, in the
firmware's order (``get_playsetset_showinfo`` / ``get_sysset_showinfo``) and with its option lists
(``get_showlabelname``).

Values are kept under the firmware's config names (core.DEFAULTS). What the desktop can honour it does:
播放模式 is the player's own order, 音量限制 caps the knob, 屏幕旋转 / 背光亮度 / 熄屏时间 / 屏保 / 歌词模式 /
按键锁 / 自定义功能键 / 车载模式-less ones drive the device, 睡眠关机 turns the device off, 开机音量 applies at
power-on. The sound-path ones (增益, 滤波器, EQ, DSD, 平衡, 无缝, 回放增益 ...) are remembered only: the
desktop's sound goes through mpv unchanged. Network, cards, firmware update: the player's own messages.
"""
from __future__ import annotations

import re
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor

from .. import i18n
from ..i18n import tr
from . import awtk
from .awtk import make
from .core import CUSTOM_FUNCTIONS, DEFAULTS, GAINS, GREY, Dialog, Page, RowSource, label

ROW_H = 108
PLAY_MODES = ("order", "repeat_all", "repeat_one", "shuffle")
MODE_KEY = {"order": "playmode_listplay", "repeat_all": "playmode_repeatall", "repeat_one": "playmode_repeattrack",
            "shuffle": "playmode_shuffle"}
FILTERS = ("filter_miniphase", "filter_linearapodizing", "filter_linearfast", "filter_linearslow",
           "filter_minifast", "filter_minislow", "filter_minidispersion")
DSD = ("dsdmode_d2p", "dsdmode_dop", "dsdmode_native")
USBVOL = ("usbvol_fixed", "usbvol_adjust")
MEMPLAY = ("memplay_off", "memplay_track", "memplay_location")
REPLAYGAIN = ("memplay_off", "replaygain_track", "album")
LIGHTTIMES = ("lighttime_15s", "lighttime_30s", "lighttime_1min", "lighttime_2min", "lighttime_5min",
              "lighttime_30min", "lighttime_off")
LANGUAGES = ("lan_simplified", "lan_traditional", "lan_english", "lan_japanese", "lan_korean", "lan_french",
             "lan_german", "lan_italian", "lan_spanish", "lan_russian", "lan_polish", "lan_ukrainian", "lan_thai")
KEYLOCK = ((3, "keylock_all"), (2, "keylock_key"), (1, "keylock_volume"))
STANDBY_TIMES = ("standbyoff_1min", "standbyoff_3min", "standbyoff_5min", "standbyoff_10min")
EQ_PRESETS = ("eq_custom1", "eq_custom2", "eq_custom3", "eq_pop", "eq_rock", "eq_dance", "eq_blues", "eq_metal",
              "eq_vocal", "eq_classical", "eq_jazz")


# ====================================================================== rows
def setting_row(w: float, text: str, icon: str | None = None) -> awtk.Node:
    r = make("list_item", (0, 0, w, ROW_H), "row", "list_itemplaylist_style")
    x = 30
    if icon:
        make("image", (20, 14, 80, 80), "img_icon", parent=r, image=icon, draw_type="center")
        x = 120
    make("hscroll_label", (x, 0, 560 - x, ROW_H), "label", "srlabel_leftwhite32_style", parent=r, text=text,
         only_focus="true", ellipses="true")
    return r


def add_switch(r: awtk.Node, on: bool) -> None:
    make("image", (r.w - 113 - 20, (ROW_H - 52) / 2, 113, 52), "img_switch", parent=r,
         image="switch_on" if on else "switch_off")


def add_arrow(r: awtk.Node) -> None:
    make("button", (r.w - 68, (ROW_H - 48) / 2, 48, 48), "btn_right", "btn_rightarrow_style", parent=r)


def add_value(r: awtk.Node, text: str) -> None:
    label(r, (r.w - 68 - 250, 0, 240, ROW_H), text, 28, GREY, align=3, name="value", kind="label")


def add_radio(r: awtk.Node, on: bool) -> None:
    make("image", (r.w - 68, (ROW_H - 48) / 2, 48, 48), "image_choice", parent=r, draw_type="center",
         image="select" if on else "unselect")


# ====================================================================== page kinds
class MenuPage(Page):
    """A firmware settings list: icon, name, then an arrow, a switch or the value."""
    ui = "systemset/display_page"
    title_key = ""

    def __init__(self, dev):
        super().__init__(dev, dev.s(self.title_key))

    def build(self) -> None:
        self.navbar(self.title)
        self.list = self.scroller("scroll_view")
        self.entries = self.make_entries()
        self.list.rows = RowSource(len(self.entries), ROW_H, self.row)

    def make_entries(self) -> list[dict]:
        return []

    def row(self, i: int) -> awtk.Node:
        e = self.entries[i]
        r = setting_row(self.list.w, self.s(e["key"]), e.get("icon"))
        if "switch" in e:
            add_switch(r, bool(int(self.dev.cfg(e["switch"]) or 0)))
            r.on_click = lambda: self.flip(e)
        else:
            if e.get("value"):
                add_value(r, e["value"]())
            add_arrow(r)
            r.on_click = e.get("open") or (lambda: None)
        return r

    def flip(self, e: dict) -> None:
        key = e["switch"]
        on = 0 if int(self.dev.cfg(key) or 0) else 1
        if e.get("confirm") and on:
            e["confirm"](lambda: self._set(key, on))
            return
        self._set(key, on)

    def _set(self, key: str, v) -> None:
        self.dev.set_cfg(key, v)
        self.refresh()

    def refresh(self) -> None:
        self.list.rows.reset()
        self.dev.screen.update()

    def shown(self) -> None:
        super().shown()
        if hasattr(self, "list"):
            self.refresh()

    def changed(self, what: str) -> None:
        super().changed(what)
        if what in ("mode", "config"):
            self.refresh()


class OptionsPage(Page):
    """playsetclass_page and the systemset radio lists: one choice, ticked."""
    ui = "playset/playsetclass_page"

    def __init__(self, dev, title: str, options, get, put, note: str = ""):
        self.options, self.get, self.put, self.note = list(options), get, put, note
        super().__init__(dev, title)

    def build(self) -> None:
        self.navbar(self.title)
        self.list = self.scroller("scroll_view")
        extra = 1 if self.note else 0
        self.list.rows = RowSource(len(self.options) + extra, ROW_H, self.row)

    def row(self, i: int) -> awtk.Node:
        if i >= len(self.options):
            r = make("list_item", (0, 0, self.list.w, ROW_H), "row", "list_notitem_style")
            label(r, (30, 0, self.list.w - 60, ROW_H), self.note, 26, GREY, kind="label", word_wrap="true")
            return r
        value, text = self.options[i]
        r = make("list_item", (0, 0, self.list.w, ROW_H), "row", "list_itemplaylist_style")
        make("hscroll_label", (30, 0, self.list.w - 130, ROW_H), "label", "srlabel_leftwhite32_style", parent=r,
             text=text, only_focus="true", ellipses="true")
        add_radio(r, value == self.get())
        r.on_click = lambda: self.pick(value)
        return r

    def pick(self, value) -> None:
        self.put(value)
        self.list.rows.reset()
        self.dev.screen.update()


def cfg_options(dev, title_key: str, cfg: str, keys, values=None, note: str = "") -> OptionsPage:
    values = list(values) if values is not None else list(range(len(keys)))
    return OptionsPage(dev, dev.s(title_key), [(v, dev.s(k)) for v, k in zip(values, keys)],
                       lambda: int(dev.cfg(cfg) or 0), lambda v: dev.set_cfg(cfg, v), note)


# ====================================================================== 播放设置
class PlaySettingsPage(MenuPage):
    ui = "playset/playset_page"
    title_key = "playset"

    def make_entries(self) -> list[dict]:
        d = self.dev
        o = lambda title, cfg, keys, values=None: lambda: d.open(cfg_options(d, title, cfg, keys, values))
        return [
            {"key": "playset_gain", "icon": "playset_gain", "open": o("playset_gain", "GAIN", GAINS)},
            {"key": "playset_filter", "icon": "playset_filter", "open": o("playset_filter", "FILTER", FILTERS)},
            {"key": "playset_eq", "icon": "playset_eq", "open": lambda: d.open(EqualizerPage(d))},
            {"key": "playset_playmode", "icon": "playset_playmode", "open": self.play_mode},
            {"key": "playset_maxvol", "icon": "playset_maxvol", "open": lambda: d.open(MaxVolPage(d))},
            {"key": "playset_bootvol", "icon": "playset_bootvol", "open": lambda: d.open(BootVolPage(d))},
            {"key": "playset_dsdoutput", "icon": "playset_dsdoutput", "open": o("playset_dsdoutput", "DSDMODE", DSD)},
            {"key": "playset_usbaudiovol", "icon": "playset_usbaudiovol",
             "open": o("playset_usbaudiovol", "USBVOLMODE", USBVOL)},
            {"key": "playset_memplay", "icon": "playset_memplay", "open": o("playset_memplay", "MEMORYPLAY", MEMPLAY)},
            {"key": "playset_gapless", "icon": "playset_gapless", "switch": "GAPLESS"},
            {"key": "playset_replaygain", "icon": "playset_replaygain",
             "open": o("playset_replaygain", "REPLAYGAIN", REPLAYGAIN)},
            {"key": "playset_balance", "icon": "playset_balance", "open": lambda: d.open(BalancePage(d))},
            {"key": "playset_foldercover", "icon": "playset_foldercover", "switch": "FOLDERCOVER"},
            {"key": "playset_folderjump", "icon": "playset_folderjump", "switch": "JUMPFOLDER"},
        ]

    def play_mode(self) -> None:
        p = self.win.player
        self.dev.open(OptionsPage(self.dev, self.s("playset_playmode"),
                                  [(m, self.s(MODE_KEY[m])) for m in PLAY_MODES], lambda: p.mode, p.set_mode))


class SliderPage(Page):
    """maxvol_page / balance_page / backlight_page: the value, the slider, − and +."""
    ui = "playset/maxvol_page"
    slider = "vol_slider"
    value_label = "label_vol"
    lo, hi = 0, 100

    def __init__(self, dev, title: str):
        super().__init__(dev, title)

    def build(self) -> None:
        self.navbar(self.title)
        r = self.root
        sl = r[self.slider]
        sl.min, sl.max = self.lo, self.hi
        sl.on_change = lambda v, final: self.set(round(v))
        for name, step in (("btn_dec", -1), ("btn_add", 1)):
            b = r[name]
            b.text = ""
            b.inline["normal"] = dict(b.inline.get("normal") or {}, bg_color=(0, 0, 0, 0), border_color=(0, 0, 0, 0),
                                      icon="dec" if step < 0 else "add")
            b.inline["pressed"] = dict(b.inline.get("pressed") or {}, bg_color=(0, 0, 0, 0),
                                       border_color=(0, 0, 0, 0), icon="dec_down" if step < 0 else "add_down")
            b.on_click = lambda s=step: self.set(self.get() + s)
            b.on_long = lambda s=step: self.set(self.get() + 10 * s)
        self.fill()

    def get(self) -> int:
        return 0

    def put(self, v: int) -> None:
        pass

    def text(self, v: int) -> str:
        return str(v)

    def set(self, v: int) -> None:
        v = max(self.lo, min(self.hi, int(v)))
        self.put(v)
        self.fill()

    def fill(self) -> None:
        r = self.root
        v = self.get()
        r[self.slider].value = v
        r[self.value_label].text = self.text(v)
        self.dev.screen.update()


class MaxVolPage(SliderPage):
    """音量限制: the knob stops here (and a louder volume comes down to it)."""

    def __init__(self, dev):
        super().__init__(dev, dev.s("playset_maxvol"))

    def get(self) -> int:
        return int(self.dev.cfg("MAXVOLUME"))

    def put(self, v: int) -> None:
        v = max(1, v)
        self.dev.set_cfg("MAXVOLUME", v)
        p = self.win.player
        if p.volume > v:
            p.set_volume(v)

    def text(self, v: int) -> str:
        return str(v)


class BootVolPage(SliderPage):
    """开机音量: on (the switch in the bar) = power on at this volume; off = the last volume."""

    def __init__(self, dev):
        super().__init__(dev, dev.s("playset_bootvol"))

    def build(self) -> None:
        super().build()
        bar = self.root.find("navigationbar_view")
        self.title_node.raw = (118, 0, 374, 82)
        sw = make("image", (492, 0, 113, 82), "img_switch", parent=bar, draw_type="center")
        sw.on_click = self.flip
        self.switch = sw
        self.fill()

    def flip(self) -> None:
        self.dev.set_cfg("BOOTVOLFLAG", 0 if int(self.dev.cfg("BOOTVOLFLAG")) else 1)
        self.fill()

    def get(self) -> int:
        return int(self.dev.cfg("BOOTVOLUME"))

    def put(self, v: int) -> None:
        self.dev.set_cfg("BOOTVOLUME", v)

    def text(self, v: int) -> str:
        return str(v)

    def fill(self) -> None:
        super().fill()
        on = bool(int(self.dev.cfg("BOOTVOLFLAG")))
        if hasattr(self, "switch"):
            self.switch.image = "switch_on" if on else "switch_off"
        self.root["view"].enabled = on
        for n in ("vol_slider", "btn_dec", "btn_add"):
            self.root[n].enabled = on


class BalancePage(SliderPage):
    ui = "playset/balance_page"
    lo, hi = -10, 10

    def __init__(self, dev):
        super().__init__(dev, dev.s("playset_balance"))

    def get(self) -> int:
        return int(self.dev.cfg("BALANCE"))

    def put(self, v: int) -> None:
        self.dev.set_cfg("BALANCE", v)

    def text(self, v: int) -> str:
        return "0" if v == 0 else (f"L{-v}" if v < 0 else f"R{v}")


class BacklightPage(SliderPage):
    """背光亮度: 0..10, the screen dims with it."""
    ui = "systemset/backlight_page"
    slider = "slider_backlight"
    value_label = "label_backlight"
    lo, hi = 0, 10

    def __init__(self, dev):
        super().__init__(dev, dev.s("display_backlight"))

    def get(self) -> int:
        return int(self.dev.cfg("LIGHTNESS"))

    def put(self, v: int) -> None:
        self.dev.set_cfg("LIGHTNESS", v)


class EqualizerPage(Page):
    """equalizer_page: the switch in the bar (EQ OFF / the ten bands), 预设 (preseteq_dialog) and 重置.
    Custom 1-3 are the user's curves; the stock presets' curves are not in the firmware's files, so they
    show flat here. Kept, not applied (mpv plays unchanged)."""
    ui = "playset/equalizer_page"

    def __init__(self, dev):
        super().__init__(dev, "EQ")

    def build(self) -> None:
        self.navbar(self.title)
        r = self.root
        bar = r.find("navigationbar_view")
        self.title_node.raw = (118, 0, 374, 82)
        self.switch = make("image", (492, 0, 113, 82), "img_switch", parent=bar, draw_type="center")
        self.switch.on_click = self.flip
        r["label_eqoff"].text = "EQ OFF"
        r["label_only48msg"].text = self.s("eq_only48kmsg")
        r["label_only48msg"].inline["normal"] = {"font_size": 26, "text_color": GREY, "text_align_h": 1}
        for k in range(10):
            sl = r[f"slider_{k}"]
            sl.props["vertical"] = "true"
            sl.on_change = lambda v, final, k=k: self.band(k, v)
        r["btn_preset"].on_click = lambda: self.dev.show_dialog(PresetDialog(self.dev, self))
        r["btn_reset"].text = self.s("eq_reset")
        r["btn_reset"].on_click = lambda: self.dev.confirm(self.s("eq_reset") + "?", self.reset)
        self.fill()

    def curve(self) -> list[int]:
        mode = int(self.dev.cfg("EQMODE"))
        if mode <= 3:
            user = self.dev.cfg("EQUSER") or [6] * 10
            if isinstance(user[0], list):
                return list(user[mode - 1]) if 1 <= mode <= len(user) else [6] * 10
            return list(user)
        return [6] * 10

    def fill(self) -> None:
        r = self.root
        on = bool(int(self.dev.cfg("EQFLAG")))
        self.switch.image = "switch_on" if on else "switch_off"
        r["view_off"].visible = not on
        r["view_on"].visible = on
        mode = max(1, min(11, int(self.dev.cfg("EQMODE"))))
        r["btn_preset"].text = self.s(EQ_PRESETS[mode - 1])
        custom = mode <= 3
        for k, v in enumerate(self.curve()):
            sl = r[f"slider_{k}"]
            sl.value = v
            sl.enabled = custom
        self.dev.screen.update()

    def flip(self) -> None:
        self.dev.set_cfg("EQFLAG", 0 if int(self.dev.cfg("EQFLAG")) else 1)
        self.fill()

    def _curves(self) -> list[list[int]]:
        user = self.dev.cfg("EQUSER") or [6] * 10
        if not isinstance(user[0], list):
            user = [list(user), [6] * 10, [6] * 10]
        return [list(c) for c in user]

    def band(self, k: int, v: float) -> None:
        mode = int(self.dev.cfg("EQMODE"))
        if mode > 3:
            return
        curves = self._curves()
        curves[mode - 1][k] = int(round(v))
        self.dev.set_cfg("EQUSER", curves)
        self.root[f"slider_{k}"].value = int(round(v))

    def preset(self, mode: int) -> None:
        self.dev.set_cfg("EQMODE", mode)
        self.fill()

    def reset(self) -> None:
        mode = int(self.dev.cfg("EQMODE"))
        if mode <= 3:
            curves = self._curves()
            curves[mode - 1] = [6] * 10
            self.dev.set_cfg("EQUSER", curves)
        self.fill()


class PresetDialog(Dialog):
    ui = "dialog/preseteq_dialog"

    def __init__(self, dev, page):
        self.page = page
        super().__init__(dev)

    def build(self) -> None:
        for k in range(11):
            b = self.root[f"button_{k + 1}"]
            b.on_click = lambda k=k: (self.close(), self.page.preset(k + 1))


# ====================================================================== 系统设置
class SystemSettingsPage(MenuPage):
    ui = "systemset/sysset_page"
    title_key = "sysset"

    def make_entries(self) -> list[dict]:
        d = self.dev
        return [
            {"key": "system_language", "icon": "system_language", "open": lambda: d.open(language_page(d))},
            {"key": "system_wifiset", "icon": "system_wifiset", "open": lambda: d.open(WirelessPage(d))},
            {"key": "system_netservice", "icon": "system_netservice", "open": lambda: d.open(NetServicePage(d))},
            {"key": "system_display", "icon": "system_display", "open": lambda: d.open(DisplayPage(d))},
            {"key": "system_powermanager", "icon": "system_powermanager", "open": lambda: d.open(PowerPage(d))},
            {"key": "system_time", "icon": "system_time", "open": lambda: d.open(DateTimePage(d))},
            {"key": "system_carmode", "icon": "system_carmode", "switch": "CARMODE"},
            {"key": "system_keylock", "icon": "system_keylock", "open": lambda: d.open(cfg_options(
                d, "system_keylock", "LOCKMODE", [k for _, k in KEYLOCK], [v for v, _ in KEYLOCK],
                note=d.s("keylock_info")))},
            {"key": "system_rotate", "icon": "system_rotate", "open": lambda: d.open(cfg_options(
                d, "system_rotate", "ROTATEMODE", ("rotate_0", "rotate_90", "rotate_180", "rotate_270")))},
            {"key": "system_custom", "icon": "system_custom", "open": lambda: d.open(cfg_options(
                d, "system_custom", "CUSTOMFUNKEY", CUSTOM_FUNCTIONS))},
            {"key": "system_update", "icon": "system_update", "open": lambda: d.open(UpdatePage(d))},
            {"key": "system_formatcard", "icon": "system_formatcard", "open": lambda: d.stoast("msg_nottf")},
            {"key": "system_reset", "icon": "system_reset", "open": lambda: d.open(ResetPage(d))},
            {"key": "system_about", "icon": "system_about", "open": lambda: d.open(AboutPage(d))},
            {"key": "system_prove", "icon": "system_prove", "open": lambda: d.open(ProvePage(d))},
        ]


def language_page(dev) -> OptionsPage:
    """语言: the desktop shows Chinese or English (switched in the normal window's 设置)."""
    current = "lan_english" if i18n.LANG == "en" else "lan_simplified"

    def put(key):
        if key != current:
            dev.toast(tr("界面语言在普通模式的「设置」页切换（简体中文 / English）"))
    return OptionsPage(dev, dev.s("system_language"), [(k, dev.s(k)) for k in LANGUAGES], lambda: current, put)


class WirelessPage(MenuPage):
    """无线设置: Wi-Fi, 蓝牙 (one or the other, as on the player), SyncLink."""
    ui = "systemset/wifiset_page"
    title_key = "system_wifiset"

    def make_entries(self) -> list[dict]:
        d = self.dev

        def wifi_on(go):
            if int(d.cfg("BLUETOOTH")):
                d.confirm(d.s("msg_wifi_open"), lambda: (d.set_cfg("BLUETOOTH", 0), go()))
            else:
                go()

        def bt_on(go):
            if int(d.cfg("WIFI")):
                d.confirm(d.s("msg_bt_open"), lambda: (d.set_cfg("WIFI", 0), go()))
            else:
                go()
        return [{"key": "wifiset_wifi", "icon": "wifiset_wifi", "switch": "WIFI", "confirm": wifi_on},
                {"key": "wifiset_bt", "icon": "wifiset_bt", "switch": "BLUETOOTH", "confirm": bt_on},
                {"key": "wifiset_synclink", "icon": "wifiset_synclink", "switch": "SYNCLINK"}]


class NetServicePage(MenuPage):
    ui = "systemset/netservice_page"
    title_key = "system_netservice"

    def make_entries(self) -> list[dict]:
        nope = lambda: self.dev.stoast("msg_notnetwork")
        return [{"key": "netservice_dlna", "icon": "netservice_dlna", "open": nope},
                {"key": "netservice_airplay", "icon": "netservice_airplay", "open": nope},
                {"key": "netservice_wifitrans", "icon": "netservice_wifitrans", "open": nope}]


class DisplayPage(MenuPage):
    ui = "systemset/display_page"
    title_key = "system_display"

    def make_entries(self) -> list[dict]:
        d = self.dev
        return [{"key": "display_lighttime", "open": lambda: d.open(cfg_options(d, "display_lighttime", "LIGHTTIME",
                                                                                 LIGHTTIMES)),
                 "value": lambda: d.s(LIGHTTIMES[int(d.cfg("LIGHTTIME")) % len(LIGHTTIMES)])},
                {"key": "display_backlight", "open": lambda: d.open(BacklightPage(d)),
                 "value": lambda: str(d.cfg("LIGHTNESS"))},
                {"key": "display_screenlock", "open": lambda: d.open(ScreenSaverPage(d))},
                {"key": "lighttime_lyricmode", "switch": "LYRICMODE"}]


class ScreenSaverPage(Page):
    """screensaver_page: 屏保 on/off (the bar's switch), 显示时间, about."""
    ui = "systemset/screensaver_page"

    def __init__(self, dev):
        super().__init__(dev, dev.s("display_screenlock"))

    def build(self) -> None:
        r = self.root
        self.navbar(self.title, title_w=374)
        bar = r.find("navigationbar_view")
        self.switch = make("image", (492, 0, 113, 82), "img_switch", parent=bar, draw_type="center")
        self.switch.on_click = lambda: self._flip("SCREENSAVER")
        r["scrlabel_timeshow"].text = tr("显示时间")
        r["item_timeshow"].on_click = lambda: self._flip("SSAVERTIMESHOW")
        r["scrlabel_about"].text = self.s("system_about")
        r["item_about"].on_click = lambda: self.dev.toast(tr("熄屏后按电源键（机身菜单 › 电源键）先显示屏保，左滑或上滑解锁。"),
                                                         3000)
        cov = r["view_covers"]
        cov.children.clear()
        img = make("image", (240, 20, 240, 240), "img_saver", parent=cov, image="screensaver_default",
                   draw_type="scale_auto")
        img.enabled = False
        self.fill()

    def _flip(self, key: str) -> None:
        self.dev.set_cfg(key, 0 if int(self.dev.cfg(key)) else 1)
        self.fill()

    def fill(self) -> None:
        self.switch.image = "switch_on" if int(self.dev.cfg("SCREENSAVER")) else "switch_off"
        self.root["imgtime_switch"].image = "switch_on" if int(self.dev.cfg("SSAVERTIMESHOW")) else "switch_off"
        self.dev.screen.update()


class PowerPage(MenuPage):
    ui = "systemset/powermanager_page"
    title_key = "system_powermanager"

    def make_entries(self) -> list[dict]:
        d = self.dev
        return [{"key": "powerm_deep_off", "open": lambda: d.open(StandbyPage(d))},
                {"key": "powerm_sleep", "open": lambda: d.open(SleepPage(d)),
                 "value": lambda: (f"{int(d.cfg('SLEEPTIME')) // 60:02d}:{int(d.cfg('SLEEPTIME')) % 60:02d}"
                                   if int(d.cfg("SLEEPFLAG")) else d.s("memplay_off"))}]


class StandbyPage(Page):
    """standbyoff_page: 省电关机 / 深度待机 and after how long (kept)."""
    ui = "systemset/standbyoff_page"

    def __init__(self, dev):
        super().__init__(dev, dev.s("powerm_deep_off"))

    def build(self) -> None:
        self.navbar(self.title)
        self.list = self.scroller("scroll_view") if self.root.find("scroll_view") else None
        host = self.list
        if host is None:
            host = make("scroll_view", (0, 82, 720, 588), "scroll_view", parent=self.root)
            self.list = host
        self.rows = [("mode", 0, "standbyoff_saver"), ("mode", 1, "standbyoff_deep")] + \
                    [("time", i, k) for i, k in enumerate(STANDBY_TIMES)] + [("info", 0, "standbyoff_deepinfo")]
        host.rows = RowSource(len(self.rows), ROW_H, self.row)

    def row(self, i: int) -> awtk.Node:
        kind, v, key = self.rows[i]
        w = self.list.w
        if kind == "info":
            r = make("list_item", (0, 0, w, ROW_H), "row", "list_notitem_style")
            label(r, (30, 0, w - 60, ROW_H), self.s(key), 24, GREY, kind="label", word_wrap="true")
            return r
        r = make("list_item", (0, 0, w, ROW_H), "row", "list_itemplaylist_style")
        make("hscroll_label", (30 if kind == "mode" else 70, 0, w - 160, ROW_H), "label",
             "srlabel_leftwhite32_style" if kind == "mode" else "srlabel_leftgray32_style", parent=r, text=self.s(key))
        cfg = "STANDBYMODE" if kind == "mode" else "SAVERTIME"
        add_radio(r, int(self.dev.cfg(cfg)) == v)
        r.on_click = lambda: (self.dev.set_cfg(cfg, v), self.list.rows.reset(), self.dev.screen.update())
        return r


class SleepPage(Page):
    """sleepshutdown_page: the switch, then hours : minutes on the two wheels (text_selector). When on,
    the device turns itself off after that long (the music stops)."""
    ui = "systemset/sleepshutdown_page"

    def __init__(self, dev):
        super().__init__(dev, dev.s("powerm_sleep"))

    def build(self) -> None:
        r = self.root
        self.navbar(self.title, title_w=374)
        bar = r.find("navigationbar_view")
        self.switch = make("image", (492, 0, 113, 82), "img_switch", parent=bar, draw_type="center")
        self.switch.on_click = self.flip
        r["label_hour"].text = tr("时")
        r["label_min"].text = tr("分")
        for name, top in (("tselector_hour", 23), ("tselector_min", 59)):
            sel = r[name]
            sel.max = top
            sel.paint_hook = self._paint_selector
            sel.wheel = lambda dy, n=sel: int(self.dev.cfg("SLEEPFLAG")) and self.step(n, -1 if dy > 0 else 1)
        self.fill()

    def minutes(self) -> int:
        return int(self.dev.cfg("SLEEPTIME"))

    def step(self, sel, d: int) -> None:
        total = self.minutes()
        h, m = divmod(total, 60)
        if sel.name == "tselector_hour":
            h = (h + d) % 24
        else:
            m = (m + d) % 60
        total = max(1, h * 60 + m)
        self.dev.set_cfg("SLEEPTIME", total)
        self.dev.apply_sleep()
        self.fill()

    def flip(self) -> None:
        self.dev.set_cfg("SLEEPFLAG", 0 if int(self.dev.cfg("SLEEPFLAG")) else 1)
        self.dev.apply_sleep()
        self.fill()

    def fill(self) -> None:
        r = self.root
        on = bool(int(self.dev.cfg("SLEEPFLAG")))
        self.switch.image = "switch_on" if on else "switch_off"
        h, m = divmod(self.minutes(), 60)
        r["tselector_hour"].value = h
        r["tselector_min"].value = m
        r["view"].enabled = on
        self.dev.screen.update()

    def _paint_selector(self, p, n, rect, st) -> bool:
        """Three rows: the one before (small, grey), the value (big), the one after."""
        top = int(n.max)
        v = int(n.value)
        on = bool(int(self.dev.cfg("SLEEPFLAG")))
        row = rect.height() / 3
        from .screen import font
        for k, d in enumerate((-1, 0, 1)):
            x = (v + d) % (top + 1)
            p.setFont(font(60 if d == 0 else 42))
            c = QColor(255, 255, 255) if d == 0 and on else QColor(110, 110, 110)
            p.setPen(c)
            p.drawText(QRectF(rect.x(), rect.y() + k * row, rect.width(), row), int(Qt.AlignCenter), f"{x:02d}")
        return True

    def touch(self, kind, pos) -> bool:
        """Tap the upper / lower third of a wheel to turn it (and the mouse wheel over it)."""
        if kind != "up" or not int(self.dev.cfg("SLEEPFLAG")):
            return False
        for name in ("tselector_hour", "tselector_min"):
            n = self.root[name]
            rr = self.dev.screen.node_rect(n)
            if rr is not None and rr.contains(pos):
                third = rr.height() / 3
                if pos.y() < rr.y() + third:
                    self.step(n, -1)
                elif pos.y() > rr.y() + 2 * third:
                    self.step(n, 1)
                return True
        return False


class DateTimePage(MenuPage):
    """日期与时间: the clock is the computer's; 24 小时制 switches the status bar's clock."""
    ui = "systemset/datetime_page"
    title_key = "system_time"

    def make_entries(self) -> list[dict]:
        d = self.dev
        follow = lambda: d.toast(tr("时间跟随这台电脑的时钟"))
        return [{"key": "timedate_auto", "switch": "AUTOSYNCTIME"},
                {"key": "timedate_manual", "open": follow, "value": lambda: time.strftime("%Y-%m-%d %H:%M")},
                {"key": "timezone_select", "open": lambda: d.open(TimezonePage(d)),
                 "value": lambda: time.strftime("%z")},
                {"key": "timedate_24h", "switch": "TIME24H"}]


class TimezonePage(OptionsPage):
    def __init__(self, dev):
        def offset(k):
            m = re.search(r"GMT([+-])(\d+):(\d+)", dev.s(k))
            return (1 if m.group(1) == "+" else -1) * (int(m.group(2)) * 60 + int(m.group(3))) if m else 0
        zones = sorted((k for k in dev.lang if k.startswith("timezone_gmt")), key=lambda k: (offset(k), k))
        super().__init__(dev, dev.s("timezone_select"), [(i, dev.s(k)) for i, k in enumerate(zones)],
                         lambda: int(dev.cfg("TIMEZONE")), lambda v: dev.set_cfg("TIMEZONE", v))


class UpdatePage(MenuPage):
    ui = "systemset/fwupdate_page"
    title_key = "system_update"

    def make_entries(self) -> list[dict]:
        return [{"key": "update_online", "open": lambda: self.dev.stoast("msg_notnetwork")},
                {"key": "update_tf", "open": lambda: self.dev.stoast("msg_nottf")}]


class ResetPage(MenuPage):
    """恢复出厂设置: both set the player-mode settings back to the stock ones (nothing else is touched)."""
    ui = "systemset/reset_page"
    title_key = "system_reset"

    def make_entries(self) -> list[dict]:
        return [{"key": "reset_initset", "open": self.reset}, {"key": "reset_factory", "open": self.reset}]

    def reset(self) -> None:
        def go():
            self.dev.reset_config()
            self.dev.toast(tr("已恢复默认设置（只影响播放器模式的设置，歌曲与歌单不受影响）"), 2600)
        self.dev.confirm(self.s("system_reset") + "?", go)


class AboutPage(Page):
    ui = "systemset/about_page"

    def __init__(self, dev):
        super().__init__(dev, dev.s("system_about"))

    def build(self) -> None:
        from ... import APP_NAME, __version__
        r = self.root
        self.navbar(self.title)
        view = self.dev.view
        vals = {"label_model": (self.s("about_model"), "XM5"),
                "label_version": (self.s("about_version"), f"{APP_NAME} {__version__}"),
                "label_tfcap": (self.s("allmusic"), tr("{0} 首", len(view.tracks))),
                "label_wifimac": (self.s("album"), str(len(view.albums))),
                "label_wifiip": (self.s("playlist"), str(len(self.win.state.playlist_list()))),
                "label_tfremain": (self.s("fav"), str(len(self.win.favorites.paths()))),
                "label_sn": (self.s("about_sn"), "")}
        for name, (k, v) in vals.items():
            n = r.find(name)
            if n is None:
                continue
            n.text = k
            n.inline["normal"] = {"font_size": 32, "text_color": (255, 255, 255, 255), "text_align_h": 2}
            val = r.find(name + "val")
            if val is not None:
                val.text = v
                val.inline["normal"] = {"font_size": 32, "text_color": (255, 219, 149, 255), "text_align_h": 3}
            if not v:
                n.parent.visible = False
        self.relayout()


class ProvePage(Page):
    ui = "systemset/techprove_page"

    def __init__(self, dev):
        super().__init__(dev, dev.s("system_prove"))

    def build(self) -> None:
        self.navbar(self.title)


__all__ = ["PlaySettingsPage", "SystemSettingsPage", "OptionsPage", "MenuPage", "EqualizerPage", "MaxVolPage",
           "BalancePage", "BacklightPage", "SleepPage", "LIGHTTIMES", "FILTERS"]
