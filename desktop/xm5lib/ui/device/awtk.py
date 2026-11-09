"""The XM5 firmware's own screen description, read as the player reads it.

The firmware draws its UI with AWTK from three kinds of compiled resources, vendored (read-only) under
``assets/xm5stock``:

* ``ui/*.bin`` -- a window or dialog: ``u32 magic 0x11221212`` then a widget tree, each widget being
  ``type[32]``, ``i32 x, y, w, h``, ``{key\\0 value\\0}* \\0``, its children, ``\\0``;
* ``styles/*.bin`` -- themes: ``u32 0xFAFBFCFD, u32 version, u32 n`` and ``n`` 100-byte records
  ``(u32 offset, state[32], name[32], widget_type[32])``; at the offset ``u32 count`` and entries
  ``u8 type, u8 name_len, u16 value_len, name\\0, value`` (type 7: a u32, type 14: a string);
* ``strings/*.bin`` -- ``u16 0, u16 n``, ``n x (u32 key, u32 value)`` offsets of NUL-terminated UTF-8.

The formats were read off the files (not taken from AWTK's source). This module turns them into
``Node`` trees with resolved geometry and styles; ``screen.py`` paints them.
"""
from __future__ import annotations

import re
import struct
from functools import lru_cache
from pathlib import Path

UI_MAGIC = 0x11221212
THEME_MAGIC = 0xFAFBFCFD

ROOT = Path(__file__).resolve().parent.parent / "assets" / "xm5stock"

ALIGN_H = {"none": 0, "center": 1, "left": 2, "right": 3}
ALIGN_V = {"none": 0, "middle": 1, "top": 2, "bottom": 3}
DRAW = {"default": 0, "center": 1, "icon": 2, "scale": 3, "scale_auto": 4, "scale_down": 5, "scale_w": 6,
        "scale_h": 7, "repeat": 8, "repeat_x": 9, "repeat_y": 10, "repeat_y_inverse": 11, "patch9": 12,
        "patch3_x": 13, "patch3_y": 14, "patch3_x_scale_y": 15, "patch3_y_scale_x": 16, "repeat9": 17,
        "repeat3_x": 18, "repeat3_y": 19}
ICON_AT = {"auto": 0, "left": 1, "right": 2, "top": 3, "bottom": 4, "centre": 5, "center": 5}
BORDER = {"none": 0, "left": 1, "right": 2, "top": 4, "bottom": 8, "all": 15}
STATES = ("normal", "pressed", "over", "disable", "focused", "selected", "normal_of_checked",
          "pressed_of_checked", "over_of_checked", "focused_of_checked")


def available() -> bool:
    return (ROOT / "ui" / "home_page.bin").is_file() and (ROOT / "styles" / "default.bin").is_file()


# ------------------------------------------------------------------ values
def color(v) -> tuple[int, int, int, int] | None:
    """'#rgb', '#rrggbb', '#rrggbbaa' (or an (r,g,b,a) tuple) -> (r, g, b, a)."""
    if v is None or v == "":
        return None
    if isinstance(v, tuple):
        return v
    s = str(v).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        if len(s) == 6:
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255
        if len(s) == 8:
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16)
    except ValueError:
        pass
    return None


