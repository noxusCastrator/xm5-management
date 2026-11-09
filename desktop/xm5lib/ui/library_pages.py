"""Albums / Artists / Tracks / Playlist pages. ``ctl`` is the MainWindow (actions, menus, data)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton,
                               QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from ..core.library import Album, album_artist, format_duration, sort_tracks
from . import catalog, style
from .models import ALBUM_ROLE, PATH_ROLE, AlbumGrid, TrackTable
from .widgets import AlbumView, GroupDelegate, TrackView
from .i18n import tr


def page() -> QWidget:
    w = QWidget()
    w.setObjectName("page")
    return w


def header(title: str, subtitle: str = "") -> tuple[QWidget, QLabel, QLabel]:
    """Page title with the count beside it, XM5 list-page style."""
    w = QWidget()
    t = QLabel(title)
    t.setObjectName("pageTitle")
    s = QLabel(subtitle)
    s.setObjectName("subtitle")
    if len(subtitle) > 48:      # a description goes under the title
        lay = QVBoxLayout(w)
        lay.setContentsMargins(26, 16, 26, 12)
        lay.setSpacing(4)
        lay.addWidget(t)
        lay.addWidget(s)
        return w, t, s
    lay = QHBoxLayout(w)
    lay.setContentsMargins(26, 16, 26, 10)
    lay.setSpacing(14)
    lay.addWidget(t, 0, Qt.AlignBottom)
    lay.addWidget(s, 0, Qt.AlignBottom)
    lay.addStretch(1)
    return w, t, s


def empty_state(icon: str, text: str, button: str, slot) -> QWidget:
    """A page with nothing in it yet: the category's icon, one line, one button."""
    w = page()
    lay = QVBoxLayout(w)
    lay.addStretch(2)
    pic = QLabel()
    pic.setPixmap(QPixmap(style.icon_path(icon)).scaled(56, 56, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    pic.setAlignment(Qt.AlignCenter)
    lay.addWidget(pic)
    big = QLabel(text)
    big.setAlignment(Qt.AlignCenter)
    big.setStyleSheet("font-size: 15pt; font-weight: 600;")
    lay.addWidget(big)
    row = QHBoxLayout()
    row.addStretch(1)
    b = QPushButton(button)
    b.setObjectName("primary")
    b.setCursor(Qt.PointingHandCursor)
    b.clicked.connect(slot)
    row.addWidget(b)
    row.addStretch(1)
    lay.addSpacing(10)
    lay.addLayout(row)
    lay.addStretch(3)
    return w


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


def track_table(ctl, **kw) -> TrackTable:
    return TrackTable(ctl, is_fav=ctl.favorites.is_fav, **kw)


def selected_paths(view: TrackView) -> list[str]:
    rows = sorted({i.row() for i in view.selectionModel().selectedRows()})
    return [view.model().index(r, 0).data(PATH_ROLE) for r in rows]


def first_selected(view: TrackView) -> int:
    rows = sorted({i.row() for i in view.selectionModel().selectedRows()})
    return rows[0] if rows else 0


def wire_track_view(view: TrackView, ctl, on_selection) -> None:
    """Double-click plays the whole list as the queue, from that row (XM5: every list is a queue)."""
    view.setContextMenuPolicy(Qt.CustomContextMenu)
    view.customContextMenuRequested.connect(lambda pos: ctl.track_menu(selected_paths(view), view.viewport().mapToGlobal(pos)))
    view.doubleClicked.connect(lambda i: ctl.play_rows(view.model().rows, i.row()))
    view.selectionModel().selectionChanged.connect(lambda *_: on_selection(selected_paths(view)))


def play_all_button(ctl, view: TrackView, label: str = tr("▶  播放全部")) -> QPushButton:
    """Plays the list from the selected row (or the top) as the queue."""
    b = QPushButton(label)
    b.setObjectName("primary")
    b.setCursor(Qt.PointingHandCursor)
    b.clicked.connect(lambda: ctl.play_rows(view.model().rows, first_selected(view)))
    return b


class AlbumsPage(QWidget):
    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack)
        # grid
        grid_page = page()
        gl = QVBoxLayout(grid_page)
        gl.setContentsMargins(0, 0, 0, 0)
        h, self.title, self.subtitle = header(tr("专辑"))
        gl.addWidget(h)
        self.model = AlbumGrid(self)
        self.grid = AlbumView()
        self.grid.setObjectName("albumGrid")
        self.grid.setModel(self.model)
        self.grid.setContentsMargins(16, 0, 16, 0)
        self.grid.doubleClicked.connect(lambda i: self.show_album(i.data(ALBUM_ROLE)))
        self.grid.selectionModel().selectionChanged.connect(self._grid_selection)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(
            lambda pos: ctl.track_menu(self._grid_paths(), self.grid.viewport().mapToGlobal(pos)))
        gl.addWidget(self.grid, 1)
        self.stack.addWidget(grid_page)
        # detail
        detail = page()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(24, 16, 24, 0)
        back = QPushButton(tr("‹  专辑"))
        back.setObjectName("backButton")
        back.setFlat(True)
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        dl.addWidget(back, 0, Qt.AlignLeft)
        top = QHBoxLayout()
        self.cover = QLabel()
        self.cover.setFixedSize(220, 220)
        self.cover.setStyleSheet("background: #141414; border-radius: 6px; color: #7d6f51;")
        self.cover.setAlignment(Qt.AlignCenter)
        top.addWidget(self.cover)
        meta = QVBoxLayout()
        meta.addStretch(1)
        self.d_title = QLabel()
        self.d_title.setObjectName("pageTitle")
        self.d_title.setWordWrap(True)
        self.d_artist = QLabel()
        self.d_artist.setStyleSheet(f"font-size: 13pt; color: {style.ACCENT};")
        self.d_info = QLabel()
        self.d_info.setObjectName("subtitle")
        self.d_info.setWordWrap(True)
        for w in (self.d_title, self.d_artist, self.d_info):
            meta.addWidget(w)
        buttons = QHBoxLayout()
        play = QPushButton(tr("▶  播放"))
        play.setToolTip(tr("从头播放这张专辑（选中歌曲时从所选那首开始）"))
        play.setObjectName("primary")
        play.clicked.connect(lambda: self.album and ctl.play_rows(self.album.tracks, first_selected(self.tracks)))
        add = QPushButton(tr("加入播放队列"))
        add.setObjectName("enqueue")
        add.clicked.connect(lambda: ctl.enqueue_paths(self._album_paths()))
        plist = QPushButton(tr("加入歌单…"))
        plist.clicked.connect(lambda: ctl.add_to_playlist_menu(self._album_paths(), plist.mapToGlobal(plist.rect().bottomLeft())))
        self.fav = QPushButton(tr("♡  最爱"))
        self.fav.setObjectName("favButton")
        self.fav.clicked.connect(self._toggle_fav)
        for b in (play, add, plist, self.fav):
            buttons.addWidget(b)
        buttons.addStretch(1)
        meta.addLayout(buttons)
        top.addSpacing(22)
        top.addLayout(meta, 1)
        dl.addLayout(top)
        self.tracks_model = track_table(ctl, show_album=False)
        self.tracks = TrackView(config_key="album", defaults=["no", "title", "artist", "duration", "encoding", "mark", "fav"])
        self.tracks.setObjectName("albumTracks")
        self.tracks.setModel(self.tracks_model)
        wire_track_view(self.tracks, ctl, ctl.set_selection_paths)
        dl.addSpacing(10)
        dl.addWidget(self.tracks, 1)
        self.stack.addWidget(detail)
        self.album: Album | None = None

    def set_albums(self, albums: list[Album], subtitle: str) -> None:
        self.model.set_albums(albums)
        self.subtitle.setText(subtitle)
        if self.album is not None:
            fresh = self.ctl.view.album_by_key.get(self.album.key)
            if fresh is None and self.album.tracks:          # renamed in the tag panel: follow its songs
                t = self.ctl.view.by_path.get(self.album.tracks[0]["path"])
                fresh = self.ctl.view.album_of(t) if t is not None else None
            if fresh is None:
                self.stack.setCurrentIndex(0)
            elif self.stack.currentIndex() == 1:
                self.show_album(fresh, select=False)      # same album, refreshed: keep the selection

    def _grid_paths(self) -> list[str]:
        return [t["path"] for i in self.grid.selectionModel().selectedIndexes() for t in i.data(ALBUM_ROLE).tracks]

    def _grid_selection(self, *_):
        self.ctl.set_selection_paths(self._grid_paths())

    def _album_paths(self) -> list[str]:
        paths = selected_paths(self.tracks)
        return paths or ([t["path"] for t in self.album.tracks] if self.album else [])

    def show_album(self, album: Album, select: bool = True) -> None:
        self.album = album
        self.d_title.setText(album.title)
        self.d_artist.setText(album.artist)
        bits = [album.year] if album.year else []
        bits += [tr("{0} 首", len(album.tracks)), format_duration(album.duration), album.formats]
        self.d_info.setText(" · ".join(b for b in bits if b))
        pix = self.ctl.cover_pixmap(album)
        if pix is not None:
            self.cover.setPixmap(rounded(pix, 220))
        else:
            self.cover.setPixmap(QPixmap(style.icon_path("home_album")).scaled(72, 72, Qt.KeepAspectRatio,
                                                                               Qt.SmoothTransformation))
        self.tracks_model.set_rows(album.tracks)
        self.stack.setCurrentIndex(1)
        self._fav_state()
        if select:
            self.ctl.set_selection_paths([t["path"] for t in album.tracks])

    def set_cover(self, pix: QPixmap) -> None:
        self.cover.setPixmap(rounded(pix, 220))

    def _fav_state(self) -> None:
        paths = [t["path"] for t in self.album.tracks] if self.album else []
        on = bool(paths) and all(self.ctl.favorites.is_fav(p) for p in paths)
        self.fav.setText(tr("♥  已是最爱") if on else tr("♡  最爱"))
        self.fav.setStyleSheet(f"color: {style.ACCENT};" if on else "")

    def _toggle_fav(self) -> None:
        self.ctl.toggle_favorite(self._album_paths())
        self._fav_state()


class GroupTracksPage(QWidget):
    """Groups on the left, their songs on the right as a list (the queue a double-click plays).

    Only 专辑 shows albums; artists, genres and resolutions list songs, like the player's queue."""

    TITLE = ""
    ICON = ""
    UNIT = tr("档")
    KEY = ""                    # the list's own column setup

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.groups: dict[str, list[dict]] = {}
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        h, _, self.subtitle = header(self.TITLE)
        lay.addWidget(h)
        split = QSplitter()
        split.setHandleWidth(1)
        self.list = QListWidget()
        self.list.setObjectName("groupList")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setItemDelegate(GroupDelegate(self.list, QPixmap(style.icon_path(self.ICON)) if self.ICON else None))
        self.list.setMouseTracking(True)
        self.list.setMinimumWidth(220)
        self.list.setMaximumWidth(340)
        self.list.currentItemChanged.connect(self._group)
        self.list.itemActivated.connect(lambda _: ctl.play_rows(self.model.rows, 0))
        split.addWidget(self.list)
        self.model = track_table(ctl)
        self.view = TrackView(config_key=self.KEY)
        self.view.setModel(self.model)
        wire_track_view(self.view, ctl, ctl.set_selection_paths)
        split.addWidget(self.view)
        h.layout().addWidget(play_all_button(ctl, self.view))
        split.setStretchFactor(1, 1)
        split.setSizes([260, 900])
        lay.addWidget(split, 1)

    def set_groups(self, groups: list[tuple[str, list[dict]]]) -> None:
        self.groups = dict(groups)
        current = self.list.currentItem().data(Qt.UserRole) if self.list.currentItem() else None
        self.list.blockSignals(True)
        self.list.clear()
        for name, tracks in groups:
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, name)
            it.setData(GroupDelegate.COUNT_ROLE, tr("{0} 首", len(tracks)))
            self.list.addItem(it)
            if name == current:
                self.list.setCurrentItem(it)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self.subtitle.setText(f"{len(groups)} {self.UNIT}")
        self._group(self.list.currentItem())

    def _group(self, item, *_):
        rows = self.groups.get(item.data(Qt.UserRole), []) if item is not None else []
        self.model.set_rows(sorted(rows, key=lambda t: ((t.get("albumartist") or t.get("artist") or "").casefold(),
                                                         (t.get("album") or "").casefold(), t.get("discnumber") or 0,
                                                         t.get("tracknumber") or 0)))


