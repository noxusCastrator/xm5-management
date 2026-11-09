"""Qt item models over the core library view."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QAbstractTableModel, QMimeData, QModelIndex, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPixmap

from ..core.library import Album, format_duration, format_rate
from . import style
from .i18n import tr

PATH_ROLE = Qt.UserRole + 1
ALBUM_ROLE = Qt.UserRole + 2
CAPTION_ROLE = Qt.UserRole + 3      # a grid tile's second line (playlists); albums use artist · year
MIME_PATHS = "application/x-xm5lib-paths"
NOW_PLAYING = {"path": None}    # set by the main window; track tables mark that row


L, R, C = int(Qt.AlignLeft | Qt.AlignVCenter), int(Qt.AlignRight | Qt.AlignVCenter), int(Qt.AlignCenter)

# The columns a track table can show: only what the XM5 itself shows about a song (its lists, the
# playing screen and 歌曲信息: artist, album, album artist, genre, era, encoding, size, location).
# id: (header, name in the 选择显示项 menu, default width, alignment)
COLUMN_DEFS = {
    "no": ("#", tr("音轨号"), 52, R),
    "title": (tr("标题"), tr("标题"), 300, L),
    "artist": (tr("艺术家"), tr("艺术家"), 190, L),
    "album": (tr("专辑"), tr("专辑"), 230, L),
    "albumartist": (tr("专辑表演者"), tr("专辑表演者"), 170, L),
    "genre": (tr("风格"), tr("风格"), 120, L),
    "era": (tr("年代"), tr("年代"), 64, R),
    "duration": (tr("时长"), tr("时长"), 64, R),
    "encoding": (tr("编码"), tr("编码"), 150, L),
    "size": (tr("大小"), tr("大小"), 80, R),
    "filename": (tr("文件名"), tr("文件名"), 220, L),
    "location": (tr("位置"), tr("位置"), 320, L),
    "mark": ("", tr("标记（改过标签 / 读取出错）"), 26, C),
    "fav": ("", tr("我的最爱"), 34, C),
    # playlists only: the play order, and the handle a song is dragged by
    "pos": (tr("序号"), tr("序号"), 64, R),
    # the playlist's song finder only
    "inlist": ("", tr("已在歌单里"), 24, C),
    "add": ("", tr("添加"), 72, C),
}
TRACK_IDS = ["no", "title", "artist", "album", "albumartist", "genre", "era", "duration", "encoding", "size",
             "filename", "location", "mark", "fav"]
MUTED_IDS = {"no", "era", "duration", "encoding", "size", "location", "mark", "pos"}


def human_size(n) -> str:
    if not n:
        return ""
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


class TrackTable(QAbstractTableModel):
    """Every column a track list can show; each page's view hides, orders and sizes them (its own header)."""

    IDS = TRACK_IDS
    DEFAULT = ["no", "title", "artist", "album", "duration", "encoding", "mark", "fav"]
    LOCKED = {"title"}                      # always shown

    def __init__(self, parent=None, show_album: bool = True, reorderable: bool = False, is_fav=None):
        super().__init__(parent)
        self.is_fav = is_fav or (lambda _p: False)
        self.rows: list[dict] = []
        self.missing: set[str] = set()
        self.show_album = show_album
        self.reorderable = reorderable

    @property
    def COLUMNS(self):                       # (header, width) per column, for the view
        return [(COLUMN_DEFS[c][0], COLUMN_DEFS[c][2]) for c in self.IDS]

    def col(self, cid: str) -> int:
        return self.IDS.index(cid) if cid in self.IDS else -1

    def set_rows(self, rows: list[dict], missing: set[str] | None = None) -> None:
        self.beginResetModel()
        self.rows = rows
        self.missing = missing or set()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.IDS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and 0 <= section < len(self.IDS):
            return COLUMN_DEFS[self.IDS[section]][0]
        return None

    def text(self, t: dict, cid: str, row: int) -> str:
        missing = t["path"] in self.missing
        if cid == "no":
            if t["path"] == NOW_PLAYING["path"] and not missing:
                return "▶"
            n, d = t.get("tracknumber"), t.get("discnumber")
            return "" if not n else (f"{d}.{n:02d}" if d and d > 1 else str(n))
        if cid == "title":
            if missing:
                return tr("{0}  （不在曲库里）", Path(t['path']).name)
            return t.get("title") or Path(t["path"]).stem
        if cid == "artist":
            return t.get("artist") or t.get("albumartist") or ""
        if cid in ("album", "albumartist", "genre"):
            return t.get(cid) or ""
        if cid == "era":
            return str(t.get("date") or "")[:4]
        if cid == "duration":
            return format_duration(t.get("duration"))
        if cid == "encoding":
            return format_rate(t) if t.get("codec") or t.get("sample_rate") else ""
        if cid == "size":
            return human_size(t.get("size"))
        if cid == "filename":
            return Path(t["path"]).name
        if cid == "location":
            return str(Path(t["path"]).parent)
        if cid == "mark":
            return "✎" if t.get("edited") else ("✱" if t.get("error") else "")
        if cid == "fav":
            return "♥" if self.is_fav(t["path"]) else ""
        return ""

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        t = self.rows[index.row()]
        cid = self.IDS[index.column()]
        if role == PATH_ROLE:
            return t["path"]
        missing = t["path"] in self.missing
        playing = t["path"] == NOW_PLAYING["path"]
        if role == Qt.DisplayRole:
            return self.text(t, cid, index.row())
        if role == Qt.ToolTipRole:
            if cid == "fav":
                return tr("最爱（右键可以取消）") if self.is_fav(t["path"]) else None
            if t.get("error"):
                return tr("读取出错：{0}", t['error'])
            if cid == "mark" and t.get("edited"):
                return tr("改过标签（只记在曲库里，文件本身没变）")
            return t["path"]
        if role == Qt.ForegroundRole:
            if missing or t.get("error"):
                return QColor(style.DANGER)
            if playing and cid in ("no", "title", "pos"):
                return QColor(style.ACCENT)
            if cid == "fav":
                return QColor(style.ACCENT)
            if cid in MUTED_IDS:
                return QColor(style.MUTED)
        if role == Qt.TextAlignmentRole:
            return COLUMN_DEFS[cid][3]
        return None

    def flags(self, index):
        f = super().flags(index)
        if index.isValid():
            f |= Qt.ItemIsDragEnabled
        elif self.reorderable:
            f |= Qt.ItemIsDropEnabled
        return f

    def _refresh(self, cids) -> None:
        cols = [self.col(c) for c in cids if self.col(c) >= 0]
        if self.rows and cols:
            self.dataChanged.emit(self.index(0, min(cols)), self.index(len(self.rows) - 1, max(cols)))

    def refresh_favs(self) -> None:
        self._refresh(["fav"])

    def refresh_playing(self) -> None:
        self._refresh(["no", "title", "pos", "inlist", "add"])

    def mimeTypes(self):
        return [MIME_PATHS]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes})
        md = QMimeData()
        md.setData(MIME_PATHS, "\n".join(self.rows[r]["path"] for r in rows).encode("utf-8"))
        md.setData("application/x-xm5lib-rows", ",".join(map(str, rows)).encode())
        return md

    def supportedDropActions(self):
        return Qt.MoveAction if self.reorderable else Qt.IgnoreAction

    def supportedDragActions(self):
        return Qt.CopyAction | Qt.MoveAction


