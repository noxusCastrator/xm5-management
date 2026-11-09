"""Player mode: the window becomes a whole XM5.

The title bar's middle key switches to it. The body is drawn upright at the real proportions with its four
keys (⏮ ⏯ ⏭ Fn) and the knob; the glass shows the firmware's own screens, drawn from its stock layouts,
themes, strings and images (``assets/xm5stock``, read-only, personal use only -- see assets/NOTICE.md), and
the keys, knob and touch behave as the firmware's handlers do (docs/XM5-UI-BEHAVIOUR.md). It is the same
player and library as the normal window: nothing about playback changes when the mode does. The whole
player scales in fixed steps (0.5x-2x, 1x = the screen's 720 px) by dragging its bottom-right corner; a
hidden x at the top right leaves the mode.

body.py   the device's metal, glass, keys and knob, rendered per scale
frame.py  the widget: body + screen image, mouse to keys / knob / touch, the corner resize
screen.py the AWTK-like renderer and the finger input on the 720 x 720 screen
core.py   the controller: keys, knob, screen on/off, lock, pages, dialogs, settings storage
dialogs.py, library.py, playing.py, settings.py   the firmware's pages
"""
from __future__ import annotations

from . import awtk


def available() -> bool:
    return awtk.available()


def create(win, parent=None, scale: float = 1.0):
    """(the XM5 widget, its controller) for ``win`` (a MainWindow)."""
    from .core import Device
    from .frame import DeviceFrame
    frame = DeviceFrame(parent, scale=scale, theme=getattr(win, "theme", "titanium"))
    device = Device(win, frame)
    return frame, device


__all__ = ["available", "create"]