class ResolutionPage(GroupTracksPage):
    TITLE = tr("解析度")
    ICON = "home_hr"
    KEY = "resolution"


class ArtistsPage(GroupTracksPage):
    TITLE = tr("歌手")
    ICON = "home_songer"
    UNIT = tr("位")
    KEY = "artists"

    def set_artists(self, artists: list[tuple[str, int]]) -> None:
        q, view = self.ctl.query, self.ctl.view
        groups = []
        for name, _ in artists:
            k = name.casefold()
            tracks = [t for a in view.search_albums(q, artist=name) for t in a.tracks
                      if view.matches(t, q) and album_artist(t).casefold() == k]
            if tracks:
                groups.append((name, tracks))
        self.set_groups(groups)


class GenrePage(GroupTracksPage):
    TITLE = tr("风格")
    ICON = "home_genre"
    UNIT = tr("种")
    KEY = "genres"


class FavoritesPage(QWidget):
    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        h, _, self.subtitle = header(tr("最爱"))
        lay.addWidget(h)
        bar = QHBoxLayout()
        bar.setContentsMargins(26, 0, 26, 10)
        self.model = track_table(ctl)
        self.view = TrackView(config_key="favorites")
        self.view.setModel(self.model)
        play = play_all_button(ctl, self.view)
        plist = QPushButton(tr("加入歌单…"))
        plist.clicked.connect(lambda: ctl.add_to_playlist_menu(self._targets(), plist.mapToGlobal(plist.rect().bottomLeft())))
        unfav = QPushButton(tr("移出最爱"))
        unfav.clicked.connect(lambda: ctl.set_favorite(selected_paths(self.view), False))
        for b in (play, plist, unfav):
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        wire_track_view(self.view, ctl, ctl.set_selection_paths)
        lay.addWidget(self.view, 1)
        self.empty = QLabel(tr("还没有最爱。右键歌曲或专辑，选择「加入最爱」。"))
        self.empty.setProperty("muted", True)
        self.empty.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.empty, 1)

    def refresh(self) -> None:
        by = self.ctl.state.by_id
        q = self.ctl.query
        rows = [by[tid] for tid in self.ctl.state.fav_ids if tid in by and self.ctl.view.matches(by[tid], q)]
        self.model.set_rows(rows, {row['path'] for row in rows if row.get('unresolved')})
        self.subtitle.setText(tr("{0} 首", len(rows)))
        self.view.setVisible(bool(rows))
        self.empty.setVisible(not rows)

    def _targets(self) -> list[str]:
        return selected_paths(self.view) or [t["path"] for t in self.model.rows]


