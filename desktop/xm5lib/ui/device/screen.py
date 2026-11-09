"""A 720x720 touch screen that draws AWTK node trees (``awtk.Node``) the way the XM5 firmware does.

One QWidget, painted with QPainter in the player's own pixels and scaled to the widget. It keeps a stack
of layers (the status bar, the current window, dialogs on top), hit-tests presses against them and turns
mouse input into what a finger does on the device: tap, long press (or right click), vertical drag with a
fling, horizontal swipe between slide-view pages, dragging a slider. The wheel scrolls lists (and turns
pages on a slide view). Marquee labels, GIFs and frame animations are driven by one timer that only runs
while something moves.

Nothing here knows about music: the page controllers (``pages.py``) build the trees and set callbacks.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QMovie, QPainter, QPainterPath, QPen, QPixmap

from . import awtk

SIZE = 720
FAMILIES = ["Arial", "Microsoft YaHei", "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC",
            "Noto Sans SC", "WenQuanYi Micro Hei", "DejaVu Sans"]


def qcolor(c) -> QColor | None:
    if c is None:
        return None
    r, g, b, a = c
    return QColor(r, g, b, a)


class Images:
    """Firmware images by name (png/jpg/gif), loaded once."""

    def __init__(self, root=None):
        self.root = root or (awtk.ROOT / "images")
        self.cache: dict[str, QPixmap | None] = {}
        self.frames: dict[str, list[tuple[QPixmap, int]]] = {}

    def path(self, name: str):
        for ext in (".png", ".jpg", ".gif", ".bmp"):
            p = self.root / f"{name}{ext}"
            if p.is_file():
                return p
        return None

    def get(self, name: str) -> QPixmap | None:
        if not name:
            return None
        if name not in self.cache:
            p = self.path(name)
            pix = QPixmap(str(p)) if p else QPixmap()
            self.cache[name] = None if pix.isNull() else pix
        return self.cache[name]

    def gif(self, name: str) -> list[tuple[QPixmap, int]]:
        """[(frame, delay ms)] of an animated GIF."""
        if name not in self.frames:
            out = []
            p = self.path(name)
            if p is not None:
                m = QMovie(str(p))
                for i in range(max(1, m.frameCount())):
                    m.jumpToFrame(i)
                    out.append((m.currentPixmap().copy(), max(20, m.nextFrameDelay() or 80)))
            self.frames[name] = out
        return self.frames[name]


IMAGES: Images | None = None


def images() -> Images:
    global IMAGES
    if IMAGES is None:
        IMAGES = Images()
    return IMAGES


_FONTS: dict[int, QFont] = {}


def font(px: int) -> QFont:
    f = _FONTS.get(px)
    if f is None:
        f = QFont()
        f.setFamilies(FAMILIES)
        f.setPixelSize(max(6, int(px)))
        f.setHintingPreference(QFont.PreferNoHinting)
        _FONTS[px] = f
    return f


class Layer:
    """A tree on screen at (x, y). ``modal`` layers take every press; one outside the tree calls
    ``outside`` (dialogs close that way). ``dim`` darkens what is below (AWTK's dialog highlight)."""

    def __init__(self, root: awtk.Node, x: float = 0, y: float = 0, modal: bool = False, dim: int = 0,
                 outside=None, name: str = "", passive: bool = False):
        self.root, self.x, self.y = root, x, y
        self.modal, self.dim, self.outside, self.name = modal, dim, outside, name
        self.passive = passive              # shown only (a toast, the volume ring): presses go through
        self.born = time.monotonic()


class Screen(QObject):
    """The touch screen's contents and its finger logic, in the player's own 720 x 720 pixels. Whoever shows
    it (``frame.DeviceFrame``) paints it with ``paint`` and feeds it ``press/move/release/wheel``; it asks for
    repaints through ``dirty`` (a rect in screen pixels, or None for all)."""
    dirty = Signal(object)

    LONG_MS = 550
    SLOP = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.host = None                    # a QWidget for the few desktop dialogs (names)
        self.raw = None                     # (kind, pos) -> bool: the firmware's window-level touch hooks
        self.active = True
        self.lang: dict[str, str] = awtk.strings("zh_CN")
        self.layers: list[Layer] = []
        self.img = images()
        self._press = None                 # dict of the gesture in progress
        self._long = QTimer(self, singleShot=True, interval=self.LONG_MS, timeout=self._long_press)
        self._timer = QTimer(self, interval=16, timeout=self._tick)
        self._flings: dict[int, tuple[awtk.Node, float]] = {}
        self._slides: dict[int, awtk.Node] = {}
        self._movers: list[tuple[awtk.Node, QRectF]] = []      # marquees, gifs, animations seen last paint
        self._t0 = time.monotonic()
        self._wheel_page_at = 0.0
        self.shade = 0                      # 0..255 of black over everything: the backlight turned down
        self.gifs_run = True                # the "now playing" GIFs move only while music plays

    # ------------------------------------------------------------------ repaint requests
    def update(self, rect=None) -> None:
        self.dirty.emit(QRectF(rect) if rect is not None else None)

    def isVisible(self) -> bool:
        return self.active

    # ------------------------------------------------------------------ layers
    def push(self, layer: Layer) -> Layer:
        self.layers.append(layer)
        self.relayout(layer)
        self.update()
        return layer

    def remove(self, layer: Layer) -> None:
        if layer in self.layers:
            self.layers.remove(layer)
            if self._press and self._press.get("layer") is layer:
                self._cancel_press()
            self.update()

    def relayout(self, layer: Layer | None = None) -> None:
        for l in [layer] if layer else self.layers:
            r = l.root
            if r.type in ("window", "system_bar"):
                r.raw = (0, 0, r.raw[2] or SIZE, r.raw[3] or SIZE)
            awtk.layout(r, SIZE, SIZE)

    def refresh(self, node: awtk.Node | None = None) -> None:
        """Repaint one node (or everything)."""
        if node is None:
            self.update()
            return
        r = self.node_rect(node)
        if r is not None:
            self.update(r)

    def layer_of(self, node: awtk.Node) -> Layer | None:
        n = node
        while n.parent is not None:
            n = n.parent
        return next((l for l in self.layers if l.root is n), None)

    def node_rect(self, node: awtk.Node) -> QRectF | None:
        l = self.layer_of(node)
        if l is None:
            return None
        x, y = node.abs_pos()
        return QRectF(l.x + x, l.y + y, node.w, node.h)

    # ------------------------------------------------------------------ painting
    def paint(self, p: QPainter, clip: QRectF) -> None:
        """Everything on screen, the painter set to screen pixels (0..720)."""
        p.fillRect(QRectF(0, 0, SIZE, SIZE), QColor(0, 0, 0))
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        p.setClipRect(QRectF(0, 0, SIZE, SIZE))
        self._movers = []
        now = time.monotonic()
        for layer in self.layers:
            if layer.dim:
                p.fillRect(QRectF(0, 0, SIZE, SIZE), QColor(0, 0, 0, layer.dim))
            self.paint_node(p, layer.root, layer.x, layer.y, clip, now)
        if self.shade:
            p.fillRect(QRectF(0, 0, SIZE, SIZE), QColor(0, 0, 0, self.shade))
        self._schedule()

    def paint_node(self, p: QPainter, n: awtk.Node, ox: float, oy: float, clip: QRectF, now: float) -> None:
        if not n.visible or n.w <= 0 or n.h <= 0:
            return
        r = QRectF(ox + n.x, oy + n.y, n.w, n.h)
        if not r.intersects(clip):
            return
        st = n.styles()
        if n.paint_hook is None or not n.paint_hook(p, n, r, st):
            self.paint_self(p, n, r, st, now)
        if not n.children and n.rows is None:
            return
        if n.type == "slide_view":
            self._paint_slides(p, n, r, clip, now)
        elif n.is_scroller:
            p.save()
            p.setClipRect(r, Qt.IntersectClip)
            inner = clip.intersected(r)
            if n.rows is not None:
                for row in n.rows.visible(n):
                    self.paint_node(p, row, r.x(), r.y() - n.scroll_y, inner, now)
            else:
                for c in n.children:
                    self.paint_node(p, c, r.x(), r.y() - n.scroll_y, inner, now)
            p.restore()
            self._paint_scrollbar(p, n, r)
        else:
            clip_kids = n.type in ("list_view", "table_view", "view", "dialog", "list_item") and n.parent is not None
            if clip_kids:
                p.save()
                p.setClipRect(r, Qt.IntersectClip)
            for c in n.children:
                self.paint_node(p, c, r.x(), r.y(), clip, now)
            if clip_kids:
                p.restore()

    # ---- the widget's own look
    def paint_self(self, p: QPainter, n: awtk.Node, r: QRectF, st: dict, now: float) -> None:
        t = n.type
        if t not in ("progress_circle", "slider", "progress_bar"):   # their colours/images are the bar's
            self.paint_background(p, n, r, st)
        if t == "image":
            pix = n.pixmap if n.pixmap is not None else self.img.get(n.image)
            self.draw_image(p, pix, r, awtk.DRAW.get(n.props.get("draw_type", "default"), 0), st, n)
            self.paint_text(p, n, r, st, now)
        elif t in ("label", "hscroll_label", "button", "tab_button", "check_button", "radio_button",
                   "digit_clock", "list_item"):
            if t != "list_item" or st.get("icon"):
                self.paint_icon(p, n, r, st)
            self.paint_text(p, n, r, st, now)
        elif t == "slider":
            self.paint_slider(p, n, r, st)
        elif t == "progress_bar":
            self.paint_progress(p, n, r, st)
        elif t == "progress_circle":
            self.paint_circle(p, n, r, st)
        elif t == "slide_indicator":
            self.paint_indicator(p, n, r, st)
        elif t == "image_animation":
            if n.data and n.data.get("running"):
                start, end = int(n.props.get("start_index", 0)), int(n.props.get("end_index", 0))
                step = int(n.props.get("interval", 100) or 100) / 1000
                k = start + int((now - self._t0) / step) % max(1, end - start + 1)
                fmt = n.props.get("format", "%s%d")
                self.draw_image(p, self.img.get(fmt % (n.image, k)), r, 1, st, n)
                self._movers.append((n, r))
        elif t == "gif":
            frames = self.img.gif(n.image)
            if frames:
                pix = frames[0][0]
                if self.gifs_run:
                    total = sum(d for _, d in frames)
                    at = int((now - self._t0) * 1000) % total
                    for pix, d in frames:
                        if at < d:
                            break
                        at -= d
                    self._movers.append((n, r))
                self.draw_image(p, pix, r, 1, st, n)

    def paint_background(self, p: QPainter, n: awtk.Node, r: QRectF, st: dict) -> None:
        bg = st.get("bg_color")
        rad = st.get("round_radius") or 0
        if bg and bg[3]:
            p.setPen(Qt.NoPen)
            p.setBrush(qcolor(bg))
            if rad:
                p.drawPath(self._rounded(r, st, rad))
            else:
                p.drawRect(r)
        img = st.get("bg_image")
        if img:
            self.draw_image(p, self.img.get(img), r, st.get("bg_image_draw_type", 12), st, n, bg=True)
        bc = st.get("border_color")
        bw = st.get("border_width", 1) or 1
        if bc and bc[3] and n.type not in ("label", "hscroll_label", "image"):
            sides = st.get("border", 15)
            pen = QPen(qcolor(bc), bw)
            pen.setJoinStyle(Qt.MiterJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            h = bw / 2
            rr = r.adjusted(h, h, -h, -h)
            if sides == 15:
                if rad:
                    p.drawPath(self._rounded(rr, st, max(0, rad - h)))
                else:
                    p.drawRect(rr)
            else:
                if sides & 4:
                    p.drawLine(QPointF(r.left(), rr.top()), QPointF(r.right(), rr.top()))
                if sides & 8:
                    p.drawLine(QPointF(r.left(), rr.bottom()), QPointF(r.right(), rr.bottom()))
                if sides & 1:
                    p.drawLine(QPointF(rr.left(), r.top()), QPointF(rr.left(), r.bottom()))
                if sides & 2:
                    p.drawLine(QPointF(rr.right(), r.top()), QPointF(rr.right(), r.bottom()))

    @staticmethod
    def _rounded(r: QRectF, st: dict, rad: float) -> QPainterPath:
        tl = st.get("round_radius_top_left", rad)
        tr = st.get("round_radius_top_right", rad)
        bl = st.get("round_radius_bottom_left", rad)
        br = st.get("round_radius_bottom_right", rad)
        tl, tr, bl, br = (min(float(v or 0), r.width() / 2, r.height() / 2) for v in (tl, tr, bl, br))
        path = QPainterPath()
        path.moveTo(r.left() + tl, r.top())
        path.lineTo(r.right() - tr, r.top())
        path.arcTo(QRectF(r.right() - 2 * tr, r.top(), 2 * tr, 2 * tr), 90, -90) if tr else path.lineTo(r.right(), r.top())
        path.lineTo(r.right(), r.bottom() - br)
        path.arcTo(QRectF(r.right() - 2 * br, r.bottom() - 2 * br, 2 * br, 2 * br), 0, -90) if br else path.lineTo(r.right(), r.bottom())
        path.lineTo(r.left() + bl, r.bottom())
        path.arcTo(QRectF(r.left(), r.bottom() - 2 * bl, 2 * bl, 2 * bl), 270, -90) if bl else path.lineTo(r.left(), r.bottom())
        path.lineTo(r.left(), r.top() + tl)
        path.arcTo(QRectF(r.left(), r.top(), 2 * tl, 2 * tl), 180, -90) if tl else path.lineTo(r.left(), r.top())
        path.closeSubpath()
        return path

    def draw_image(self, p: QPainter, pix: QPixmap | None, r: QRectF, draw: int, st: dict, n: awtk.Node,
                   bg: bool = False) -> None:
        if pix is None or pix.isNull():
            return
        iw, ih = pix.width() / pix.devicePixelRatio(), pix.height() / pix.devicePixelRatio()
        xo, yo = st.get("x_offset", 0) or 0, st.get("y_offset", 0) or 0
        if draw in (0,):                                           # at the top-left, as is
            target = QRectF(r.x() + xo, r.y() + yo, iw, ih)
        elif draw in (1, 2) or (draw == 5 and iw <= r.width() and ih <= r.height()):
            if draw == 2 and (iw > r.width() or ih > r.height()):  # icon: shrink to fit
                k = min(r.width() / iw, r.height() / ih)
                iw, ih = iw * k, ih * k
            target = QRectF(r.x() + (r.width() - iw) / 2 + xo, r.y() + (r.height() - ih) / 2 + yo, iw, ih)
        elif draw in (4, 5):                                       # keep the aspect, fit, centre
            k = min(r.width() / iw, r.height() / ih)
            target = QRectF(r.x() + (r.width() - iw * k) / 2, r.y() + (r.height() - ih * k) / 2, iw * k, ih * k)
        elif draw == 6:
            k = r.width() / iw
            target = QRectF(r.x(), r.y(), r.width(), ih * k)
        elif draw == 7:
            k = r.height() / ih
            target = QRectF(r.x(), r.y(), iw * k, r.height())
        elif draw == 12 and bg and (abs(iw - r.width()) > 1 or abs(ih - r.height()) > 1) and iw < r.width() * 1.5:
            self._patch9(p, pix, r)
            return
        elif draw == 12 and iw > r.width() * 1.5:                # a picture bigger than the window: cover it
            k = max(r.width() / iw, r.height() / ih)
            target = QRectF(r.x() + (r.width() - iw * k) / 2, r.y(), iw * k, ih * k)
        else:                                                      # scale / patch9 at its own size
            target = r
        p.drawPixmap(target, pix, QRectF(pix.rect()))

    @staticmethod
    def _patch9(p: QPainter, pix: QPixmap, r: QRectF) -> None:
        w, h = pix.width(), pix.height()
        cx, cy = min(w // 3, int(r.width() / 2)), min(h // 3, int(r.height() / 2))
        xs_src = [0, cx, w - cx, w]
        ys_src = [0, cy, h - cy, h]
        xs = [r.left(), r.left() + cx, r.right() - cx, r.right()]
        ys = [r.top(), r.top() + cy, r.bottom() - cy, r.bottom()]
        for i in range(3):
            for j in range(3):
                src = QRectF(xs_src[i], ys_src[j], xs_src[i + 1] - xs_src[i], ys_src[j + 1] - ys_src[j])
                dst = QRectF(xs[i], ys[j], xs[i + 1] - xs[i], ys[j + 1] - ys[j])
                if src.width() > 0 and src.height() > 0 and dst.width() > 0 and dst.height() > 0:
                    p.drawPixmap(dst, pix, src)

    def paint_icon(self, p: QPainter, n: awtk.Node, r: QRectF, st: dict) -> None:
        name = st.get("icon")
        if not name:
            return
        pix = self.img.get(name)
        if pix is None:
            return
        iw, ih = pix.width(), pix.height()
        at = st.get("icon_at", 0) or 0
        text = self._text(n)
        if at == 1 or (at == 0 and text and n.type in ("check_button", "radio_button")):
            ml = st.get("margin_left", st.get("margin", 0)) or 0
            target = QRectF(r.x() + ml, r.y() + (r.height() - ih) / 2, iw, ih)
        elif at == 2:
            mr = st.get("margin_right", st.get("margin", 0)) or 0
            target = QRectF(r.right() - mr - iw, r.y() + (r.height() - ih) / 2, iw, ih)
        else:
            mb = st.get("margin_bottom", 0) or 0
            mt = st.get("margin_top", 0) or 0
            target = QRectF(r.x() + (r.width() - iw) / 2, r.y() + mt + (r.height() - mt - mb - ih) / 2, iw, ih)
        p.drawPixmap(target, pix, QRectF(pix.rect()))

    def _text(self, n: awtk.Node) -> str:
        if n.type == "digit_clock":
            fmt = n.props.get("format", "hh:mm")
            if fmt == "h12:mm":                                  # 24 小时制 off
                return time.strftime("%I:%M").lstrip("0")
            return time.strftime(fmt.replace("hh", "%H").replace("mm", "%M").replace("ss", "%S")
                                 .replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d"))
        return n.label(self.lang)

    def paint_text(self, p: QPainter, n: awtk.Node, r: QRectF, st: dict, now: float) -> None:
        text = self._text(n)
        if not text:
            return
        c = st.get("text_color") or (68, 68, 68, 255)
        if not c[3]:
            return
        f = font(st.get("font_size") or 18)
        p.setFont(f)
        p.setPen(qcolor(c))
        ah = st.get("text_align_h", 1 if n.type != "list_item" else 2) or 1
        av = st.get("text_align_v", 1) or 1
        ml = st.get("margin_left", st.get("margin", 0)) or 0
        mr = st.get("margin_right", st.get("margin", 0)) or 0
        if n.type in ("check_button", "radio_button") and st.get("icon"):
            pix = self.img.get(st["icon"])
            if pix is not None and (st.get("icon_at", 0) or 0) in (0, 1):
                ml += pix.width() + (st.get("spacer", 2) or 2)
        box = r.adjusted(ml, 0, -mr, 0)
        wrap = n.props.get("line_wrap") == "true" or n.props.get("word_wrap") == "true"
        valign = {1: Qt.AlignVCenter, 2: Qt.AlignTop, 3: Qt.AlignBottom}.get(av, Qt.AlignVCenter)
        halign = {1: Qt.AlignHCenter, 2: Qt.AlignLeft, 3: Qt.AlignRight}.get(ah, Qt.AlignHCenter)
        if wrap or "\n" in text:
            p.drawText(box, int(halign | valign | Qt.TextWordWrap), text)
            return
        fm = QFontMetricsF(f)
        tw = fm.horizontalAdvance(text)
        if tw <= box.width() + 0.5:
            p.drawText(box, int(halign | valign | Qt.TextSingleLine), text)
            return
        # too long: a marquee when the firmware scrolls it, otherwise cut at the edge
        shift = 0.0
        if n.type == "hscroll_label" and n.props.get("loop") == "true" and n.props.get("only_focus") != "true":
            shift = self._marquee(n, tw - box.width(), now)
            self._movers.append((n, r))
        p.save()
        p.setClipRect(box, Qt.IntersectClip)
        p.drawText(QRectF(box.x() - shift, box.y(), tw + 4, box.height()), int(Qt.AlignLeft | valign), text)
        p.restore()

    def _marquee(self, n: awtk.Node, over: float, now: float) -> float:
        lull = int(n.props.get("lull", 2000) or 2000) / 1000
        speed = 45.0
        run = over / speed
        yoyo = n.props.get("yoyo") == "true"
        period = (2 * (lull + run)) if yoyo else (lull + run + lull / 2)
        key = getattr(n, "_marquee_t0", None)
        if key is None or getattr(n, "_marquee_text", None) != n.text:
            n._marquee_t0, n._marquee_text = now, n.text
        t = (now - n._marquee_t0) % period
        if t < lull:
            return 0.0
        t -= lull
        if t < run:
            return t * speed
        if not yoyo:
            return over
        t -= run
        if t < lull:
            return over
        t -= lull
        return max(0.0, over - t * speed)

    def paint_slider(self, p: QPainter, n: awtk.Node, r: QRectF, st: dict) -> None:
        span = max(1e-6, n.max - n.min)
        f = max(0.0, min(1.0, (n.value - n.min) / span))
        if n.props.get("vertical") == "true":
            self._paint_vslider(p, n, r, st, f)
            return
        bgi, fgi = self.img.get(st.get("bg_image", "")), self.img.get(st.get("fg_image", ""))
        if bgi is not None:
            bh = bgi.height()
            bar = QRectF(r.x(), r.y() + (r.height() - bh) / 2, r.width(), bh)
            p.drawPixmap(bar, bgi, QRectF(bgi.rect()))
            if fgi is not None and f > 0:
                fw = r.width() * f
                p.drawPixmap(QRectF(bar.x(), bar.y(), fw, bh), fgi, QRectF(0, 0, fgi.width() * f, fgi.height()))
        else:
            bh = float(n.props.get("bar_size", 0) or 0) or min(r.height(), max(2.0, r.height() / 4)) if r.height() > 6 \
                else r.height()
            bar = QRectF(r.x(), r.y() + (r.height() - bh) / 2, r.width(), bh)
            bc, fc = st.get("bg_color"), st.get("fg_color")
            if bc and bc[3]:
                p.fillRect(bar, qcolor(bc))
            if fc and fc[3]:
                p.fillRect(QRectF(bar.x(), bar.y(), bar.width() * f, bar.height()), qcolor(fc))
        icon = self.img.get(st.get("icon", ""))
        if icon is not None and n.enabled:
            iw, ih = icon.width(), icon.height()
            cx = r.x() + r.width() * f
            p.drawPixmap(QRectF(cx - iw / 2, r.y() + (r.height() - ih) / 2, iw, ih), icon, QRectF(icon.rect()))

    def _paint_vslider(self, p: QPainter, n: awtk.Node, r: QRectF, st: dict, f: float) -> None:
        """An upright slider (the EQ bands): the track image or colours, the knob at the value."""
        bgi = self.img.get(st.get("bg_image", ""))
        if bgi is not None:
            p.drawPixmap(QRectF(r.x() + (r.width() - bgi.width()) / 2, r.y(), bgi.width(), r.height()), bgi,
                         QRectF(bgi.rect()))
        else:
            bw = 4.0
            bar = QRectF(r.x() + (r.width() - bw) / 2, r.y(), bw, r.height())
            bc, fc = st.get("bg_color") or (51, 51, 51, 255), st.get("fg_color") or (255, 219, 149, 255)
            if bc[3]:
                p.fillRect(bar, qcolor(bc))
            if fc[3] and n.enabled:
                p.fillRect(QRectF(bar.x(), bar.bottom() - bar.height() * f, bw, bar.height() * f), qcolor(fc))
        icon = self.img.get(st.get("icon", "") or "eq_ball")
        if icon is not None:
            iw, ih = icon.width(), icon.height()
            cy = r.bottom() - r.height() * f
            p.save()
            if not n.enabled:
                p.setOpacity(0.45)
            p.drawPixmap(QRectF(r.x() + (r.width() - iw) / 2, cy - ih / 2, iw, ih), icon, QRectF(icon.rect()))
            p.restore()

    def paint_progress(self, p: QPainter, n: awtk.Node, r: QRectF, st: dict) -> None:
        f = max(0.0, min(1.0, (n.value - n.min) / max(1e-6, n.max - n.min)))
        fc = st.get("fg_color") or (51, 143, 255, 255)
        if n.props.get("vertical") == "true":
            fr = QRectF(r.x(), r.bottom() - r.height() * f, r.width(), r.height() * f)
        else:
            fr = QRectF(r.x(), r.y(), r.width() * f, r.height())
        if fc[3]:
            p.fillRect(fr, qcolor(fc))

    def paint_circle(self, p: QPainter, n: awtk.Node, r: QRectF, st: dict) -> None:
        f = max(0.0, min(1.0, (n.value - n.min) / max(1e-6, n.max - n.min)))
        lw = float(n.props.get("line_width", 8) or 8)
        rr = r.adjusted(lw / 2, lw / 2, -lw / 2, -lw / 2)
        bc = st.get("bg_color")
        if bc and bc[3]:
            p.setPen(QPen(qcolor(bc), lw))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(rr)
        fc = st.get("fg_color") or (255, 219, 149, 255)
        if f > 0:
            pen = QPen(qcolor(fc), lw)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            start = float(n.props.get("start_angle", 90) or 90)
            p.drawArc(rr, int(-start * 16), int(-360 * f * 16))

    def paint_indicator(self, p: QPainter, n: awtk.Node, r: QRectF, st: dict) -> None:
        count = int(getattr(n, "count", 0) or 0)
        if count < 2:
            return
        cur = int(n.value)
        spacing = float(n.props.get("spacing", 16) or 16)
        d = float(n.props.get("size", 8) or 8)
        d = min(d, r.height())
        total = count * d + (count - 1) * (spacing - d if spacing > d else spacing)
        step = spacing if spacing > d else d + spacing
        total = d + (count - 1) * step
        x = r.x() + (r.width() - total) / 2
        y = r.y() + (r.height() - d) / 2
        fg = qcolor(st.get("fg_color") or (126, 126, 126, 255))
        sel = qcolor(st.get("selected_fg_color") or (255, 255, 255, 255))
        p.setPen(Qt.NoPen)
        for i in range(count):
            p.setBrush(sel if i == cur else fg)
            p.drawEllipse(QRectF(x + i * step, y, d, d))

    def _paint_scrollbar(self, p: QPainter, n: awtk.Node, r: QRectF) -> None:
        total = n.rows.height() if n.rows is not None else n.content_h
        if total <= r.height() + 1:
            return
        bar = getattr(n, "bar", None)                       # the page's scroll_bar_m sibling, if any
        x = r.right() + (bar.w - 6) / 2 if bar is not None else r.right() - 8
        if bar is not None and not bar.visible:
            return
        k = r.height() / total
        h = max(40.0, r.height() * k)
        y = r.y() + (r.height() - h) * (n.scroll_y / max(1.0, total - r.height()))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(128, 128, 128, 200))
        p.drawRoundedRect(QRectF(x, y, 6, h), 3, 3)

    def _paint_slides(self, p: QPainter, n: awtk.Node, r: QRectF, clip: QRectF, now: float) -> None:
        pages = [c for c in n.children if c.visible]
        if not pages:
            return
        cur = max(0, min(len(pages) - 1, int(getattr(n, "page", 0))))
        dx = float(getattr(n, "drag", 0.0))
        p.save()
        p.setClipRect(r, Qt.IntersectClip)
        for i, c in enumerate(pages):
            off = (i - cur) * r.width() + dx
            if abs(off) >= r.width():
                continue
            self.paint_node(p, c, r.x() + off - c.x, r.y(), clip, now)
        p.restore()

    # ------------------------------------------------------------------ animation
    def _schedule(self) -> None:
        if not self.isVisible():
            self._movers = []
        busy = bool(self._flings or self._slides or self._movers)
        interval = 16 if (self._flings or self._slides) else 40     # marquees and GIFs: 25 frames a second
        if busy:
            if self._timer.interval() != interval:
                self._timer.setInterval(interval)
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()

    def _tick(self) -> None:
        if not self.isVisible():
            self._flings.clear()
            self._slides.clear()
            self._schedule()
            return
        dirty = False
        for key, (node, v) in list(self._flings.items()):
            v *= 0.94
            if abs(v) < 20:
                del self._flings[key]
                continue
            self._flings[key] = (node, v)
            if not self.scroll_by(node, v * 0.016):
                del self._flings[key]
            dirty = True
        for key, node in list(self._slides.items()):
            d = float(getattr(node, "drag", 0.0))
            d *= 0.65
            if abs(d) < 1:
                d = 0.0
                del self._slides[key]
            node.drag = d
            dirty = True
        if dirty:
            self.update()
        else:
            for n, r in self._movers:
                self.update(r)
        self._schedule()

    def scroll_by(self, node: awtk.Node, dy: float) -> bool:
        total = node.rows.height() if node.rows is not None else node.content_h
        top = max(0.0, total - node.h)
        new = max(0.0, min(top, node.scroll_y + dy))
        moved = abs(new - node.scroll_y) > 0.01
        node.scroll_y = new
        return moved

    def scroll_to(self, node: awtk.Node, y: float) -> None:
        self.scroll_by(node, y - node.scroll_y)
        self.update()

    def set_page(self, slide: awtk.Node, index: int, animate: bool = True) -> None:
        pages = [c for c in slide.children if c.visible]
        index = max(0, min(len(pages) - 1, index))
        old = int(getattr(slide, "page", 0))
        if animate and index != old:
            slide.drag = float(getattr(slide, "drag", 0.0)) + (index - old) * slide.w
            self._slides[id(slide)] = slide
        elif not animate:
            slide.drag = 0.0
            self._slides.pop(id(slide), None)
        slide.page = index
        if callable(getattr(slide, "on_page", None)):
            slide.on_page(index)
        self.update()
        self._schedule()

    # ------------------------------------------------------------------ hit testing
    def _hit(self, n: awtk.Node, x: float, y: float, ox: float, oy: float, path: list) -> bool:
        if getattr(n, "passthrough", False):
            return False
        if not n.visible or not n.enabled and n.type not in ("label", "image", "view"):
            return False
        r = QRectF(ox + n.x, oy + n.y, n.w, n.h)
        if not r.contains(x, y):
            return False
        path.append((n, r))
        if n.type == "slide_view":
            pages = [c for c in n.children if c.visible]
            if pages:
                cur = max(0, min(len(pages) - 1, int(getattr(n, "page", 0))))
                c = pages[cur]
                self._hit(c, x, y, r.x() - c.x + float(getattr(n, "drag", 0.0)), r.y(), path)
            return True
        if n.is_scroller:
            kids = n.rows.visible(n) if n.rows is not None else n.children
            for c in reversed(list(kids)):
                if self._hit(c, x, y, r.x(), r.y() - n.scroll_y, path):
                    break
            return True
        for c in reversed(n.children):
            if self._hit(c, x, y, r.x(), r.y(), path):
                break
        return True

    def hit_path(self, x: float, y: float):
        for layer in reversed(self.layers):
            if layer.passive:
                continue
            path: list = []
            if self._hit(layer.root, x, y, layer.x, layer.y, path):
                return layer, path
            if layer.modal:
                return layer, None
        return None, None

    # ------------------------------------------------------------------ input
    def press(self, pos: QPointF, button=Qt.LeftButton) -> None:
        if self.raw is not None and button == Qt.LeftButton and self.raw("down", pos):
            return
        layer, path = self.hit_path(pos.x(), pos.y())
        if layer is not None and path is None:                 # outside a modal dialog
            self._press = {"outside": layer}
            return
        if not path:
            return
        target = next(((n, r) for n, r in reversed(path) if n.clickable), None)
        vscroll = next((n for n, _ in reversed(path) if n.is_scroller), None)
        hslide = next((n for n, _ in reversed(path)
                       if n.type == "slide_view" and len([c for c in n.children if c.visible]) > 1), None)
        if button == Qt.RightButton:
            if target and target[0].on_long:
                target[0].on_long()
            return
        if button != Qt.LeftButton:
            return
        if vscroll is not None:
            self._flings.pop(id(vscroll), None)
        self._press = {"layer": layer, "pos": pos, "target": target, "v": vscroll, "h": hslide, "mode": "press",
                       "samples": [(time.monotonic(), pos.y())], "scroll0": vscroll.scroll_y if vscroll else 0}
        if target is not None:
            node, r = target
            if node.type == "slider" and node.on_change is not None:
                self._press["mode"] = "slider"
                node.pressed = True
                self._slider_to(node, r, pos.x(), final=False, y=pos.y())
                return
            node.pressed = True
            if node.on_long is not None:
                self._long.start()
            self.refresh(node)

    def _slider_to(self, node: awtk.Node, r: QRectF, x: float, final: bool, y: float | None = None) -> None:
        if node.props.get("vertical") == "true" and y is not None:
            f = max(0.0, min(1.0, (r.bottom() - y) / max(1.0, r.height())))
        else:
            f = max(0.0, min(1.0, (x - r.x()) / max(1.0, r.width())))
        node.value = node.min + f * (node.max - node.min)
        node.on_change(node.value, final)
        self.update()

    def _cancel_press(self) -> None:
        pr = self._press or {}
        t = pr.get("target")
        if t:
            t[0].pressed = False
        self._long.stop()
        self._press = None
        self.update()

    def _long_press(self) -> None:
        pr = self._press
        if not pr or pr.get("mode") != "press" or not pr.get("target"):
            return
        node = pr["target"][0]
        node.pressed = False
        pr["mode"] = "done"
        self.update()
        if node.on_long:
            node.on_long()

    def move(self, pos: QPointF) -> None:
        if self.raw is not None and self.raw("move", pos):
            self._cancel_press()
            return
        pr = self._press
        if not pr or "pos" not in pr:
            return
        dx, dy = pos.x() - pr["pos"].x(), pos.y() - pr["pos"].y()
        mode = pr["mode"]
        if mode == "slider":
            node, r = pr["target"]
            self._slider_to(node, r, pos.x(), final=False, y=pos.y())
            return
        if mode == "press" and math.hypot(dx, dy) > self.SLOP:
            if pr["v"] is not None and abs(dy) >= abs(dx):
                mode = pr["mode"] = "scroll"
            elif pr["h"] is not None and abs(dx) > abs(dy):
                mode = pr["mode"] = "swipe"
            elif pr["v"] is not None:
                mode = pr["mode"] = "scroll"
            if mode != "press" and pr.get("target"):
                pr["target"][0].pressed = False
                self._long.stop()
                self.update()
        if mode == "scroll":
            v = pr["v"]
            self.scroll_by(v, (pr["scroll0"] - dy) - v.scroll_y)
            pr["samples"].append((time.monotonic(), pos.y()))
            del pr["samples"][:-6]
            self.update()
        elif mode == "swipe":
            h = pr["h"]
            pages = [c for c in h.children if c.visible]
            cur = int(getattr(h, "page", 0))
            if (cur == 0 and dx > 0) or (cur == len(pages) - 1 and dx < 0):
                dx *= 0.3
            h.drag = dx
            self.update()

    def release(self, pos: QPointF) -> None:
        if self.raw is not None and self.raw("up", pos):
            self._cancel_press()
            return
        pr, self._press = self._press, None
        self._long.stop()
        if not pr:
            return
        if "outside" in pr:
            l = pr["outside"]
            if l.outside:
                l.outside()
            return
        mode = pr["mode"]
        if mode == "slider":
            node, r = pr["target"]
            node.pressed = False
            self._slider_to(node, r, pos.x(), final=True, y=pos.y())
            return
        if mode == "press" and pr.get("target"):
            node, r = pr["target"]
            node.pressed = False
            self.update()
            cur = self.node_rect(node) or r
            if cur.adjusted(-6, -6, 6, 6).contains(pos) and node.on_click:
                node.on_click()
            return
        if mode == "scroll":
            v = pr["v"]
            self.scroll_by(v, (pr["scroll0"] - (pos.y() - pr["pos"].y())) - v.scroll_y)
            self.update()
            s = pr["samples"]
            if len(s) >= 2 and s[-1][0] - s[0][0] > 0:
                v = -(s[-1][1] - s[0][1]) / (s[-1][0] - s[0][0])
                if time.monotonic() - s[-1][0] < 0.08 and abs(v) > 150:
                    self._flings[id(pr["v"])] = (pr["v"], max(-6000.0, min(6000.0, v)))
            self._schedule()
        elif mode == "swipe":
            h = pr["h"]
            dx = float(getattr(h, "drag", 0.0))
            cur = int(getattr(h, "page", 0))
            if dx < -h.w / 5:
                cur += 1
            elif dx > h.w / 5:
                cur -= 1
            pages = [c for c in h.children if c.visible]
            cur = max(0, min(len(pages) - 1, cur))
            old = int(getattr(h, "page", 0))
            if cur == old == len(pages) - 1 and dx < -h.w / 12 and callable(getattr(h, "on_end", None)):
                h.drag = 0.0
                self.update()
                h.on_end()                           # swiped on past the last page (home: 选择显示项)
                return
            h.drag = dx + (cur - old) * h.w
            h.page = cur
            self._slides[id(h)] = h
            if cur != old and callable(getattr(h, "on_page", None)):
                h.on_page(cur)
            self._schedule()
            self.update()

    def scrolling(self, node: awtk.Node) -> bool:
        """``node`` is being dragged or is still gliding."""
        pr = self._press or {}
        return (pr.get("mode") == "scroll" and pr.get("v") is node) or id(node) in self._flings

    def busy(self) -> bool:
        """A finger is down (the frame then keeps sending moves even outside the screen)."""
        return self._press is not None

    def wheel(self, pos: QPointF, dy: int) -> bool:
        """The mouse wheel over the screen: scroll the list under it (or turn a slide view's page).
        False when nothing there scrolls."""
        _, path = self.hit_path(pos.x(), pos.y())
        if not path or not dy:
            return False
        hook = next((n for n, _ in reversed(path) if callable(getattr(n, "wheel", None))), None)
        if hook is not None:                                   # a wheel of its own (text_selector)
            hook.wheel(dy)
            self.update()
            return True
        v = next((n for n, _ in reversed(path) if n.is_scroller), None)
        if v is not None:
            self._flings.pop(id(v), None)
            self.scroll_by(v, -dy / 120 * 150)
            self.update()
            return True
        h = next((n for n, _ in reversed(path)
                  if n.type == "slide_view" and len([c for c in n.children if c.visible]) > 1), None)
        if h is not None:
            if time.monotonic() - self._wheel_page_at > 0.35:
                self._wheel_page_at = time.monotonic()
                self.set_page(h, int(getattr(h, "page", 0)) + (1 if dy < 0 else -1))
            return True
        return False


class RowSource:
    """Rows of a virtual list: ``count`` rows of ``row_h`` built on demand by ``build(i) -> Node`` and kept
    while near the view. ``height()`` is what the scroller scrolls over."""

    def __init__(self, count: int, row_h: float, build, cols: int = 1, top: float = 0, bottom: float = 0):
        self.count, self.row_h, self.build, self.cols = count, row_h, build, max(1, cols)
        self.top, self.bottom = top, bottom
        self.cache: dict[int, awtk.Node] = {}

    def height(self) -> float:
        return self.top + math.ceil(self.count / self.cols) * self.row_h + self.bottom

    def reset(self, count: int | None = None) -> None:
        if count is not None:
            self.count = count
        self.cache.clear()

    def row(self, i: int, owner: awtk.Node) -> awtk.Node:
        n = self.cache.get(i)
        if n is None:
            n = self.build(i)
            n.parent = owner
            self.cache[i] = n
        cw = owner.w / self.cols
        n.x, n.y = (i % self.cols) * cw, self.top + (i // self.cols) * self.row_h
        if n.w <= 0:
            n.w = cw
        return n

    def visible(self, owner: awtk.Node):
        first = max(0, int((owner.scroll_y - self.top) // self.row_h) * self.cols)
        last = min(self.count, (int((owner.scroll_y - self.top + owner.h) // self.row_h) + 1) * self.cols)
        if len(self.cache) > 160:
            keep = range(max(0, first - 40), last + 40)
            for k in [k for k in self.cache if k not in keep]:
                del self.cache[k]
        return [self.row(i, owner) for i in range(first, last)]


__all__ = ["Screen", "Layer", "RowSource", "images", "font", "SIZE"]