class PlaylistTable(TrackTable):
    """A playlist's rows in play order. 序号 -- the playlist's own column, always first -- is the order,
    and the handle a song is dragged by."""

    IDS = ["pos"] + TRACK_IDS
    DEFAULT = ["pos", "title", "artist", "album", "duration", "encoding", "mark", "fav"]
    LOCKED = {"pos", "title"}
    FIRST = "pos"

    def text(self, t: dict, cid: str, row: int) -> str:
        if cid == "pos":
            if t["path"] == NOW_PLAYING["path"] and t["path"] not in self.missing:
                return "▶"
            return str(row + 1)
        return super().text(t, cid, row)

    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and self.IDS[index.column()] == "pos" and role == Qt.ToolTipRole and self.reorderable:
            return tr("按住序号上下拖动，调整播放顺序")
        return super().data(index, role)

    def mimeData(self, indexes):
        md = super().mimeData(indexes)
        # missing songs keep their place in the list but are never dragged into other lists
        rows = sorted({i.row() for i in indexes})
        md.setData(MIME_PATHS, "\n".join(self.rows[r]["path"] for r in rows
                                         if self.rows[r]["path"] not in self.missing).encode("utf-8"))
        return md


class FinderTable(TrackTable):
    """Search results under a playlist; ✓ marks songs already in it, the last column adds one."""

    IDS = ["inlist"] + TRACK_IDS + ["add"]
    DEFAULT = ["inlist", "title", "artist", "album", "duration", "encoding", "add"]
    LOCKED = {"title", "add"}
    LAST = "add"

    def __init__(self, parent=None, is_fav=None, count_in=None):
        super().__init__(parent, is_fav=is_fav)
        self.count_in = count_in or (lambda _p: 0)

    @property
    def ADD_COL(self) -> int:
        return self.col("add")

    def text(self, t: dict, cid: str, row: int) -> str:
        if cid == "add":
            return tr("添加")
        if cid == "inlist":
            return "✓" if self.count_in(t["path"]) else ""
        return super().text(t, cid, row)

    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and self.IDS[index.column()] == "add":
            t = self.rows[index.row()]
            if role == Qt.ForegroundRole:
                return QColor(style.ACCENT)
            if role == Qt.ToolTipRole:
                n = self.count_in(t["path"])
                return tr("加到歌单末尾（歌单里已有 {0} 次）", n) if n else tr("加到歌单末尾")
            if role == Qt.FontRole:
                return bold(QFont())
        return super().data(index, role)


