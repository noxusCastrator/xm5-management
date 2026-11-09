"""Optional installed first-launch import, before any destination profile is opened."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout)

from ..core.profile_import import import_portable_profile
from . import i18n


def _text(english: str, chinese: str) -> str:
    return english if i18n.LANG == "en" else chinese


class _ImportTask(QThread):
    def __init__(self, source, destination, importer, parent):
        super().__init__(parent)
        self.source, self.destination, self.importer = source, destination, importer
        self.error = ""

    def run(self):
        try:
            self.importer(self.source, self.destination)
        except Exception as exc:
            self.error = str(exc)


class ProfileImportDialog(QDialog):
    def __init__(self, destination: Path, *, importer=import_portable_profile):
        super().__init__()
        self.destination, self.importer = Path(destination), importer
        self.task = None
        self.setObjectName("profileImportDialog")
        self.setWindowTitle(_text("Your XM5 Studio profile", "XM5 Studio 个人资料"))
        self.setWindowModality(Qt.ApplicationModal)
        self.resize(620, 260)
        layout = QVBoxLayout(self)
        intro = QLabel(_text("Import settings, playlists, favorites, notes and EQ presets from a portable XM5 application, or continue with a fresh profile. Close the portable application before importing. Its folder and original music stay in place.",
                             "可以从便携版 XM5 应用导入设置、歌单、收藏、备注和 EQ 预设，或使用全新的个人资料。导入前请关闭便携版应用；其文件夹和原始音乐会保留在原处。"))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        label = QLabel(_text("Portable application folder", "便携版应用文件夹"))
        layout.addWidget(label)
        row = QHBoxLayout()
        self.source = QLineEdit()
        self.source.setObjectName("portableProfileSource")
        label.setBuddy(self.source)
        row.addWidget(self.source, 1)
        self.browse = QPushButton(_text("Choose folder…", "选择文件夹…"))
        self.browse.clicked.connect(self._browse)
        row.addWidget(self.browse)
        layout.addLayout(row)
        self.status = QLabel(_text("Choose the portable application folder containing config.json and data/backend.", "请选择包含 config.json 和 data/backend 的便携版应用文件夹。"))
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        layout.addWidget(self.status)
        layout.addStretch(1)
        row = QHBoxLayout()
        self.cancel = QPushButton(_text("Cancel", "取消"))
        self.cancel.clicked.connect(self.reject)
        row.addWidget(self.cancel)
        row.addStretch(1)
        self.fresh = QPushButton(_text("Continue fresh", "使用全新资料"))
        self.fresh.setObjectName("continueFresh")
        self.fresh.setDefault(True)
        self.fresh.clicked.connect(self.accept)
        row.addWidget(self.fresh)
        self.copy = QPushButton(_text("Import profile", "导入资料"))
        self.copy.setObjectName("importProfile")
        self.copy.setAutoDefault(False)
        self.copy.setEnabled(False)
        self.copy.clicked.connect(self._import)
        row.addWidget(self.copy)
        layout.addLayout(row)
        self.source.textChanged.connect(lambda value: self.copy.setEnabled(bool(value.strip()) and self.task is None))

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, _text("Choose portable application folder", "选择便携版应用文件夹"))
        if folder:
            self.source.setText(folder)

    def _import(self):
        if self.task is not None or not self.source.text().strip():
            return
        self.task = _ImportTask(Path(self.source.text().strip()), self.destination, self.importer, self)
        self.task.finished.connect(self._finished)
        for widget in (self.source, self.browse, self.copy, self.fresh, self.cancel):
            widget.setEnabled(False)
        self.status.setText(_text("Copying the selected profile. Keep this window open until copying finishes…", "正在复制所选资料。请保持此窗口打开，等待复制完成…"))
        self.task.start()

    def _finished(self):
        task, self.task = self.task, None
        error = task.error
        task.deleteLater()
        if not error:
            self.accept()
            return
        self.status.setText(_text("Import failed: {0}\nThe installed profile was not created. Choose another source, continue fresh, or cancel.",
                                  "导入失败：{0}\n未创建安装版资料。请选择其他来源、使用全新资料或取消。").format(error))
        for widget in (self.source, self.browse, self.copy, self.fresh, self.cancel):
            widget.setEnabled(True)

    def reject(self):
        if self.task is None:
            super().reject()

    def accept(self):
        if self.task is None:
            super().accept()

    def closeEvent(self, event):
        if self.task is not None:
            event.ignore()
        else:
            super().closeEvent(event)


def choose_installed_profile(locations) -> bool:
    if locations.mode != "installed" or locations.config.exists():
        return True
    dialog = ProfileImportDialog(locations.root)
    return dialog.exec() == QDialog.Accepted
