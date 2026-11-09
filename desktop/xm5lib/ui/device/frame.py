"""Player mode's window contents: the whole XM5, pixel exact, at one of the fixed scales.

Everything is drawn in physical pixels (``Body`` images and the screen's own image are blitted 1:1 on the
display, whatever Windows' scaling is): the body at the chosen scale, the four keys (pressed or not), the
knob's current frame, and the screen -- the ``Screen`` contents rendered into an image of exactly
720 * scale pixels, rotated as the firmware's screen rotation says, black while the screen is off.

Mouse: the screen gets finger input; the keys press and release (the controller decides what they do);
the knob turns with the wheel (or a drag along it) -- the wheel anywhere on the metal turns it too; the
bottom-right corner resizes the whole player in fixed steps; the hidden x at the top right leaves player
mode; dragging the metal moves the window; right click on the metal opens the window menu.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen, QTransform
from PySide6.QtWidgets import QWidget

from ..i18n import tr
from .body import SCALES, Body, Geometry
from .screen import SIZE, Screen

KEY_CODES = ("prev", "play", "next", "fn")


class DeviceFrame(QWidget):
    exitRequested = Signal()
    sizeWanted = Signal(QSize)              # logical size the window should take (scale or display changed)
    menuRequested = Signal(QPoint)          # global position, right click on the metal
    keyPressed = Signal(str)                # "prev" / "play" / "next" / "fn"
    keyReleased = Signal(str, bool)         # key, long press
    knobTurned = Signal(int)                # notches (+: clockwise = louder)
    scaleChanged = Signal(float)

    LONG_MS = 800

    def __init__(self, parent=None, scale: float = 1.0, theme: str = "titanium"):
        super().__init__(parent)
        self.setObjectName("xm5device")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.body = Body()
        self.screen = Screen(self)
        self.screen.host = self
        self.screen.dirty.connect(self._screen_dirty)
        self.s = scale if scale in SCALES else 1.0
        self.g = Geometry(self.s)
        self.theme = theme
        self.rotation = 0                   # quarter turns of the screen contents (firmware's ROTATEMODE)
        self.screen_on = True
        self.pressed_key: str | None = None
        self._key_long = QTimer(self, singleShot=True, interval=self.LONG_MS, timeout=self._key_long_fired)
        self._key_was_long = False
        self.knob_frame = 0
        self._knob_pending = 0
        self._knob_timer = QTimer(self, interval=14, timeout=self._knob_tick)
        self._knob_drag = None
        self._drag = None                   # "screen" | "grip" | "key" | "knob"
        self._grip0 = None
        self._hover_x = False
        self._hover_grip = False
        self._img: QImage | None = None     # the screen's pixels
        self._img_dirty: list[QRectF] | None = None   # screen rects to redraw; None = all
        self._wheel_acc = 0
        self.typer = None                   # key event -> bool (the controller: typing into a field)
        self.editing = None                 # () -> bool: a field has the focus (Backspace deletes, not "back")

    # ------------------------------------------------------------------ size
    def dpr(self) -> float:
        return self.devicePixelRatioF() or 1.0

    def logical_size(self) -> QSize:
        d = self.dpr()
        w, h = self.g.size
        return QSize(math.ceil(w / d), math.ceil(h / d))

    def sizeHint(self) -> QSize:
        return self.logical_size()

    def set_scale(self, s: float) -> None:
        if s not in SCALES:
            s = min(SCALES, key=lambda x: abs(x - s))
        if s == self.s and self._img is not None:
            return
        self.s = s
        self.g = Geometry(s)
        self._img = None
        self.sizeWanted.emit(self.logical_size())
        self.scaleChanged.emit(s)
        self.update()

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self.update()

    def set_rotation(self, quarter_turns: int) -> None:
        self.rotation = quarter_turns % 4
        self._img_dirty = None
        self.update()

    def set_screen_on(self, on: bool) -> None:
        self.screen_on = on
        self.screen.active = on
        self._img_dirty = None
        self.update(self._to_logical(self.g.screen))

    # ------------------------------------------------------------------ coordinates
    def _phys(self, pos) -> QPointF:
        d = self.dpr()
        return QPointF(pos.x() * d, pos.y() * d)

    def _to_logical(self, r: QRect | QRectF) -> QRect:
        d = self.dpr()
        r = QRectF(r)
        return QRectF(r.x() / d, r.y() / d, r.width() / d, r.height() / d).toAlignedRect().adjusted(-1, -1, 1, 1)

    def _rot(self) -> QTransform:
        """Screen pixels -> unrotated screen pixels (what the panel shows)."""
        t = QTransform()
        t.translate(SIZE / 2, SIZE / 2)
        t.rotate(90 * self.rotation)
        t.translate(-SIZE / 2, -SIZE / 2)
        return t

    def _screen_point(self, phys: QPointF) -> QPointF:
        sr = self.g.screen
        p = QPointF((phys.x() - sr.x()) / self.s, (phys.y() - sr.y()) / self.s)
        inv, _ = self._rot().inverted()
        return inv.map(p)

    def _screen_dirty(self, rect) -> None:
        if rect is None or self._img_dirty is None:
            self._img_dirty = None
        else:
            self._img_dirty.append(rect)
        if rect is None:
            self.update(self._to_logical(self.g.screen))
            return
        r = self._rot().mapRect(QRectF(rect)).adjusted(-2, -2, 2, 2)
        sr = self.g.screen
        phys = QRectF(sr.x() + r.x() * self.s, sr.y() + r.y() * self.s, r.width() * self.s, r.height() * self.s)
        self.update(self._to_logical(phys))

    # ------------------------------------------------------------------ painting
    def _render_screen(self) -> QImage:
        side = self.g.screen.width()
        if self._img is None or self._img.width() != side:
            self._img = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
            self._img_dirty = None
        if self._img_dirty == []:
            return self._img
        p = QPainter(self._img)
        p.scale(self.s, self.s)
        p.setTransform(self._rot(), True)
        if self._img_dirty is None:
            clips = [QRectF(0, 0, SIZE, SIZE)]
        else:
            clips = self._img_dirty
        self._img_dirty = []
        for c in clips:
            p.save()
            c = c.adjusted(-2, -2, 2, 2).intersected(QRectF(0, 0, SIZE, SIZE))
            p.setClipRect(c)
            self.screen.paint(p, c)
            p.restore()
        p.end()
        return self._img

    def paintEvent(self, e):
        d = self.dpr()
        p = QPainter(self)
        p.setRenderHints(QPainter.SmoothPixmapTransform | QPainter.Antialiasing)
        body = self.body.image(self.s, self.theme)
        body.setDevicePixelRatio(d)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.drawImage(QPointF(0, 0), body)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        g = self.g
        if self.pressed_key:
            k = self.body.key(self.s, self.theme, True)
            k.setDevicePixelRatio(d)
            r = g.keys[self.pressed_key]
            p.drawImage(QPointF(r.x() / d, r.y() / d), k)
        for name, r in g.keys.items():
            if name != self.pressed_key:
                k = self.body.key(self.s, self.theme, False)
                k.setDevicePixelRatio(d)
                p.drawImage(QPointF(r.x() / d, r.y() / d), k)
        frames = self.body.knob_frames(self.theme)
        kn = g.knob
        p.drawImage(QRectF(kn.x() / d, kn.y() / d, kn.width() / d, kn.height() / d), frames[self.knob_frame % len(frames)])
        sr = g.screen
        if self.screen_on:
            img = self._render_screen()
            img.setDevicePixelRatio(d)
            p.drawImage(QPointF(sr.x() / d, sr.y() / d), img)
        else:
            p.fillRect(QRectF(sr.x() / d, sr.y() / d, sr.width() / d, sr.height() / d), QColor(2, 2, 2))
        if self._hover_x:
            r = QRectF(g.close.x() / d, g.close.y() / d, g.close.width() / d, g.close.height() / d)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(196, 43, 28, 235))
            p.drawRoundedRect(r, 4, 4)
            p.setPen(QPen(QColor(255, 255, 255), 1.6))
            c = r.center()
            a = min(r.width(), r.height()) * .2
            p.drawLine(QPointF(c.x() - a, c.y() - a), QPointF(c.x() + a, c.y() + a))
            p.drawLine(QPointF(c.x() - a, c.y() + a), QPointF(c.x() + a, c.y() - a))
        if self._hover_grip or self._drag == "grip":
            r = QRectF(g.grip.x() / d, g.grip.y() / d, g.grip.width() / d, g.grip.height() / d)
            p.setPen(QPen(QColor(90, 90, 90, 170), 1.2))
            for i in range(3):
                o = 4 + i * 4
                p.drawLine(QPointF(r.right() - o, r.bottom() - 2), QPointF(r.right() - 2, r.bottom() - o))
            if self._drag == "grip":
                p.setPen(QColor(40, 40, 40))
                txt = f"{self.s:g}x"
                p.drawText(QRectF(r.x() - 60, r.y() - 22, 80, 20), int(Qt.AlignRight | Qt.AlignVCenter), txt)
        p.end()

    # ------------------------------------------------------------------ knob
    def turn_knob(self, notches: int) -> None:
        """Roll the knurl (one knurl period per notch) and report the turn (+ louder)."""
        self._knob_pending = max(-18, min(18, self._knob_pending + notches * 6))
        if not self._knob_timer.isActive():
            self._knob_timer.start()
        self.knobTurned.emit(notches)

    def _knob_tick(self) -> None:
        if not self._knob_pending:
            self._knob_timer.stop()
            return
        d = 1 if self._knob_pending > 0 else -1
        self._knob_pending -= d
        self.knob_frame = (self.knob_frame + d) % 6
        self.update(self._to_logical(self.g.knob))

    # ------------------------------------------------------------------ input
    def _where(self, phys: QPointF) -> str:
        g = self.g
        pt = phys.toPoint()
        if g.close.contains(pt):
            return "close"
        if g.grip.contains(pt):
            return "grip"
        if g.screen.contains(pt):
            return "screen"
        for name, r in g.keys.items():
            if r.contains(pt):
                return "key:" + name
        if g.knob.contains(phys):
            return "knob"
        if g.body.contains(phys):
            return "metal"
        return "outside"

    def mousePressEvent(self, e):
        phys = self._phys(e.position())
        where = self._where(phys)
        if e.button() == Qt.RightButton:
            if where == "screen" and self.screen_on:
                self.screen.press(self._screen_point(phys), Qt.RightButton)
            else:
                self.menuRequested.emit(e.globalPosition().toPoint())
            return
        if e.button() != Qt.LeftButton:
            return
        if where == "close":
            self._drag = "close"
        elif where == "grip":
            self._drag = "grip"
            self._grip0 = (e.globalPosition(), self.s, self.g.size)
            self.update()
        elif where == "screen":
            self._drag = "screen"
            if self.screen_on:
                self.screen.press(self._screen_point(phys))
            else:
                self.keyPressed.emit("screen")
        elif where.startswith("key:"):
            self._drag = "key"
            self.pressed_key = where[4:]
            self._key_was_long = False
            self._key_long.start()
            self.keyPressed.emit(self.pressed_key)
            self.update(self._to_logical(self.g.keys[self.pressed_key]))
        elif where == "knob":
            self._drag = "knob"
            self._knob_drag = phys.x()
        elif where == "metal":
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()

    def mouseMoveEvent(self, e):
        phys = self._phys(e.position())
        if self._drag is None:
            where = self._where(phys)
            hx, hg = where == "close", where == "grip"
            if hx != self._hover_x or hg != self._hover_grip:
                self._hover_x, self._hover_grip = hx, hg
                self.setToolTip(tr("退出播放器模式") if hx else (tr("拖动缩放播放器") if hg else ""))
                self.setCursor(Qt.SizeFDiagCursor if hg else (Qt.PointingHandCursor if hx else Qt.ArrowCursor))
                self.update(self._to_logical(self.g.close.adjusted(-2, -2, 2, 2)))
                self.update(self._to_logical(self.g.grip.adjusted(-30, -30, 2, 2)))
            return
        if self._drag == "screen" and self.screen_on:
            self.screen.move(self._screen_point(phys))
        elif self._drag == "grip":
            g0, s0, (w0, h0) = self._grip0
            d = self.dpr()
            dx = (e.globalPosition().x() - g0.x()) * d
            dy = (e.globalPosition().y() - g0.y()) * d
            want = s0 * max((w0 + dx) / w0, (h0 + dy) / h0)
            s = min(SCALES, key=lambda x: abs(x - want))
            if s != self.s:
                self.set_scale(s)
        elif self._drag == "knob":
            step = 18 * self.dpr()
            moved = phys.x() - self._knob_drag
            if abs(moved) >= step:
                n = int(moved / step)
                self._knob_drag += n * step
                self.turn_knob(n)

    def mouseReleaseEvent(self, e):
        phys = self._phys(e.position())
        drag, self._drag = self._drag, None
        if drag == "close":
            if self._where(phys) == "close":
                self.exitRequested.emit()
        elif drag == "screen":
            if self.screen_on:
                self.screen.release(self._screen_point(phys))
        elif drag == "key":
            key = self.pressed_key
            self._key_long.stop()
            self.pressed_key = None
            if key:
                self.update(self._to_logical(self.g.keys[key]))
                if not self._key_was_long:
                    self.keyReleased.emit(key, False)
        elif drag == "grip":
            self.update()

    def _key_long_fired(self) -> None:
        if self.pressed_key and self._drag == "key":
            self._key_was_long = True
            self.keyReleased.emit(self.pressed_key, True)

    def mouseDoubleClickEvent(self, e):
        phys = self._phys(e.position())
        if self._where(phys) in ("screen",) or self._where(phys).startswith("key:"):
            self.mousePressEvent(e)

    def wheelEvent(self, e):
        phys = self._phys(e.position())
        where = self._where(phys)
        dy = e.angleDelta().y() or e.angleDelta().x()
        if where == "screen" and self.screen_on and self.screen.wheel(self._screen_point(phys), dy):
            e.accept()
            return
        if where in ("screen", "knob", "metal", "key:prev", "key:play", "key:next", "key:fn"):
            if dy * self._wheel_acc < 0:
                self._wheel_acc = 0
            self._wheel_acc += dy
            n = int(self._wheel_acc / 120)
            if n:
                self._wheel_acc -= n * 120
                self.turn_knob(n)
        e.accept()

    def event(self, e):
        if e.type() == QEvent.ShortcutOverride and self.editing is not None and self.editing() and \
                (e.key() in (Qt.Key_Backspace, Qt.Key_Return, Qt.Key_Enter) or
                 (e.text() and e.text().isprintable() and not e.modifiers() & (Qt.ControlModifier | Qt.AltModifier))):
            e.accept()                      # the window's Backspace = back shortcut must not take it
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        if self.typer is not None and self.typer(e):
            e.accept()
            return
        super().keyPressEvent(e)

    def leaveEvent(self, _):
        if self._hover_x or self._hover_grip:
            self._hover_x = self._hover_grip = False
            self.setCursor(Qt.ArrowCursor)
            self.update()


__all__ = ["DeviceFrame", "KEY_CODES"]
