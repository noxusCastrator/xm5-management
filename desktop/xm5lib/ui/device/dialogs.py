"""The firmware's dialogs, the status bar and its drop-down panel, the lock / power-off screens and the
on-screen keyboard (kb_default_t26)."""
from __future__ import annotations

import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetricsF, QPen

from ..i18n import tr
from . import awtk
from .awtk import make
from .core import GREY, Dialog, Page, battery, label
from .screen import Layer, font

# ====================================================================== status bar
VOL_IMAGES = ("bar_vol0", "bar_vol1", "bar_vol2", "bar_vol3", "bar_vol4")


def fill_bar(r: awtk.Node, dev) -> None:
    """The status bar's values (the bar, and its copy at the top of the drop-down panel)."""
    p = dev.win.player
    r["img_playstate"].image = "bar_play" if p.state == "playing" else "bar_pause"
    r["img_playstate"].visible = p.current is not None
    v = p.volume
    r["img_vol"].image = VOL_IMAGES[0 if v == 0 else min(4, 1 + v * 4 // 101)]
    r["label_vol"].text = str(v)
    r["label_eq"].visible = bool(int(dev.cfg("EQFLAG")))
    r["img_lockkey"].visible = bool(int(dev.cfg("LOCKFLAG")))
    r["img_bt"].visible = bool(int(dev.cfg("BLUETOOTH")))
    r["img_wifi"].visible = bool(int(dev.cfg("WIFI")))
    r["img_wifi"].image = "bar_wifi0"
    r["image_synclink"].visible = bool(int(dev.cfg("SYNCLINK")))
    r["digit_clock"].props["format"] = "hh:mm" if int(dev.cfg("TIME24H")) else "h12:mm"
    b = battery()
    r["label_battery"].visible = r["img_battery"].visible = b is not None
    if b is not None:
        r["label_battery"].text = f"{b[0]}%"
        r["img_battery"].image = "bar_charge" if b[1] else "bar_battery"
        r["progress_battery"].value = 0 if b[1] else b[0]


class StatusBar:
    def __init__(self, dev):
        self.dev = dev
        self.root = awtk.load_ui("system_bar", theme="default")
        self.root.raw = (0, 0, 720, 50)
        self.layer = Layer(self.root, 0, 0, name="system_bar")
        dev.screen.relayout(self.layer)
        self.update()

    def update(self) -> None:
        fill_bar(self.root, self.dev)
        awtk.layout(self.root, 720, 50)

    def tick(self) -> None:
        self.update()
        self.dev.screen.update(QRectF(0, 0, 720, 50))


class StatusPanel(Dialog):
    """The drop-down panel: it follows the finger down from the status bar and back up. WiFi / Bluetooth /
    key lock / gain / USB mode / output and the brightness, 播放设置 and 系统设置. WiFi and Bluetooth exclude
    each other as on the player (it asks first); the brightness dims the screen; the rest are remembered."""
    ui = "dialog/statusbar_dialog"

    def build(self) -> None:
        r = self.root
        r.props["self_layout"] = "default(x=0,y=0,w=100%,h=100%)"
        self.layer.dim = 0
        self.layer.modal = True
        self.layer.outside = None
        self.offset = 0.0
        r["img_wifiswitch"].on_click = self.wifi
        r["img_btswitch"].on_click = self.bt
        r["img_lock"].on_click = lambda: self._flip("LOCKFLAG", 2)
        r["img_gain"].on_click = lambda: self._flip("GAIN", 3)
        r["img_usbmode"].on_click = lambda: self._flip("USBMODE", 3)
        r["img_po"].on_click = lambda: self._flip("OUTPUTWAY", 2)
        from .settings import PlaySettingsPage, SystemSettingsPage
        r["img_playset"].on_click = lambda: (self.dev.close_panel(), self.dev.open(PlaySettingsPage(self.dev)))
        r["img_sysset"].on_click = lambda: (self.dev.close_panel(), self.dev.open(SystemSettingsPage(self.dev)))
        for n in ("img_playset", "img_sysset"):
            r[n].props["image_pressed"] = r[n].image + "_down"
        sl = r["slider_backlight"]
        sl.max = 10
        sl.on_change = self.brightness
        self.fill()

    def set_offset(self, y: float) -> None:
        """y: where the panel's bottom edge is (0 = up out of sight, 720 = fully down)."""
        self.offset = y
        self.layer.y = y - 720

    def fill(self) -> None:
        r, d = self.root, self.dev
        fill_bar(r["view_systembar"], d)
        r["img_wifiswitch"].image = "drop_wifiopen" if int(d.cfg("WIFI")) else "drop_wifi"
        r["img_btswitch"].image = "drop_btopen" if int(d.cfg("BLUETOOTH")) else "drop_bt"
        r["img_lock"].image = "drop_keylockopen" if int(d.cfg("LOCKFLAG")) else "drop_keylock"
        g = int(d.cfg("GAIN")) % 3
        r["img_gain"].image = ("drop_lowgain", "drop_mediumgain", "drop_highgain")[g]
        u = int(d.cfg("USBMODE")) % 3
        r["img_usbmode"].image = ("drop_usbstorage", "drop_usbdac", "drop_usbaudio")[u]
        o = int(d.cfg("OUTPUTWAY")) % 2
        r["img_po"].image = ("drop_po", "drop_lo")[o]
        texts = {"label_wifi": "WiFi", "label_bt": tr("蓝牙"), "label_lock": self.s("customfun_keylock"),
                 "label_gain": self.s(("gain_low", "gain_middle", "gain_high")[g]),
                 "label_usbmode": self.s(("usbmode_storge", "usbmode_dac", "usbmode_audio")[u]),
                 "label_outputway": ("PO", "LO")[o], "label_playset": self.s("playset"),
                 "label_sysset": self.s("sysset")}
        for name, text in texts.items():
            r[name].text = text
        r["slider_backlight"].value = float(d.cfg("LIGHTNESS"))
        awtk.layout(r, 720, 720)

    def _flip(self, key: str, n: int) -> None:
        if key == "OUTPUTWAY" and not int(self.dev.cfg(key)):
            self.dev.confirm(self.s("msg_lowarn"), lambda: self._set(key, 1))
            return
        self._set(key, (int(self.dev.cfg(key)) + 1) % n)

    def _set(self, key: str, v) -> None:
        self.dev.set_cfg(key, v)
        self.fill()
        self.dev.screen.update()

    def wifi(self) -> None:
        d = self.dev
        if int(d.cfg("WIFI")):
            self._set("WIFI", 0)
        elif int(d.cfg("BLUETOOTH")):
            d.confirm(self.s("msg_wifi_open"), lambda: (d.set_cfg("BLUETOOTH", 0), self._set("WIFI", 1)))
        else:
            self._set("WIFI", 1)

    def bt(self) -> None:
        d = self.dev
        if int(d.cfg("BLUETOOTH")):
            self._set("BLUETOOTH", 0)
        elif int(d.cfg("WIFI")):
            d.confirm(self.s("msg_bt_open"), lambda: (d.set_cfg("WIFI", 0), self._set("BLUETOOTH", 1)))
        else:
            self._set("BLUETOOTH", 1)
            d.stoast("msg_btinit_run")

    def brightness(self, value: float, final: bool) -> None:
        self.root["slider_backlight"].value = round(value)
        if final or True:
            self.dev.set_cfg("LIGHTNESS", int(round(value)))

    def changed(self, what: str) -> None:
        fill_bar(self.root["view_systembar"], self.dev)


# ====================================================================== simple dialogs
class MessageDialog(Dialog):
    """msginfo_dialog: a short message, on its own after a moment."""
    ui = "dialog/msginfo_dialog"
    passive = True

    def __init__(self, dev, text):
        self.text = text
        super().__init__(dev)

    def build(self) -> None:
        r = self.root
        lines = max(1, round(QFontMetricsF(font(32)).horizontalAdvance(self.text) / 520 + .49))
        h = max(160, 80 + 44 * lines)
        r.props["self_layout"] = f"default(x=c,y=m,w=600,h={h})"
        lab = make("label", (30, 0, 540, h), "label_msg", parent=r, text=self.text, word_wrap="true")
        lab.inline["normal"] = {"font_size": 32, "text_color": (255, 255, 255, 255), "text_align_h": 1}


class ConfirmDialog(Dialog):
    """confirminfo_dialog: the question above, 取消 | 确定 below."""
    ui = "dialog/confirminfo_dialog"

    def __init__(self, dev, text, yes, no=None):
        self.text, self.yes, self.no = text, yes, no
        super().__init__(dev)

    def build(self) -> None:
        r = self.root
        make("label", (30, 0, 540, 225), "label_msg", parent=r, text=self.text, word_wrap="true").inline["normal"] = {
            "font_size": 32, "text_color": (255, 255, 255, 255), "text_align_h": 1}
        r["btn_cancel"].text = self.s("msg_no")
        r["btn_enter"].text = self.s("msg_yes")
        r["btn_cancel"].on_click = self._no
        r["btn_enter"].on_click = lambda: (self.close(), self.yes())

    def _no(self) -> None:
        self.close()
        if self.no:
            self.no()


class ChoiceDialog(Dialog):
    """sortselect_dialog: a title, radio rows, 取消 (choosing closes it)."""
    ui = "dialog/sortselect_dialog"

    def __init__(self, dev, title, options, current, chosen):
        self.title, self.options, self.current, self.chosen = title, options, current, chosen
        super().__init__(dev)

    def build(self) -> None:
        r = self.root
        n = len(self.options)
        rows_h = min(5, n) * 80
        h = 80 + rows_h + 80
        r.props["self_layout"] = f"default(x=c,y=m,w=600,h={h})"
        r["hscroll_label"].text = self.title
        r["hscroll_label"].style = "msg_leftwhite36_style"
        lv = r["list_view"]
        lv.raw = (0, 80, 600, rows_h)
        lv.inline["normal"] = {"bg_color": (0, 0, 0, 0), "border_color": (0, 0, 0, 0)}
        sv = r["scrollview_sortselect"]
        sv.children.clear()
        for i, (key, text) in enumerate(self.options):
            item = make("list_item", (0, 0, 600, 80), f"opt{i}", "list_itemplaylist_style", parent=sv)
            label(item, (40, 0, 440, 80), text, 30)
            make("image", (600 - 40 - 48, 16, 48, 48), "radio", parent=item, draw_type="center",
                 image="select" if key == self.current else "unselect")
            item.on_click = lambda k=key: self.pick(k)
        ret = r["button_return"]
        ret.raw = (0, 80 + rows_h, 600, 80)
        ret.text = self.s("msg_no")
        ret.on_click = self.close

    def pick(self, key) -> None:
        self.close()
        self.chosen(key)


class VolumeDialog(Dialog):
    """volume_dialog: the ring, the number and the speaker; gone 1.5 s after the last turn."""
    ui = "dialog/volume_dialog"
    passive = True

    def __init__(self, dev):
        super().__init__(dev)
        self.timer = QTimer(dev, singleShot=True, interval=1500, timeout=dev._hide_volume)

    def set(self, v: int) -> None:
        r = self.root
        r["progress_circle"].value = v
        r["label_volume"].text = str(v)
        r["image_volume"].image = f"volume_{0 if v == 0 else min(4, 1 + v * 4 // 101)}"
        self.timer.start()


class HomeMenuDialog(Dialog):
    """homemenu_dialog (long press on home): which tiles the home pages show, in their order."""
    ui = "dialog/homemenu_dialog"

    def build(self) -> None:
        from .library import home_items, MENU_ORDER
        r = self.root
        r["label"].text = self.s("menu_showitem")
        self.chosen = list(home_items(self.dev))
        sv = r["scroll_view"]
        sv.children.clear()
        self.rows = {}
        for key in MENU_ORDER:
            item = make("list_item", (0, 0, 530, 96), f"item_{key}", "list_homeitem_style", parent=sv)
            icon = make("image", (30, 16, 64, 64), "icon", parent=item, draw_type="scale_auto", image=f"home_{key}")
            label(item, (120, 0, 300, 96), self.s(key), 32)
            box = make("image", (450, 24, 48, 48), "check", parent=item, draw_type="center")
            self.rows[key] = box
            item.on_click = lambda k=key: self.toggle(k)
        self.refresh()
        r["btn_reset"].text = self.s("menu_reset")
        r["btn_enter"].text = self.s("msg_yes")
        r["btn_reset"].on_click = self.reset
        r["btn_enter"].on_click = self.ok

    def refresh(self) -> None:
        for key, box in self.rows.items():
            box.image = "mulselected" if key in self.chosen else "unselect"
        self.dev.screen.update()

    def toggle(self, key: str) -> None:
        if key in self.chosen:
            if len(self.chosen) > 1:
                self.chosen.remove(key)
        else:
            self.chosen.append(key)
        self.refresh()

    def reset(self) -> None:
        from .library import default_home
        self.chosen = list(default_home(self.dev))
        self.refresh()

    def ok(self) -> None:
        from .library import MENU_ORDER
        order = [k for k in MENU_ORDER if k in self.chosen]
        self.dev.set_cfg("HOMEMENU", order)
        self.close()
        self.dev.home.rebuild()


# ====================================================================== full-screen overlays
class SaverPage(Page):
    """saverscreen_page: the lock screen (屏保): time, date, battery, the song with play / next; slide away."""
    ui = "saverscreen_page"
    fullscreen = True

    def build(self) -> None:
        r = self.root
        r["img_screensaver"].image = "screensaver_default"
        r["view_shade"].inline["normal"] = {"bg_color": (0, 0, 0, 90)}
        for n in ("clock_time", "clock_date"):
            r[n].inline["normal"] = {"text_color": (255, 255, 255, 255), "font_size": 120 if n == "clock_time" else 40}
        r["clock_date"].props["format"] = "YYYY-MM-DD"
        r["view_timeinfo"].visible = bool(int(self.dev.cfg("SSAVERTIMESHOW")))
        r["img_play"].on_click = self.win.player.toggle
        r["btn_next"].on_click = self.win.player.next
        r["btn_next"].inline["normal"] = {"bg_color": (0, 0, 0, 0), "border_color": (0, 0, 0, 0), "icon": "next"}
        r["btn_next"].inline["pressed"] = {"bg_color": (0, 0, 0, 0), "border_color": (0, 0, 0, 0), "icon": "next_down"}
        for n in ("scrlabel_title", "scrlabel_artist"):
            r[n].inline["normal"] = {"text_color": (255, 255, 255, 255) if n == "scrlabel_title" else GREY,
                                     "font_size": 32 if n == "scrlabel_title" else 26, "text_align_h": 2}
        self._y0 = None
        self.changed("track")

    def changed(self, what: str) -> None:
        r, p = self.root, self.win.player
        t = p.current
        r["view_playinfo"].visible = t is not None
        if t is not None:
            r["scrlabel_title"].text = t.get("title") or ""
            r["scrlabel_artist"].text = t.get("artist") or ""
            r["img_playcover"].pixmap = self.dev.track_cover(t)
            r["img_play"].image = "playing_playing" if p.state == "playing" else "playing_stop"
        b = battery()
        r["label_battery"].text = f"{b[0]}%" if b else ""
        r["img_battery"].visible = b is not None
        r["pgbar_battery"].value = b[0] if b else 100

    def touch(self, kind, pos) -> bool:
        """A swipe (left or up) unlocks; taps go to the play / next keys."""
        if kind == "down":
            self._y0 = (pos.x(), pos.y())
            return False
        if kind == "up" and self._y0 is not None:
            dx, dy = pos.x() - self._y0[0], pos.y() - self._y0[1]
            self._y0 = None
            if dx < -120 or dy < -120:
                self.dev.close_overlay()
                return True
        return False


class PowerOffPage(Page):
    """poweroff_page: the ONIX goodbye picture (and the battery) before the screen goes dark."""
    ui = "poweroff_page"
    fullscreen = True

    def build(self) -> None:
        b = battery()
        self.root["img_battery"].visible = False
        self.root["label_battery"].text = ""

    def touch(self, kind, pos) -> bool:
        return True


class BootPage(Page):
    """Power on: the ONIX logo (the firmware's boot picture)."""
    ui = "poweroff_page"
    fullscreen = True

    def build(self) -> None:
        self.root["img_shutdown"].image = "logo"
        self.root["img_battery"].visible = False
        self.root["label_battery"].text = ""

    def touch(self, kind, pos) -> bool:
        return True


# ====================================================================== text input
class EditField:
    """An AWTK ``edit`` node made live: text, the hint when empty, a caret while focused; tapping it opens
    the on-screen keyboard (kb_default_t26). The computer's keyboard types into it too."""

    def __init__(self, page, node: awtk.Node, hint: str = "", changed=None, submit=None, limit: int = 64):
        self.page, self.dev, self.node = page, page.dev, node
        self.text = ""
        self.hint = hint
        self.on_changed, self.on_submit, self.limit = changed, submit, limit
        self.keyboard: Keyboard | None = None
        node.on_click = self.open_keyboard
        node.paint_hook = self._paint

    def _paint(self, p, n, r, st) -> bool:
        fs = int(st.get("font_size") or 32)
        p.setFont(font(fs))
        box = r.adjusted(8, 0, -8, 0)
        if self.text:
            p.setPen(QColor(255, 255, 255))
            fm = QFontMetricsF(font(fs))
            tw = fm.horizontalAdvance(self.text)
            shift = max(0.0, tw - box.width() + 6)
            p.save()
            p.setClipRect(box, Qt.IntersectClip)
            p.drawText(QRectF(box.x() - shift, box.y(), tw + 4, box.height()), int(Qt.AlignLeft | Qt.AlignVCenter),
                       self.text)
            p.restore()
            x = box.x() + min(tw, box.width() - 6) + 2
        else:
            p.setPen(QColor(110, 110, 110))
            p.drawText(box, int(Qt.AlignLeft | Qt.AlignVCenter), self.hint)
            x = box.x() + 1
        if self.dev.edit is self and int(time.monotonic() * 2) % 2 == 0:
            p.setPen(QPen(QColor(255, 219, 149), 2))
            p.drawLine(x, r.y() + r.height() * .25, x, r.y() + r.height() * .75)
        self.dev.screen._movers.append((n, r))
        return True

    def focus(self) -> None:
        self.dev.focus_edit(self)

    def open_keyboard(self) -> None:
        self.focus()
        if self.keyboard is None:
            self.keyboard = Keyboard(self)
            self.dev.show_dialog(self.keyboard)

    def keyboard_open(self) -> bool:
        return self.keyboard is not None

    def close_keyboard(self) -> None:
        if self.keyboard is not None:
            self.dev.close_dialog(self.keyboard)
            self.keyboard = None

    def set_text(self, text: str) -> None:
        self.text = text[:self.limit]
        self.dev.screen.update()
        if self.on_changed:
            self.on_changed(self.text)

    def insert(self, s: str) -> None:
        self.set_text(self.text + s)

    def backspace(self) -> None:
        self.set_text(self.text[:-1])

    def submit(self) -> None:
        self.close_keyboard()
        if self.on_submit:
            self.on_submit(self.text)


class Keyboard(Dialog):
    """kb_default_t26 at the bottom of the screen: abc / ABC / 123 / #+= pages, space, ⌫, OK."""
    ui = "kb_default_t26"

    def __init__(self, edit: EditField):
        self.edit = edit
        super().__init__(edit.dev)
        self.layer.dim = 0
        self.layer.outside = self.edit.close_keyboard

    def build(self) -> None:
        r = self.root
        r.theme = awtk.Theme("keyboard")
        r["candidates"].visible = False
        self.pages = r["panel"]
        self.page("lower")
        for n in r.walk():
            if n.type != "button":
                continue
            name = n.name
            if name.startswith("page:"):
                target = name[5:]
                if target == "chinese":                  # (no pinyin engine here: stays English)
                    n.text = "EN"
                    n.on_click = lambda: None
                else:
                    n.on_click = lambda t=target: self.page(t)
            elif name == "key:backspace":
                n.inline["normal"] = dict(n.inline.get("normal") or {}, icon="keyboard_del")
                n.on_click = self.edit.backspace
            elif name == "space":
                n.on_click = lambda: self.edit.insert(" ")
            elif name == "action":
                n.on_click = self.edit.submit
            elif name.startswith("key:") and len(name) == 5:
                n.on_click = lambda c=name[4]: self.edit.insert(c)
            elif n.text and len(n.text) == 1:
                n.on_click = lambda c=n.text: self.edit.insert(c)
            if name == "page:upper" and not n.text:
                n.inline["normal"] = dict(n.inline.get("normal") or {}, icon="keyboard_shift0")

    def page(self, name: str) -> None:
        kids = self.pages.children
        for i, c in enumerate(kids):
            c.visible = c.name == name
        self.dev.screen.update()


__all__ = ["StatusBar", "StatusPanel", "MessageDialog", "ConfirmDialog", "ChoiceDialog", "VolumeDialog",
           "HomeMenuDialog", "SaverPage", "PowerOffPage", "BootPage", "EditField", "Keyboard", "fill_bar"]