class AlbumGrid(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.albums: list[Album] = []
        self.pixmaps: dict[str, QPixmap] = {}
        self.placeholder = QPixmap()

    def set_albums(self, albums: list[Album]) -> None:
        self.beginResetModel()
        self.albums = albums
        self.endResetModel()

    def set_cover(self, cover_id: str, pixmap: QPixmap) -> None:
        self.pixmaps[cover_id] = pixmap
        for row, a in enumerate(self.albums):
            if a.cover_id == cover_id:
                idx = self.index(row)
                self.dataChanged.emit(idx, idx, [Qt.DecorationRole])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.albums)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        a = self.albums[index.row()]
        if role == Qt.DisplayRole:
            return a.title
        if role == ALBUM_ROLE:
            return a
        if role == Qt.DecorationRole:
            return self.pixmaps.get(a.cover_id)
        if role == Qt.ToolTipRole:
            return f"{a.title}\n{a.artist}\n{len(a.tracks)} tracks · {format_duration(a.duration)}\n{a.formats}"
        return None

    def flags(self, index):
        return super().flags(index) | Qt.ItemIsDragEnabled

    def mimeTypes(self):
        return [MIME_PATHS]

    def mimeData(self, indexes):
        md = QMimeData()
        paths = [t["path"] for i in indexes for t in self.albums[i.row()].tracks]
        md.setData(MIME_PATHS, "\n".join(paths).encode("utf-8"))
        return md


def bold(font: QFont) -> QFont:
    f = QFont(font)
    f.setBold(True)
    return f


GRID_SIZE = QSize(196, 252)   # minimum cell; AlbumView stretches cells to fill the row
COVER = 168
CAPTION = 56
