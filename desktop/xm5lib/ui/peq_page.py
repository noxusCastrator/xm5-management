"""PEQ presets: import from Peace / APO text, edit, preview response, export firmware text."""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QGridLayout, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..core import peace, peq
from . import style
from .library_pages import header
from .widgets import GroupDelegate
from .i18n import tr

RATES = [44100, 48000, 96000, 192000]


class PeacePreview(QDialog):
    """Shows every saved Peace profile with its verdict; the user picks which to import."""

    def __init__(self, results: list[peace.PeaceResult], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("导入 Peace 配置"))
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        ok = sum(r.ok for r in results)
        lay.addWidget(QLabel(tr("读取了 {0} 个配置（只读）：{1} 个可以转换，{2} 个不行。不行的配置里有无法精确表示的设置；这里不做近似。", len(results), ok, len(results) - ok)))
        self.tree = QTreeWidget()
        self.tree.setObjectName("peaceTree")
        self.tree.setHeaderLabels([tr("配置"), tr("结果")])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.results = results
        for r in results:
            it = QTreeWidgetItem([r.name, tr("{0} 个频段，前级 {1:g} dB", len(r.preset.bands), r.preset.preamp) if r.ok
                                  else tr("不能转换")])
            it.setData(0, Qt.UserRole, r)
            if r.ok:
                it.setCheckState(0, Qt.Checked)
                it.setForeground(1, QColor(style.OK))
            else:
                it.setForeground(1, QColor(style.DANGER))
            for reason in r.reasons:
                QTreeWidgetItem(it, ["", "✗ " + reason]).setForeground(1, QColor(style.DANGER))
            for note in r.notes:
                QTreeWidgetItem(it, ["", "· " + note]).setForeground(1, QColor(style.MUTED))
            self.tree.addTopLevelItem(it)
            it.setExpanded(not r.ok)
        lay.addWidget(self.tree, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("导入勾选的"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("取消"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def chosen(self) -> list[peace.PeaceResult]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            r = it.data(0, Qt.UserRole)
            if r.ok and it.checkState(0) == Qt.Checked:
                out.append(r)
        return out


class PeqPage(QWidget):
    COLS = [tr("启用"), tr("类型"), tr("声道"), "Fc (Hz)", tr("增益 (dB)"), "Q"]

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.setObjectName("page")
        self.current: str | None = None
        self.loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        h, _, self.subtitle = header(tr("PEQ 预设"), tr("管理多个 PEQ 来源，保留原始配置；已转换的预设单独导出，可在电脑上试听。"))
        lay.addWidget(h)
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(24, 0, 8, 12)
        self.list = QListWidget()
        self.list.setObjectName("presetList")
        self.list.currentItemChanged.connect(self._select)
        ll.addWidget(self.list, 1)
        self.list.setItemDelegate(GroupDelegate(self.list, QPixmap(style.icon_path("home_playset")), 40))
        self.list.setMouseTracking(True)
        grid = QGridLayout()
        grid.setSpacing(6)
        for i, (label, fn, name) in enumerate([(tr("新建预设"), self.new_preset, "newPreset"),
                                               (tr("复制"), self.duplicate, "duplicatePreset"),
                                               (tr("导入可选 Peace 配置…"), self.import_peace, "importPeace"),
                                               (tr("导入 APO 文本…"), self.import_text, "importText"),
                                               (tr("打开 PEQ 编辑器"), ctl.open_peace, "openPeace"),
                                               (tr("删除"), self.delete, "deletePreset")]):
            b = QPushButton(label)
            b.setObjectName(name)
            if name == "deletePreset":
                b.setStyleSheet(f"color: {style.DANGER};")
            b.clicked.connect(fn)
            grid.addWidget(b, i // 2, i % 2)
        ll.addLayout(grid)
        ll.addWidget(QLabel(tr("PEQ 来源文件夹（只读）")))
        self.peace_sources = QListWidget()
        self.peace_sources.setObjectName("peqSources")
        self.peace_sources.setMaximumHeight(105)
        ll.addWidget(self.peace_sources)
        peace = QHBoxLayout()
        self.peace_dir = QLabel("")
        self.peace_dir.setObjectName("peaceDir")
        peace.addWidget(self.peace_dir, 1)
        for label, fn, name in [(tr("添加…"), self.choose_peace_dir, "addPeqSource"),
                                (tr("重新定位…"), self.relink_peace_dir, "relinkPeqSource"),
                                (tr("移除"), self.remove_peace_dir, "removePeqSource")]:
            pick = QPushButton(label)
            pick.setObjectName(name)
            pick.clicked.connect(fn)
            peace.addWidget(pick)
        ll.addLayout(peace)
        self._show_peace_dir()
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 24, 12)
        row = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setObjectName("presetName")
        self.name.setPlaceholderText(tr("预设名称"))
        self.preamp = QLineEdit()
        self.preamp.setObjectName("preamp")
        self.preamp.setMaximumWidth(90)
        self.rate = QComboBox()
        self.rate.addItems([f"{r / 1000:g} kHz" for r in RATES])
        self.rate.setCurrentIndex(1)
        row.addWidget(QLabel(tr("名称")))
        row.addWidget(self.name, 1)
        row.addWidget(QLabel(tr("前级 dB")))
        row.addWidget(self.preamp)
        row.addWidget(QLabel(tr("曲线采样率")))
        row.addWidget(self.rate)
        rl.addLayout(row)
        from .widgets import ResponsePlot
        self.plot = ResponsePlot()
        self.plot.setObjectName("responsePlot")
        rl.addWidget(self.plot, 2)
        self.status = QLabel()
        self.status.setObjectName("peqStatus")
        self.status.setWordWrap(True)
        rl.addWidget(self.status)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setObjectName("bandTable")
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.itemChanged.connect(self._edited)
        rl.addWidget(self.table, 2)
        brow = QHBoxLayout()
        for label, fn, name in [(tr("添加频段"), self.add_band, "addBand"), (tr("删除频段"), self.remove_band, "removeBand")]:
            b = QPushButton(label)
            b.setObjectName(name)
            b.clicked.connect(fn)
            brow.addWidget(b)
        brow.addStretch(1)
        self.save_btn = QPushButton(tr("保存预设"))
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        self.export_btn = QPushButton(tr("导出 .txt…"))
        self.export_btn.setObjectName("exportTxt")
        self.export_btn.clicked.connect(self.export_txt)
        copy = QPushButton(tr("复制文本"))
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text.toPlainText()))
        self.listen = QPushButton(tr("电脑试听"))
        self.listen.setObjectName("peqListen")
        self.listen.setCheckable(True)
        self.listen.toggled.connect(self._listen)
        for b in (self.listen, copy, self.export_btn, self.save_btn):
            brow.addWidget(b)
        rl.addLayout(brow)
        self.text = QPlainTextEdit()
        self.text.setObjectName("apoText")
        self.text.setReadOnly(True)
        self.text.setMaximumHeight(150)
        self.text.setStyleSheet("font-family: Consolas, monospace; font-size: 9pt;")
        rl.addWidget(self.text, 1)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 900])
        lay.addWidget(split, 1)
        self.name.textEdited.connect(self._edited)
        self.preamp.textEdited.connect(self._edited)
        self.rate.currentIndexChanged.connect(self._edited)
        ctl.state.presetsChanged.connect(lambda: self.reload())
        engine = ctl.player.backend
        if hasattr(engine, "peqChanged"):
            engine.peqChanged.connect(self._listen_state)
        self.reload()

    # ---------------------------------------------------------------- list
    def reload(self, select: str | None = None) -> None:
        select = select or self.current
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.ctl.state.presets:
            it = QListWidgetItem(p["name"])
            it.setData(Qt.UserRole, p["name"])
            it.setToolTip(p.get("source") or "created in XM5 Library")
            self.list.addItem(it)
            if p["name"] == select:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self._select(self.list.currentItem())
        self.subtitle.setText(tr("{0} 个预设 · 保存在曲库里，原始来源文件不变", self.list.count()))
        self._listen_state()

    def _select(self, item, *_):
        if item is None:
            self.current = None
            self.load(peq.Preset("New preset"))
            return
        name = item.data(Qt.UserRole)
        d = self.ctl.state.preset(name)
        self.current = name
        if d:
            try:
                self.load(peq.Preset.from_dict(d))
            except (KeyError, TypeError, ValueError) as exc:
                self.load(peq.Preset(name))
                self.status.setText(tr("✗ 这个预设的数据无法读取（{0}）", exc))
                self.status.setStyleSheet(f"color: {style.DANGER};")
        self._listen_state()

    # ---------------------------------------------------------------- editor
    def load(self, p: peq.Preset) -> None:
        self.loading = True
        self.source, self.source_sha = p.source, p.source_sha256
        self.notes = p.notes
        self.name.setText(p.name)
        self.preamp.setText(peq.fmt(p.preamp))
        self.table.setRowCount(0)
        for b in p.bands:
            self._append(b)
        self.loading = False
        self._edited()

    def _append(self, b: peq.Band) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        on = QTableWidgetItem()
        on.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        on.setCheckState(Qt.Checked if b.enabled else Qt.Unchecked)
        self.table.setItem(r, 0, on)
        for col, options, value in ((1, peq.TYPES, b.type), (2, peq.CHANNELS, b.channel)):
            cb = QComboBox()
            cb.addItems(options)
            cb.setCurrentText(value)
            cb.currentIndexChanged.connect(self._edited)
            self.table.setCellWidget(r, col, cb)
        for col, v, freq in ((3, b.fc, True), (4, b.gain, False), (5, b.q, False)):
            self.table.setItem(r, col, QTableWidgetItem(peq.fmt(v, freq)))

    def preset(self) -> tuple[peq.Preset, list[str]]:
        errors = []

        def num(text, label):
            try:
                v = float(text.strip().replace(",", "."))
                if not math.isfinite(v):
                    raise ValueError
                return v
            except ValueError:
                errors.append(tr("{0}：「{1}」不是数字", label, text))
                return float("nan")

        p = peq.Preset(self.name.text().strip(), num(self.preamp.text(), tr("前级")), notes=self.notes,
                       source=self.source, source_sha256=self.source_sha)
        for r in range(self.table.rowCount()):
            p.bands.append(peq.Band(
                self.table.cellWidget(r, 1).currentText(),
                num(self.table.item(r, 3).text(), tr("频段 {0} Fc", r + 1)),
                num(self.table.item(r, 4).text(), tr("频段 {0} 增益", r + 1)),
                num(self.table.item(r, 5).text(), tr("频段 {0} Q", r + 1)),
                self.table.item(r, 0).checkState() == Qt.Checked,
                self.table.cellWidget(r, 2).currentText()))
        return p, errors

    def _edited(self, *_):
        if self.loading:
            return
        p, errors = self.preset()
        errors += [e for e in peq.validate(p) if "not a number" not in e and "nan" not in e]
        rate = RATES[self.rate.currentIndex()]
        if errors:
            self.plot.set_preset(None)
            self.text.setPlainText("")
            self.status.setText("✗ " + "\n✗ ".join(errors))
            self.status.setStyleSheet(f"color: {style.DANGER};")
            self.save_btn.setEnabled(False)
            self.export_btn.setEnabled(False)
            return
        self.plot.set_preset(p, rate)
        try:
            text = peq.to_apo_text(p)
            peq.parse_apo_text(text, p.name)
        except ValueError as exc:
            self.status.setText(f"✗ {exc}")
            self.status.setStyleSheet(f"color: {style.DANGER};")
            self.save_btn.setEnabled(False)
            self.export_btn.setEnabled(False)
            return
        self.text.setPlainText(text)
        peak, f = peq.headroom(p, rate) if not self.plot.error else (0, 0)
        msg = tr("✓ 固件文本有效 · 启用 {0}/{1} 个频段 · {2:g} kHz 下约 {3:,.0f} Hz 处峰值 {4:+.1f} dB（估算，不保证不削波）", len(p.active()), peq.MAX_BANDS, rate / 1000, f, peak)
        if self.plot.error:
            msg = f"⚠ {self.plot.error}"
        self.status.setText(msg + (tr("\n来源：{0}", self.source) if self.source else ""))
        self.status.setStyleSheet(f"color: {style.OK if not self.plot.error else style.ACCENT};")
        self.save_btn.setEnabled(True)
        self.export_btn.setEnabled(True)

    def add_band(self):
        self._append(peq.Band())
        self._edited()

    def remove_band(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._edited()

    # ---------------------------------------------------------------- actions
    def save(self):
        p, errors = self.preset()
        if errors or peq.validate(p):
            return
        if p.name != self.current and self.ctl.state.preset(p.name) is not None:
            if not self.ctl.quiet and not self.ctl.ask(tr("替换预设"), tr("替换已有的预设「{0}」？", p.name), tr("替换"), tr("取消")):
                return
        old = self.current
        self.current = p.name

        def saved():
            self.reload(p.name)
            self.ctl.toast(tr("已保存预设「{0}」", p.name))
        self.ctl.state.save_preset(p.to_dict(), ok=saved, replace_name=old)

    def _unique(self, name: str) -> str:
        names = {p["name"] for p in self.ctl.state.presets}
        cand, n = name, 2
        while cand in names:
            cand = f"{name} ({n})"
            n += 1
        return cand

    def new_preset(self):
        p = peq.Preset(self._unique("New preset"), 0.0, [peq.Band("PK", 1000, 0, 1)])
        self.ctl.state.save_preset(p.to_dict(), ok=lambda: self.reload(p.name))

    def duplicate(self):
        p, errors = self.preset()
        if errors:
            return
        p.name = self._unique(p.name)
        self.ctl.state.save_preset(p.to_dict(), ok=lambda: self.reload(p.name))

    def delete(self):
        if self.current and (self.ctl.quiet or self.ctl.ask(tr("删除预设"), tr("删除预设「{0}」？Peace 的文件不受影响；删除后可以按 Ctrl+Z 撤销。", self.current), tr("删除"), tr("取消"))):
            name, self.current = self.current, None
            self.ctl.state.delete_preset(name, ok=lambda: self.reload())

    # ---------------------------------------------------------------- desktop preview through the backend player
    def _engine(self):
        engine = self.ctl.player.backend
        return engine if hasattr(engine, "set_peq") else None

    def _listen_state(self) -> None:
        engine = self._engine()
        self.listen.blockSignals(True)
        if engine is None:
            self.listen.setEnabled(False)
            self.listen.setChecked(False)
            self.listen.setToolTip(tr("电脑端 PEQ 试听需要后端播放器（mpv）"))
        else:
            self.listen.setEnabled(bool(self.current))
            self.listen.setChecked(bool(self.current) and engine.peq_name == self.current)
            self.listen.setToolTip(tr("把这个已保存的预设用到电脑播放上（只影响电脑试听，不会写进 XM5）")
                                   + (tr("\n现在：{0}", engine.peq_name) if engine.peq_name else ""))
        self.listen.blockSignals(False)

    def _listen(self, on: bool) -> None:
        engine = self._engine()
        if engine is None or not self.current:
            return
        name = self.current if on else None
        engine.set_peq(name, ok=lambda: self.ctl.toast(tr("电脑播放已使用 PEQ「{0}」", name) if name else tr("电脑播放已关闭 PEQ")),
                       err=lambda e: (self.ctl.toast_error(tr("PEQ 没有应用：{0}", e.message)), self._listen_state()))

    def _show_peace_dir(self) -> None:
        selected = {self.peace_sources.item(i).data(Qt.UserRole) for i in range(self.peace_sources.count())
                    if self.peace_sources.item(i).checkState() == Qt.Checked}
        initialized = getattr(self, "_sources_initialized", False)
        current = self.peace_sources.currentItem()
        current_id = current.data(Qt.UserRole) if current else None
        self.peace_sources.clear()
        sources = self.ctl.prefs.get("peq_sources")
        for source in sources:
            available = Path(source["path"]).is_dir()
            item = QListWidgetItem(source["path"] + ("" if available else tr("  · 找不到这个文件夹")))
            item.setData(Qt.UserRole, source["id"])
            item.setToolTip(source["path"])
            item.setCheckState(Qt.Checked if not initialized or source["id"] in selected else Qt.Unchecked)
            if not available:
                item.setForeground(QColor(style.DANGER))
            self.peace_sources.addItem(item)
            if source["id"] == current_id:
                self.peace_sources.setCurrentItem(item)
        self._sources_initialized = True
        self.peace_dir.setText(tr("{0} 个来源", len(sources)))

    def _sources_changed(self):
        self._show_peace_dir()
        if hasattr(self.ctl, "pages"):
            self.ctl.pages["export"].refresh_sources()
            self.ctl.restart_backend()

    def choose_peace_dir(self) -> None:
        roots = self.ctl.prefs.get("peq_roots") or []
        d = QFileDialog.getExistingDirectory(self, tr("选择 Peace / Equalizer APO 的配置文件夹"),
                                             roots[-1] if roots else "")
        if d:
            source_id = self.ctl.prefs.register_source("peq", d)
            self._sources_changed()
            for i in range(self.peace_sources.count()):
                if self.peace_sources.item(i).data(Qt.UserRole) == source_id:
                    self.peace_sources.item(i).setCheckState(Qt.Checked)

    def relink_peace_dir(self) -> None:
        item = self.peace_sources.currentItem()
        if item is None:
            return
        d = QFileDialog.getExistingDirectory(self, tr("重新定位 PEQ 来源文件夹"), item.toolTip())
        if d:
            self.ctl.prefs.relink_source("peq", item.data(Qt.UserRole), d)
            self._sources_changed()

    def remove_peace_dir(self) -> None:
        item = self.peace_sources.currentItem()
        if item is not None:
            self.ctl.prefs.remove_source("peq", item.data(Qt.UserRole))
            self._sources_changed()

    def import_peace(self, folder: str | None = None, auto: bool = False):
        if isinstance(folder, bool):                  # clicked(bool) is not a path
            folder = None
        if folder:
            folders = [str(folder)]
        else:
            selected = {self.peace_sources.item(i).data(Qt.UserRole) for i in range(self.peace_sources.count())
                        if self.peace_sources.item(i).checkState() == Qt.Checked}
            folders = [s["path"] for s in self.ctl.prefs.get("peq_sources") if s["id"] in selected]
        if not folders:
            self.ctl.error(tr("先添加并勾选一个 PEQ 来源文件夹。"))
            return None
        results = []
        for source in folders:
            if not Path(source).is_dir():
                self.ctl.error(tr("这些来源找不到，请重新定位或取消勾选：\n{0}", source))
                return None
            try:
                results.extend(peace.read_folder(Path(source)))
            except OSError as exc:
                self.ctl.error(tr("读不了 {0} 里的 Peace 配置：{1}", source, exc))
                return None
        dlg = PeacePreview(results, self)
        if auto:
            return dlg
        if dlg.exec() == QDialog.Accepted:
            self.apply_peace(dlg.chosen())
        return None

    def apply_peace(self, chosen: list[peace.PeaceResult]) -> None:
        taken = {p["name"] for p in self.ctl.state.presets}
        for r in chosen:
            p = r.preset
            base, n = p.name, 2
            while p.name in taken:
                p.name = f"{base} ({n})"
                n += 1
            taken.add(p.name)
            self.ctl.state.save_preset(p.to_dict())
        if chosen:
            self.ctl.state.refresh_presets(lambda: self.reload(chosen[0].preset.name))
        self.ctl.toast(tr("已把 {0} 个 Peace 配置导入为预设", len(chosen)))

    def import_text(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("导入 Equalizer APO 文本"), "", tr("文本预设 (*.txt)"))
        if path:
            self.import_text_file(Path(path))

    def import_text_file(self, path: Path):
        """Strict: the backend accepts only text the XM5 PEQ supports in full (nothing is dropped silently)."""
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError:
            self.ctl.error(tr("{0}：不是 UTF-8 文本（这里不支持 UTF-16 和 .peace 文件）", path.name))
            return
        except OSError as exc:
            self.ctl.error(tr("读不了 {0}：{1}", path.name, exc))
            return
        name = self._unique(path.stem)

        def done(_r):
            self.ctl.state.refresh_presets(lambda: self.reload(name))
            self.ctl.toast(tr("已导入预设「{0}」", name))
        self.ctl.backend.command("presets.import", {"name": name, "text": text}, ok=done,
                                 err=lambda e: self.ctl.error(tr("{0} 没有导入：{1}", path.name, e.message)))

    def export_txt(self):
        p, errors = self.preset()
        if errors:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("导出固件 PEQ 文本"), peq.safe_filename(p.name), tr("文本 (*.txt)"))
        if path:
            target = Path(path)
            try:
                with open(target, "x", encoding="utf-8", newline="\n") as f:
                    f.write(peq.to_apo_text(p))
            except FileExistsError:
                self.ctl.error(tr("{0} 已经存在；换个文件名吧（什么都没有覆盖）。", target))
                return
            self.ctl.toast(tr("已写出 {0}", target))

    def rename_current(self):
        name, ok = QInputDialog.getText(self, tr("重命名预设"), tr("名称"), text=self.current or "")
        if ok and name.strip():
            self.name.setText(name.strip())
            self.save()
