"""The window as an XM5 flipped top-to-bottom.

Read from the player (titanium / black aluminium editions):
- straight edges with a soft corner chamfer;
- 13 half-round notches bitten into each long side, along the screen;
- three through-grooves across the whole width, at the same pitch as the notches;
- a plain block beyond the grooves (here on top) carrying the model engraving;
- where the player has its cone knob, the ScopeWatch "language" roller sits, flush with the top edge.

The roller and the groove profile are ports of the ScopeWatch 2.0.2 CSS (rotary-control and the milled
seam), drawn with QPainter in CSS pixel units and scaled. The window is frameless and translucent so the
notched silhouette and the drop shadow show; moving/resizing use the system handlers.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap, QPolygonF
from PySide6.QtWidgets import QWidget

from . import style
from .i18n import tr

_TEXTURE: QPixmap | None = None

# geometry of the case (px)
SIDE = 30          # metal between the case edge and the glass, left/right
BOTTOM = 18        # metal below the glass (the player's top edge)
TOP = 106          # plain block above the grooves (the player's bottom block)
CORNER = 9         # soft chamfer
BEVEL = 4          # ScopeWatch --sw-bevel
NOTCHES = 13
WHEEL_SCALE = 1.25  # ScopeWatch wheel is 74x73 CSS px


def texture() -> QPixmap:
    global _TEXTURE
    if _TEXTURE is None:
        _TEXTURE = QPixmap(str(style.ASSETS / "sandblast.png"))
    return _TEXTURE


def metal(theme: str) -> dict:
    return style.METAL.get(theme, style.METAL["titanium"])


def rgb(c: QColor | str) -> tuple[float, float, float]:
    c = QColor(c)
    return c.redF() * 255, c.greenF() * 255, c.blueF() * 255


def mix(a, b, t: float, alpha: int = 255) -> QColor:
    """CSS color-mix(in srgb, a, b t)."""
    x, y = rgb(a), rgb(b)
    return QColor(*(round(x[i] * (1 - t) + y[i] * t) for i in range(3)), alpha)


def shade(base: str, normal, light=(-.48, -.31, .82)) -> QColor:
    """scopewatch-wheel-geometry.js: orthographic metal lighting from a surface normal."""
    def norm(v):
        n = math.hypot(*v) or 1
        return [x / n for x in v]
    n, l = norm(normal), norm(light)
    diffuse = max(0.0, sum(a * b for a, b in zip(n, l)))
    grazing = 1 - max(0.0, n[2])
    level = .48 + diffuse * .48 - grazing * .07
    spec = diffuse ** 15 * .12
    return QColor(*(round(max(0, min(255, ch * level + 255 * spec))) for ch in rgb(base)))


def wheel_surfaces(base: str) -> dict:
    s = math.sqrt(.5)
    return {"bevel_left": shade(base, (-s, 0, s)), "bevel_right": shade(base, (s, 0, s)),
            "wall_left": shade(base, (-1, 0, .08)), "wall_right": shade(base, (1, 0, .08)),
            "end_lip": shade(base, (0, 1, .16))}


def overlay_texture(p: QPainter, r: QRectF, opacity: float, mode=QPainter.CompositionMode_Overlay) -> None:
    tex = texture()
    if tex.isNull():
        return
    p.save()
    p.setCompositionMode(mode)
    p.setOpacity(opacity)
    p.drawTiledPixmap(r, tex)
    p.restore()


def paint_metal(p: QPainter, r: QRectF, theme: str, light: QRectF | None = None) -> None:
    """ScopeWatch instrument-module: base + milling + panel light field + sand-blast overlay.

    ``light`` anchors the light field (default: ``r``) so a taller case does not shift the shading on top.
    """
    m = metal(theme)
    p.fillRect(r, QColor(m["base"]))
    fill, r = r, (light or r)
    # --sw-panel-light-field: 157deg, 36 % white -> transparent 43 % -> 36 % black (softened for a large face)
    a = math.radians(157 - 90)
    d = QPointF(math.cos(a), math.sin(a))
    half = (abs(d.x()) * r.width() + abs(d.y()) * r.height()) / 2
    c = r.center()
    g = QLinearGradient(c - d * half, c + d * half)
    g.setColorAt(0, QColor(255, 255, 255, int(255 * m["light"])))
    g.setColorAt(.43, QColor(255, 255, 255, 0))
    g.setColorAt(1, QColor(0, 0, 0, int(255 * m["light"] * .8)))
    p.fillRect(fill, g)
    # --milling: 96deg, #ffffff1c -> transparent 35 % -> #00000011
    mill = QLinearGradient(r.topLeft(), QPointF(r.right(), r.top() + r.width() * .1))
    mill.setColorAt(0, QColor(255, 255, 255, 0x1c))
    mill.setColorAt(.35, QColor(255, 255, 255, 0))
    mill.setColorAt(1, QColor(0, 0, 0, 0x11))
    p.fillRect(fill, mill)
    overlay_texture(p, fill, m["texture"])


def seam(p: QPainter, x0: float, x1: float, y: float, theme: str, h: float = 10) -> None:
    """The ScopeWatch milled seam (``.device::before``) as one through-groove centred on ``y``."""
    m = metal(theme)
    k = m["seam"]
    r = QRectF(x0, y - h / 2, x1 - x0, h)
    g = QLinearGradient(r.topLeft(), r.bottomLeft())
    g.setColorAt(0, QColor(0, 0, 0, 0))
    g.setColorAt(.13, QColor(0, 0, 0, int(255 * k)))
    g.setColorAt(.39, QColor(0, 0, 0, int(255 * .31)))
    g.setColorAt(.51, QColor(0, 0, 0, int(255 * .46)))
    g.setColorAt(.74, QColor(255, 255, 255, int(255 * .13)))
    g.setColorAt(.89, QColor(255, 255, 255, int(255 * k)))
    g.setColorAt(1, QColor(255, 255, 255, 0))
    p.fillRect(r, g)
    # inset 0 1px 1px #0007, inset 0 -1px 1px #fff4
    p.fillRect(QRectF(r.left(), r.top(), r.width(), 1), QColor(0, 0, 0, 0x77 // 2))
    p.fillRect(QRectF(r.left(), r.bottom() - 1, r.width(), 1), QColor(255, 255, 255, 0x44 // 2))
    overlay_texture(p, r, m["texture"] * .62)


# ---- the case, in photo pixels (window orientation: y = 0 is the player's bottom edge)
# Spec units are the pixels of the super-resolved XM5 Ti front render (1254x1254, device 695 px wide); the
# templates themselves are sampled from two sharper quarter crops at 2.5 px per unit
# (tools/extract_case_hires.py). The glass values are the bezel lip, measured on the top-left crop.
SPEC = dict(
    corner_top=48,          # rows of the corner tiles at the window top (device bottom edge)
    corner_bottom=42,       # rows of the corner tiles at the window bottom (device top edge)
    edge_top=14, edge_bottom=8,
    tile=26,                # side tile width
    groove_top=161, groove_h=25, groove_pitch=57,
    glass_gap=25,           # groove -> glass rect; the unlit top edge turns dark 3 further in (at 28)
    notch_first=56, notch_pitch=59, notch_tail=74.5,
    glass_side=31.4, glass_bottom=15.4, glass_radius=20.4,     # outer edge of the bezel lip
    knob_right=36, knob_w=265, knob_h=112,  # the rendered, rolling wheel (v4-v7 sprites), flush with the top edge
)


# The hardware never rescales: s is fixed at the size it has in the default 1500x920 window (1 groove).
# Making the window taller only lengthens the screen part; a side notch is added each time a full
# notch pitch fits (keeping at least notch_tail of metal below the last one).
def spec_height(grooves: int) -> float:
    S = SPEC
    last_groove_c = S["groove_top"] + S["groove_pitch"] * (grooves - 1) + S["groove_h"] / 2
    last_notch = last_groove_c + S["notch_first"] + S["notch_pitch"] * (NOTCHES - 1)
    return last_notch + S["notch_tail"]


CASE_SCALE = (920 - 2 * 12) / spec_height(1)


def header_height(grooves: int) -> float:
    """Photo px from the top edge to the glass: the fixed part (block, knob, grooves)."""
    S = SPEC
    return S["groove_top"] + S["groove_pitch"] * (grooves - 1) + S["groove_h"] + S["glass_gap"]


class Templates:
    """Photo-sampled assets: (multiply image, colour-dodge image) per name, plus images and case.json.

    The templates are stored at ``meta["density"]`` px per spec unit (2.5 for the super-resolved crops);
    ``fit`` hands them out resampled to the exact device-pixel size they are drawn at (area-averaged when
    shrinking), so nothing is left to the painter's bilinear sampling.
    """

    _cache: dict | None = None
    _fitted: dict = {}

    @classmethod
    def get(cls) -> dict:
        if cls._cache is None:
            import json
            base = style.ASSETS / "case"
            cls._cache = {"meta": json.loads((base / "case.json").read_text(encoding="utf-8"))}
            for f in base.glob("*_mul.png"):
                name = f.name[:-8]
                mul = QImage(str(f)).convertToFormat(QImage.Format_RGB32)
                dodge = QImage(str(base / f"{name}_dodge.png")).convertToFormat(QImage.Format_RGB32)
                cls._cache[name] = (mul, dodge)
            mask = style.ASSETS / "knob" / "knob_face_mask.png"
            if mask.exists():
                grey = QImage(str(mask)).convertToFormat(QImage.Format_Grayscale8)
                cls._cache["knob_face_mask"] = _alpha_from_grey(grey).convertToFormat(
                    QImage.Format_ARGB32_Premultiplied)
        return cls._cache

    @classmethod
    def has(cls, name: str) -> bool:
        return name in cls.get()

    @classmethod
    def _resample(cls, img: QImage, w: int, h: int) -> QImage:
        if img.width() == w and img.height() == h:
            return img
        return img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    @classmethod
    def fit(cls, name: str, wd: float, hd: float):
        """The template ``name`` at wd x hd device px. One-row / one-column profiles keep that dimension
        (the painter stretches a constant exactly)."""
        item = cls.get()[name]
        ref = item[0] if isinstance(item, tuple) else item
        w = 1 if ref.width() == 1 else max(1, round(wd))
        h = 1 if ref.height() == 1 else max(1, round(hd))
        key = (name, w, h)
        hit = cls._fitted.get(key)
        if hit is None:
            if len(cls._fitted) > 600:
                cls._fitted.clear()
            hit = tuple(cls._resample(i, w, h) for i in item) if isinstance(item, tuple) else cls._resample(item, w, h)
            cls._fitted[key] = hit
        return hit


def _lookup(prof: list, d: float) -> float:
    """Linear interpolation in a per-row list at fractional row d (clamped)."""
    if not prof:
        return 0.0
    d = min(len(prof) - 1.0, max(0.0, d))
    lo = int(d)
    hi = min(lo + 1, len(prof) - 1)
    return prof[lo] + (prof[hi] - prof[lo]) * (d - lo)


class Case(QWidget):
    """The metal body, assembled from photo-sampled tiles on a photo-measured outline.

    Sides: corner tiles at both ends, one tile per side notch (a whole notch period, so tiles abut without
    seams), groove-end tiles, and a plain band between them. Middle: edge profiles and the through-groove
    profile. The outline uses the same per-row silhouette as the tiles, so no backdrop shows. The wheel and its
    arch cut are the XM5Knob widget on top (the edge profile stops under it). Shading is applied as a
    multiplier of the face colour (Multiply + ColorDodge), which is what makes both finishes work.

    The metal is rendered once per size/finish into an image and cut out with an anti-aliased mask of the
    outline (a clip path would leave stair steps on the notches); paintEvent only blits that image.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("case")
        self.theme = "titanium"
        self.margin = 12
        self.grooves = 1
        self.wheel: QWidget | None = None
        self.engraving: QWidget | None = None
        self.glass: QWidget | None = None
        self._img: QImage | None = None
        self._key = None
        self._dpr = 1.0
        self.scale: float | None = None     # px per spec unit; None = the window's fixed CASE_SCALE
        self.setMouseTracking(True)

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self.update()

    def set_grooves(self, n: int) -> None:
        self.grooves = 3 if n == 3 else 1
        self._place()
        self.update()

    def set_parts(self, wheel, engraving, glass) -> None:
        self.wheel, self.engraving, self.glass = wheel, engraving, glass
        for w in (wheel, engraving, glass):
            w.setParent(self)
        self._place()

    # ---- geometry
    def geometry_spec(self) -> dict:
        S = SPEC
        m = self.margin
        body = QRectF(self.rect()).adjusted(m, m, -m, -m)
        s = self.scale or CASE_SCALE
        t = body.top()
        d = self.devicePixelRatioF() or 1.0
        snap = lambda v: round(v * d) / d          # tiles start on device pixels; the outline follows them
        grooves = [snap(t + s * (S["groove_top"] + S["groove_pitch"] * i)) for i in range(self.grooves)]
        last_c = t + s * (S["groove_top"] + S["groove_pitch"] * (self.grooves - 1) + S["groove_h"] / 2)
        first = last_c + s * S["notch_first"]
        pitch = s * S["notch_pitch"]
        half = pitch / 2
        room = body.bottom() - s * S["notch_tail"] - first
        count = int(room / pitch + 1e-6) + 1 if room >= 0 else 0
        notches = [snap(first + pitch * k - half) + half for k in range(count)]
        glass_top = grooves[-1] + s * (S["groove_h"] + S["glass_gap"])
        glass = QRectF(body.left() + s * S["glass_side"], glass_top,
                       body.width() - 2 * s * S["glass_side"], body.bottom() - s * S["glass_bottom"] - glass_top)
        w, h = round(S["knob_w"] * s), round(S["knob_h"] * s)
        knob = QRectF(round(body.right() - s * S["knob_right"] - w), round(t), w, h)
        return {"body": body, "s": s, "grooves": grooves, "notches": notches, "glass": glass, "knob": knob}

    def min_body_height(self, glass_min: float = 360) -> float:
        """Smallest body that keeps the fixed header and a usable screen."""
        return CASE_SCALE * (header_height(self.grooves) + SPEC["glass_bottom"]) + glass_min

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place()

    def _place(self) -> None:
        g = self.geometry_spec()
        body, s = g["body"], g["s"]
        if self.glass is not None:
            self.glass.setGeometry(g["glass"].toAlignedRect())
            if hasattr(self.glass, "set_scale"):
                self.glass.set_scale(s)
        if self.wheel is not None:
            if isinstance(self.wheel, XM5Knob):
                self.wheel.setFixedSize(round(g["knob"].width()), round(g["knob"].height()))
                self.wheel.move(round(g["knob"].left()), round(g["knob"].top()))
            else:
                self.wheel.move(round(body.right() - 60 - self.wheel.width()), round(body.top()))
        if self.engraving is not None:
            if hasattr(self.engraving, "set_scale"):
                self.engraving.set_scale(s)
            self.engraving.move(round(body.left() + s * self.engraving.origin_x),
                                round(body.top() + s * self.engraving.origin_y))

    def _indent(self, y: float, g: dict, side: str) -> float:
        """Inward offset (px) of the outline at height y, from the tiles' measured silhouettes."""
        meta = Templates.get()["meta"]
        s, S = g["s"], SPEC
        D = meta.get("density", 1.0)               # indent samples per spec unit (sample centres at (i+.5)/D)

        def look(name, d):
            return s * _lookup(meta[f"{name}_{side}_indent"], d * D - .5)

        T, B = g["body"].top(), g["body"].bottom()
        d = (y - T) / s
        if d < S["corner_top"]:
            return look("corner_top", d)
        d = (B - y) / s
        if d < S["corner_bottom"]:
            return look("corner_bottom", S["corner_bottom"] - d)
        for gt in g["grooves"]:
            d = (y - gt) / s
            if 0 <= d < S["groove_h"]:
                return look("groove_end", d)
        half = S["notch_pitch"] / 2
        for c in g["notches"]:
            d = (y - c) / s + half
            if 0 <= d < 2 * half:
                return look("notch", d)
        return 0.0

    def body_path(self, step: float | None = None) -> QPainterPath:
        g = self.geometry_spec()
        b = g["body"]
        if not self.margin:                       # maximised: plain rectangle
            path = QPainterPath()
            path.addRect(b)
            return path
        step = step or .5 / (self.devicePixelRatioF() or 1.0)    # default: half a device pixel
        ys = []
        y = b.top()
        while y < b.bottom():
            ys.append(y)
            y += step
        ys.append(b.bottom())
        path = QPainterPath()
        path.moveTo(b.left() + self._indent(ys[0], g, "L"), b.top())
        for y in ys:
            path.lineTo(b.right() - self._indent(y, g, "R"), y)
        for y in reversed(ys):
            path.lineTo(b.left() + self._indent(y, g, "L"), y)
        path.closeSubpath()
        return path

    # ---- frameless window behaviour
    def _edges(self, pos) -> Qt.Edges:
        e = Qt.Edges()
        if self.margin == 0:
            return e
        x, y, w, h, k = pos.x(), pos.y(), self.width(), self.height(), self.margin + 4
        if x < k:
            e |= Qt.LeftEdge
        if x > w - k:
            e |= Qt.RightEdge
        if y < k:
            e |= Qt.TopEdge
        if y > h - k:
            e |= Qt.BottomEdge
        return e

    def mouseMoveEvent(self, e):
        edges = self._edges(e.position().toPoint())
        cursors = {Qt.LeftEdge: Qt.SizeHorCursor, Qt.RightEdge: Qt.SizeHorCursor, Qt.TopEdge: Qt.SizeVerCursor,
                   Qt.BottomEdge: Qt.SizeVerCursor, Qt.LeftEdge | Qt.TopEdge: Qt.SizeFDiagCursor,
                   Qt.RightEdge | Qt.BottomEdge: Qt.SizeFDiagCursor, Qt.RightEdge | Qt.TopEdge: Qt.SizeBDiagCursor,
                   Qt.LeftEdge | Qt.BottomEdge: Qt.SizeBDiagCursor}
        self.setCursor(cursors.get(edges, Qt.ArrowCursor))

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return super().mousePressEvent(e)
        handle = self.window().windowHandle()
        if handle is None:
            return
        edges = self._edges(e.position().toPoint())
        handle.startSystemResize(edges) if edges else handle.startSystemMove()

    def mouseDoubleClickEvent(self, e):
        if getattr(self.window(), "player_mode", False):     # the player has one size
            return
        if self.glass is not None and e.position().y() < self.glass.geometry().top():
            w = self.window()
            w.showNormal() if w.isMaximized() else w.showMaximized()

    # ---- painting
    def _snap(self, rect: QRectF) -> QRectF:
        """Snap to device pixels: neighbouring pieces then share exact edges (no doubled or skipped rows)."""
        d = self.devicePixelRatioF() or 1.0
        x0, y0 = round(rect.left() * d) / d, round(rect.top() * d) / d
        x1, y1 = round(rect.right() * d) / d, round(rect.bottom() * d) / d
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def _tpl(self, p: QPainter, name: str, rect: QRectF) -> None:
        rect = self._snap(rect)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        d = self._dpr
        mul, dodge = Templates.fit(name, rect.width() * d, rect.height() * d)
        p.setCompositionMode(QPainter.CompositionMode_Multiply)
        p.drawImage(rect, mul)
        p.setCompositionMode(QPainter.CompositionMode_Screen if self.theme == "aluminum"
                             else QPainter.CompositionMode_ColorDodge)
        p.drawImage(rect, dodge)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)

    def _image(self, w: int, h: int) -> QImage:
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(self._dpr)
        img.fill(Qt.transparent)
        return img

    def render_case(self) -> QImage:
        """The whole widget (shadow + metal) at device resolution; cached until size/finish/layout change."""
        d = self.devicePixelRatioF() or 1.0
        key = (self.width(), self.height(), d, self.theme, self.grooves, self.margin,
               isinstance(self.wheel, XM5Knob), self.scale)
        if self._img is not None and key == self._key:
            return self._img
        self._dpr = d
        W, H = max(1, round(self.width() * d)), max(1, round(self.height() * d))
        g = self.geometry_spec()
        path = self.body_path()
        out = self._image(W, H)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        if self.margin and getattr(self, "shadow", True):
            # soft drop shadow: the outline filled at half resolution, box-averaged down to 1/8 and stretched
            # back (bilinear) -- a ~8 px falloff for a few ms, where stroking the notched outline took ~150
            w, h = self.width(), self.height()
            lo = QImage(max(1, w // 2), max(1, h // 2), QImage.Format_ARGB32_Premultiplied)
            lo.fill(Qt.transparent)
            sp = QPainter(lo)
            sp.setRenderHint(QPainter.Antialiasing)
            sp.scale(lo.width() / w, lo.height() / h)
            sp.translate(0, 3)
            sp.fillPath(self.body_path(step=2.0), QColor(0, 0, 0, 160))
            sp.end()
            lo = lo.scaled(max(1, w // 8), max(1, h // 8), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(QRectF(0, 0, w, h), lo)
        metal = self._image(W, H)
        q = QPainter(metal)
        body = g["body"]
        paint_metal(q, body, self.theme,
                    QRectF(body.left(), body.top(), body.width(), g["s"] * spec_height(self.grooves)))
        q.end()
        # the wheel's cut: keep the plain face under it (the edge strip runs on only beside the cut and as a
        # thin dark line across it -- see tools/fix_knob_junction.py)
        face, kr = None, None
        if isinstance(self.wheel, XM5Knob) and Templates.has("knob_face_mask"):
            k = g["knob"]
            kr = QRect(round(k.left() * d), round(k.top() * d), round(k.width() * d), round(k.height() * d))
            face = metal.copy(kr)
            face.setDevicePixelRatio(1.0)
            fm = QPainter(face)
            fm.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            fm.drawImage(0, 0, Templates.fit("knob_face_mask", kr.width(), kr.height()))
            fm.end()
        q = QPainter(metal)
        q.setRenderHint(QPainter.SmoothPixmapTransform)
        self._paint_metal(q, g)
        if face is not None:
            q.drawImage(QRectF(kr.x() / d, kr.y() / d, kr.width() / d, kr.height() / d), face)
        if self.margin:                                   # anti-aliased outline
            mask = self._image(W, H)
            mp = QPainter(mask)
            mp.setRenderHint(QPainter.Antialiasing)
            mp.fillPath(path, QColor(0, 0, 0))
            mp.end()
            q.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            q.drawImage(QPointF(0, 0), mask)
        q.end()
        p.drawImage(QPointF(0, 0), metal)
        p.end()
        self._img, self._key = out, key
        return out

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawImage(QPointF(0, 0), self.render_case())

    def _paint_metal(self, p: QPainter, g: dict) -> None:
        """Everything on the face colour: side tiles, bands, grooves, edges (paint_metal comes first)."""
        body, s, S = g["body"], g["s"], SPEC
        L, R, T, B = body.left(), body.right(), body.top(), body.bottom()
        tw = S["tile"] * s
        ct, cb = S["corner_top"] * s, S["corner_bottom"] * s
        gh = S["groove_h"] * s
        half = S["notch_pitch"] * s / 2
        # side tiles; the plain band fills what the tiles leave: "band" along the block and between grooves,
        # "band_lead" from the last groove to the first notch, "band_s" along the screen
        tiles = [(T, T + ct, "corner_top")] + [(y, y + gh, "groove_end") for y in g["grooves"]] + \
                [(c - half, c + half, "notch") for c in g["notches"]] + [(B - cb, B, "corner_bottom")]
        tiles.sort()
        screen = g["notches"][0] - half if g["notches"] else g["grooves"][-1] + gh

        def band(a, b):
            name = "band"
            if a >= screen - .01 and Templates.has("band_s_L"):
                name = "band_s"
            elif abs(b - screen) < .01 and Templates.has("band_lead_L"):
                name = "band_lead"
            self._tpl(p, f"{name}_L", QRectF(L, a, tw, b - a))
            self._tpl(p, f"{name}_R", QRectF(R - tw, a, tw, b - a))

        y = T
        for a, b, name in tiles:
            if a > y:
                band(y, a)
            self._tpl(p, f"{name}_L", QRectF(L, a, tw, b - a))
            self._tpl(p, f"{name}_R", QRectF(R - tw, a, tw, b - a))
            y = max(y, b)
        if y < B:
            band(y, B)
        # through-grooves
        for gt in g["grooves"]:
            self._tpl(p, "groove", QRectF(L + tw, gt, body.width() - 2 * tw, gh))
        # the player's bottom edge (window top), right across: under the wheel's cut render_case puts the
        # plain face back except for the strip's outer dark line; the XM5Knob widget draws cut and wheel on top
        et = S["edge_top"] * s
        self._tpl(p, "edge_top", QRectF(L + tw, T, body.width() - 2 * tw, et))
        # the player's top edge (window bottom)
        eb = S["edge_bottom"] * s
        self._tpl(p, "edge_bottom", QRectF(L + tw, B - eb, body.width() - 2 * tw, eb))


class LanguageWheel(QWidget):
    """ScopeWatch ``.rotary-control`` (the language wheel), ported 1:1 in CSS px and scaled.

    A cylinder seen in front elevation inside a slot that opens at the case edge. Rolling it moves the knurl
    by 21 px per step with the same 0.42 s ease as ``groove-forward/back``; it emits ``rolled(pixels)``.
    """

    rolled = Signal(int)
    W, H, FACE, BEV, WALL, CUT = 74, 73, 79, 9, 12, 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme = "titanium"
        self.groove = 0.0
        self._from = 0.0
        self._drag = None
        self._anim = QVariantAnimation(self, duration=420)
        self._anim.setEasingCurve(QEasingCurve(QEasingCurve.BezierSpline))
        curve = QEasingCurve(QEasingCurve.BezierSpline)
        curve.addCubicBezierSegment(QPointF(.2, .72), QPointF(.25, 1), QPointF(1, 1))
        self._anim.setEasingCurve(curve)
        self._anim.valueChanged.connect(self._step)
        self.setCursor(Qt.SizeHorCursor)
        self.setToolTip(tr("滚轮：滚动或左右拖动来翻页"))

    def set_theme(self, theme):
        self.theme = theme
        self.update()

    def _step(self, v):
        self.groove = float(v)
        self.update()

    def turn(self, direction: int, pixels: int) -> None:
        start = self.groove
        self._anim.stop()
        self._anim.setStartValue(start)
        self._anim.setEndValue(start + 21 * (1 if direction >= 0 else -1))
        self._anim.start()
        self.rolled.emit(pixels)

    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.angleDelta().x()
        if dy:
            self.turn(-1 if dy > 0 else 1, int(-dy * .8))

    def mousePressEvent(self, e):
        self._drag = e.position().x()

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        x = e.position().x()
        dx = x - self._drag
        self._drag = x
        self._anim.stop()
        self.groove += dx / WHEEL_SCALE
        self.update()
        self.rolled.emit(int(-dx * 3))

    def mouseReleaseEvent(self, _):
        self._drag = None

    def paintEvent(self, _):
        m = metal(self.theme)
        u = m["base"]                                 # --popup-underlay
        act = m["wheel"]                              # --active-wheel
        surf = wheel_surfaces(u)
        W, H, F, B, CUT = self.W, self.H, self.FACE, self.BEV, self.CUT
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(self.width() / W, self.height() / H)
        clip = QPainterPath()
        clip.addPolygon(QPolygonF([QPointF(CUT, 0), QPointF(W - CUT, 0), QPointF(W, CUT), QPointF(W, H),
                                   QPointF(0, H), QPointF(0, CUT)]))
        clip.closeSubpath()
        p.setClipPath(clip)
        # .rotary-pocket
        p.fillRect(QRectF(0, 0, W, H), mix(u, "#000", .59))
        # .rotary-body
        x0 = (W - F) / 2
        body = QRectF(x0, -1, F, H + 2)
        hi, mid_hi, lo = mix(act, "#fff", .48), mix(act, "#fff", .18), mix(act, "#000", .58)
        g = QLinearGradient(body.topLeft(), body.topRight())
        for t, c in ((0, lo), (.05, mix(act, "#000", .31)), (.17, mix(act, "#000", .09)), (.31, mid_hi), (.47, hi),
                     (.55, mix(act, "#fff", .12)), (.68, QColor(act)), (.84, mix(act, "#000", .22)), (1, lo)):
            g.setColorAt(t, c)
        p.fillRect(body, g)
        for side, c in ((0, mix(act, "#000", .58)), (1, mix(act, "#000", .68))):     # inset ±2px 0 3px
            sg = QLinearGradient(QPointF(body.left() if side == 0 else body.right(), 0),
                                 QPointF(body.left() + 5 if side == 0 else body.right() - 5, 0))
            c0 = QColor(c)
            c1 = QColor(c)
            c1.setAlpha(0)
            sg.setColorAt(0, c0)
            sg.setColorAt(1, c1)
            p.fillRect(QRectF(body.left(), body.top(), 5, body.height()) if side == 0 else
                       QRectF(body.right() - 5, body.top(), 5, body.height()), sg)
        # ::before knurl: repeating-linear-gradient(90deg, transparent 0 2px, #05080b4d 2 3, #fff4 3 4, transparent 4 7)
        p.save()
        p.setClipRect(body, Qt.IntersectClip)
        p.setCompositionMode(QPainter.CompositionMode_Overlay)
        start = body.left() - 20 + (self.groove % 7) - 7
        x = start
        while x < body.right() + 20:
            p.fillRect(QRectF(x + 2, body.top(), 1, body.height()), QColor(5, 8, 11, 0x4d))
            p.fillRect(QRectF(x + 3, body.top(), 1, body.height()), QColor(255, 255, 255, 0x44))
            x += 7
        # ::after (multiply): dark flanks + top/bottom edge lines
        p.setCompositionMode(QPainter.CompositionMode_Multiply)
        fl = QLinearGradient(body.topLeft(), body.topRight())
        fl.setColorAt(0, QColor(5, 8, 11, 0x80))
        fl.setColorAt(.15, QColor(255, 255, 255, 0))
        fl.setColorAt(.77, QColor(255, 255, 255, 0))
        fl.setColorAt(1, QColor(5, 8, 11, 0x94))
        p.fillRect(body, fl)
        vg = QLinearGradient(QPointF(0, body.top()), QPointF(0, body.bottom()))
        top_c, bot_c, clear = mix(act, "#fff", .28), mix(act, "#000", .48), QColor(255, 255, 255)
        e1, e2, e3 = 1 / body.height(), 2 / body.height(), 3 / body.height()
        vg.setStops([(0, top_c), (e1, top_c), (e2, clear), (1 - e3, clear), (1 - e1, bot_c), (1, bot_c)])
        p.fillRect(body, vg)
        p.restore()
        # .rotary-wall-left/-right live inside .rotary-pocket (z-index 0), under the wider body: not visible.
        # .rotary-bevel-left: surface gradient, multiplied sand-blast, 1 px inner edge line
        bl = surf["bevel_left"]
        left = QRectF(0, 0, B, H)
        g = QLinearGradient(left.topLeft(), left.topRight())
        g.setColorAt(0, mix(bl, "#fff", .05))
        g.setColorAt(.58, bl)
        g.setColorAt(1, mix(bl, "#000", .13))
        p.fillRect(left, g)
        p.save()
        p.setClipRect(left, Qt.IntersectClip)
        overlay_texture(p, left, m["texture"] * 1.18, QPainter.CompositionMode_Multiply)
        p.restore()
        line = QLinearGradient(QPointF(0, 0), QPointF(0, H))
        line.setColorAt(0, mix(u, "#fff", .18))
        line.setColorAt(.52, mix(u, "#000", .21))
        line.setColorAt(1, mix(u, "#fff", .07))
        p.fillRect(QRectF(B - 1, 0, 1, H), line)
        # .rotary-bevel-right (2.0 renderer): metal + 16 % black + bloom profile toward the outer edge
        right = QRectF(W - B, 0, B, H)
        p.fillRect(right, QColor(u))
        p.fillRect(right, QColor(0, 0, 0, int(255 * .16)))
        bloom = QLinearGradient(right.topLeft(), right.topRight())
        sigma = .045 + .48 * .31          # --sw-bloom-scatter 31 %
        decay = 1 - .8 * ((289 + H / 2) / (301 + 308))     # attenuation at the wheel's place in the device
        for i in range(17):
            t = i / 16
            bloom.setColorAt(t, QColor(255, 255, 255, int(255 * decay * math.exp(-.5 * ((1 - t) / sigma) ** 2))))
        p.fillRect(right, bloom)
        p.save()
        p.setClipRect(right, Qt.IntersectClip)
        overlay_texture(p, right, .3, QPainter.CompositionMode_SoftLight)
        p.restore()
        # .rotary-shell::after: end lip
        lip = surf["end_lip"]
        g = QLinearGradient(QPointF(0, H - 2), QPointF(0, H))
        g.setColorAt(0, mix(lip, "#fff", .09))
        g.setColorAt(1, lip)
        p.setOpacity(.82)
        p.fillRect(QRectF(0, H - 2, W, 2), g)
        p.setOpacity(1)
        # the 45-degree cut corners (wheel-housing-facet) stay clipped: the case metal shows through


class XM5Knob(QWidget):
    """The XM5 Ti volume wheel: a gold, diamond-knurled bullet in a chamfered arch cut through the case edge.

    Pre-rendered sprites (``tools/render_knob.py``, fitted to a photo of the XM5 Ti) at 2x; the chamfers are
    alpha overlays so the case metal shows through. The mouse wheel turns it: each notch rolls the knurl
    ROLL frames (a quick animation) and emits ``rolled(STEP)`` -- the player's volume moves 6 per notch.
    This is the v4-v7 wheel, restored in v10 at the user's request (the photo knob of v8-v10 could not roll).
    """

    rolled = Signal(int)
    W, H, FRAMES = 204, 86, 6
    STEP = 6                 # volume per notch (v20: twice v19)
    ROLL = 6                 # knurl frames per notch (one full knurl turn), one every 12 ms

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme = "titanium"
        self.frame = 0
        self._sprites: dict[str, list[QPixmap]] = {}
        self._pending = 0
        self._acc = 0
        self._roll = QTimer(self, interval=12, timeout=self._tick)
        self.resize(self.W, self.H)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def _frames(self) -> list[QPixmap]:
        finish = self.theme if self.theme in ("titanium", "aluminum") else \
            ("aluminum" if self.theme == "sw_dark" else "titanium")
        if finish not in self._sprites:
            base = style.ASSETS / "knob"
            # wheel_*: the v4-v7 sprites with the junction to the edge strip cut as on the photo
            # (tools/fix_knob_junction.py); the untouched originals are knob_* (old-assets-v7/knob-v7)
            name = "wheel" if (base / f"wheel_{finish}_0.png").exists() else "knob"
            self._sprites[finish] = [QPixmap(str(base / f"{name}_{finish}_{i}.png")) for i in range(self.FRAMES)]
        return self._sprites[finish]

    def set_theme(self, theme):
        self.theme = theme
        self.update()

    def turn(self, steps: int) -> None:
        """``steps`` notches (positive: down/quieter)."""
        self._pending = max(-3 * self.ROLL, min(3 * self.ROLL, self._pending + steps * self.ROLL))
        if not self._roll.isActive():
            self._tick()
            self._roll.start()
        self.rolled.emit(steps * self.STEP)

    def _tick(self) -> None:
        if not self._pending:
            self._roll.stop()
            return
        d = 1 if self._pending > 0 else -1
        self._pending -= d
        self.frame = (self.frame + d) % self.FRAMES
        self.update()

    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.angleDelta().x()
        if dy * self._acc < 0:                       # direction changed: start counting again
            self._acc = 0
        self._acc += dy
        notches = int(self._acc / 120)               # touchpads send small steps: one notch per 120
        if notches:
            self._acc -= notches * 120
            self.turn(-notches)
        e.accept()

    def mousePressEvent(self, e):
        e.ignore()                      # dragging the knob area still moves the window

    def paintEvent(self, _):
        pix = self._frames()[self.frame]
        if pix.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(self.rect(), pix)

class Engraving(QWidget):
    """The printed mark "onix x TITANIUM" / "onix x ALUMINUM", traced from the product photo (alpha masks).

    Clicking the material word switches the finish (``clicked``); elsewhere the mouse falls through to the
    case so the window can still be dragged.
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme = "titanium"
        self.s = 1.0
        self.hover = False
        self._masks: dict[str, QImage] = {}
        self._tinted: dict[tuple, QImage] = {}
        w = Templates.get()["meta"]["wordmark"]
        self.k = w["scale"]                      # mask px per photo px
        self.origin_x, self.origin_y = w["mark_x"], w["top"]
        self.word_dx = w["word_x"] - w["mark_x"]
        self.setMouseTracking(True)
        self.set_scale(.85)

    def _mask(self, name: str):
        if name not in self._masks:
            self._masks[name] = QImage(str(style.ASSETS / "case" / f"word_{name}.png")).convertToFormat(
                QImage.Format_Grayscale8)
        return self._masks[name]

    def _tint(self, name: str, colour: QColor):
        key = (name, colour.rgb())
        if key not in self._tinted:
            m = self._mask(name)
            img = QImage(m.size(), QImage.Format_ARGB32_Premultiplied)
            img.fill(colour)
            q = QPainter(img)
            q.setCompositionMode(QPainter.CompositionMode_DestinationIn)
            q.drawImage(0, 0, _alpha_from_grey(m))
            q.end()
            self._tinted[key] = img
        return self._tinted[key]

    def _word(self) -> str:
        return "aluminum" if self.theme == "aluminum" else "titanium"

    def set_theme(self, theme):
        self.theme = theme
        self.update()

    def set_scale(self, s: float) -> None:
        self.s = s
        w = (self.word_dx + max(self._mask("titanium").width(), self._mask("aluminum").width()) / self.k) * s
        self.setFixedSize(int(w) + 2, int(self._mask("mark").height() / self.k * s) + 2)
        self.update()

    def _rect(self, name: str, dx: float) -> QRectF:
        m = self._mask(name)
        return QRectF(dx * self.s, 0, m.width() / self.k * self.s, m.height() / self.k * self.s)

    def label_rect(self) -> QRectF:
        return self._rect(self._word(), self.word_dx).adjusted(-3, -3, 3, 3)

    def _over(self, pos) -> bool:
        return self.label_rect().contains(QPointF(pos))

    def mouseMoveEvent(self, e):
        over = self._over(e.position())
        if over != self.hover:
            self.hover = over
            self.setCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor)
            self.setToolTip(tr("切换 钛 / 铝 配色") if over else "")
            self.update()
        e.ignore()

    def leaveEvent(self, _):
        if self.hover:
            self.hover = False
            self.setCursor(Qt.ArrowCursor)
            self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._over(e.position()):
            e.accept()
        else:
            e.ignore()                       # falls through to the case: move the window

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._over(e.position()):
            self.clicked.emit()
            e.accept()
        else:
            e.ignore()

    def mouseDoubleClickEvent(self, e):
        e.accept() if self._over(e.position()) else e.ignore()

    def paintEvent(self, _):
        m = metal(self.theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        gold = QColor(m["etch"])
        p.drawImage(self._rect("mark", 0), self._tint("mark", gold))
        word = self._word()
        p.drawImage(self._rect(word, self.word_dx), self._tint(word, gold.lighter(118) if self.hover else gold))


def _alpha_from_grey(grey):
    """A grey mask as an ARGB image whose alpha is the grey value (for DestinationIn)."""
    bpl = grey.bytesPerLine()
    data = bytes(grey.constBits())[:bpl * grey.height()]
    w, h = grey.width(), grey.height()
    argb = bytearray(w * h * 4)
    for y in range(h):
        row = data[y * bpl:y * bpl + w]
        o = y * w * 4
        argb[o + 3:o + 4 * w:4] = row          # BGRA little-endian: alpha is the 4th byte
    img = QImage(bytes(argb), w, h, w * 4, QImage.Format_ARGB32)
    return img.copy()


class Glass(QWidget):
    """The black glass and its bezel: a bright lip, a short slope, a black gap and the thin bright edge of
    the glass itself, measured across the bezel on the top-left crop (case.json "glass"). The window's top
    edge (the player's bottom edge of the glass) is in shadow on the photo, so it gets no lip."""

    STEP = .25                                     # spec units per ring
    FALLBACK = (.95, .39, .19, .11, .02)           # v9 profile, 1 spec unit per ring

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("glass")
        self.theme = "titanium"
        self.s = 1.0
        self._rings = None

    def set_theme(self, theme):
        self.theme = theme
        self.update()

    def set_scale(self, s):
        self.s = s
        self.update()

    def rings(self):
        """[(inset in spec units, ("face", k) | ("grey", 0..255))] outside -> inside, and the top cut."""
        if self._rings is None:
            g = Templates.get()["meta"].get("glass")
            if not g:
                self._rings = ([(i, ("face", k)) for i, k in enumerate(self.FALLBACK)], 0.0, len(self.FALLBACK))
                return self._rings
            u, v = g["u"], g["side"]
            level = sorted(v[-12:])[6]                                       # the glass beyond the bezel
            start = next(i for i in range(len(v)) if v[i] > 1.015)          # outer edge of the lip
            gap = next(i for i in range(start, len(v)) if v[i] < .02)         # the black gap
            end = max(i for i in range(len(v)) if v[i] > level + .02) + 2     # past the glass edge line
            out, top_cut = [], None
            for i in range(start, end):
                inset = (u[i] - u[start])
                if i < gap + 4 or v[i] < .02:
                    out.append((inset, ("face", v[i])))
                else:                                 # the glass's own edge: a faint grey line on black
                    out.append((inset, ("grey", 3 + max(0.0, v[i] - level) * 230)))
                if top_cut is None and i > start + 2 and v[i] < .15:
                    top_cut = inset
            self._rings = (out, top_cut or 0.0, u[end] - u[start])
        return self._rings

    def paintEvent(self, _):
        p = QPainter(self)
        self.paint_on(p, QRectF(self.rect()))

    def paint_on(self, p: QPainter, r: QRectF) -> None:
        """The glass and bezel in ``r`` (also used off-screen by player mode's device)."""
        p.setRenderHint(QPainter.Antialiasing)
        s = self.s
        p.save()
        p.translate(r.topLeft())
        r = QRectF(0, 0, r.width(), r.height())
        rad = SPEC["glass_radius"] * s
        face = QColor(metal(self.theme)["base"])
        p.setPen(Qt.NoPen)
        rings, top_cut, depth = self.rings()
        for inset, (kind, k) in rings:
            if kind == "face":
                c = QColor(min(255, int(face.red() * k)), min(255, int(face.green() * k)), min(255, int(face.blue() * k)))
            else:
                c = QColor(int(k), int(k), int(k))
            p.setBrush(c)
            d = inset * s
            p.drawRoundedRect(r.adjusted(d, d, -d, -d), max(0.0, rad - d), max(0.0, rad - d))
        d = depth * s
        p.setBrush(QColor("#030303"))
        p.drawRoundedRect(r.adjusted(d, d, -d, -d), max(0.0, rad - d), max(0.0, rad - d))
        if top_cut:
            # the unlit edge: plain face down to where the gap starts, fading out round the corners
            w = r.width()
            g = QLinearGradient(QPointF(0, 0), QPointF(w, 0))
            clear = QColor(face)
            clear.setAlpha(0)
            a0, a1 = min(.45, rad * .55 / w), min(.49, rad * 1.1 / w)
            g.setStops([(0, clear), (a0, clear), (a1, face), (1 - a1, face), (1 - a0, clear), (1, clear)])
            p.fillRect(QRectF(0, 0, w, top_cut * s), g)
        p.restore()


class Glare(QWidget):
    """Faint reflection on the glass, over the content and transparent to the mouse."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        band = QPainterPath()
        band.moveTo(w * .58, 0)
        band.lineTo(w * .72, 0)
        band.lineTo(w * .46, h)
        band.lineTo(w * .32, h)
        band.closeSubpath()
        g = QLinearGradient(QPointF(w * .32, 0), QPointF(w * .72, 0))
        g.setColorAt(0, QColor(255, 255, 255, 0))
        g.setColorAt(.5, QColor(255, 255, 255, 7))
        g.setColorAt(1, QColor(255, 255, 255, 0))
        p.fillPath(band, g)


__all__ = ["Case", "SPEC", "CASE_SCALE", "spec_height", "header_height", "Templates", "XM5Knob", "LanguageWheel", "Engraving", "Glass", "Glare", "paint_metal", "seam"]
