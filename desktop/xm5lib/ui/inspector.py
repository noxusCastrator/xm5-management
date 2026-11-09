"""Right-hand tag panel, laid out like Mp3tag's but only with what the XM5 shows about a song (标题,
艺术家, 专辑, 年份 · 音轨 · 风格, 专辑艺术家; the folder; the front cover). The fields are what is in the
selected files, and 保存 writes them into the files (``tagfile``). With several files a field that
differs shows <保留> (left alone unless you type over it); <空白> clears it. The cover is changed with a
right-click and written with the other fields. The mouse wheel never changes a field: it scrolls.

The files are read in the background; a newer selection simply wins.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from PySide6.QtCore import QObject, QThreadPool, Qt, Signal
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMenu, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from . import style, tagfile
from .console_panel import ConsolePage
from .i18n import tr

LABELS = {"title": tr("标题"), "artist": tr("艺术家"), "album": tr("专辑"), "date": tr("年份"), "tracknumber": tr("音轨"),
          "genre": tr("风格"), "albumartist": tr("专辑艺术家")}
KEEP = tr("<保留>")
BLANK = tr("<空白>")
MAX_FILES = 2000
NUMBER = re.compile(r"^\d+(/\d+)?$")
COVER = 80


class _Reader(QObject):
    done = Signal(int, list)


class TagField(QComboBox):
    """An editable combo like Mp3tag's: the value, and in the list <保留>/<空白> and the values found."""
    edited = Signal()
    submitted = Signal()

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.setObjectName(f"tag_{key}")
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMaxVisibleItems(16)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(4)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.lineEdit().textEdited.connect(lambda _t: self.edited.emit())
        self.lineEdit().returnPressed.connect(self.submitted.emit)
        self.activated.connect(lambda _i: self.edited.emit())
        self.initial = ""
        self.mixed = False

    def load(self, values: list[str]) -> None:
        distinct = list(dict.fromkeys(values))
        self.mixed = len(distinct) > 1
        self.blockSignals(True)
        self.clear()
        if self.mixed:
            self.addItem(KEEP)
        self.addItem(BLANK)
        for v in distinct[:60]:
            if v:
                self.addItem(v)
        self.initial = KEEP if self.mixed else (distinct[0] if distinct else "")
        self.setEditText(self.initial)
        self.lineEdit().setCursorPosition(0)
        self.blockSignals(False)

    def wheelEvent(self, e):
        e.ignore()                      # let the panel scroll; the value changes only by typing or the list

    def change(self) -> str | None:
        """None: leave the field alone; "" clears it; otherwise the new text."""
        text = self.currentText()
        if self.mixed and text == KEEP:
            return None
        if text == BLANK:
            text = ""
        text = text.strip()
        if not self.mixed and text == self.initial.strip():
            return None
        return text


class CoverBox(QLabel):
    menu = Signal(object)
    activated = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("tagCover")
        self.setFixedSize(COVER, COVER)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("QLabel#tagCover { background: #141414; border: 1px solid #262626; border-radius: 4px; color: #6f6f6f; }")
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: self.menu.emit(self.mapToGlobal(pos)))
        self.setCursor(Qt.PointingHandCursor)

    def mouseDoubleClickEvent(self, e):
        self.activated.emit()


