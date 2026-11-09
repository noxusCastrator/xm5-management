"""The XM5 itself, upright, at a fixed set of sizes.

Geometry is in spec units (the pixels of the XM5 Ti front render the case templates were measured on,
reference/xm5ti-front-superres.png): the device is 695 x 1126, the 720 x 720 screen sits at (70.35, 58) and
is 554.3 units wide, the four keys (⏮ ⏯ ⏭ Fn) are 81-unit squares centred 728.5 units down, their printed
symbols 67.5 units above them, the gold knob in its arch cut at the bottom right. Everything is drawn at
``k = s * 720 / 554.3`` px per unit, so at scale 1 the screen is exactly the player's 720 physical pixels.

The metal is the normal window's case (photo templates) rendered in its own orientation with three grooves
and flipped back upright; glass and bezel come from the same code; the wordmark is drawn upright; the knob
sprites (tools/render_knob.py at 714 px, enough for scale 2) are flipped. The keys are drawn here from the
photo's tones so they stay sharp at every size.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient, QRegion)
from PySide6.QtWidgets import QWidget

from .. import hardware, style

DEVICE_W = 695.0
GROOVES = 3
DEVICE_H = hardware.spec_height(GROOVES)            # 1126
SCREEN = (70.35, 58.0, 554.3)                      # x, y, side (spec units, upright)
UNIT = 720 / SCREEN[2]                             # px per spec unit at scale 1
KEY_SIZE = 81.0
KEY_Y = 728.5
LABEL_Y = 661.0
KEYS = (("prev", 137.75), ("play", 277.75), ("next", 417.25), ("fn", 557.25))
# 720 * s and every key position stay whole pixels; k/4 steps (the Windows display scales) keep a short,
# regular resampling pattern for the bitmaps, 1/2 and 2 an exact one.
SCALES = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
MARGIN = 18                                        # transparent px round the body for its shadow


class _Case(hardware.Case):
    """The window's case drawn in raw pixels (no device-pixel ratio): the caller decides the size."""

    def devicePixelRatioF(self):
        return 1.0


def flip(img: QImage) -> QImage:
    try:
        return img.flipped(Qt.Orientation.Vertical)
    except AttributeError:                                     # Qt < 6.9
        return img.mirrored(False, True)


class Geometry:
    """Pixel rectangles (physical px, window coordinates) at one scale."""

    def __init__(self, s: float):
        self.s = s
        self.k = k = s * UNIT
        self.m = m = MARGIN
        self.body = QRectF(m, m, round(DEVICE_W * k), round(DEVICE_H * k))
        self.size = (int(self.body.width()) + 2 * m, int(self.body.height()) + 2 * m)
        side = round(720 * s)
        self.screen = QRect(round(m + SCREEN[0] * k), round(m + SCREEN[1] * k), side, side)
        ks = round(KEY_SIZE * k)
        self.keys = {name: QRect(round(m + cx * k - ks / 2), round(m + KEY_Y * k - ks / 2), ks, ks)
                     for name, cx in KEYS}
        lh = 16 * k
        self.labels = {name: QRectF(m + cx * k - 30 * k, m + LABEL_Y * k - lh / 2, 60 * k, lh) for name, cx in KEYS}
        S = hardware.SPEC
        kw, kh = S["knob_w"] * k, S["knob_h"] * k
        self.knob = QRectF(round(m + (DEVICE_W - S["knob_right"]) * k - kw), round(m + DEVICE_H * k - kh),
                           round(kw), round(kh))
        c = max(28, round(30 * s))
        self.close = QRect(int(self.body.right() - c - 6 * k), int(self.body.top() + 4 * k), c, round(c * .8))
        g = max(18, round(26 * s))
        self.grip = QRect(self.size[0] - g - 2, self.size[1] - g - 2, g, g)