def _number(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def normalize(key: str, v):
    """A style value from a theme (typed) or an inline ``style:state:key`` attribute (text) -> one shape:
    colours as RGBA tuples, enums as AWTK's integers, numbers as ints, the rest as strings."""
    if key.endswith("color"):
        return color(v)
    if isinstance(v, str):
        low = v.strip().lower()
        if key == "text_align_h":
            return ALIGN_H.get(low, _number(v, 1))
        if key == "text_align_v":
            return ALIGN_V.get(low, _number(v, 1))
        if key.endswith("draw_type"):
            return DRAW.get(low, _number(v, 0))
        if key == "icon_at":
            return ICON_AT.get(low, _number(v, 0))
        if key == "border":
            if low in BORDER:
                return BORDER[low]
            return sum(BORDER.get(p.strip(), 0) for p in low.split("|")) if "|" in low else _number(v, 15)
        if key in ("font_size", "round_radius", "border_width", "margin", "margin_left", "margin_right",
                   "margin_top", "margin_bottom", "spacer", "x_offset", "y_offset", "icon_offset_x"):
            return _number(v, 0)
    return v


# ------------------------------------------------------------------ strings
@lru_cache(maxsize=8)
def strings(lang: str = "zh_CN") -> dict[str, str]:
    p = ROOT / "strings" / f"{lang}.bin"
    if not p.is_file():
        return {}
    b = p.read_bytes()
    _, n = struct.unpack_from("<HH", b, 0)
    out = {}
    for i in range(n):
        ko, vo = struct.unpack_from("<II", b, 4 + 8 * i)
        k = b[ko:b.index(b"\0", ko)].decode("utf-8", "replace")
        out[k] = b[vo:b.index(b"\0", vo)].decode("utf-8", "replace")
    return out


# ------------------------------------------------------------------ themes
@lru_cache(maxsize=32)
def theme_file(name: str) -> dict:
    """{(widget_type, style_name, state): {key: value}} of one styles/*.bin (empty when absent)."""
    p = ROOT / "styles" / f"{name}.bin"
    if not p.is_file():
        return {}
    b = p.read_bytes()
    magic, _version, nr = struct.unpack_from("<III", b, 0)
    if magic != THEME_MAGIC:
        raise ValueError(f"{p.name}: not an AWTK theme")
    cstr = lambda raw: raw.split(b"\0", 1)[0].decode("ascii", "replace")
    out = {}
    for i in range(nr):
        rec = 12 + i * 100
        (off,) = struct.unpack_from("<I", b, rec)
        state, sname, wtype = cstr(b[rec + 4:rec + 36]), cstr(b[rec + 36:rec + 68]), cstr(b[rec + 68:rec + 100])
        (count,) = struct.unpack_from("<I", b, off)
        p_ = off + 4
        d = {}
        for _ in range(count):
            t, nl, vl = struct.unpack_from("<BBH", b, p_)
            p_ += 4
            key = b[p_:p_ + nl - 1].decode("ascii", "replace")
            p_ += nl
            raw = b[p_:p_ + vl]
            p_ += vl
            if t == 0x0e:
                v = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
            elif vl == 4:
                (u,) = struct.unpack("<I", raw)
                v = (raw[0], raw[1], raw[2], raw[3]) if key.endswith("color") else u
            else:
                continue
            d[key] = normalize(key, v) if isinstance(v, str) else v
        out[(wtype, sname, state)] = d
    return out


class Theme:
    """A window's theme: its own styles/<page>.bin over styles/default.bin, like the firmware's lookup."""

    def __init__(self, page: str | None = None):
        self.layers = ([theme_file(page)] if page else []) + [theme_file("default")]
        self._cache: dict = {}

    def get(self, wtype: str, sname: str, state: str) -> dict:
        key = (wtype, sname, state)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out = None
        for name in ((sname, "default") if sname != "default" else ("default",)):
            for st in ((state, "normal") if state != "normal" else ("normal",)):
                for layer in self.layers:
                    d = layer.get((wtype, name, st))
                    if d is not None:
                        out = d
                        break
                if out is not None:
                    break
            if out is not None:
                break
        self._cache[key] = out = dict(out or {})
        return out


# ------------------------------------------------------------------ widgets
class Node:
    """One AWTK widget. ``x, y, w, h`` are resolved (relative to the parent); ``raw`` keeps the file's."""

    def __init__(self, wtype: str, name: str = "", rect=(0, 0, 0, 0), props: dict | None = None):
        self.type = wtype
        self.props = dict(props or {})
        self.name = name or self.props.get("name", "")
        self.raw = tuple(rect)
        self.x, self.y, self.w, self.h = rect
        self.inline: dict[str, dict] = {}
        for k in list(self.props):
            if k.startswith("style:"):
                _, state, key = k.split(":", 2)
                self.inline.setdefault(state, {})[key] = normalize(key, self.props.pop(k))
        self.children: list[Node] = []
        self.parent: Node | None = None
        p = self.props
        self.visible = p.get("visible", "true") != "false"
        self.enabled = p.get("enable", "true") != "false"
        self.style = p.get("style", "default") or "default"
        self.text = p.get("text", "")
        self.tr_text = p.get("tr_text", "")
        self.image = p.get("image", "")
        self.pixmap = None                       # a QPixmap shown instead of ``image`` (covers)
        self.value = float(p.get("value", 0) or 0)
        self.min = float(p.get("min", 0) or 0)
        self.max = float(p.get("max", 100) or 100)
        self.checked = p.get("value") == "true" if wtype in ("switch", "check_button", "radio_button") else False
        self.pressed = False
        self.selected = False
        self.on_click = None                     # callables set by the page controllers
        self.on_long = None
        self.on_change = None
        self.paint_hook = None                   # (painter, node, QRectF) -> bool: True = drawn, skip default
        self.rows = None                         # a RowSource for virtual lists
        self.scroll_y = 0.0
        self.content_h = 0.0
        self.data = None                         # whatever the controller wants to hang on it
        self.theme: Theme | None = None          # set on window roots

    # ---- tree
    def add(self, child: "Node") -> "Node":
        child.parent = self
        self.children.append(child)
        return child

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def find(self, name: str) -> "Node | None":
        for n in self.walk():
            if n.name == name:
                return n
        return None

    def __getitem__(self, name: str) -> "Node":
        n = self.find(name)
        if n is None:
            raise KeyError(name)
        return n

    def root_theme(self) -> Theme:
        n = self
        while n is not None:
            if n.theme is not None:
                return n.theme
            n = n.parent
        return Theme()

    def abs_pos(self) -> tuple[float, float]:
        x = y = 0.0
        n = self
        while n is not None:
            x += n.x
            y += n.y - (n.parent.scroll_y if n.parent is not None and n.parent.is_scroller else 0)
            n = n.parent
        return x, y

    @property
    def is_scroller(self) -> bool:
        return self.type in ("table_client", "scroll_view") or self.rows is not None

    @property
    def clickable(self) -> bool:
        return (self.on_click is not None or self.on_long is not None or self.on_change is not None) \
            and self.enabled and self.visible

    # ---- style
    def state_name(self) -> str:
        if not self.enabled:
            return "disable"
        if self.type in ("switch", "check_button", "radio_button") and self.checked:
            return "pressed_of_checked" if self.pressed else "normal_of_checked"
        if self.type == "tab_button" and self.checked:
            return "pressed_of_active" if self.pressed else "normal_of_active"
        if self.pressed:
            return "pressed"
        if self.selected:
            return "selected"
        return "normal"

    def styles(self, state: str | None = None) -> dict:
        """The theme's style for the state with this widget's inline ``style:<state>:<key>`` values on top
        (a state without inline values borrows the inline ``normal`` ones)."""
        state = state or self.state_name()
        d = dict(self.root_theme().get(self.type, self.style, state))
        inline = self.inline.get(state)
        if inline is None:
            inline = self.inline.get("normal")
        if inline:
            d.update(inline)
        return d

    def label(self, lang: dict) -> str:
        if self.tr_text:
            return lang.get(self.tr_text, self.tr_text)
        return self.text

    def __repr__(self):
        return f"<{self.type} {self.name!r} {self.x},{self.y},{self.w},{self.h}>"


def _cstr(buf: bytes, off: int) -> tuple[str, int]:
    end = buf.index(b"\0", off)
    return buf[off:end].decode("utf-8", "replace"), end + 1


def _widget(buf: bytes, off: int) -> tuple[Node, int]:
    wtype = buf[off:off + 32].split(b"\0", 1)[0].decode("ascii", "replace")
    rect = struct.unpack_from("<4i", buf, off + 32)
    off += 48
    props = {}
    while buf[off] != 0:
        k, off = _cstr(buf, off)
        v, off = _cstr(buf, off)
        props[k] = v
    off += 1
    node = Node(wtype, rect=rect, props=props)
    while buf[off] != 0:
        child, off = _widget(buf, off)
        node.add(child)
    return node, off + 1


def load_ui(name: str, theme: str | None = None) -> Node:
    """A window/dialog from ui/<name>.bin (``name`` may hold a sub-folder, e.g. ``dialog/volume_dialog``),
    styled with styles/<theme or page name>.bin over the default theme."""
    b = (ROOT / "ui" / f"{name}.bin").read_bytes()
    if struct.unpack_from("<I", b, 0)[0] != UI_MAGIC:
        raise ValueError(f"{name}: not an AWTK UI file")
    root, _ = _widget(b, 4)
    root.theme = Theme(theme or Path(name).name)
    return root


# ------------------------------------------------------------------ layout
_ARGS = re.compile(r"(\w+)\s*=\s*([^,\)]+)")


def _args(spec: str) -> dict[str, str]:
    m = re.search(r"\((.*)\)", spec or "")
    return {k: v.strip() for k, v in _ARGS.findall(m.group(1))} if m else {}


def _size(v: str, parent: float, raw: float) -> float:
    if v is None:
        return raw
    v = v.strip()
    if v.endswith("%"):
        f = float(v[:-1] or 0) * parent / 100
        return parent + f if f < 0 else f
    n = float(v)
    return parent + n if n < 0 else n


def _pos(v: str, parent: float, size: float, raw: float, centre: str, far: str) -> float:
    if v is None:
        return raw
    v = v.strip()
    if v.endswith("%"):
        return float(v[:-1] or 0) * parent / 100
    if v[0] in (centre, far) and (len(v) == 1 or v[1] == ":"):
        off = float(v[2:]) if len(v) > 2 else 0
        return (parent - size) / 2 + off if v[0] == centre else parent - size - off
    return float(v)


def layout(node: Node, pw: float, ph: float, own: bool = True) -> None:
    """Resolve ``self_layout`` (x/y as number, %, c/m centre, r/b far edge; w/h as number, %, or negative =
    parent minus) and ``children_layout`` (``r=1,c=0`` a row of fixed-width children, ``r=1,c=N`` N equal
    columns, ``r=0,c=1`` a column; ``s`` spacing, ``xm/ym`` margins, ``a=right``) down the tree.
    ``own=False`` keeps the node's rect (it was placed by its parent's children_layout)."""
    if own:
        rx, ry, rw, rh = node.raw
        spec = node.props.get("self_layout")
        if spec:
            a = _args(spec)
            w, h = _size(a.get("w"), pw, rw), _size(a.get("h"), ph, rh)
            node.x, node.y = _pos(a.get("x"), pw, w, rx, "c", "r"), _pos(a.get("y"), ph, h, ry, "m", "b")
            node.w, node.h = w, h
        else:
            node.x, node.y, node.w, node.h = rx, ry, rw, rh
    if node.type == "scroll_view" and node.parent is not None and node.parent.type == "list_view":
        ih = float(node.parent.props.get("item_height") or node.parent.props.get("default_item_height") or 0)
        y = 0.0
        for c in node.children:
            if not c.visible:
                continue
            h = c.raw[3] or ih
            c.raw = (c.raw[0], y, c.raw[2] or node.w, h)
            y += h
        node.content_h = y
    cl = node.props.get("children_layout")
    if cl:
        a = _args(cl)
        rows, cols = int(a.get("r", 0) or 0), int(a.get("c", 0) or 0)
        s = float(a.get("s", 0) or 0)
        xm, ym = float(a.get("xm", a.get("m", 0)) or 0), float(a.get("ym", a.get("m", 0)) or 0)
        vis = [c for c in node.children if c.visible]
        if rows == 1 and cols > 0:
            cw = (node.w - 2 * xm - (cols - 1) * s) / cols
            for i, c in enumerate(vis):
                c.x, c.y, c.w, c.h = xm + i * (cw + s), ym, cw, node.h - 2 * ym
        elif rows == 1:
            total = sum(c.raw[2] for c in vis) + s * max(0, len(vis) - 1)
            x = node.w - xm - total if a.get("a") == "right" else xm
            for c in vis:
                c.x, c.y, c.w, c.h = x, ym, c.raw[2], c.raw[3] or node.h - 2 * ym
                x += c.w + s
        else:
            y = ym
            for c in vis:
                c.x, c.y, c.w, c.h = xm, y, c.raw[2] or node.w - 2 * xm, c.raw[3]
                y += c.h + s
        for c in node.children:
            layout(c, node.w, node.h, own=not c.visible)
    else:
        for c in node.children:
            layout(c, node.w, node.h)


def make(wtype: str, rect, name: str = "", style: str = "default", parent: Node | None = None, **props) -> Node:
    """A widget built in code (what the firmware's C code adds at run time: rows, tiles, navbar buttons)."""
    props = {k: str(v) for k, v in props.items()}
    props["style"] = style
    n = Node(wtype, name=name, rect=rect, props=props)
    if parent is not None:
        parent.add(n)
    return n


__all__ = ["Node", "Theme", "load_ui", "layout", "make", "strings", "available", "color", "normalize", "ROOT"]