class Inspector(QWidget):
    """The right-hand column. A row of tabs (标签 now; a 脚本 tab is planned next to it) with the current
    tab's actions at the far right, then that tab's page. The 标签 page lists only what the XM5 shows
    about a song and fits in the height of the sidebar's categories, so it never needs scrolling."""
    # (paths, {field: text}, cover: tagfile.KEEP | None | Picture, note -- always None now)
    save_requested = Signal(list, dict, object, object)
    lyrics_requested = Signal(list)          # 歌词… (lyrics_dialog, storage-20261104), one song at a time
    message = Signal(str)                    # shown by the window (status line)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("inspector")
        self.setMinimumWidth(280)
        self.setMaximumWidth(380)
        self.tracks: list[dict] = []
        self.files: list[tagfile.FileTags] = []
        self.cover_change = tagfile.KEEP
        self._gen = 0
        self._reader = _Reader()
        self._reader.done.connect(self._loaded)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 2, 12, 4)
        outer.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tags_tab = QPushButton(tr("标签"))
        self.tags_tab.setObjectName("panelTab")
        self.tags_tab.setCheckable(True)
        self.tags_tab.setChecked(True)
        self.tab_group.addButton(self.tags_tab, 0)
        head.addWidget(self.tags_tab)
        self.console_tab = QPushButton(tr("指令"))
        self.console_tab.setObjectName("panelTab")
        self.console_tab.setCheckable(True)
        self.tab_group.addButton(self.console_tab, 1)
        head.addSpacing(8)
        head.addWidget(self.console_tab)
        head.addStretch(1)
        self.clear_log = QPushButton(tr("清屏"))
        self.clear_log.setObjectName("panelAction")
        self.stop_run = QPushButton(tr("停止"))
        self.stop_run.setObjectName("panelAction")
        head.addWidget(self.clear_log)
        head.addWidget(self.stop_run)
        self.discard = QPushButton(tr("放弃"))
        self.discard.setObjectName("panelAction")
        self.discard.setToolTip(tr("放弃还没保存的修改"))
        self.discard.clicked.connect(lambda: self._fill())
        self.save = QPushButton(tr("保存"))
        self.save.setObjectName("panelSave")
        self.save.setToolTip(tr("写入文件（Ctrl+S）"))
        self.save.clicked.connect(self._save)
        head.addWidget(self.discard)
        head.addWidget(self.save)
        outer.addLayout(head)
        self.pages = QStackedWidget()
        self.pages.setObjectName("panelPages")
        outer.addWidget(self.pages)
        outer.addStretch(1)
        self.tab_group.idClicked.connect(self.show_tab)
        self._running = False

        page = QWidget()
        page.setObjectName("tagPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(0)
        self.notice = QLabel("")                 # only when a file cannot be changed
        self.notice.setObjectName("tagNotice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        lay.addWidget(self.notice)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(0)
        self.fields: dict[str, TagField] = {}
        row = 0

        def label(text, r, c, span=1):
            l = QLabel(text)
            l.setObjectName("tagLabel")
            l.setContentsMargins(1, 3, 0, 1)
            grid.addWidget(l, r, c, 1, span)

        def field(key, r, c, span=1):
            f = TagField(key)
            f.edited.connect(self._dirty)
            f.submitted.connect(self._save)
            self.fields[key] = f
            grid.addWidget(f, r, c, 1, span)

        for key in ("title", "artist", "album"):
            label(LABELS[key], row, 0, 3)
            field(key, row + 1, 0, 3)
            row += 2
        for c, key in enumerate(("date", "tracknumber", "genre")):
            label(LABELS[key], row, c)
            field(key, row + 1, c)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 4)
        row += 2
        label(LABELS["albumartist"], row, 0, 3)
        field("albumartist", row + 1, 0, 3)
        row += 2
        label(tr("文件夹"), row, 0, 3)
        self.directory = QLineEdit()
        self.directory.setObjectName("tagDirectory")
        self.directory.setReadOnly(True)
        grid.addWidget(self.directory, row + 1, 0, 1, 3)
        lay.addLayout(grid)

        cover_row = QHBoxLayout()
        cover_row.setContentsMargins(0, 6, 0, 0)
        self.cover = CoverBox()
        self.cover.menu.connect(self._cover_menu)
        self.cover.activated.connect(self.choose_cover)
        cover_row.addWidget(self.cover, 0, Qt.AlignTop)
        self.cover_info = QLabel("")
        self.cover_info.setObjectName("coverInfo")
        self.cover_info.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.cover_info.setWordWrap(True)
        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.addWidget(self.cover_info, 1)
        self.lyrics_button = QPushButton(tr("歌词…"))
        self.lyrics_button.setObjectName("manageLyrics")
        self.lyrics_button.clicked.connect(lambda: self.lyrics_requested.emit(list(self.tracks)))
        side.addWidget(self.lyrics_button, 0, Qt.AlignLeft | Qt.AlignBottom)
        cover_row.addSpacing(10)
        cover_row.addLayout(side, 1)
        lay.addLayout(cover_row)
        self.pages.addWidget(page)
        self.console_page = ConsolePage(match=page)
        self.console_page.busyChanged.connect(self._console_busy)
        self.clear_log.clicked.connect(self.console_page.clear)
        self.stop_run.clicked.connect(self.console_page.stop)
        self.pages.addWidget(self.console_page)

        QShortcut(QKeySequence.Save, self, activated=self._save, context=Qt.WindowShortcut)
        self.setStyleSheet(
            f"QLabel#tagLabel {{ color: {style.MUTED}; font-size: 9pt; }}"
            f"QComboBox, QLineEdit#tagDirectory {{ padding: 2px 6px; }}"
            f"QLabel#coverInfo {{ color: #bdbdbd; font-size: 9pt; }}"
            f"QLabel#tagNotice {{ color: {style.DANGER}; font-size: 9pt; padding: 2px 0 2px 0; }}"
            f"QPushButton#panelTab {{ background: transparent; border: none; border-bottom: 2px solid transparent;"
            f" border-radius: 0; color: {style.MUTED}; font-weight: 600; padding: 4px 2px 3px 2px; min-width: 0; }}"
            f"QPushButton#panelTab:checked {{ color: {style.ACCENT}; border-bottom-color: {style.ACCENT}; }}"
            f"QPushButton#panelTab:hover:!checked {{ color: #e6e6e6; }}"
            f"QPushButton#panelAction {{ background: transparent; border: 1px solid #333; border-radius: 4px;"
            f" color: #d8d8d8; padding: 2px 10px; min-width: 0; }}"
            f"QPushButton#panelAction:hover {{ border-color: #555; color: #fff; }}"
            f"QPushButton#panelAction:disabled {{ border-color: #262626; color: #555; }}"
            f"QPushButton#panelSave {{ background: {style.ACCENT}; color: #111; border: 1px solid {style.ACCENT};"
            f" border-radius: 4px; font-weight: 600; padding: 2px 12px; min-width: 0; }}"
            f"QPushButton#panelSave:disabled {{ background: #2a2a2a; border-color: #2a2a2a; color: #666; }}")
        self.set_tracks([])

    # ------------------------------------------------------------------ loading
    def set_tracks(self, tracks: list[dict]) -> None:
        if tracks and [t.get("path") for t in tracks] == [t.get("path") for t in self.tracks] and self.dirty():
            self.tracks = tracks                     # same files, unsaved edits: keep them
            return
        self.tracks = tracks
        self.files = []
        self.cover_change = tagfile.KEEP
        self._gen += 1
        n = len(tracks)
        self.lyrics_button.setEnabled(n == 1)
        self._enable(False)
        self._clear()
        if not n:
            return
        if n > MAX_FILES:
            self._note_problem(tr("一次最多编辑 {0} 首", MAX_FILES))
            return
        gen, paths = self._gen, [t["path"] for t in tracks]

        def work():
            files = [tagfile.read(p) for p in paths]
            self._reader.done.emit(gen, files)
        QThreadPool.globalInstance().start(work)

    def reload(self) -> None:
        """Read the same files again (after they were written)."""
        tracks, self.tracks, self.files = self.tracks, [], []
        self.set_tracks(tracks)

    def _loaded(self, gen: int, files: list) -> None:
        if gen != self._gen:
            return
        self.files = files
        self._fill()

    def _fill(self) -> None:
        files = self.files
        self.cover_change = tagfile.KEEP
        for key, f in self.fields.items():
            f.load([x.fields.get(key, "") for x in files] if files else [])
        dirs = {str(Path(t["path"]).parent) for t in self.tracks}
        self.directory.setText(dirs.pop() if len(dirs) == 1 else KEEP)
        self.directory.setCursorPosition(0)
        self.directory.setToolTip(self.directory.text())
        self._enable(any(f.writable for f in files))
        self._show_cover()
        problems = [f for f in files if not f.writable]
        if problems:
            why = problems[0].error or tr("这种格式不能写标签")
            self._note_problem(tr("这个文件不能修改：{0}", why) if len(files) == 1 else
                               tr("{0} 首不能修改（{1}），保存时会跳过", len(problems), why))
        else:
            self._note_problem("")
        self._dirty()

    def _note_problem(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def _clear(self) -> None:
        for f in self.fields.values():
            f.load([])
        self.directory.clear()
        self.cover.clear()
        self.cover.setText("")
        self.cover_info.setText("")
        self._note_problem("")
        self._dirty()

    def _enable(self, on: bool) -> None:
        for f in self.fields.values():
            f.setEnabled(on)
        self.cover.setEnabled(on and any(f.covers for f in self.files))

    # ------------------------------------------------------------------ cover
    def _shown_cover(self):
        """(picture or None, mixed)."""
        if self.cover_change is not tagfile.KEEP:
            return self.cover_change, False
        covers = [f.cover for f in self.files]
        keys = {hashlib.sha1(c.data).hexdigest() if c else None for c in covers}
        if len(keys) > 1:
            return None, True
        return (covers[0] if covers else None), False

    def _show_cover(self) -> None:
        pic, mixed = self._shown_cover()
        if pic is not None:
            pix = QPixmap()
            pix.loadFromData(pic.data)
            if not pix.isNull():
                self.cover.setPixmap(pix.scaled(COVER - 2, COVER - 2, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.cover.setText("?")
            size = f"{pic.size / 1024:.0f} KB" if pic.size < 1 << 20 else f"{pic.size / (1 << 20):.1f} MB"
            lines = [pic.mime, f"{pic.width}×{pic.height}" if pic.width else "", size,
                     tr("正面封面") if pic.kind == 3 else tr("其他图片")]
            if self.cover_change is not tagfile.KEEP:
                lines.append(tr("（未保存）"))
            self.cover_info.setText("\n".join(x for x in lines if x))
        else:
            self.cover.clear()
            self.cover.setText(KEEP if mixed else tr("没有封面") if self.files else "")
            self.cover_info.setText(tr("（未保存）") if self.cover_change is None else "")
        self.cover.setToolTip(tr("右键：更换 / 删除 / 导出封面；双击更换") if self.cover.isEnabled() else "")

    def _cover_menu(self, pos) -> None:
        if not self.cover.isEnabled():
            return
        pic, _ = self._shown_cover()
        menu = QMenu(self)
        menu.addAction(tr("更换封面…"), self.choose_cover)
        a = menu.addAction(tr("删除封面"), self.remove_cover)
        a.setEnabled(pic is not None or any(f.pictures for f in self.files) and self.cover_change is not None)
        e = menu.addAction(tr("导出封面…"), self.export_cover)
        e.setEnabled(pic is not None)
        menu.exec(pos)

    def choose_cover(self, path: str | None = None) -> None:
        if not self.cover.isEnabled():
            return
        if not path:
            start = str(Path(self.tracks[0]["path"]).parent) if self.tracks else ""
            path, _ = QFileDialog.getOpenFileName(self, tr("选择封面图片"), start, tr("图片 (*.jpg *.jpeg *.png)"))
            if not path:
                return
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            self.message.emit(tr("读不了图片：{0}", exc))
            return
        pic = tagfile.picture(data)
        if pic.mime not in ("image/jpeg", "image/png") or not pic.width:
            self.message.emit(tr("只能用 JPEG 或 PNG 图片"))
            return
        if pic.size > tagfile.MAX_COVER_BYTES:
            self.message.emit(tr("图片太大（最多 16 MB）"))
            return
        self.cover_change = pic
        self._show_cover()
        self._dirty()

    def remove_cover(self) -> None:
        self.cover_change = None
        self._show_cover()
        self._dirty()

    def export_cover(self) -> None:
        pic, _ = self._shown_cover()
        if pic is None:
            return
        ext = "png" if pic.mime == "image/png" else "jpg"
        start = str(Path(self.tracks[0]["path"]).parent / f"cover.{ext}") if self.tracks else f"cover.{ext}"
        path, _ = QFileDialog.getSaveFileName(self, tr("导出封面"), start)
        if path:
            try:
                Path(path).write_bytes(pic.data)
            except OSError as exc:
                self.message.emit(tr("没能保存：{0}", exc))

    # ------------------------------------------------------------------ saving
    def changes(self) -> dict[str, str]:
        out = {}
        for key, f in self.fields.items():
            v = f.change()
            if v is not None:
                out[key] = v
        return out

    def dirty(self) -> bool:
        return bool(self.files) and bool(self.changes() or self.cover_change is not tagfile.KEEP)

    def _dirty(self, *_):
        self._actions()

    def _actions(self) -> None:
        """The tab row's right end holds the current tab's actions, always in the same place:
        放弃/保存 on 标签 (enabled while there are unsaved edits), 清屏/停止 on 指令 (停止 while running)."""
        tags = self.pages.currentIndex() == 0
        dirty = tags and self.dirty()
        for b in (self.save, self.discard):
            b.setVisible(tags)
            b.setEnabled(dirty)
        self.clear_log.setVisible(not tags)
        self.stop_run.setVisible(not tags)
        self.stop_run.setEnabled(self._running)

    def show_tab(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        (self.tags_tab if index == 0 else self.console_tab).setChecked(True)
        self._actions()
        if index == 1:
            self.console_page.input.setFocus()

    def _console_busy(self, on: bool) -> None:
        self._running = on
        self._actions()

    def _save(self) -> None:
        if not self.dirty() or not self.isVisibleTo(self.window()):
            return
        changes = self.changes()
        for key in ("tracknumber",):
            v = changes.get(key)
            if v and not NUMBER.match(v):
                self.message.emit(tr("{0}要写成数字，比如 3 或 3/12", LABELS[key]))
                return
        self.save.setEnabled(False)
        self.discard.setEnabled(False)
        self.save_requested.emit([t["path"] for t in self.tracks], changes, self.cover_change, None)


__all__ = ["Inspector", "LABELS", "KEEP", "BLANK"]
