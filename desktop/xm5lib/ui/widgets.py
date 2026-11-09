"""Custom widgets: album grid delegate, track table view, EQ response plot."""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QListView, QMenu, QStyle, QStyledItemDelegate, QTableView, QWidget

from ..core import peq
from . import style
from .models import ALBUM_ROLE, CAPTION, CAPTION_ROLE, GRID_SIZE, MIME_PATHS
from .i18n import tr


class AlbumDelegate(QStyledItemDelegate):
    """Square cover, XM5-style caption; gold frame when selected, lift on hover."""

    def sizeHint(self, option, index):
        return GRID_SIZE

    def paint(self, p: QPainter, option, index):
        title = index.data(Qt.DisplayRole) or ""
        sub = index.data(CAPTION_ROLE)
        if sub is None:
            album = index.data(ALBUM_ROLE)
            sub = album.artist + (f" · {album.year}" if album.year else "")
        r = option.rect
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        pad = 12
        side = max(60, min(r.width() - 2 * pad, r.height() - CAPTION - pad))
        cover = QRect(r.x() + (r.width() - side) // 2, r.y() + pad - (2 if hover else 0), side, side)
        # drop shadow
        p.setPen(Qt.NoPen)
        for i, a in ((6, 30), (3, 50)):
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(QRectF(cover.adjusted(-i // 2, i // 2, i // 2, i)), 6, 6)
        pix = index.data(Qt.DecorationRole)
        path = QPainterPath()
        path.addRoundedRect(QRectF(cover), 4, 4)
        p.setClipPath(path)
        if pix is not None and not pix.isNull():
            scaled = pix.scaled(cover.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            sx = (scaled.width() - cover.width()) // 2
            sy = (scaled.height() - cover.height()) // 2
            p.drawPixmap(cover, scaled, QRect(sx, sy, cover.width(), cover.height()))
        else:
            p.fillRect(cover, QColor("#141414"))
            ph = placeholder()
            if not ph.isNull():
                s = int(side * .46)
                p.drawPixmap(QRect(cover.center().x() - s // 2, cover.center().y() - s // 2, s, s), ph)
        if hover and not selected:
            p.fillRect(cover, QColor(255, 255, 255, 14))
        p.setClipping(False)
        if selected:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(style.ACCENT), 2))
            p.drawRoundedRect(QRectF(cover).adjusted(-2, -2, 2, 2), 5, 5)
        text = QRect(cover.left(), cover.bottom() + 9, cover.width(), CAPTION - 12)
        f = QFont(option.font)
        f.setPointSizeF(10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(style.ACCENT if selected else style.TEXT))
        fm = QFontMetrics(f)
        p.drawText(text, Qt.AlignLeft | Qt.AlignTop, fm.elidedText(title, Qt.ElideRight, text.width()))
        f.setBold(False)
        f.setPointSizeF(9)
        p.setFont(f)
        p.setPen(QColor(style.MUTED))
        fm2 = QFontMetrics(f)
        p.drawText(text.adjusted(0, fm.height() + 2, 0, 0), Qt.AlignLeft | Qt.AlignTop,
                   fm2.elidedText(sub, Qt.ElideRight, text.width()))
        p.restore()


_PLACEHOLDER = None


def placeholder() -> QPixmap:
    global _PLACEHOLDER
    if _PLACEHOLDER is None:
        _PLACEHOLDER = QPixmap(style.icon_path("home_album"))
    return _PLACEHOLDER


class AlbumView(QListView):
    """Icon grid whose cells stretch so the columns always fill the width."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setGridSize(GRID_SIZE)
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setItemDelegate(AlbumDelegate(self))
        self.setDragEnabled(False)                  # only a playlist's own rows are dragged
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WA_Hover, True)
        self.setViewportMargins(12, 4, 4, 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        w = self.viewport().width() - 2
        cols = max(1, w // GRID_SIZE.width())
        cell = max(GRID_SIZE.width(), w // cols)
        side = cell - 24
        size = QSize(cell, side + 12 + CAPTION)
        if size != self.gridSize():
            self.setGridSize(size)


COLUMN_STORE = None     # the window's Prefs: each list keeps its own columns under "columns"/<config_key>


class TrackView(QTableView):
    """Track table. ``reordered(rows, target)`` for internal moves, ``dropped(paths, target)`` for drops.

    Columns: right-click the header for 选择显示项 (which columns, from what the XM5 shows about a song);
    drag a header to move a column, drag its edge to resize it; 标题 takes the remaining width. Every list
    (``config_key``) remembers its own columns.

    A reorderable view (a playlist) is re-ordered by grabbing a song by ``grab_column`` (the 序号) and
    dragging it up or down; an accent line shows where it will land. Pressing elsewhere in a row and
    dragging selects a range instead. Songs dragged in (from the playlist's finder) land at the line.
    Other lists cannot be dragged at all (``draggable`` is only for the finder that feeds a playlist).

    A list that is refilled with the same songs (the library was reloaded, e.g. after saving tags)
    keeps its selection."""

    reordered = Signal(list, int)
    dropped = Signal(list, int)

    def __init__(self, parent=None, reorderable: bool = False, grab_column: str = "pos", config_key: str | None = None,
                 defaults: list[str] | None = None, draggable: bool = False):
        super().__init__(parent)
        self.reorderable = reorderable
        self.grab = grab_column
        self.config_key = config_key
        self.defaults = defaults
        self._press_col = -1
        self._drop_row: int | None = None
        self._applying = False
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(36)
        h = self.horizontalHeader()
        h.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        h.setHighlightSections(False)
        h.setStretchLastSection(False)
        h.setSectionsMovable(True)
        h.setMinimumSectionSize(20)
        h.setContextMenuPolicy(Qt.CustomContextMenu)
        h.customContextMenuRequested.connect(self._header_menu)
        h.sectionMoved.connect(self._moved)
        h.sectionResized.connect(self._resized)
        self._save_timer = QTimer(self, singleShot=True, interval=500, timeout=self.save_columns)
        self.setSortingEnabled(False)
        self.setAcceptDrops(reorderable)
        self.setDragDropMode(QAbstractItemView.DragDrop if reorderable else
                             QAbstractItemView.DragOnly if draggable else QAbstractItemView.NoDragDrop)
        self.setDragEnabled(reorderable or draggable)
        self.setDropIndicatorShown(False)           # our own line, see paintEvent
        self.setDragDropOverwriteMode(False)
        self.setDefaultDropAction(Qt.MoveAction if reorderable else Qt.CopyAction)

    # ---- columns
    def setModel(self, model):
        super().setModel(model)
        self.apply_columns()
        if model is not None and not self.reorderable:
            model.modelAboutToBeReset.connect(self._keep_selection)
            model.modelReset.connect(self._restore_selection)

    def _keep_selection(self) -> None:
        from .models import PATH_ROLE
        sm = self.selectionModel()
        self._kept = {i.data(PATH_ROLE) for i in sm.selectedRows()} if sm is not None else set()

    def _restore_selection(self) -> None:
        kept, self._kept = getattr(self, "_kept", set()), set()
        m, sm = self.model(), self.selectionModel()
        if not kept or sm is None or not hasattr(m, "rows"):
            return
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        sel = QItemSelection()
        for r, row in enumerate(m.rows):
            if row.get("path") in kept:
                sel.select(m.index(r, 0), m.index(r, m.columnCount() - 1))
        if not sel.isEmpty():
            sm.blockSignals(True)                   # the same songs: nothing for the inspector to redo
            sm.select(sel, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            sm.blockSignals(False)
            self.viewport().update()

    def _config(self) -> dict:
        if COLUMN_STORE is None or not self.config_key:
            return {}
        return dict((COLUMN_STORE.get("columns") or {}).get(self.config_key) or {})

    def _grab_logical(self) -> int:
        m = self.model()
        return m.col(self.grab) if m is not None and hasattr(m, "col") else -1

    def apply_columns(self) -> None:
        m = self.model()
        if m is None or not hasattr(m, "IDS"):
            return
        from .models import COLUMN_DEFS
        ids, cfg = m.IDS, self._config()
        visible = [c for c in (cfg.get("visible") or self.defaults or m.DEFAULT) if c in ids]
        for c in m.LOCKED:
            if c not in visible:
                visible.insert(min(1, len(visible)), c)
        first, last = getattr(m, "FIRST", None), getattr(m, "LAST", None)
        if first:
            visible = [first] + [c for c in visible if c != first]
        if last:
            visible = [c for c in visible if c != last] + [last]
        order = visible + [c for c in ids if c not in visible]
        h = self.horizontalHeader()
        widths = cfg.get("widths") or {}
        self._auto = not widths                     # until the user sizes a column, text columns share the width
        self._applying = True
        try:
            for target, cid in enumerate(order):
                cur = h.visualIndex(ids.index(cid))
                if cur != target:
                    h.moveSection(cur, target)
            for logical, cid in enumerate(ids):
                super().setColumnHidden(logical, cid not in visible)
                h.setSectionResizeMode(logical, QHeaderView.Stretch if cid == "title" else QHeaderView.Interactive)
                if cid != "title":
                    self.setColumnWidth(logical, int(widths.get(cid, COLUMN_DEFS[cid][2])))
        finally:
            self._applying = False

    FLEX = {"artist": .22, "album": .26, "albumartist": .2, "genre": .12, "filename": .24, "location": .3}
    TITLE_SHARE = .44

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "_auto", False):
            self._fit()

    def _fit(self) -> None:
        """Default widths: the text columns and 标题 share what the fixed columns leave."""
        from .models import COLUMN_DEFS
        m = self.model()
        if m is None or not hasattr(m, "IDS"):
            return
        shown = [(i, c) for i, c in enumerate(m.IDS) if not self.isColumnHidden(i) and c != "title"]
        flex = [(i, c) for i, c in shown if c in self.FLEX]
        fixed = sum(COLUMN_DEFS[c][2] for _, c in shown if c not in self.FLEX)
        room = max(200, self.viewport().width() - fixed - 2)
        total = sum(self.FLEX[c] for _, c in flex) + self.TITLE_SHARE
        self._applying = True
        try:
            for i, c in flex:
                self.setColumnWidth(i, max(60, int(room * self.FLEX[c] / total)))
        finally:
            self._applying = False

    def save_columns(self) -> None:
        m = self.model()
        if COLUMN_STORE is None or not self.config_key or m is None or not hasattr(m, "IDS"):
            return
        h, ids = self.horizontalHeader(), m.IDS
        visible = [ids[h.logicalIndex(v)] for v in range(len(ids)) if not self.isColumnHidden(h.logicalIndex(v))]
        cols = dict(COLUMN_STORE.get("columns") or {})
        widths = dict((cols.get(self.config_key) or {}).get("widths") or {})
        if not getattr(self, "_auto", False):
            widths.update({cid: self.columnWidth(i) for i, cid in enumerate(ids)
                           if cid != "title" and not self.isColumnHidden(i) and self.columnWidth(i) >= 20})
        cols[self.config_key] = {"visible": visible, "widths": widths}
        COLUMN_STORE.set("columns", cols)

    def _moved(self, logical, old, new) -> None:
        if self._applying:
            return
        m = self.model()
        first, last = getattr(m, "FIRST", None), getattr(m, "LAST", None)
        h = self.horizontalHeader()
        self._applying = True
        try:                                         # 序号 stays first, 添加 stays last
            if first and h.visualIndex(m.col(first)) != 0:
                h.moveSection(h.visualIndex(m.col(first)), 0)
            if last and h.visualIndex(m.col(last)) != h.count() - 1:
                h.moveSection(h.visualIndex(m.col(last)), h.count() - 1)
        finally:
            self._applying = False
        self._save_timer.start()

    def _resized(self, logical, *_):
        m = self.model()
        if self._applying or m is None or not hasattr(m, "IDS") or m.IDS[logical] == "title":
            return
        self._auto = False                          # the user sized a column: keep widths from now on
        self._save_timer.start()

    def _header_menu(self, pos) -> None:
        from .models import COLUMN_DEFS
        m = self.model()
        if m is None or not hasattr(m, "IDS"):
            return
        menu = QMenu(self)
        head = menu.addAction(tr("选择显示项"))
        head.setEnabled(False)
        menu.addSeparator()
        for logical, cid in enumerate(m.IDS):
            a = menu.addAction(COLUMN_DEFS[cid][1])
            a.setCheckable(True)
            a.setChecked(not self.isColumnHidden(logical))
            a.setEnabled(cid not in m.LOCKED)
            a.toggled.connect(lambda on, l=logical, c=cid: self.show_column(c, on))
        menu.addSeparator()
        menu.addAction(tr("恢复默认"), self.reset_columns)
        menu.exec(self.horizontalHeader().mapToGlobal(pos))

    def show_column(self, cid: str, on: bool) -> None:
        from .models import COLUMN_DEFS
        m = self.model()
        logical = m.col(cid)
        if logical < 0 or cid in m.LOCKED:
            return
        super().setColumnHidden(logical, not on)
        if getattr(self, "_auto", False):
            self._fit()
        if on:
            if cid != "title" and self.columnWidth(logical) < 20:
                self.setColumnWidth(logical, COLUMN_DEFS[cid][2])
            h = self.horizontalHeader()            # a column switched on goes before the trailing marker columns
            tail = [h.visualIndex(m.col(c)) for c in ("mark", "fav", "add")
                    if m.col(c) >= 0 and not self.isColumnHidden(m.col(c))]
            target = min(tail) if tail else None
            cur = h.visualIndex(logical)
            if target is not None and cur > target:
                self._applying = True
                h.moveSection(cur, target)
                self._applying = False
        self.save_columns()

    def reset_columns(self) -> None:
        if COLUMN_STORE is not None and self.config_key:
            cols = dict(COLUMN_STORE.get("columns") or {})
            cols.pop(self.config_key, None)
            COLUMN_STORE.set("columns", cols)
        self.apply_columns()

    # ---- grab a song by its 序号 to move it
    def mousePressEvent(self, e):
        idx = self.indexAt(e.position().toPoint())
        self._press_col = idx.column() if idx.isValid() else -1
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self.reorderable and e.buttons() & Qt.LeftButton and self._press_col != self._grab_logical()
                and self.state() == QAbstractItemView.NoState):
            self.setState(QAbstractItemView.DragSelectingState)     # elsewhere in the row: select, not drag
        if self.reorderable and not e.buttons():
            idx = self.indexAt(e.position().toPoint())
            grab = idx.isValid() and idx.column() == self._grab_logical()
            self.viewport().setCursor(Qt.OpenHandCursor if grab else Qt.ArrowCursor)
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        self.viewport().unsetCursor()
        super().leaveEvent(e)

    def startDrag(self, actions):
        if self.reorderable:
            self.viewport().setCursor(Qt.ClosedHandCursor)
        super().startDrag(actions)
        self._drop_row = None
        self.viewport().update()

    def _insert_row(self, pos) -> int:
        idx = self.indexAt(pos)
        if not idx.isValid():
            return self.model().rowCount()
        r = self.visualRect(idx)
        return idx.row() + (1 if pos.y() > r.center().y() else 0)

    def dragEnterEvent(self, e):
        if self.reorderable and e.mimeData().hasFormat(MIME_PATHS):
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if self.reorderable and e.mimeData().hasFormat(MIME_PATHS):
            row = self._insert_row(e.position().toPoint())
            if row != self._drop_row:
                self._drop_row = row
                self.viewport().update()
            e.setDropAction(Qt.MoveAction if e.source() is self else Qt.CopyAction)
            e.accept()
            super().dragMoveEvent(e)            # keeps auto-scroll near the edges
            e.accept()
        else:
            super().dragMoveEvent(e)

    def dragLeaveEvent(self, e):
        self._drop_row = None
        self.viewport().update()
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        if not self.reorderable or not e.mimeData().hasFormat(MIME_PATHS):
            return super().dropEvent(e)
        target = self._insert_row(e.position().toPoint())
        self._drop_row = None
        self.viewport().update()
        if e.source() is self and e.mimeData().hasFormat("application/x-xm5lib-rows"):
            rows = [int(x) for x in bytes(e.mimeData().data("application/x-xm5lib-rows")).decode().split(",") if x]
            self.reordered.emit(rows, target)
        else:
            paths = bytes(e.mimeData().data(MIME_PATHS)).decode("utf-8").splitlines()
            self.dropped.emit(paths, target)
        e.setDropAction(Qt.CopyAction)          # the model is never asked to remove the source rows
        e.accept()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drop_row is None:
            return
        n = self.model().rowCount()
        if n == 0:
            y = 2
        elif self._drop_row < n:
            y = self.visualRect(self.model().index(self._drop_row, 0)).top()
        else:
            y = self.visualRect(self.model().index(n - 1, 0)).bottom() + 1
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(style.ACCENT), 2))
        p.drawLine(QPointF(14, y), QPointF(self.viewport().width() - 6, y))
        p.setBrush(QColor(style.BG))
        p.drawEllipse(QPointF(9, y), 4, 4)
        p.end()


class GripDelegate(QStyledItemDelegate):
    """序号 cell: grip dots, then the position -- the handle a playlist row is dragged by."""

    def paint(self, p: QPainter, option, index):
        from PySide6.QtWidgets import QApplication, QStyleOptionViewItem
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""
        widget = opt.widget
        (widget.style() if widget is not None else QApplication.style()).drawControl(QStyle.CE_ItemViewItem, opt, p, widget)
        r = option.rect
        hot = bool(option.state & (QStyle.State_MouseOver | QStyle.State_Selected))
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(style.ACCENT if hot else style.ACCENT_LO))
        cx, cy = r.left() + 13, r.center().y() + 1
        for dx in (0, 5):
            for dy in (-5, 0, 5):
                p.drawEllipse(QPointF(cx + dx, cy + dy), 1.35, 1.35)
        fg = index.data(Qt.ForegroundRole)
        p.setPen(fg if isinstance(fg, QColor) else QColor(style.MUTED))
        p.setFont(option.font)
        p.drawText(r.adjusted(26, 0, -8, 0), Qt.AlignRight | Qt.AlignVCenter, text)
        p.restore()


class ResponsePlot(QWidget):
    """Magnitude response (dB) on a log frequency axis; L and R drawn separately when they differ."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.preset: peq.Preset | None = None
        self.rate = 48000
        self.error = ""
        self.freqs = peq.log_frequencies(320)
        self.curves: dict[str, list[float]] = {}
        self.setMinimumHeight(220)

    def sizeHint(self):
        return QSize(600, 260)

    def set_preset(self, p: peq.Preset | None, rate: int = 48000) -> None:
        self.preset, self.rate, self.error = p, rate, ""
        self.curves = {}
        if p is not None:
            try:
                self.curves = peq.response(p, rate, self.freqs)
            except ValueError as exc:
                self.error = str(exc)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#0a0a0a"))
        m = 42
        plot = QRectF(m, 12, self.width() - m - 12, self.height() - 12 - 28)
        values = [v for c in self.curves.values() for v in c]
        span = max(12.0, math.ceil(max([abs(v) for v in values] + [6]) / 6) * 6)
        lo, hi = -span, span

        def x(f):
            return plot.left() + plot.width() * math.log(f / 20) / math.log(1000)

        def y(db):
            return plot.top() + plot.height() * (hi - db) / (hi - lo)

        grid = QPen(QColor("#1c1c1c"))
        p.setPen(grid)
        small = QFont(self.font())
        small.setPointSize(8)
        p.setFont(small)
        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            p.setPen(grid)
            p.drawLine(QPointF(x(f), plot.top()), QPointF(x(f), plot.bottom()))
            p.setPen(QColor(style.MUTED))
            label = f"{f // 1000}k" if f >= 1000 else str(f)
            p.drawText(QRectF(x(f) - 20, plot.bottom() + 4, 40, 16), Qt.AlignCenter, label)
        step = 6 if span <= 24 else 12
        db = lo
        while db <= hi:
            p.setPen(QPen(QColor("#4a4232" if db == 0 else "#1c1c1c"), 1))
            p.drawLine(QPointF(plot.left(), y(db)), QPointF(plot.right(), y(db)))
            p.setPen(QColor(style.MUTED))
            p.drawText(QRectF(0, y(db) - 8, m - 6, 16), Qt.AlignRight | Qt.AlignVCenter, f"{db:+g}")
            db += step
        if self.error:
            p.setPen(QColor(style.DANGER))
            p.drawText(plot, Qt.AlignCenter, self.error)
            return
        if not self.curves:
            return
        same = self.curves["L"] == self.curves["R"]
        colours = {"L": QColor(style.ACCENT), "R": QColor("#7fb2e5")}
        for ch, curve in self.curves.items():
            if same and ch == "R":
                break
            path = QPainterPath()
            for i, (f, v) in enumerate(zip(self.freqs, curve)):
                pt = QPointF(x(f), y(max(lo, min(hi, v))))
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            p.setPen(QPen(colours[ch], 2))
            p.drawPath(path)
        if self.preset is not None:
            for b in self.preset.active():
                if b.gain:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(240, 205, 129, 170) if b.channel == "ALL" else QColor(127, 178, 229, 150))
                    p.drawEllipse(QPointF(x(b.fc), y(max(lo, min(hi, b.gain + self.preset.preamp)))), 3.5, 3.5)
        p.setPen(QColor(style.MUTED))
        legend = f"{self.rate // 1000} kHz RBJ · includes preamp" + ("" if same else " · gold L, blue R")
        p.drawText(plot.adjusted(6, 4, -6, 0), Qt.AlignRight | Qt.AlignTop, legend)


class GroupDelegate(QStyledItemDelegate):
    """XM5 list row: optional gold icon, name, muted count on the right, hairline separator."""

    COUNT_ROLE = Qt.UserRole + 20
    SUB_ROLE = Qt.UserRole + 21      # optional second (muted) line, e.g. a music folder's path
    WARN_ROLE = Qt.UserRole + 22     # count shown as a problem (red)

    def __init__(self, parent=None, icon: QPixmap | None = None, height: int = 46):
        super().__init__(parent)
        self.icon, self.height = icon, height

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 60 if index.data(self.SUB_ROLE) else self.height)

    def paint(self, p: QPainter, option, index):
        r = option.rect
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        selected = bool(option.state & QStyle.State_Selected)
        if selected:
            p.fillRect(r, QColor(style.SELECT))
            p.fillRect(QRect(r.left(), r.top(), 3, r.height()), QColor(style.ACCENT))
        elif option.state & QStyle.State_MouseOver:
            p.fillRect(r, QColor(style.HOVER))
        p.fillRect(QRect(r.left() + 14, r.bottom(), r.width() - 14, 1), QColor(style.LINE))
        x = r.left() + 16
        if self.icon is not None and not self.icon.isNull():
            s = 22
            p.setOpacity(1 if selected else .85)
            p.drawPixmap(QRect(x, r.center().y() - s // 2, s, s), self.icon)
            p.setOpacity(1)
            x += s + 12
        count = index.data(self.COUNT_ROLE)
        f = QFont(option.font)
        f.setPointSizeF(9)
        p.setFont(f)
        cw = 0
        if count not in (None, ""):
            ctext = str(count)
            cw = QFontMetrics(f).horizontalAdvance(ctext) + 18
            p.setPen(QColor(style.DANGER if index.data(self.WARN_ROLE) else
                            style.ACCENT_LO if not selected else style.ACCENT))
            p.drawText(QRect(r.right() - cw, r.top(), cw - 14, r.height()), Qt.AlignRight | Qt.AlignVCenter, ctext)
        f.setPointSizeF(10.5)
        p.setFont(f)
        p.setPen(QColor(style.ACCENT if selected else style.TEXT))
        name = index.data(Qt.DisplayRole) or ""
        sub = index.data(self.SUB_ROLE)
        if sub:
            top = QRect(x, r.top() + 9, r.right() - cw - x - 4, r.height() // 2 - 6)
            p.drawText(top, Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(f).elidedText(name, Qt.ElideRight, top.width()))
            f.setPointSizeF(8.5)
            p.setFont(f)
            p.setPen(QColor(style.MUTED))
            low = QRect(x, r.center().y() + 1, top.width(), r.height() // 2 - 9)
            p.drawText(low, Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(f).elidedText(str(sub), Qt.ElideMiddle, low.width()))
            p.restore()
            return
        rect = QRect(x, r.top(), r.right() - cw - x - 4, r.height())
        p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(f).elidedText(name, Qt.ElideRight, rect.width()))
        p.restore()


class NavDelegate(QStyledItemDelegate):
    """Rail entry: firmware icon + label; gold bar and tint when selected."""

    def __init__(self, parent=None, height: int = 38):
        super().__init__(parent)
        self.height = height

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.height)

    def paint(self, p: QPainter, option, index):
        r = option.rect
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        selected = bool(option.state & QStyle.State_Selected)
        if selected:
            g = QLinearGradient(r.topLeft(), r.topRight())
            g.setColorAt(0, QColor(240, 205, 129, 46))
            g.setColorAt(1, QColor(240, 205, 129, 0))
            p.fillRect(r, g)
            p.fillRect(QRect(r.left(), r.top() + 6, 3, r.height() - 12), QColor(style.ACCENT))
        elif option.state & QStyle.State_MouseOver:
            p.fillRect(r, QColor(style.HOVER))
        icon = index.data(Qt.DecorationRole)
        x = r.left() + 18
        if isinstance(icon, QPixmap) and not icon.isNull():
            s = 22
            p.setOpacity(1 if selected else .78)
            p.drawPixmap(QRect(x, r.center().y() - s // 2, s, s), icon)
            p.setOpacity(1)
        x += 34
        f = QFont(option.font)
        f.setPointSizeF(10.5)
        f.setBold(selected)
        p.setFont(f)
        p.setPen(QColor(style.ACCENT if selected else "#d6d6d6"))
        name = index.data(Qt.DisplayRole) or ""
        rect = QRect(x, r.top(), r.right() - x - 8, r.height())
        p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(f).elidedText(name, Qt.ElideRight, rect.width()))
        p.restore()


class StatusPlay(QWidget):
    """The firmware status bar's left end: play state, speaker with level, volume number (bar_* icons).

    Scrolling over it changes the volume, like the wheel."""

    stepped = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "stopped"
        self.volume = 0
        self.icons = {n: QPixmap(style.icon_path(n)) for n in
                      ("bar_play", "bar_pause", "bar_vol0", "bar_vol1", "bar_vol2", "bar_vol3", "bar_vol4")}
        self.setMinimumWidth(140)
        self.setToolTip(tr("音量（在这里或右上角滚轮上滚动调节）"))

    def set_state(self, state: str) -> None:
        self.state = state
        self.update()

    def set_volume(self, v: int) -> None:
        self.volume = v
        self.update()

    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.angleDelta().x()
        if dy:
            self.stepped.emit(1 if dy > 0 else -1)
        e.accept()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        h = self.height()
        k = 15 / 26                                         # firmware icons are 26 px tall on a 480 px panel
        x = 0.0
        if self.state in ("playing", "paused"):
            pix = self.icons["bar_play" if self.state == "playing" else "bar_pause"]
            w = pix.width() * k
            p.drawPixmap(QRectF(x, (h - 26 * k) / 2, w, 26 * k), pix, QRectF(pix.rect()))
            x += w + 9
        v = self.volume
        level = 0 if v <= 0 else min(4, 1 + (v - 1) * 4 // 100)
        pix = self.icons[f"bar_vol{level}"]
        w = pix.width() * k
        p.drawPixmap(QRectF(x, (h - 26 * k) / 2, w, 26 * k), pix, QRectF(pix.rect()))
        x += w + 5
        f = QFont(self.font())
        f.setPointSizeF(10.5)
        p.setFont(f)
        p.setPen(QColor(style.TEXT))
        p.drawText(QRectF(x, 0, 60, h), Qt.AlignLeft | Qt.AlignVCenter, str(v))


class SeekBar(QWidget):
    """Thin progress line along the top of the play bar; click or drag to seek."""

    seek = Signal(int)            # ms

    def __init__(self, parent=None):
        super().__init__(parent)
        self.position = 0
        self.duration = 0
        self._drag = None
        self.setFixedHeight(9)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

    def set_position(self, ms: int) -> None:
        if self._drag is None:
            self.position = ms
            self.update()

    def set_duration(self, ms: int) -> None:
        self.duration = ms
        self.update()

    def _ms(self, x: float) -> int:
        return int(max(0.0, min(1.0, x / max(1, self.width()))) * self.duration)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.duration > 0:
            self._drag = self._ms(e.position().x())
            self.update()

    def mouseMoveEvent(self, e):
        if self.duration > 0:
            ms = self._ms(e.position().x())
            s = ms // 1000
            self.setToolTip(f"{s // 60}:{s % 60:02d}")
            if self._drag is not None:
                self._drag = ms
                self.update()

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            ms, self._drag = self._drag, None
            self.position = ms
            self.seek.emit(ms)
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        w = self.width()
        y = self.height() - 3
        p.fillRect(QRectF(0, y, w, 2), QColor("#242424"))
        pos = self._drag if self._drag is not None else self.position
        if self.duration > 0:
            fw = w * max(0.0, min(1.0, pos / self.duration))
            p.fillRect(QRectF(0, y, fw, 2), QColor(style.ACCENT))
            if self.underMouse() or self._drag is not None:
                p.setRenderHint(QPainter.Antialiasing)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(style.ACCENT_HI))
                p.drawEllipse(QPointF(fw, y + 1), 4, 4)

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()
