"""Card export: explicit source selection, pinned preview and cancellable execution.

The chosen parent contains exactly xm5; backend ownership/conflict checks govern
all writes. Return and recovery use the versioned card-layout helper.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QScrollArea, QFrame)

from . import style
from .library_pages import header
from .i18n import tr

PHASES = {"copy": tr("复制原文件"), "art": tr("生成封面小图"), "playlists": tr("写歌单"), "presets": tr("写 EQ 预设"),
          "manifest": tr("写清单"), "verify": tr("校验")}
RETURN_STATUS = {"unchanged": (tr("没有变化"), style.MUTED), "pc_only": (tr("只在电脑上改过，卡上是旧的（不用带回）"), style.MUTED),
                 "changed": (tr("在 XM5 上改过，可以带回"), style.ACCENT), "new_playlist": (tr("XM5 上新建的歌单"), style.ACCENT),
                 "conflict": (tr("电脑和 XM5 上都改过，需要你选"), style.WARN),
                 "unresolved": (tr("有条目对不上卡上的文件，不能带回"), style.DANGER)}
RETURN_STATUS.update({"missing_playlist": (tr("卡上没有此歌单，需要选择是否删除电脑版本"), style.WARN),
                      "already_absent": (tr("电脑和卡上都没有此歌单"), style.MUTED)})


def gb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1e9:.2f} GB" if n >= 1e8 else f"{n / 1e6:.1f} MB"


def safe_preset(name: str) -> bool:
    return name.isascii() and not name.startswith(".") and not any(c in name for c in '\\/:*?"<>|') and \
        len((name if name.lower().endswith(".txt") else name + ".txt").encode()) <= 63


def job_error(job: dict) -> str:
    err = job.get("error") or (job.get("result") or {}).get("error") or job.get("status")
    if isinstance(err, dict):
        return tr("{0}（{1}）", err.get('message'), err.get('code'))
    return str(err)


class ExportPage(QWidget):
    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.plan: dict | None = None
        self.watch = None
        self.result_root: str | None = None
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        h, _, subtitle = header(tr("TF 卡"), tr("选择来源和上级位置，在其中建立 xm5 文件夹；预览通过后才写入。原始来源保持不变，卡上改过的歌单可以带回。"))
        subtitle.setWordWrap(True)
        subtitle.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(h)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("cardTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._export_tab(), tr("导出到 TF 卡"))
        self.tabs.addTab(self._return_tab(), tr("从 TF 卡带回 / 恢复"))
        self.tabs.addTab(self._organize_tab(), tr("卡上整理"))
        wrap = QVBoxLayout()
        wrap.setContentsMargins(24, 0, 24, 16)
        wrap.addWidget(self.tabs, 1)
        lay.addLayout(wrap, 1)

    # ================================================================== export
    def _export_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("page")
        body = QHBoxLayout(tab)
        body.setContentsMargins(0, 10, 0, 0)
        left = QVBoxLayout()
        dest = QGroupBox(tr("目标位置"))
        df = QFormLayout(dest)
        self.parent_edit = QLineEdit()
        self.parent_edit.setObjectName("exportParent")
        browse = QPushButton(tr("浏览…"))
        browse.clicked.connect(self._browse)
        prow = QHBoxLayout()
        prow.addWidget(self.parent_edit, 1)
        prow.addWidget(browse)
        df.addRow(tr("上级文件夹"), prow)
        self.name_edit = QLineEdit("xm5")
        self.name_edit.setReadOnly(True)
        self.name_edit.setObjectName("exportName")
        df.addRow(tr("应用文件夹"), self.name_edit)
        left.addWidget(dest)

        src = QGroupBox(tr("内容"))
        sl = QVBoxLayout(src)
        self.music = QLabel("")
        self.music.setWordWrap(True)
        sl.addWidget(self.music)
        self.music_sources = QListWidget()
        self.music_sources.setObjectName("exportMusicSources")
        self.music_sources.setMaximumHeight(85)
        sl.addWidget(self.music_sources)
        sl.addWidget(QLabel(tr("PEQ 来源原文件（勾选的按原结构复制）：")))
        self.peq_sources = QListWidget()
        self.peq_sources.setObjectName("exportPeqSources")
        self.peq_sources.setMaximumHeight(80)
        sl.addWidget(self.peq_sources)
        self.auto_lyrics = QCheckBox(tr("自动查找缺失歌词（会向歌词服务发送所选歌曲信息）"))
        self.auto_lyrics.setObjectName("exportAutoLyrics")
        self.auto_lyrics.setChecked(bool(self.ctl.settings_value("lyrics.auto_search")))
        self.auto_lyrics.setChecked(False)
        self.auto_lyrics.setEnabled(False)
        self.auto_lyrics.setToolTip(tr("导出只使用已准备的本地歌词。在线搜索请在歌词管理中执行。"))
        sl.addWidget(self.auto_lyrics)
        sl.addWidget(QLabel(tr("歌单（勾选的写成 XM5 原厂可导入的歌单）：")))
        self.lists = QListWidget()
        self.lists.setObjectName("exportPlaylists")
        self.lists.setMaximumHeight(130)
        sl.addWidget(self.lists)
        sl.addWidget(QLabel(tr("单独生成的 PEQ 预设：")))
        self.presets = QListWidget()
        self.presets.setObjectName("exportPresets")
        self.presets.setMaximumHeight(110)
        sl.addWidget(self.presets)
        left.addWidget(src)

        gain = QGroupBox(tr("原曲峰值补正"))
        gf = QFormLayout(gain)
        self.peak_enabled = QCheckBox(tr("启用峰值补正"))
        self.peak_enabled.setChecked(self.ctl.prefs.get("peak_correction_enabled", True))
        self.midpoint = QDoubleSpinBox()
        self.midpoint.setObjectName("exportGainMidpoint")
        self.midpoint.setRange(-60, 0)
        self.midpoint.setSuffix(" dBFS")
        self.midpoint.setValue(self.ctl.prefs.get("midpoint_dbfs", 0.0))
        self.gain_limit = QDoubleSpinBox()
        self.gain_limit.setObjectName("exportGainLimit")
        self.gain_limit.setRange(0, 12)
        self.gain_limit.setSuffix(" dB")
        self.gain_limit.setValue(self.ctl.prefs.get("correction_limit_db", 4.0))
        gf.addRow(self.peak_enabled)
        gf.addRow(tr("中点"), self.midpoint)
        gf.addRow(tr("最大补正"), self.gain_limit)
        for control in (self.peak_enabled, self.midpoint, self.gain_limit):
            (control.toggled if isinstance(control, QCheckBox) else control.valueChanged).connect(self._invalidate)
        left.addWidget(gain)

        what = QGroupBox(tr("卡上会有什么"))
        wl = QVBoxLayout(what)
        info = QLabel(tr("音乐和 EQ 保留原目录。只在卡上副本中加入 XM5 小图、歌词与峰值信息，音频和原图保留。Struct 保存可重建索引、相对路径歌单、最爱和歌单封面。更新不删除未选文件；旧树保留为备份。"))
        info.setWordWrap(True)
        info.setProperty("muted", True)
        wl.addWidget(info)
        left.addWidget(what)
        left.addStretch(1)
        left_panel = QWidget()
        left_panel.setLayout(left)
        source_scroll = QScrollArea()
        source_scroll.setWidgetResizable(True)
        source_scroll.setFrameShape(QFrame.NoFrame)
        source_scroll.setWidget(left_panel)
        body.addWidget(source_scroll, 1)

        right = QVBoxLayout()
        self.summary = QLabel(tr("先预览方案，查看大小、剩余空间和潜在问题。"))
        self.summary.setObjectName("exportSummary")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-size: 11pt;")
        self.summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        right.addWidget(self.summary)
        self.details = QPlainTextEdit()
        self.details.setObjectName("exportDetails")
        self.details.setReadOnly(True)
        right.addWidget(self.details, 1)
        self.progress = QProgressBar()
        self.progress.setObjectName("exportProgress")
        self.progress.setValue(0)
        right.addWidget(self.progress)
        self.progress_label = QLabel("")
        self.progress_label.setProperty("muted", True)
        right.addWidget(self.progress_label)
        brow = QHBoxLayout()
        self.preview_btn = QPushButton(tr("预览方案"))
        self.preview_btn.setObjectName("previewPlan")
        self.preview_btn.clicked.connect(self.preview)
        self.run_btn = QPushButton(tr("导出"))
        self.run_btn.setObjectName("primary")
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self.run)
        self.cancel_btn = QPushButton(tr("取消"))
        self.cancel_btn.setObjectName("cancelExport")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        self.open_btn = QPushButton(tr("打开文件夹"))
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(lambda: self.result_root and _open(self.result_root))
        for b in (self.preview_btn, self.run_btn, self.cancel_btn, self.open_btn):
            brow.addWidget(b)
        right.addLayout(brow)
        body.addLayout(right, 1)
        self.name_edit.textEdited.connect(self._invalidate)
        self.parent_edit.textEdited.connect(self._invalidate)
        self.lists.itemChanged.connect(self._invalidate)
        self.presets.itemChanged.connect(self._invalidate)
        self.music_sources.itemChanged.connect(self._invalidate)
        self.peq_sources.itemChanged.connect(self._invalidate)
        self.auto_lyrics.toggled.connect(self._invalidate)
        self.ctl.state.playlistsChanged.connect(self._library_changed)
        self.ctl.state.tracksChanged.connect(self._library_changed)
        return tab

    def refresh_sources(self) -> None:
        self._invalidate()
        checked_l = self._checked(self.lists)
        checked_p = self._checked(self.presets)
        self._fill_sources(self.music_sources, "music")
        self._fill_sources(self.peq_sources, "peq")
        self.music.setText(tr("音乐来源（勾选的按原结构复制）：") + tr("\n曲库里 {0} 首", len(self.ctl.view.tracks)))
        self.lists.blockSignals(True)
        self.lists.clear()
        for pl in self.ctl.state.playlist_list():
            _rows, missing = self.ctl.state.playlist_rows(pl["id"])
            it = QListWidgetItem(f"{pl['name']}  ({pl['count']})" + (tr("  · 有缺失条目") if missing else ""))
            it.setData(Qt.UserRole, pl["id"])
            it.setCheckState(Qt.Checked if pl["id"] in checked_l else Qt.Unchecked)
            if missing:
                it.setForeground(QColor(style.DANGER))
                it.setToolTip(tr("Card-only tracks keep their playlist paths. Their audio is not copied."))
            self.lists.addItem(it)
        self.lists.blockSignals(False)
        self.presets.blockSignals(True)
        self.presets.clear()
        for p in self.ctl.state.presets:
            it = QListWidgetItem(p["name"])
            it.setData(Qt.UserRole, p["name"])
            if safe_preset(p["name"]):
                it.setCheckState(Qt.Checked if p["name"] in checked_p else Qt.Unchecked)
            else:
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
                it.setToolTip(tr("预设名要用英文字母、数字（卡上的文件名限制）；在 PEQ 页面改个名字就能导出"))
            self.presets.addItem(it)
        self.presets.blockSignals(False)
        if not self.parent_edit.text():
            self.parent_edit.setText(self.ctl.settings_value("export_parent") or "")

    def _fill_sources(self, widget: QListWidget, kind: str) -> None:
        initialized = bool(widget.property("sourcesInitialized"))
        known = {widget.item(i).data(Qt.UserRole) for i in range(widget.count())}
        selected = self._checked(widget) if initialized else self.ctl.settings_value("export_" + kind + "_sources")
        widget.blockSignals(True)
        widget.clear()
        for source in self.ctl.source_records(kind):
            available = Path(source["path"]).is_dir()
            item = QListWidgetItem(source["path"] + ("" if available else tr("  · 找不到这个文件夹")))
            item.setData(Qt.UserRole, source["id"])
            item.setData(Qt.UserRole + 1, source["path"])
            item.setData(Qt.UserRole + 2, source["label"])
            item.setCheckState(Qt.Checked if selected is None or source["id"] in selected or
                               (initialized and source["id"] not in known) else Qt.Unchecked)
            if not available:
                item.setForeground(QColor(style.DANGER))
            widget.addItem(item)
        widget.setProperty("sourcesInitialized", True)
        widget.blockSignals(False)

    @staticmethod
    def _selected_sources(widget: QListWidget) -> dict[str, str]:
        return {str(Path(widget.item(i).data(Qt.UserRole + 1)).resolve()): widget.item(i).data(Qt.UserRole)
                for i in range(widget.count()) if widget.item(i).checkState() == Qt.Checked}

    @staticmethod
    def _selected_labels(widget: QListWidget) -> dict[str, str]:
        return {str(Path(widget.item(i).data(Qt.UserRole + 1)).resolve()): widget.item(i).data(Qt.UserRole + 2)
                for i in range(widget.count()) if widget.item(i).checkState() == Qt.Checked}

    @staticmethod
    def _checked(lst: QListWidget) -> set:
        return {lst.item(i).data(Qt.UserRole) for i in range(lst.count())
                if lst.item(i).flags() & Qt.ItemIsEnabled and lst.item(i).checkState() == Qt.Checked}

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, tr("选择 xm5 文件夹的上级位置"), self.parent_edit.text())
        if d:
            self.parent_edit.setText(d)
            self._invalidate()

    def _library_changed(self) -> None:
        if self.plan is not None and not self.running():
            self._invalidate()
            self.summary.setText(tr("曲库或歌单在预览之后有变化，请重新预览。"))
            self.summary.setStyleSheet(f"font-size: 11pt; color: {style.WARN};")

    def _invalidate(self, *_):
        if not self.running():
            self.run_btn.setEnabled(False)
            self.plan = None

    def running(self) -> bool:
        return self.watch is not None

    def _busy(self, on: bool) -> None:
        self.preview_btn.setEnabled(not on)
        self.cancel_btn.setEnabled(on)
        if on:
            self.run_btn.setEnabled(False)
            self.open_btn.setEnabled(False)

    def preview(self) -> None:
        parent = self.parent_edit.text().strip()
        from ..core.card_layout import destination_for
        music_sources = self._selected_sources(self.music_sources)
        peq_sources = self._selected_sources(self.peq_sources)
        roots = list(music_sources)
        if not roots:
            self.ctl.error(tr("先勾选一个音乐来源文件夹。"))
            return
        missing = [r for r in roots + list(peq_sources) if not Path(r).is_dir()]
        if missing:
            self.ctl.error(tr("这些来源找不到，请重新定位或取消勾选：\n{0}", "\n".join(missing)))
            return
        if not parent or not Path(parent).is_dir():
            self.ctl.error(tr("先选一个存在的上级文件夹。"))
            return
        destination = destination_for(Path(parent).resolve())
        lists = sorted(self._checked(self.lists), key=lambda pid: (self.ctl.state.playlist(pid) or {}).get("name", ""))
        broken = []
        for pid in lists:
            playlist = self.ctl.state.playlist(pid)
            if playlist:
                rows, missing = self.ctl.state.playlist_rows(pid)
                if any(row["path"] in missing and not row.get("card_reference") for row in rows):
                    broken.append(playlist["name"])
        if broken:
            self.ctl.error(tr("这些歌单里有不在曲库里的歌（标红的条目），卡上的歌单找不到它们：\n  ")
                           + "\n  ".join(broken) + tr("\n\n先在歌单里移除红色条目，或者这次不勾选它们。"))
            return
        presets = sorted(self._checked(self.presets))
        self.ctl.set_setting("export_parent", parent)
        self.ctl.set_setting("export_music_sources", list(music_sources.values()))
        self.ctl.set_setting("export_peq_sources", list(peq_sources.values()))
        self.ctl.set_setting("lyrics.auto_search", self.auto_lyrics.isChecked())
        args = {"format": "XM5I", "roots": roots, "peq_roots": list(peq_sources), "source_ids": music_sources,
                "source_labels": self._selected_labels(self.music_sources),
                "peq_source_labels": self._selected_labels(self.peq_sources),
                "peq_source_ids": peq_sources, "destination": str(destination), "playlist_ids": lists,
                "preset_names": presets, "lyrics_auto_search": self.auto_lyrics.isChecked(),
                "existing": "update" if destination.exists() else "refuse"}
        args.update(midpoint_dbfs=self.midpoint.value(), correction_limit_db=self.gain_limit.value(),
                    peak_correction_enabled=self.peak_enabled.isChecked())
        for key in ("midpoint_dbfs", "correction_limit_db", "peak_correction_enabled"):
            self.ctl.prefs.set(key, args[key])
        self.summary.setText(tr("正在生成方案（会读取并校验每个文件，可能要一会儿）…"))
        self.summary.setStyleSheet("font-size: 11pt;")
        self.details.setPlainText("")
        self._busy(True)

        def planned(job):
            self.watch = None
            self._busy(False)
            self.show_plan(job.get("result") or {}, parent)

        def accepted(r):
            self.watch = self.ctl.backend.watch(r["job_id"], done=planned,
                                                failed=lambda job: self._plan_failed(job_error(job)))

        def start():
            self.ctl.backend.command("cards.plan", args, lane="jobs", ok=accepted,
                                     err=lambda e: self._plan_failed(tr("{0}（{1}）", e.message, e.code)))
        self.ctl.ensure_roots(export=[parent], then=start)

    def _plan_failed(self, why: str) -> None:
        self.watch = None
        self._busy(False)
        self.plan = None
        self.summary.setText(tr("✗ 没能生成方案"))
        self.summary.setStyleSheet(f"font-size: 11pt; color: {style.DANGER};")
        self.details.setPlainText(why)

    def show_plan(self, res: dict, parent: str) -> None:
        plan = res.get("plan") or {}
        self.plan = res
        s = plan.get("summary") or {}
        try:
            free = shutil.disk_usage(parent).free
        except OSError:
            free = None
        need = s.get("estimated_bytes")                 # the backend's complete estimate (originals + art + index)
        if need is None:
            need = (s.get("original_bytes") or 0) + len(plan.get("artworks") or {}) * 873_856
        ok = free is None or free > need + 50_000_000
        lines = [tr("文件夹：{0}", plan.get('destination')),
                 tr("歌曲：{0} 首 · 原文件 {1} 个", s.get('tracks', 0), s.get('original_files', 0)),
                 tr("大小：约 {0} · 上级文件夹剩余 {1}", gb(need), gb(free)),
                 tr("封面小图：{0} 张 · 歌单：{1} · EQ 预设：{2}", s.get('distinct_artworks', 0), s.get('playlists', 0), len(plan.get('presets') or []))]
        self.summary.setText((tr("✓ 可以导出\n") if ok else tr("✗ 空间不够\n")) + "\n".join(lines))
        self.summary.setStyleSheet(f"font-size: 11pt; color: {style.OK if ok else style.DANGER};")
        detail = []
        warnings = plan.get("warnings") or []
        if warnings:
            detail += [tr("提醒")] + [f"  {w.get('message', w) if isinstance(w, dict) else w}" for w in warnings[:50]] + [""]
        roots = plan.get("roots") or []
        if roots:
            detail += [tr("文件夹对应")] + [f"  {r['source']}  →  {r['label']}/" for r in roots] + [""]
        if plan.get("playlists"):
            detail += [tr("歌单")] + [tr("  {0}  ({1} 首)", p['path'], len(p['tracks'])) for p in plan["playlists"]] + [""]
        if plan.get("presets"):
            presets = plan["presets"]
            detail += ["EQ"] + ([f"  {name}" for name in presets] if isinstance(presets, dict)
                                  else [f"  {p['path']}" for p in presets]) + [""]
        files = plan.get("files") or []
        detail += [tr("文件（前 200 个，共 {0} 个）", len(files))] + [f"  {f['path']}" for f in files[:200]]
        self.details.setPlainText("\n".join(detail))
        self.run_btn.setEnabled(ok)
        self.progress.setValue(0)
        self.progress_label.setText("")

    def run(self) -> None:
        if self.plan is None:
            return
        res = self.plan
        self._busy(True)
        self.summary.setText(tr("正在导出…"))
        self.summary.setStyleSheet("font-size: 11pt;")

        def progress(job):
            p = job.get("progress") or {}
            if isinstance(p, dict) and p.get("total"):
                self.progress.setMaximum(int(p["total"]))
                self.progress.setValue(int(p.get("current") or 0))
                where = Path(str(p.get("path") or "")).name
                self.progress_label.setText(f"{PHASES.get(p.get('phase'), p.get('phase') or '')}  "
                                            f"{p.get('current')}/{p.get('total')}  {where}")

        def finished(job):
            self.watch = None
            self._busy(False)
            self.plan = None
            out = job.get("result") or {}
            status = out.get("status") if job.get("status") == "succeeded" else job.get("status")
            root = out.get("root") or (res.get("plan") or {}).get("destination")
            self.result_root = root
            self.open_btn.setEnabled(bool(root) and Path(root).exists())
            if status == "complete":
                self.summary.setText(tr("✓ 导出完成，已校验\n{0}\n保留完整 xm5 文件夹及内部结构。", root))
                self.summary.setStyleSheet(f"font-size: 11pt; color: {style.OK};")
                self.progress.setValue(self.progress.maximum())
                self.ctl.toast(tr("导出完成，已校验"))
                ver = out.get("verification") or {}
                self.details.setPlainText(tr("校验：{0}\n构建号：{1}", tr('通过') if ver.get('ok') else ver, out.get('build_id')))
            else:
                word = tr("已取消") if status == "cancelled" else tr("没有完成")
                self.summary.setText(tr("✗ 导出{0}：这个文件夹有未完成标记，请先恢复或重新导出\n{1}", word, root or ''))
                self.summary.setStyleSheet(f"font-size: 11pt; color: {style.DANGER};")
                self.details.setPlainText("" if status == "cancelled" else job_error(job))
                self.ctl.toast(tr("导出{0}", word))

        def accepted(r):
            self.watch = self.ctl.backend.watch(r["job_id"], progress=progress, done=finished, failed=finished)

        def refused(e):
            self._busy(False)
            if e.code == "PLAN_STALE":
                self.plan = None
                self.summary.setText(tr("曲库在预览之后有变化，请重新预览。"))
                self.summary.setStyleSheet(f"font-size: 11pt; color: {style.WARN};")
            else:
                self.summary.setText(tr("✗ 导出没有开始：{0}", e.message))
                self.summary.setStyleSheet(f"font-size: 11pt; color: {style.DANGER};")
        self.ctl.backend.command("cards.execute", {"plan_id": res["plan_id"], "digest": res["digest"]},
                                 lane="jobs", ok=accepted, err=refused)

    def cancel(self) -> None:
        if self.watch is not None:
            self.watch.cancel()
            self.progress_label.setText(tr("正在取消…"))

    # ================================================================== return / restore
    def _return_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("page")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 10, 0, 0)
        g = QGroupBox(tr("导出过的文件夹，或插在电脑上的 TF 卡"))
        gl = QHBoxLayout(g)
        self.card_edit = QLineEdit(self.ctl.settings_value("card_root") or "")
        self.card_edit.setObjectName("cardRoot")
        self.card_edit.setPlaceholderText(tr("例如 E:\\  或  D:\\XM5-card-20261101-2030"))
        pick = QPushButton(tr("浏览…"))
        pick.clicked.connect(self._pick_card)
        self.check_btn = QPushButton(tr("检查"))
        self.check_btn.setObjectName("primary")
        self.check_btn.clicked.connect(self.inspect_card)
        gl.addWidget(self.card_edit, 1)
        gl.addWidget(pick)
        gl.addWidget(self.check_btn)
        lay.addWidget(g)
        self.card_info = QLabel(tr("只读检查：不会改动卡上的任何文件。带回歌单时先比对版本；两边都改过时由你决定，不会自动合并。"))
        self.card_info.setWordWrap(True)
        self.card_info.setProperty("muted", True)
        lay.addWidget(self.card_info)
        self.tree = QTreeWidget()
        self.tree.setObjectName("cardReturn")
        self.tree.setHeaderLabels([tr("歌单 / 文件"), tr("状态"), ""])
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 380)
        self.tree.setRootIsDecorated(False)
        lay.addWidget(self.tree, 1)
        brow = QHBoxLayout()
        self.restore_btn = QPushButton(tr("把原文件恢复到新文件夹…"))
        self.restore_btn.setObjectName("restoreCard")
        self.restore_btn.setToolTip(tr("按导出时的原始目录结构，把卡上的原文件复制到一个新文件夹（XM5 上后加的文件另外放）"))
        self.restore_btn.clicked.connect(self.restore)
        brow.addWidget(self.restore_btn)
        recover = QPushButton(tr("恢复中断的更新"))
        recover.clicked.connect(self.recover_activation)
        brow.addWidget(recover)
        brow.addStretch(1)
        lay.addLayout(brow)
        return tab

    def _pick_card(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("选择导出文件夹或 TF 卡"), self.card_edit.text())
        if d:
            self.card_edit.setText(d)

    def _card_root(self) -> str | None:
        root = self.card_edit.text().strip()
        from ..core.card_layout import detect_layout
        try:
            if not root:
                raise ValueError("empty path")
            from ..core.inline_card import library
            if (Path(root) / library.MANIFEST).is_file() or (Path(root) / "Struct/library.json").is_file():
                root = str(library.card_root(root))
            else:
                detect_layout(Path(root))
        except (OSError, ValueError) as exc:
            self.ctl.error(tr("没有找到有效的应用清单：{0}", exc))
            return None
        self.ctl.set_setting("card_root", root)
        return root

    def inspect_card(self) -> None:
        root = self._card_root()
        if root is None:
            return
        self.tree.clear()
        from ..core.card_layout import playlist_files
        from ..core.inline_card import library
        lists = (library.playlist_paths(root)
                 if (Path(root) / library.MANIFEST).is_file() else list(playlist_files(Path(root))))
        self.card_info.setText(tr("正在检查 {0} 个歌单…", len(lists)) if lists else tr("这张卡上没有歌单。"))

        def start():
            for m3u in lists:
                self._reconcile(root, m3u)
            self.ctl.backend.command("cards.inbox", {"root": root}, lane="jobs", err=lambda e: None,
                                     ok=lambda r: self.ctl.backend.watch(
                                         r["job_id"], done=lambda job: self._show_inbox(job.get("result") or {})))
        self.ctl.ensure_roots(export=[root], then=start)

    def _reconcile(self, root: str, m3u: Path) -> None:
        it = QTreeWidgetItem([m3u.stem, tr("检查中…"), ""])
        self.tree.addTopLevelItem(it)

        def show(job):
            p = job.get("result") or {}
            text, colour = RETURN_STATUS.get(p.get("status"), (str(p.get("status", "?")), style.MUTED))
            current = self.ctl.state.playlist(p.get("playlist_id")) if p.get("playlist_id") else None
            it.setText(0, (current or {}).get("name") or p.get("name") or m3u.stem)
            extra = tr("（{0} 首）", len(p.get('proposed_tracks') or []))
            if p.get("unresolved"):
                extra = tr("（{0} 条对不上）", len(p['unresolved']))
            elif p.get("card_only_references"):
                extra += tr("  · Card-only references: {0}", len(p['card_only_references']))
            it.setText(1, text + extra)
            it.setForeground(1, QColor(colour))
            it.setData(0, Qt.UserRole, (root, str(m3u), p))
            self._actions(it, p)
            pending = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
            left = sum(1 for x in pending if x.text(1) == tr("检查中…"))
            if left:
                self.card_info.setText(tr("正在检查…（还剩 {0} 个歌单）", left))
                return
            todo = sum(1 for x in pending if x.data(0, Qt.UserRole) and x.data(0, Qt.UserRole)[2].get("status")
                       in ("changed", "new_playlist", "conflict"))
            self.card_info.setText(tr("检查完成：{0} 个歌单可以带回，右边有按钮。", todo) if todo else
                                   tr("检查完成：卡上的歌单和电脑上一致，不需要带回。"))

        def failed(job):
            it.setText(1, tr("检查失败：{0}", job_error(job)))
            it.setForeground(1, QColor(style.DANGER))
        self.ctl.backend.command("cards.reconcile", {"root": root, "m3u": str(m3u)}, lane="jobs",
                                 ok=lambda r: self.ctl.backend.watch(r["job_id"], done=show, failed=failed),
                                 err=lambda e: it.setText(1, tr("检查失败：{0}", e.message)))

    def _actions(self, it: QTreeWidgetItem, p: dict) -> None:
        status = p.get("status")
        buttons = []
        if status == "changed":
            buttons.append((tr("带回电脑"), lambda: self._import(it, None)))
            if p.get("format") == "XM5I":
                buttons.append((tr("保留电脑版本"), lambda: self._import(it, "pc")))
        elif status == "new_playlist":
            buttons.append((tr("带回为新歌单"), lambda: self._import(it, None)))
        elif status == "conflict":
            if p.get("format") == "XM5I":
                buttons.append((tr("保留电脑版本"), lambda: self._import(it, "pc")))
            buttons.append((tr("用 XM5 上的版本"), lambda: self._import(it, "device")))
            buttons.append((tr("另存为新歌单"), lambda: self._import(it, "copy")))
        elif status == "missing_playlist":
            buttons.append((tr("保留电脑版本"), lambda: self._import(it, "pc")))
            buttons.append((tr("确认删除电脑版本"), lambda: self._import(it, "device")))
        if not buttons:
            return
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        for label, fn in buttons:
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        self.tree.setItemWidget(it, 2, box)

    def _import(self, it: QTreeWidgetItem, decision: str | None) -> None:
        root, m3u, p = it.data(0, Qt.UserRole)
        args = {"root": root, "m3u": m3u, "input_sha256": p.get("input_sha256")}
        if p.get("playlist_id"):
            args["playlist_id"] = p["playlist_id"]
        current = p.get("current_snapshot") or {}
        new = decision == "copy" or not current
        revision = None if new else current.get("revision")
        if decision:
            args["decision"] = decision
        if new:
            base = tr("{0}（XM5）", p.get('name') or Path(m3u).stem) if decision == "copy" else (p.get("name") or Path(m3u).stem)
            args["new_name"] = self.ctl.state.unique_name(base, numbered=False)

        def done(job):
            res = job.get("result") or {}
            if res.get("applied") or res.get("acknowledged"):
                it.setText(1, tr("已保留电脑版本，可在下次更新时写回。") if res.get("acknowledged") else tr("✓ 已带回电脑"))
                it.setForeground(1, QColor(style.OK))
                self.tree.removeItemWidget(it, 2)
                self.ctl.state.refresh_playlists()
                if p.get("favorites") or p.get("card_only_references"):
                    self.ctl.state.load_all()
                self.ctl.toast(tr("已保留电脑版本") if res.get("acknowledged") else tr("已带回歌单「{0}」", it.text(0)))
            else:
                it.setText(1, tr("没有改动：{0}", res.get('reason')))

        def failed(job):
            it.setText(1, tr("✗ 没有带回：{0}（重新检查后再试）", job_error(job)))
            it.setForeground(1, QColor(style.DANGER))
        self.ctl.backend.command("cards.import", args, revision=revision, lane="jobs",
                                 ok=lambda r: self.ctl.backend.watch(r["job_id"], done=done, failed=failed),
                                 err=lambda e: it.setText(1, tr("✗ 没有带回：{0}", e.message)))

    def _show_inbox(self, res: dict) -> None:
        entries = res.get("entries") or []
        if not entries:
            return
        head = QTreeWidgetItem([tr("应用收件箱"), tr("{0} 个文件（只做了检查，不会自动导入）", len(entries)), ""])
        self.tree.addTopLevelItem(head)
        for e in entries[:200]:
            kind = {"playlist": tr("歌单"), "preset": tr("EQ 预设"), "audio": tr("音乐"), "image": tr("图片")}.get(e.get("kind"), tr("其他"))
            self.tree.addTopLevelItem(QTreeWidgetItem(["  " + e.get("inbox_relative", ""), kind + (
                tr("：{0}", e['reason']) if e.get("reason") else ""), ""]))

    def restore(self) -> None:
        root = self._card_root()
        if root is None:
            return
        parent = QFileDialog.getExistingDirectory(self, tr("选择放恢复结果的上级文件夹"), self.ctl.settings_value("export_parent") or "")
        if not parent:
            return
        dest = Path(parent) / time.strftime("XM5-restore-%Y%m%d-%H%M")
        self.card_info.setText(tr("正在恢复到 {0} …", dest))

        def done(job):
            self.card_info.setText(tr("✓ 已恢复到 {0}（XM5 上后加的文件和歌单另外放在里面的单独文件夹）", dest))
            self.ctl.toast(tr("恢复完成"))

        def failed(job):
            self.card_info.setText(tr("✗ 恢复没有完成：{0}", job_error(job)))

        def start():
            self.ctl.backend.command("cards.restore", {"root": root, "destination": str(dest)}, lane="jobs",
                                     ok=lambda r: self.ctl.backend.watch(r["job_id"], done=done, failed=failed),
                                     err=lambda e: self.card_info.setText(tr("✗ 没有恢复：{0}", e.message)))
        self.ctl.ensure_roots(export=[root, parent], then=start)

    def recover_activation(self):
        root = Path(self.card_edit.text().strip()).absolute()
        if root.name == "xm5":
            root = root.parent
        if not root.is_dir():
            self.ctl.error(tr("先选一个存在的上级文件夹。"))
            return
        def finished(job):
            self.card_info.setText(str(job.get("result") or job_error(job)))
        def start():
            self.ctl.backend.command("cards.recover_update", {"control_directory": str(root)}, lane="jobs",
                ok=lambda receipt: self.ctl.backend.watch(receipt["job_id"], done=finished, failed=finished),
                err=lambda error: self.card_info.setText(error.message))
        self.ctl.ensure_roots(export=[str(root)], then=start)

    # ================================================================== explicit card-only maintenance
    def _organize_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel(tr("只整理明确选中的卡上歌曲；电脑音乐来源不会被修改。")))
        row = QHBoxLayout()
        self.organize_root = QLineEdit()
        self.organize_root.setObjectName("organizeCardRoot")
        browse = QPushButton(tr("浏览…"))
        def pick_root():
            value = QFileDialog.getExistingDirectory(self, tr("选择 TF 卡上级位置"), self.organize_root.text())
            if value:
                self.organize_root.setText(value)
        browse.clicked.connect(pick_root)
        row.addWidget(self.organize_root, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        self.organize_files = QPlainTextEdit()
        self.organize_files.setObjectName("organizeSelectedTracks")
        self.organize_files.setPlaceholderText("xm5/music/Collection/Album/Track.flac")
        layout.addWidget(self.organize_files, 1)
        buttons = QHBoxLayout()
        choose = QPushButton(tr("选择歌曲…"))
        choose.clicked.connect(self._pick_organize_tracks)
        prepare = QPushButton(tr("准备所选歌曲"))
        prepare.setObjectName("organizeSelected")
        prepare.clicked.connect(lambda: self._maintenance("storage.inline.organize"))
        rebuild = QPushButton(tr("重建 Struct 索引"))
        rebuild.setObjectName("rebuildInlineIndex")
        rebuild.clicked.connect(lambda: self._maintenance("storage.inline.rebuild"))
        for control in (choose, prepare, rebuild):
            buttons.addWidget(control)
        layout.addLayout(buttons)
        self.maintenance_result = QPlainTextEdit()
        self.maintenance_result.setReadOnly(True)
        self.maintenance_result.setMaximumHeight(160)
        layout.addWidget(self.maintenance_result)
        return tab

    def _pick_organize_tracks(self):
        root = Path(self.organize_root.text()).absolute()
        chosen, _ = QFileDialog.getOpenFileNames(self, tr("选择卡上的歌曲"), str(root / "xm5/music"),
            "Music (*.flac *.wav *.aif *.aiff *.mp3 *.m4a)")
        try:
            self.organize_files.setPlainText("\n".join(Path(path).relative_to(root).as_posix() for path in chosen))
        except ValueError:
            self.ctl.error(tr("所选文件必须在指定的 TF 卡目录内。"))

    def _maintenance(self, command):
        if self.running():
            self.ctl.error(tr("请等待当前卡操作完成。"))
            return
        root = self.organize_root.text().strip()
        if not root or not Path(root).is_dir():
            self.ctl.error(tr("先选一个存在的上级文件夹。"))
            return
        args = {"root": root}
        if command.endswith("organize"):
            tracks = [line.strip() for line in self.organize_files.toPlainText().splitlines() if line.strip()]
            if not tracks:
                self.ctl.error(tr("先选择需要准备的歌曲。"))
                return
            args.update(tracks=tracks, analyze_peak=self.peak_enabled.isChecked())
        self.maintenance_result.setPlainText(tr("正在处理…"))
        def finished(job):
            import json
            self.watch = None
            self.maintenance_result.setPlainText(json.dumps(job.get("result") or job.get("error"), ensure_ascii=False, indent=2))
        def start():
            self.ctl.backend.command(command, args, lane="jobs",
                ok=lambda receipt: setattr(self, "watch", self.ctl.backend.watch(receipt["job_id"], done=finished, failed=finished)),
                err=lambda error: self.maintenance_result.setPlainText(error.message))
        self.ctl.ensure_roots(export=[root], then=start)


def _open(path: str) -> None:
    try:
        os.startfile(path)                      # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


__all__ = ["ExportPage"]
