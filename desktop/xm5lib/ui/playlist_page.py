"""Playlist page: a playlist presented like an album, and edited in place (Spotify-style flow).

* Header like the album page: cover, name, count/duration, ▶ 播放 / 随机播放 / 加入播放队列 / 添加歌曲 / ⋯.
  Clicking the cover or the name opens 编辑详情 (name + cover image).
* The list is the play order: songs are added at the end, and a song is moved by grabbing its name and
  dragging it up or down. Delete removes the selection. Songs missing from the library keep their place.
* 添加歌曲 opens a finder under the list: search the library and add with one click (or drag in).

Things the XM5 cannot use are left out (description, collaborators, public/private, date added).
"""
from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QItemSelectionModel, QModelIndex, QRegularExpression, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPainterPath, QPixmap, QRegularExpressionValidator, QShortcut
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QSplitter,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..core import library
from ..core.library import format_duration
from . import style
from .library_pages import empty_state, header
from .models import CAPTION_ROLE, FinderTable, PlaylistTable
from .widgets import AlbumView, GripDelegate, TrackView
from .i18n import tr

BAD_NAME = '\\/:*?"<>|'


def rounded(pix: QPixmap, side: int, radius: int = 6) -> QPixmap:
    src = pix.scaled(side, side, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(side, side)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, side, side, radius, radius)
    p.setClipPath(path)
    p.drawPixmap((side - src.width()) // 2, (side - src.height()) // 2, src)
    p.end()
    return out


def mosaic(pixes: list[QPixmap], side: int) -> QPixmap:
    """Four album covers in a square, like a streaming app's playlist without its own picture."""
    out = QPixmap(side, side)
    out.fill(QColor("#141414"))
    p = QPainter(out)
    half = side // 2
    for i, pix in enumerate(pixes[:4]):
        tile = pix.scaled(half, half, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        p.drawPixmap((i % 2) * half, (i // 2) * half, tile, (tile.width() - half) // 2, (tile.height() - half) // 2,
                     half, half)
    p.end()
    return rounded(out, side)


def placeholder(side: int) -> QPixmap:
    out = QPixmap(side, side)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, side, side, 6, 6)
    p.fillPath(path, QColor("#141414"))
    icon = QPixmap(style.icon_path("home_playlist"))
    if not icon.isNull():
        s = side // 3
        icon = icon.scaled(s, s, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p.drawPixmap((side - icon.width()) // 2, (side - icon.height()) // 2, icon)
    p.end()
    return out


def playlist_cover(ctl, pl: dict | None, rows: list[dict], missing: set, side: int,
                   cache: dict) -> tuple[QPixmap | None, bool]:
    """(picture, is_custom): the playlist's own image, else a mosaic of its first albums' covers."""
    if pl is None:
        return None, False
    cov = pl.get("cover")
    if cov and cov.get("path"):
        key = cov.get("sha256") or cov["path"]
        pix = cache.get(key)
        if pix is None and Path(cov["path"]).is_file():
            pix = QPixmap(cov["path"])
            if not pix.isNull():
                pix = pix.scaled(440, 440, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                cache[key] = pix
        if pix is not None and not pix.isNull():
            return pix, True
    view = ctl.view
    seen, pixes, albums = set(), [], []
    for row in rows:
        if row["path"] in missing:
            continue
        alb = view.album_of(row)
        if alb is None or alb.cover_id in seen:
            continue
        seen.add(alb.cover_id)
        albums.append(alb)
        pix = ctl.cover_pixmap(alb)
        if pix is not None:
            pixes.append(pix)
        if len(pixes) >= 4 or len(albums) >= 12:
            break
    ctl.request_covers(albums)
    if len(pixes) >= 4:
        return mosaic(pixes, side), False
    if pixes:
        return pixes[0], False
    return None, False


class ClickLabel(QLabel):
    clicked = Signal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class CoverTile(QLabel):
    """A square cover that says what clicking it does when the pointer is over it."""

    clicked = Signal()

    def __init__(self, side: int, hint: str = tr("编辑详情"), parent=None):
        super().__init__(parent)
        self.side, self.hint, self.hover = side, hint, False
        self.setFixedSize(side, side)
        self.setCursor(Qt.PointingHandCursor)
        self.setPixmap(placeholder(side))

    def set_side(self, side: int) -> None:
        self.side = side
        self.setFixedSize(side, side)

    def enterEvent(self, e):
        self.hover = True
        self.update()

    def leaveEvent(self, e):
        self.hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self.hover:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.side, self.side, 6, 6)
        p.fillPath(path, QColor(0, 0, 0, 150))
        p.setPen(QColor(style.ACCENT))
        f = QFont(self.font())
        f.setPointSizeF(11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, "✎\n" + self.hint)
        p.end()


class EditDetails(QDialog):
    """编辑详情: name and cover picture -- what a playlist on the XM5 can actually show."""

    def __init__(self, parent, name: str, cover: QPixmap | None, custom: bool, start_dir: str = ""):
        super().__init__(parent)
        self.setWindowTitle(tr("编辑详情"))
        self.setModal(True)
        self.setMinimumWidth(560)
        self.image: str | None = None          # newly chosen file
        self.remove = False
        self.custom = custom
        self.start_dir = start_dir
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(14)
        title = QLabel(tr("编辑详情"))
        title.setStyleSheet("font-size: 15pt; font-weight: 600;")
        lay.addWidget(title)
        row = QHBoxLayout()
        row.setSpacing(20)
        left = QVBoxLayout()
        left.setSpacing(6)
        self.tile = CoverTile(180, tr("选择图片"))
        self.base = cover
        self.tile.setPixmap(rounded(cover, 180) if cover is not None else placeholder(180))
        self.tile.clicked.connect(self.choose)
        left.addWidget(self.tile)
        links = QHBoxLayout()
        pick = QPushButton(tr("选择图片…"))
        pick.setObjectName("crumb")
        pick.setCursor(Qt.PointingHandCursor)
        pick.clicked.connect(self.choose)
        self.drop = QPushButton(tr("移除图片"))
        self.drop.setObjectName("crumb")
        self.drop.setCursor(Qt.PointingHandCursor)
        self.drop.clicked.connect(self.clear)
        self.drop.setVisible(custom)
        links.addWidget(pick)
        links.addWidget(self.drop)
        links.addStretch(1)
        left.addLayout(links)
        left.addStretch(1)
        row.addLayout(left)
        right = QVBoxLayout()
        right.setSpacing(6)
        lab = QLabel(tr("名称"))
        lab.setProperty("muted", True)
        right.addWidget(lab)
        self.name = QLineEdit(name)
        self.name.setObjectName("playlistName")
        self.name.setMaxLength(100)
        self.name.setPlaceholderText(tr("给歌单起个名字"))
        self.name.setValidator(QRegularExpressionValidator(QRegularExpression(r'[^\\/:*?"<>|]*'), self))
        self.name.textChanged.connect(self._check)
        self.name.selectAll()
        right.addWidget(self.name)
        rule = QLabel(tr('不能使用 \\ / : * ? " < > |（导出到 TF 卡时，歌单名就是文件名）'))
        rule.setProperty("muted", True)
        rule.setWordWrap(True)
        right.addWidget(rule)
        right.addSpacing(10)
        note = QLabel(tr("封面在电脑上设置：原图原样保存在曲库里；导出到 TF 卡时另外生成播放器用的小图，原图不会被改动。"))
        note.setProperty("muted", True)
        note.setWordWrap(True)
        right.addWidget(note)
        right.addStretch(1)
        row.addLayout(right, 1)
        lay.addLayout(row)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(tr("取消"))
        cancel.clicked.connect(self.reject)
        self.save = QPushButton(tr("保存"))
        self.save.setObjectName("primary")
        self.save.setDefault(True)
        self.save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save)
        lay.addLayout(buttons)
        self._check()

    def _check(self, *_):
        self.save.setEnabled(bool(self.name.text().strip()))

    def choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("选择封面图片"), self.start_dir,
                                              tr("图片 (*.jpg *.jpeg *.png *.webp *.bmp *.gif)"))
        if not path:
            return
        pix = QPixmap(path)
        if pix.isNull():
            self.tile.setToolTip(tr("这个文件打不开，换一张吧"))
            return
        self.image, self.remove = path, False
        self.tile.setPixmap(rounded(pix, 180))
        self.drop.setVisible(True)

    def clear(self) -> None:
        self.image, self.remove = None, self.custom
        self.tile.setPixmap(placeholder(180))
        self.drop.setVisible(False)

    def result_name(self) -> str:
        return self.name.text().strip()


class PlaylistPage(QWidget):
    changed = Signal()

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.pid: str | None = None
        self._covers: dict[str, QPixmap] = {}
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 0)          # the album page's detail layout
        lay.setSpacing(0)
        back = QPushButton(tr("‹  歌单"))
        back.setObjectName("backButton")
        back.setFlat(True)
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(ctl.show_playlists)
        lay.addWidget(back, 0, Qt.AlignLeft)

        top = QHBoxLayout()
        self.cover = CoverTile(220)
        self.cover.setObjectName("playlistCover")
        self.cover.clicked.connect(self.edit_details)
        top.addWidget(self.cover)
        top.addSpacing(22)
        meta = QVBoxLayout()
        meta.addStretch(1)
        self.title = ClickLabel("")
        self.title.setObjectName("pageTitle")
        self.title.setWordWrap(True)
        self.title.setToolTip(tr("编辑名称和封面"))
        self.title.clicked.connect(self.edit_details)
        meta.addWidget(self.title)
        self.artists = QLabel("")
        self.artists.setStyleSheet(f"font-size: 13pt; color: {style.ACCENT};")
        meta.addWidget(self.artists)
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("subtitle")
        self.subtitle.setWordWrap(True)
        meta.addWidget(self.subtitle)
        bar = QHBoxLayout()
        self.buttons = {}
        for key, label, name in (("play", tr("▶  播放"), "primary"), ("queue", tr("加入播放队列"), "enqueue"),
                                 ("shuffle", tr("随机播放"), "pl_shuffle"), ("find", tr("＋  添加歌曲"), "pl_find"),
                                 ("more", "⋯", "pl_more")):
            b = QPushButton(label)
            b.setObjectName(name)
            b.setCursor(Qt.PointingHandCursor)
            self.buttons[key] = b
            bar.addWidget(b)
        self.buttons["play"].setToolTip(tr("按歌单顺序播放（选中歌曲时从所选那首开始）"))
        self.buttons["find"].setCheckable(True)
        self.buttons["more"].setToolTip(tr("更多"))
        self.buttons["more"].setFixedWidth(44)
        bar.addStretch(1)
        meta.addLayout(bar)
        top.addLayout(meta, 1)
        lay.addLayout(top)
        lay.addSpacing(10)

        self.split = QSplitter(Qt.Vertical)
        self.split.setHandleWidth(1)
        self.split.setChildrenCollapsible(False)
        listbox = QStackedWidget()
        self.listbox = listbox
        self.model = PlaylistTable(ctl, is_fav=ctl.favorites.is_fav, reorderable=True)
        self.view = TrackView(reorderable=True, grab_column="pos", config_key="playlist")
        self.view.setObjectName("playlistTable")
        self.view.setModel(self.model)
        self.view.setItemDelegateForColumn(self.model.col("pos"), GripDelegate(self.view))
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        self.view.doubleClicked.connect(lambda i: self._play(i.row()))
        self.view.selectionModel().selectionChanged.connect(lambda *_: ctl.set_selection_paths(self.selected_paths()))
        self.view.reordered.connect(self._reordered)
        self.view.dropped.connect(self._dropped)
        listbox.addWidget(self.view)
        self.empty = QLabel(tr("还没有歌曲 · 点「＋ 添加歌曲」"))
        self.empty.setProperty("muted", True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setMinimumHeight(90)
        listbox.addWidget(self.empty)
        self.split.addWidget(listbox)
        self.split.addWidget(self._finder())
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        lay.addWidget(self.split, 1)
        lay.addSpacing(10)

        b = self.buttons
        b["play"].clicked.connect(lambda: self._play(self._first_selected()))
        b["shuffle"].clicked.connect(self._shuffle)
        b["queue"].clicked.connect(lambda: ctl.enqueue_paths(self._present(self.selected_paths() or self._all_paths())))
        b["find"].toggled.connect(self.show_finder)
        b["more"].clicked.connect(lambda: self._more(b["more"].mapToGlobal(b["more"].rect().bottomLeft())))
        for keys, fn in ((QKeySequence.Delete, self.remove_selected), (QKeySequence("Alt+Up"), lambda: self._nudge(-1)),
                         (QKeySequence("Alt+Down"), lambda: self._nudge(1))):
            QShortcut(keys, self.view, activated=fn, context=Qt.WidgetShortcut)
        st = ctl.state
        st.playlistChanged.connect(lambda pid: pid == self.pid and self.refresh())
        st.playlistsChanged.connect(self._maybe_gone)
        st.tracksChanged.connect(lambda: self.pid and self.refresh())
        st.favoritesChanged.connect(self.model.refresh_favs)
        self.show_finder(False)

    # ------------------------------------------------------------------ finder
    def _finder(self) -> QWidget:
        box = QWidget()
        box.setObjectName("page")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(6)
        head = QHBoxLayout()
        cap = QLabel(tr("为歌单添加歌曲"))
        cap.setStyleSheet("font-size: 12pt; font-weight: 600;")
        head.addWidget(cap)
        head.addSpacing(12)
        self.find = QLineEdit()
        self.find.setObjectName("playlistFind")
        self.find.setPlaceholderText(tr("搜索歌名、歌手、专辑或风格"))
        self.find.setClearButtonEnabled(True)
        self.find.setMaximumWidth(420)
        self._find_timer = QTimer(self, singleShot=True, interval=160, timeout=self._search)
        self.find.textChanged.connect(lambda _: self._find_timer.start())
        self.find.returnPressed.connect(self._add_selected_results)
        head.addWidget(self.find, 1)
        self.find_info = QLabel("")
        self.find_info.setProperty("muted", True)
        head.addWidget(self.find_info)
        head.addStretch(1)
        close = QPushButton(tr("收起"))
        close.setObjectName("crumb")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(lambda: self.buttons["find"].setChecked(False))
        head.addWidget(close)
        lay.addLayout(head)
        self.results_model = FinderTable(self.ctl, is_fav=self.ctl.favorites.is_fav, count_in=self._count_in)
        self.results = TrackView(config_key="finder", draggable=True)
        self.results.setObjectName("playlistFinder")
        self.results.setModel(self.results_model)
        self.results.clicked.connect(self._result_clicked)
        self.results.doubleClicked.connect(lambda i: i.column() != self.results_model.ADD_COL and self._add_rows([i.row()]))
        self.results.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results.customContextMenuRequested.connect(lambda pos: self.ctl.track_menu(
            self._result_paths(), self.results.viewport().mapToGlobal(pos)))
        lay.addWidget(self.results, 1)
        self.finder_box = box
        return box

    def show_finder(self, on: bool) -> None:
        self.finder_box.setVisible(on)
        side = 140 if on else 220                  # a smaller header leaves room for the search results
        if self.cover.side != side:
            self.cover.set_side(side)
            self.refresh_cover()
        if self.buttons["find"].isChecked() != on:
            self.buttons["find"].blockSignals(True)
            self.buttons["find"].setChecked(on)
            self.buttons["find"].blockSignals(False)
        if on:
            self._fit_split()
            self.find.setFocus()
            self.find.selectAll()
            self._search()

    def _fit_split(self) -> None:
        """An empty or short playlist leaves the room to the finder; a long one shares it."""
        total = max(200, self.split.height())
        n = len(self.model.rows)
        top = 110 if n == 0 else min(int(total * .42), 40 + 36 * n)
        self.split.setSizes([top, total - top])

    def _search(self) -> None:
        q = self.find.text().strip()
        if not q:
            self.results_model.set_rows([])
            self.find_info.setText("")
            return
        hits = self.ctl.view.search_tracks(q)
        hits = library.sort_tracks(hits) if len(hits) < 2000 else hits
        hits.sort(key=lambda t: ((t.get("albumartist") or t.get("artist") or "").casefold(),
                                 (t.get("album") or "").casefold(), t.get("discnumber") or 0, t.get("tracknumber") or 0))
        self.results_model.set_rows(hits[:300])
        self.find_info.setText(tr("{0} 首", len(hits)) + (tr("（显示前 300 首）") if len(hits) > 300 else ""))

    def _count_in(self, path: str) -> int:
        pl = self.ctl.state.playlist(self.pid) if self.pid else None
        tid = self.ctl.state.id_for(path)
        return sum(1 for o in (pl or {}).get("items", []) if o["track_id"] == tid) if tid else 0

    def _result_paths(self) -> list[str]:
        rows = sorted({i.row() for i in self.results.selectionModel().selectedRows()})
        return [self.results_model.rows[r]["path"] for r in rows]

    def _result_clicked(self, idx) -> None:
        if idx.column() == self.results_model.ADD_COL:
            self._add_rows([idx.row()])

    def _add_selected_results(self) -> None:
        rows = sorted({i.row() for i in self.results.selectionModel().selectedRows()})
        self._add_rows(rows or ([0] if self.results_model.rows else []))

    def _add_rows(self, rows: list[int]) -> None:
        paths = [self.results_model.rows[r]["path"] for r in rows if 0 <= r < len(self.results_model.rows)]
        if not paths or self.pid is None:
            return
        again = sum(1 for p in paths if self._count_in(p))
        n = self.ctl.state.append(self.pid, paths)
        if n:
            self.view.scrollToBottom()
            name = self.results_model.rows[rows[0]].get("title") if len(paths) == 1 else tr("{0} 首", n)
            self.ctl.toast(tr("已加入歌单：{0}", name) + (tr("（其中 {0} 首歌单里已有，又加了一次）", again) if again else ""))
            self.results_model.refresh_playing()
            top, bottom = self.results_model.index(0, 0), self.results_model.index(len(self.results_model.rows) - 1, 0)
            self.results_model.dataChanged.emit(top, bottom.siblingAtColumn(self.results_model.ADD_COL))

    # ------------------------------------------------------------------ content
    def load(self, pid: str) -> None:
        if pid != self.pid:
            self.view.clearSelection()
        self.pid = pid
        self.refresh()
        pl = self.ctl.state.playlist(pid)
        self.show_finder(bool(pl) and not pl.get("items"))

    def _maybe_gone(self) -> None:
        if self.pid is not None and self.ctl.state.playlist(self.pid) is None:
            self.pid = None
            self.model.set_rows([])

    def refresh(self) -> None:
        pl = self.ctl.state.playlist(self.pid) if self.pid else None
        if pl is None:
            return
        rows, missing = self.ctl.state.playlist_rows(self.pid)
        keep = self.selected_rows()
        self.model.set_rows(rows, missing)
        sm = self.view.selectionModel()
        for r in keep:
            if r < len(rows):
                sm.select(self.model.index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self.listbox.setCurrentWidget(self.view if rows else self.empty)
        if self.finder_box.isVisible():
            self._fit_split()
        present = [t for t in rows if t["path"] not in missing]
        dur = sum((t.get("duration") or 0) for t in present)
        self.title.setText(pl["name"])
        names = list(dict.fromkeys(library.album_artist(t) for t in present))
        if not names:
            self.artists.setText(tr("空歌单") if not rows else "")
        elif len(names) <= 3:
            self.artists.setText(tr("、").join(names))
        else:
            self.artists.setText(tr("{0} 等 {1} 位艺术家", tr('、').join(names[:2]), len(names)))
        formats = sorted({library.format_rate(t) for t in present})
        bits = [tr("{0} 首", len(rows))] + ([format_duration(dur)] if dur else [])
        if formats:
            bits.append(", ".join(formats[:3]) + (" …" if len(formats) > 3 else ""))
        if missing:
            bits.append(tr("{0} 首不在曲库中（标红，位置保留）", len(missing)))
        self.subtitle.setText(" · ".join(bits))
        for k in ("play", "shuffle", "queue"):
            self.buttons[k].setEnabled(len(rows) > len(missing))
        self.refresh_cover()
        if self.finder_box.isVisible() and self.results_model.rows:
            self.results_model.dataChanged.emit(self.results_model.index(0, 0),
                                                self.results_model.index(max(0, len(self.results_model.rows) - 1),
                                                                         self.results_model.columnCount() - 1))

    def cover_pixmap(self, side: int = 220) -> tuple[QPixmap | None, bool]:
        pl = self.ctl.state.playlist(self.pid) if self.pid else None
        return playlist_cover(self.ctl, pl, self.model.rows, self.model.missing, side, self._covers)

    def refresh_cover(self) -> None:
        side = self.cover.side
        pix, custom = self.cover_pixmap(side)
        self.cover.setPixmap(rounded(pix, side) if pix is not None else placeholder(side))
        self.cover.setToolTip(tr("更换封面或名称") if custom else tr("设置封面图片或名称"))

    # ------------------------------------------------------------------ selection helpers
    def selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.view.selectionModel().selectedRows()})

    def selected_paths(self) -> list[str]:
        return [self.model.rows[r]["path"] for r in self.selected_rows() if r < len(self.model.rows)]

    def _all_paths(self) -> list[str]:
        return [t["path"] for t in self.model.rows]

    def _present(self, paths: list[str]) -> list[str]:
        return [p for p in paths if p not in self.model.missing and p in self.ctl.view.by_path]

    def _first_selected(self) -> int:
        rows = self.selected_rows()
        return rows[0] if rows else 0

    def _select(self, rows: list[int]) -> None:
        sm = self.view.selectionModel()
        sm.clearSelection()
        for r in rows:
            sm.select(self.model.index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
        if rows:
            self.view.scrollTo(self.model.index(rows[0], 0))

    # ------------------------------------------------------------------ actions
    def _play(self, row: int) -> None:
        self.ctl.play_rows(self.model.rows, row)

    def _shuffle(self) -> None:
        present = [i for i, t in enumerate(self.model.rows) if t["path"] not in self.model.missing]
        if not present:
            return
        self.ctl.player.set_mode("shuffle")
        self.ctl.play_rows(self.model.rows, random.choice(present))

    def _reordered(self, rows: list[int], target: int) -> None:
        new = self.ctl.state.move_rows(self.pid, rows, target)
        self._select(new)

    def _nudge(self, delta: int) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        if (delta < 0 and rows[0] == 0) or (delta > 0 and rows[-1] == len(self.model.rows) - 1):
            return
        target = rows[0] - 1 if delta < 0 else rows[-1] + 2
        self._select(self.ctl.state.move_rows(self.pid, rows, target))

    def _dropped(self, paths: list[str], target: int) -> None:
        if self.pid is None:
            return
        n = self.ctl.state.append(self.pid, paths, at=target)
        if n:
            self._select(list(range(target, target + n)))
            self.ctl.toast(tr("已加入歌单：{0} 首", n))

    def remove_selected(self) -> None:
        rows = self.selected_rows()
        if rows and self.pid is not None:
            self.ctl.state.remove_rows(self.pid, rows)
            self.ctl.toast(tr("已从歌单移除 {0} 首（Ctrl+Z 撤销）", len(rows)))

    def _to_edge(self, top: bool) -> None:
        rows = self.selected_rows()
        if rows:
            self._select(self.ctl.state.move_rows(self.pid, rows, 0 if top else len(self.model.rows)))

    def _menu(self, pos) -> None:
        paths = self._present(self.selected_paths())
        extra = [(tr("从歌单中移除\tDelete"), self.remove_selected), (tr("移到最前"), lambda: self._to_edge(True)),
                 (tr("移到最后"), lambda: self._to_edge(False))]
        if not paths:
            if self.selected_rows():
                menu = QMenu(self)
                for label, fn in extra:
                    menu.addAction(label, fn)
                menu.exec(self.view.viewport().mapToGlobal(pos))
            return
        self.ctl.track_menu(paths, self.view.viewport().mapToGlobal(pos), extra=extra)

    def _more(self, pos) -> None:
        if self.pid is None:
            return
        pl = self.ctl.state.playlist(self.pid)
        menu = QMenu(self)
        menu.addAction(tr("编辑详情…"), self.edit_details)
        if pl and pl.get("cover"):
            menu.addAction(tr("移除封面图片"), lambda: self.ctl.state.set_cover(self.pid, None))
        menu.addSeparator()
        menu.addAction(tr("导入 M3U/M3U8 到这个歌单…"), lambda: self.ctl.import_m3u(self.pid))
        menu.addAction(tr("导出为 M3U8…"), lambda: self.ctl.export_m3u(self.pid))
        menu.addSeparator()
        menu.addAction(tr("删除歌单…"), lambda: self.ctl.delete_playlist(self.pid))
        menu.exec(pos)

    def edit_details(self) -> None:
        if self.pid is None:
            return
        pid = self.pid
        pl = self.ctl.state.playlist(pid)
        if pl is None:
            return
        pix, custom = self.cover_pixmap(180)
        dlg = EditDetails(self, pl["name"], pix, custom, self.ctl.settings_value("cover_dir") or "")
        if self.ctl.quiet:
            self.ctl.last_dialog = dlg
            dlg.show()
            return
        if dlg.exec() == QDialog.Accepted:
            self.apply_details(pid, dlg)

    def apply_details(self, pid: str, dlg: EditDetails) -> None:
        self.ctl.state.rename_playlist(pid, dlg.result_name())
        if dlg.image:
            self.ctl.set_setting("cover_dir", str(Path(dlg.image).parent))
            self.ctl.state.set_cover(pid, dlg.image)
        elif dlg.remove:
            self.ctl.state.set_cover(pid, None)


__all__ = ["PlaylistPage", "EditDetails", "CoverTile", "mosaic", "rounded", "placeholder"]


# ---------------------------------------------------------------------------------------------- 歌单 (overview)
PID_ROLE = Qt.UserRole + 30
TILE = 240


class PlaylistGrid(QAbstractListModel):
    """The playlists as album-like tiles: cover, name, songs · duration."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[dict] = []

    def set_items(self, items: list[dict]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def set_pixmaps(self, pixes: dict) -> None:
        for row, it in enumerate(self.items):
            pix = pixes.get(it["id"])
            if pix is not None and pix is not it["pix"]:
                it["pix"] = pix
                idx = self.index(row)
                self.dataChanged.emit(idx, idx, [Qt.DecorationRole])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        if role == Qt.DisplayRole:
            return it["name"]
        if role == CAPTION_ROLE:
            return it["sub"]
        if role == Qt.DecorationRole:
            return it["pix"]
        if role == PID_ROLE:
            return it["id"]
        if role == Qt.ToolTipRole:
            return f"{it['name']}\n{it['sub']}"
        return None


class PlaylistsPage(QWidget):
    """歌单: every playlist as a tile, next to the library's other categories. Double-click opens one."""

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self._covers: dict[str, QPixmap] = {}
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        h, _, self.subtitle = header(tr("歌单"))
        new = QPushButton(tr("＋  新建歌单"))
        new.setObjectName("primary")
        new.setCursor(Qt.PointingHandCursor)
        new.clicked.connect(lambda: ctl.create_playlist())
        imp = QPushButton(tr("导入 M3U8…"))
        imp.setCursor(Qt.PointingHandCursor)
        imp.clicked.connect(lambda: ctl.import_m3u())
        h.layout().addWidget(imp)
        h.layout().addWidget(new)
        self.new_btn = new
        lay.addWidget(h)
        self.stack = QStackedWidget()
        self.model = PlaylistGrid(self)
        self.grid = AlbumView()
        self.grid.setObjectName("playlistGrid")
        self.grid.setModel(self.model)
        self.grid.doubleClicked.connect(lambda i: ctl.open_playlist(i.data(PID_ROLE)))
        self.grid.selectionModel().selectionChanged.connect(lambda *_: ctl.set_selection_paths(self._paths()))
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._menu)
        self.stack.addWidget(self.grid)
        self.stack.addWidget(empty_state("home_playlist", tr("还没有歌单"), tr("＋  新建歌单"), lambda: ctl.create_playlist()))
        lay.addWidget(self.stack, 1)
        self._cover_timer = QTimer(self, singleShot=True, interval=150, timeout=self.refresh_covers)

    def _rows(self, pid: str) -> tuple[list[dict], set]:
        return self.ctl.state.playlist_rows(pid)

    def refresh(self) -> None:
        items = []
        for pl in self.ctl.state.playlist_list():
            rows, missing = self._rows(pl["id"])
            secs = sum(t.get("duration") or 0 for t in rows if t["path"] not in missing)
            sub = " · ".join(b for b in (tr("{0} 首", len(rows)), format_duration(secs)) if b)
            pix, _ = playlist_cover(self.ctl, pl, rows, missing, TILE, self._covers)
            items.append({"id": pl["id"], "name": pl["name"], "sub": sub, "pix": pix or placeholder(TILE)})
        self.model.set_items(items)
        self.subtitle.setText(tr("{0} 个", len(items)) if items else "")
        self.stack.setCurrentIndex(0 if items else 1)
        self.new_btn.setVisible(bool(items))

    def covers_arrived(self) -> None:
        self._cover_timer.start()

    def refresh_covers(self) -> None:
        pixes = {}
        for it in self.model.items:
            pl = self.ctl.state.playlist(it["id"])
            rows, missing = self._rows(it["id"])
            pix, _ = playlist_cover(self.ctl, pl, rows, missing, TILE, self._covers)
            if pix is not None:
                pixes[it["id"]] = pix
        self.model.set_pixmaps(pixes)

    def _selected(self) -> list[str]:
        return [i.data(PID_ROLE) for i in sorted(self.grid.selectionModel().selectedIndexes(), key=lambda i: i.row())]

    def _paths(self, pids: list[str] | None = None) -> list[str]:
        out = []
        for pid in (self._selected() if pids is None else pids):
            rows, missing = self._rows(pid)
            out += [t["path"] for t in rows if t["path"] not in missing]
        return out

    def _menu(self, pos) -> None:
        ctl = self.ctl
        idx = self.grid.indexAt(pos)
        menu = QMenu(self)
        if idx.isValid():
            pid = idx.data(PID_ROLE)
            pids = self._selected() if pid in self._selected() else [pid]
            if len(pids) == 1:
                menu.addAction(tr("打开"), lambda: ctl.open_playlist(pid))
            menu.addAction(tr("▶  播放"), lambda: ctl.play_paths(self._paths(pids)))
            menu.addAction(tr("加入播放队列"), lambda: ctl.enqueue_paths(self._paths(pids)))
            if len(pids) == 1:
                menu.addSeparator()
                menu.addAction(tr("编辑详情…"), lambda: ctl.rename_playlist(pid))
                menu.addAction(tr("导出 M3U8…"), lambda: ctl.export_m3u(pid))
                menu.addAction(tr("删除…"), lambda: ctl.delete_playlist(pid))
            menu.addSeparator()
        menu.addAction(tr("新建歌单"), lambda: ctl.create_playlist())
        menu.addAction(tr("导入 M3U/M3U8…"), lambda: ctl.import_m3u())
        menu.exec(self.grid.viewport().mapToGlobal(pos))