class Body:
    """The static picture of the device at one scale and finish, and its moving parts."""

    def __init__(self):
        self._cache: dict = {}
        self._knob: dict = {}
        self._keys: dict = {}

    # ---- the still picture
    def image(self, s: float, theme: str) -> QImage:
        key = (s, theme)
        img = self._cache.get(key)
        if img is None:
            if len(self._cache) > 3:
                self._cache.clear()
                self._keys.clear()
            img = self._cache[key] = self._render(s, theme)
        return img

    def _render(self, s: float, theme: str) -> QImage:
        g = Geometry(s)
        W, H = g.size
        case = _Case()
        case.shadow = False
        case.grooves = GROOVES
        case.margin = g.m
        case.scale = g.k
        case.theme = theme
        case.wheel = hardware.XM5Knob()                 # (only so the knob's cut gets plain face under it)
        case.resize(W, H)
        metal = case.render_case().copy()
        gs = case.geometry_spec()
        glass = hardware.Glass()
        glass.set_theme(theme)
        glass.set_scale(g.k)
        p = QPainter(metal)
        glass.paint_on(p, gs["glass"])
        p.end()
        up = flip(metal)
        out = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self._shadow(p, up, g)
        p.drawImage(0, 0, up)
        self._wordmark(p, case, gs, g, theme, H)
        for name in g.keys:
            self._label(p, name, g)
        p.end()
        case.deleteLater()
        return out

    @staticmethod
    def _shadow(p: QPainter, up: QImage, g: Geometry) -> None:
        """A soft shadow under the body: its alpha, shrunk, blurred by resampling, offset downwards."""
        w, h = up.width(), up.height()
        a = up.convertToFormat(QImage.Format_Alpha8)
        lo = a.scaled(max(1, w // 10), max(1, h // 10), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        sh = QImage(lo.size(), QImage.Format_ARGB32_Premultiplied)
        sh.fill(Qt.transparent)
        q = QPainter(sh)
        q.drawImage(0, 0, lo)
        q.setCompositionMode(QPainter.CompositionMode_SourceIn)
        q.fillRect(sh.rect(), QColor(0, 0, 0, 120))
        q.end()
        p.save()
        p.setOpacity(.85)
        p.drawImage(QRectF(0, 4 * g.s + 2, w, h), sh)
        p.restore()

    @staticmethod
    def _wordmark(p: QPainter, case, gs: dict, g: Geometry, theme: str, H: int) -> None:
        """ONIX × TITANIUM, upright, where the flipped case puts its block (the bottom-left)."""
        eng = hardware.Engraving()
        eng.set_theme(theme)
        eng.set_scale(g.k)
        body = gs["body"]
        x = body.left() + g.k * eng.origin_x
        y = body.top() + g.k * eng.origin_y                    # window orientation
        y_up = H - y - eng.height()
        img = QImage(eng.width(), eng.height(), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        eng.render(img, QPoint(0, 0), QRegion(), QWidget.RenderFlag.DrawChildren)
        p.drawImage(QPointF(x, y_up), img)
        eng.deleteLater()

    @staticmethod
    def _label(p: QPainter, name: str, g: Geometry) -> None:
        """The key's symbol printed on the glass (photo tone)."""
        r = g.labels[name]
        k = g.k
        c = QColor(124, 113, 98)
        p.save()
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        cx, cy = r.center().x(), r.center().y()
        h = 13.5 * k                                           # glyph height
        tw = 10.5 * k                                          # one triangle's width
        bar = 2.6 * k

        def tri(x0, pointing_left):
            path = QPainterPath()
            if pointing_left:
                path.moveTo(x0 + tw, cy - h / 2)
                path.lineTo(x0, cy)
                path.lineTo(x0 + tw, cy + h / 2)
            else:
                path.moveTo(x0, cy - h / 2)
                path.lineTo(x0 + tw, cy)
                path.lineTo(x0, cy + h / 2)
            path.closeSubpath()
            p.drawPath(path)

        if name == "prev":
            x = cx - (bar + 0.8 * k + 2 * tw) / 2
            p.drawRect(QRectF(x, cy - h / 2, bar, h))
            tri(x + bar + 0.8 * k, True)
            tri(x + bar + 0.8 * k + tw, True)
        elif name == "next":
            x = cx - (bar + 0.8 * k + 2 * tw) / 2
            tri(x, False)
            tri(x + tw, False)
            p.drawRect(QRectF(x + 2 * tw + 0.8 * k, cy - h / 2, bar, h))
        elif name == "play":
            x = cx - (tw + 2.2 * k + 2 * bar + 2.2 * k) / 2
            tri(x, False)
            p.drawRect(QRectF(x + tw + 2.2 * k, cy - h / 2, bar, h))
            p.drawRect(QRectF(x + tw + 2.2 * k + bar + 2.2 * k, cy - h / 2, bar, h))
        else:
            f = QFont()
            f.setFamilies(["Arial", "Helvetica", "DejaVu Sans"])
            f.setBold(True)
            f.setPixelSize(max(6, round(15.5 * k)))
            p.setFont(f)
            p.setPen(c)
            p.drawText(r, int(Qt.AlignCenter), "Fn")
        p.restore()

    # ---- the keys (drawn: crisp at every size)
    def key(self, s: float, theme: str, pressed: bool) -> QImage:
        ck = (s, theme, pressed)
        img = self._keys.get(ck)
        if img is None:
            g = Geometry(s)
            r = next(iter(g.keys.values()))
            img = QImage(r.width(), r.height(), QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            p = QPainter(img)
            p.setRenderHints(QPainter.Antialiasing)
            self._paint_key(p, QRectF(0, 0, r.width(), r.height()), g.k, theme, pressed)
            p.end()
            self._keys[ck] = img
        return img

    @staticmethod
    def _paint_key(p: QPainter, r: QRectF, k: float, theme: str, pressed: bool) -> None:
        dark = theme == "aluminum"
        tone = (lambda c: QColor(*[max(0, int(v * .36)) for v in c])) if dark else (lambda c: QColor(*c))
        dim = .93 if pressed else 1.0

        def c(*rgb):
            q = tone(rgb)
            return QColor(int(q.red() * dim), int(q.green() * dim), int(q.blue() * dim))
        rad = 10.5 * k
        # outer frame
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, c(236, 228, 219))
        g.setColorAt(1, c(214, 205, 195))
        p.setPen(QPen(c(150, 140, 130), max(1.0, .9 * k)))
        p.setBrush(g)
        p.drawRoundedRect(r.adjusted(.5, .5, -.5, -.5), rad, rad)
        # the groove round the face
        inset = 4.2 * k
        p.setPen(QPen(c(96, 88, 80), max(1.0, 1.1 * k)))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(inset, inset, -inset, -inset), rad - inset * .6, rad - inset * .6)
        face = r.adjusted(inset + 1.2 * k, inset + 1.2 * k, -inset - 1.2 * k, -inset - 1.2 * k)
        g = QLinearGradient(face.topLeft(), face.bottomLeft())
        g.setColorAt(0, c(230, 222, 212))
        g.setColorAt(1, c(220, 211, 201))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(face, rad * .55, rad * .55)
        # the concave dish: dark at the upper-left rim, light towards the lower right
        cx, cy = r.center().x(), r.center().y() + (.6 * k if pressed else 0)
        R = 30.5 * k
        dish = QRectF(cx - R, cy - R, 2 * R, 2 * R)
        rg = QRadialGradient(QPointF(cx + R * .18, cy + R * .25), R * 1.25)
        rg.setColorAt(0, c(232, 224, 214))
        rg.setColorAt(.55, c(216, 207, 197))
        rg.setColorAt(.85, c(176, 165, 155) if not pressed else c(160, 150, 140))
        rg.setColorAt(1, c(146, 136, 126))
        p.setBrush(rg)
        p.drawEllipse(dish)
        p.setPen(QPen(c(238, 232, 224), max(1.0, .8 * k)))
        p.setBrush(Qt.NoBrush)
        p.drawArc(dish.adjusted(.6 * k, .6 * k, -.6 * k, -.6 * k), -30 * 16, -120 * 16)
        # the pointer at nine o'clock
        path = QPainterPath()
        path.moveTo(cx - R + 1.5 * k, cy - 4.2 * k)
        path.lineTo(cx - R * .33, cy + .8 * k)
        path.lineTo(cx - R + 1.5 * k, cy + 1.2 * k)
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(c(122, 112, 102))
        p.drawPath(path)

    # ---- the knob
    def knob_frames(self, theme: str) -> list[QImage]:
        if theme not in self._knob:
            finish = "aluminum" if theme == "aluminum" else "titanium"
            base = style.ASSETS / "knob_xl"
            if not (base / f"wheel_{finish}_0.png").exists():
                base = style.ASSETS / "knob"
            frames = []
            for i in range(hardware.XM5Knob.FRAMES):
                f = base / f"wheel_{finish}_{i}.png"
                if not f.exists():
                    f = base / f"knob_{finish}_{i}.png"
                frames.append(flip(QImage(str(f)).convertToFormat(QImage.Format_ARGB32_Premultiplied)))
            self._knob[theme] = frames
        return self._knob[theme]


__all__ = ["Body", "Geometry", "SCALES", "UNIT", "DEVICE_W", "DEVICE_H"]