class FolderPage(QWidget):
    """文件夹: the music folders themselves (add / rescan / remove at the top level) and, inside each, the
    folders on disk drilled down like the player's 文件夹 menu. The window only ever reads these folders."""

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.root = catalog.Folder(tr("音乐"), ())
        self.path: tuple[str, ...] = ()
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        h, self.title, self.subtitle = header(tr("文件夹"))
        lay.addWidget(h)
        self.crumbs = QHBoxLayout()
        self.crumbs.setContentsMargins(22, 0, 26, 8)
        self.crumbs.setSpacing(2)
        lay.addLayout(self.crumbs)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)

        # no music folder yet
        self.empty = empty_state("home_folder", tr("还没有音乐文件夹"), tr("＋  添加音乐文件夹"), self.add_folder)
        self.stack.addWidget(self.empty)

        # browser
        body = page()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        self.split = QSplitter(Qt.Vertical)
        self.split.setHandleWidth(1)
        self.list = QListWidget()
        self.list.setObjectName("groupList")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setItemDelegate(GroupDelegate(self.list, QPixmap(style.icon_path("home_folder"))))
        self.list.setMouseTracking(True)
        self.list.itemActivated.connect(lambda it: self.open(self.path + (it.data(Qt.UserRole),)))
        self.list.itemDoubleClicked.connect(lambda it: self.open(self.path + (it.data(Qt.UserRole),)))
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._folder_menu)
        self.split.addWidget(self.list)
        self.model = track_table(ctl)
        self.view = TrackView(config_key="folders")
        self.view.setModel(self.model)
        wire_track_view(self.view, ctl, ctl.set_selection_paths)
        self.split.addWidget(self.view)
        bl.addWidget(self.split, 1)
        self.stack.addWidget(body)
        self.add_btn = QPushButton(tr("＋  添加文件夹"))
        self.add_btn.setObjectName("addFolder")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(self.add_folder)
        h.layout().addWidget(self.add_btn)
        self.play_all = play_all_button(ctl, self.view)
        h.layout().addWidget(self.play_all)

    # ------------------------------------------------------------------ data
    def set_root(self, root: catalog.Folder) -> None:
        self.root = root
        if catalog.find_folder(root, self.path) is None:
            self.path = ()
        self.open(self.path)

    def open(self, path: tuple[str, ...]) -> None:
        node = catalog.find_folder(self.root, path)
        if node is None:
            return
        self.path = path
        roots = self.ctl.music_roots()
        self.stack.setCurrentWidget(self.empty if not roots and not self.ctl.query else self.stack.widget(1))
        self.list.clear()
        top = not path
        for name in (node.folders if top else sorted(node.folders, key=str.casefold)):
            sub = node.folders[name]
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, name)
            if top:
                exists = bool(sub.root) and Path(sub.root).is_dir()
                it.setData(GroupDelegate.SUB_ROLE, sub.root)
                it.setData(GroupDelegate.COUNT_ROLE, tr("{0} 首  ›", sub.total) if exists else tr("找不到这个文件夹"))
                it.setData(GroupDelegate.WARN_ROLE, not exists)
                it.setToolTip(sub.root)
            else:
                it.setData(GroupDelegate.COUNT_ROLE, tr("{0} 首  ›", sub.total))
            self.list.addItem(it)
        self.model.set_rows(sort_tracks(node.tracks))
        self.list.setVisible(bool(node.folders))
        self.view.setVisible(bool(node.tracks))
        self.play_all.setVisible(bool(node.tracks) or (top and node.total > 0))
        self.add_btn.setVisible(top and self.stack.currentWidget() is not self.empty)
        if node.folders and node.tracks:
            self.split.setSizes([max(140, min(46 * len(node.folders), 360)), 400])
        if top:
            self.subtitle.setText(tr("{0} 个音乐文件夹 · {1} 首", len(roots), node.total))
        else:
            self.subtitle.setText(tr("{0} 个文件夹 · {1} 首", len(node.folders), node.total))
        self._crumbs()

    def _crumbs(self) -> None:
        while self.crumbs.count():
            w = self.crumbs.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        parts = [(tr("音乐"), ())] + [(p, self.path[:i + 1]) for i, p in enumerate(self.path)]
        for i, (name, path) in enumerate(parts):
            last = i == len(parts) - 1
            b = QPushButton(name)
            b.setObjectName("crumbCurrent" if last else "crumb")
            b.setCursor(Qt.PointingHandCursor)
            if not last:
                b.clicked.connect(lambda _=False, p=path: self.open(p))
            self.crumbs.addWidget(b)
            if not last:
                sep = QLabel("›")
                sep.setProperty("muted", True)
                self.crumbs.addWidget(sep)
        self.crumbs.addStretch(1)
        for i in range(self.crumbs.count()):                 # at the top the title says it all
            w = self.crumbs.itemAt(i).widget()
            if w is not None:
                w.setVisible(bool(self.path))

    def _all_paths(self, node) -> list[str]:
        out = [t["path"] for t in node.tracks]
        for sub in node.folders.values():
            out += self._all_paths(sub)
        return out

    def all_paths(self) -> list[str]:
        node = catalog.find_folder(self.root, self.path)
        return self._all_paths(node) if node is not None else []

    # ------------------------------------------------------------------ actions
    def add_folder(self) -> None:
        start = (self.ctl.music_roots() or [""])[-1]
        folder = QFileDialog.getExistingDirectory(self, tr("选择音乐文件夹"), str(Path(start).parent) if start else "")
        if folder:
            self.ctl.add_music_folder(folder)

    def remove_folder(self, root: str) -> None:
        if self.ctl.quiet or self.ctl.ask(
                tr("从曲库移除"), tr("把「{0}」从曲库移除？\n\n文件夹和里面的音乐不会被删除；歌单里来自这个文件夹的歌会标红保留，以后再添加回来就会恢复。", root), tr("移除"), tr("取消")):
            self.ctl.remove_music_folder(root)

    def _folder_menu(self, pos) -> None:
        it = self.list.itemAt(pos)
        if it is None:
            return
        node = catalog.find_folder(self.root, self.path + (it.data(Qt.UserRole),))
        if node is None:
            return
        gpos = self.list.viewport().mapToGlobal(pos)
        if self.path:                                           # a folder inside a music folder
            self.ctl.track_menu(self._all_paths(node), gpos)
            return
        root = node.root
        extra = [(tr("打开"), lambda: self.open((node.name,))), (tr("重新扫描这个文件夹"), lambda: self.ctl.scan_folder(root)),
                 (tr("在资源管理器中打开"), lambda: _explore(root)), (tr("从曲库移除…"), lambda: self.remove_folder(root))]
        paths = self._all_paths(node)
        if paths:
            self.ctl.track_menu(paths, gpos, extra=extra, explorer=False)
            return
        menu = QMenu(self)
        for label, fn in extra:
            menu.addAction(label, fn)
        menu.exec(gpos)

    def go_up(self) -> None:
        if self.path:
            self.open(self.path[:-1])


def _explore(path: str) -> None:
    try:
        subprocess.Popen(["explorer.exe", os.path.normpath(path)])
    except OSError:
        pass


class TracksPage(QWidget):
    SORT_KEYS = {"no": "tracknumber", "title": "title", "artist": "artist", "album": "album",
                 "albumartist": "albumartist", "genre": "genre", "era": "date", "duration": "duration",
                 "encoding": "codec", "size": "size", "filename": "path", "location": "path"}

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        h, _, self.subtitle = header(tr("所有歌曲"))
        lay.addWidget(h)
        self.model = track_table(ctl)
        self.view = TrackView(config_key="tracks")
        self.view.setObjectName("trackTable")
        self.view.setModel(self.model)
        h.layout().addWidget(play_all_button(ctl, self.view))
        self.view.horizontalHeader().setSectionsClickable(True)
        self.view.horizontalHeader().sectionClicked.connect(self._sort)
        wire_track_view(self.view, ctl, ctl.set_selection_paths)
        lay.addWidget(self.view, 1)
        self.sort = ("album", False)

    def set_tracks(self, tracks: list[dict], subtitle: str) -> None:
        self.subtitle.setText(subtitle)
        self.model.set_rows(self._sorted(tracks))

    def _sorted(self, tracks):
        cid, desc = self.sort
        if cid == "album":
            rows = sorted(tracks, key=lambda t: ((t.get("album") or "").casefold(), t.get("discnumber") or 0,
                                                 t.get("tracknumber") or 0))
        else:
            key = self.SORT_KEYS.get(cid, "title")
            rows = sorted(tracks, key=lambda t: (t.get(key) is None, str(t.get(key) or "").casefold()
                                                 if not isinstance(t.get(key), (int, float)) else t.get(key)))
        return rows[::-1] if desc else rows

    def _sort(self, logical):
        cid = self.model.IDS[logical]
        if cid not in self.SORT_KEYS:
            return
        self.sort = (cid, not self.sort[1] if self.sort[0] == cid else False)
        self.model.set_rows(self._sorted(self.model.rows))


__all__ = ["AlbumsPage", "ArtistsPage", "GenrePage", "ResolutionPage", "FavoritesPage", "FolderPage", "TracksPage",
           "sort_tracks", "first_selected"]
