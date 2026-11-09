"""Prepare an update card through the firmware service; no GUI filesystem writes."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from .. import __version__
from . import i18n


_TEXT = {
    "title": ("制作升级卡", "Create update card"),
    "section": ("播放器固件", "Player firmware"),
    "intro": ("选择可移动盘，制作固件升级卡。此操作不会刷写播放器。", "Prepare a removable drive for the firmware update. This does not flash the player."),
    "app": ("应用版本", "Application version"),
    "firmware": ("配套固件", "Paired firmware"),
    "hash": ("固件 SHA-256", "Firmware SHA-256"),
    "drive": ("目标盘", "Target drive"),
    "choose": ("请选择目标盘…", "Select a drive…"),
    "refresh": ("刷新", "Refresh"),
    "close": ("关闭", "Close"),
    "confirm": ("确认", "Confirm"),
    "cancel": ("取消", "Cancel"),
    "loading": ("正在检查固件并查找可移动盘…", "Checking firmware and finding removable drives…"),
    "ready": ("先备份目标盘，再选择要制作升级卡的盘。", "Back up the target drive, then select the drive to prepare."),
    "no_drives": ("没有找到可移动盘或 USB 存储。连接读卡器后点击刷新。", "No removable drive or USB storage found. Connect a card reader and refresh."),
    "unavailable": ("无法使用配套固件：{0}", "Paired firmware unavailable: {0}"),
    "unavailable_reason": ("缺少固件或固件校验未通过。", "Firmware is missing or failed verification."),
    "unavailable_version": ("不可用", "Unavailable"),
    "drive_info": ("{0} · 可用 {1} · {2}", "{0} · {1} free · {2}"),
    "unnamed": ("未命名", "Unnamed"),
    "unknown": ("未知", "Unknown"),
    "planning": ("正在检查选中的盘；尚未删除任何文件…", "Inspecting the selected drive; no files have been removed…"),
    "warning": ("所选盘 {0} 的全部内容将被删除。完成后仅保留 update.bin。", "ALL contents of the selected drive {0} will be removed. Only update.bin will remain."),
    "plan_details": ("卷标：{0}\n容量：{1}\n将删除：{2} 个文件，{3}\n配套固件：{4}", "Label: {0}\nCapacity: {1}\nTo remove: {2} files, {3}\nPaired firmware: {4}"),
    "backup": ("请先手动备份目标盘。也可<a href=\"export\">打开 TF 卡导出页面</a>。", "Back up the selected drive manually before continuing. You can also <a href=\"export\">open TF Card Export</a>."),
    "cancelled": ("已取消。目标盘未作更改。", "Cancelled. No changes were made to the drive."),
    "working": ("正在制作升级卡。请勿断开目标盘；完成前不能关闭此窗口。", "Creating the update card. Keep the drive connected; this window must stay open until completion."),
    "wait": ("请等待当前操作完成后再关闭窗口。", "Wait for the current operation to finish before closing this window."),
    "phase_clear": ("正在清空目标盘…", "Clearing the target drive…"),
    "phase_copy": ("正在复制 update.bin…", "Copying update.bin…"),
    "phase_verify": ("正在校验已复制的固件…", "Verifying the copied firmware…"),
    "phase_prepare": ("正在重新检查目标盘…", "Rechecking the target drive…"),
    "success": ("升级卡已制作完成：{0}\n已复制文件的 SHA-256：{1}\n请安全弹出目标盘，然后按照官方 XM5 升级步骤安装固件。", "Update card ready: {0}\nCopied file SHA-256: {1}\nSafely eject the drive, then follow the official XM5 update procedure to install the firmware."),
    "failed": ("制作升级卡失败：{0}\n目标盘可能已被部分清空。请检查目标盘并恢复备份后再使用。", "Creating the update card failed: {0}\nThe drive may be partially cleared. Inspect it and restore your backup before using it."),
    "inspect_failed": ("无法检查目标盘或固件：{0}\n未开始清空目标盘。", "Could not inspect the drive or firmware: {0}\nClearing the drive has not started."),
    "jobs_busy": ("请先等待扫描或导入任务完成。", "Wait for the scan or import job to finish first."),
    "export_busy": ("请先等待 TF 卡任务完成。", "Wait for the TF card job to finish first."),
    "playback_busy": ("请先停止电脑端播放，再制作升级卡。", "Stop desktop playback before creating an update card."),
}


def text(key: str, *args) -> str:
    value = _TEXT[key][1 if i18n.LANG == "en" else 0]
    return value.format(*args) if args else value


def _size(value) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return text("unknown")
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1000


def blocked_reason(ctl) -> str:
    """Read the same scan/export ownership state as the existing pages."""
    state = getattr(ctl, "state", None)
    if state is not None and (getattr(state, "busy", "") or getattr(state, "job", None) is not None):
        return text("jobs_busy")
    export = getattr(ctl, "pages", {}).get("export")
    if export is not None and export.running():
        return text("export_busy")
    if getattr(getattr(ctl, "player", None), "state", "stopped") in ("playing", "paused"):
        return text("playback_busy")
    return ""


class _FirmwareTask(QThread):
    progress = Signal(object)

    def __init__(self, operation: str, action, parent):
        super().__init__(parent)
        self.operation, self.action = operation, action
        self.result = None
        self.error = ""

    def run(self) -> None:
        try:
            self.result = self.action(self.progress.emit)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class _FirmwareConfirmation(QDialog):
    """The sole destructive confirmation, after a read-only service plan."""

    def __init__(self, plan: dict, parent):
        super().__init__(parent)
        self.setWindowTitle(text("title"))
        self.setWindowModality(Qt.ApplicationModal)
        self.resize(580, 330)
        self.export_requested = False
        layout = QVBoxLayout(self)
        self.warning = QLabel(text("warning", plan["root"]))
        self.warning.setObjectName("firmwareClearWarning")
        self.warning.setTextFormat(Qt.PlainText)
        self.warning.setWordWrap(True)
        font = self.warning.font()
        font.setBold(True)
        self.warning.setFont(font)
        layout.addWidget(self.warning)
        details = QLabel(text("plan_details", plan.get("label") or text("unnamed"),
                             _size(plan.get("capacity")), plan.get("files_count", 0),
                             _size(plan.get("bytes_to_remove")), plan["firmware_version"]))
        details.setTextFormat(Qt.PlainText)
        details.setWordWrap(True)
        layout.addWidget(details)
        self.backup = QLabel(text("backup").replace('<a ', '<a style="color: #2383e2;" '))
        self.backup.setObjectName("firmwareBackupLink")
        self.backup.setWordWrap(True)
        self.backup.setTextFormat(Qt.RichText)
        self.backup.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.backup.setOpenExternalLinks(False)
        self.backup.linkActivated.connect(self._export)
        layout.addWidget(self.backup)
        layout.addStretch(1)
        self.buttons = QDialogButtonBox()
        self.confirm = self.buttons.addButton(text("confirm"), QDialogButtonBox.AcceptRole)
        self.confirm.setObjectName("firmwareConfirm")
        self.confirm.setAutoDefault(False)
        self.cancel = self.buttons.addButton(text("cancel"), QDialogButtonBox.RejectRole)
        self.cancel.setObjectName("firmwareCancel")
        self.cancel.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.cancel.setFocus()

    def _export(self, link: str) -> None:
        if link == "export":
            self.export_requested = True
            self.reject()


class FirmwareCardDialog(QDialog):
    """A modal, single-operation view over FirmwareCardService.

    ``service`` is injectable so widget tests cannot discover or write real cards.
    All volume validation, planning, copying and deletion belong to that service.
    """

    def __init__(self, ctl, *, service=None, app_root: Path | None = None):
        super().__init__(ctl if isinstance(ctl, QWidget) else None)
        self.ctl = ctl
        self._service = service
        self._app_root = Path(app_root or Path(__file__).resolve().parents[2])
        self._task: _FirmwareTask | None = None
        self._info: dict = {}
        self._targets: list[dict] = []
        self._confirmation = None
        self._complete = False
        self.setObjectName("firmwareCardDialog")
        self.setWindowTitle(text("title"))
        self.setWindowModality(Qt.ApplicationModal)
        self.resize(660, 410)
        layout = QVBoxLayout(self)
        intro = QLabel(text("intro"))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.app_version = QLabel(__version__)
        self.firmware_version = QLabel(text("unavailable_version"))
        self.firmware_hash = QLabel("")
        for label in (self.app_version, self.firmware_version, self.firmware_hash):
            label.setTextFormat(Qt.PlainText)
        self.firmware_hash.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.firmware_hash.setWordWrap(True)
        form.addRow(text("app"), self.app_version)
        form.addRow(text("firmware"), self.firmware_version)
        form.addRow(text("hash"), self.firmware_hash)
        row = QHBoxLayout()
        self.targets = QComboBox()
        self.targets.setObjectName("firmwareTarget")
        self.targets.setMinimumContentsLength(30)
        self.targets.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.targets.currentIndexChanged.connect(self._target_changed)
        row.addWidget(self.targets, 1)
        self.refresh_button = QPushButton(text("refresh"))
        self.refresh_button.setObjectName("firmwareRefresh")
        self.refresh_button.clicked.connect(self.refresh_targets)
        row.addWidget(self.refresh_button)
        form.addRow(text("drive"), row)
        self.drive_info = QLabel("")
        self.drive_info.setTextFormat(Qt.PlainText)
        self.drive_info.setWordWrap(True)
        form.addRow("", self.drive_info)
        layout.addLayout(form)
        self.status = QLabel(text("loading"))
        self.status.setObjectName("firmwareStatus")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setObjectName("firmwareProgress")
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.create_button = QPushButton(text("title"))
        self.create_button.setObjectName("firmwareCreate")
        self.create_button.setEnabled(False)
        self.create_button.setAutoDefault(False)
        self.create_button.clicked.connect(self.create_card)
        buttons.addWidget(self.create_button)
        self.close_button = QPushButton(text("close"))
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self._guard_timer = QTimer(self, interval=250, timeout=self._update_controls)
        self._guard_timer.start()
        self.finished.connect(self._guard_timer.stop)
        if isinstance(ctl, QWidget):
            ctl.installEventFilter(self)
        QTimer.singleShot(0, self.refresh_targets)

    def running(self) -> bool:
        return self._task is not None

    def _start(self, operation: str, action) -> None:
        if self.running() or self._confirmation is not None:
            return
        self._task = _FirmwareTask(operation, action, self)
        self._task.progress.connect(self._progress)
        self._task.finished.connect(self._finished)
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self._update_controls()
        self._task.start()

    def refresh_targets(self) -> None:
        if self.running() or self._confirmation is not None:
            return
        self._complete = False
        self._info = {}
        self.targets.clear()
        self.targets.addItem(text("choose"), None)
        self.status.setText(text("loading"))

        def inspect(_progress):
            if self._service is None:
                from ..core.firmware_card import FirmwareCardService
                self._service = FirmwareCardService(self._app_root)
            return {"firmware": self._service.firmware_info(), "targets": self._service.list_targets()}

        self._start("refresh", inspect)

    def _target_changed(self, _index=0) -> None:
        target = self.targets.currentData()
        self.drive_info.setText(text("drive_info", _size(target.get("capacity")), _size(target.get("free")),
                                    target.get("filesystem") or text("unknown")) if target else "")
        self._update_controls()

    def _update_controls(self) -> None:
        busy = self.running() or self._confirmation is not None
        reason = blocked_reason(self.ctl)
        self.targets.setEnabled(not busy and not self._complete)
        self.refresh_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        self.create_button.setEnabled(not busy and not self._complete and not reason
                                      and bool(self._info.get("available")) and bool(self.targets.currentData()))
        self.create_button.setToolTip(reason)
        if self._confirmation is not None:
            self._confirmation.confirm.setEnabled(not reason)
            self._confirmation.confirm.setToolTip(reason)

    def create_card(self) -> None:
        if self.running() or self._confirmation is not None or self._complete:
            return
        reason = blocked_reason(self.ctl)
        if reason:
            self.status.setText(reason)
            return
        if not self._info.get("available"):
            self.status.setText(text("unavailable", self._info.get("error") or text("unavailable_reason")))
            return
        target = self.targets.currentData()
        if not target:
            return
        target_id = str(target["id"])
        self.status.setText(text("planning"))
        self._start("plan", lambda _progress: self._service.plan(target_id))

    def _finished(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        self.progress.setVisible(False)
        operation, result, error = task.operation, task.result, task.error
        task.deleteLater()
        if error:
            self.status.setText(text("failed" if operation == "execute" else "inspect_failed", error))
        elif operation == "refresh":
            self._info = result["firmware"]
            self._targets = result["targets"]
            self.app_version.setText(str(self._info.get("app_version") or __version__))
            self.firmware_version.setText(str(self._info.get("version") or text("unavailable_version")))
            self.firmware_hash.setText(str(self._info.get("sha256") or ""))
            for target in self._targets:
                self.targets.addItem(f"{target['root']} · {target.get('label') or text('unnamed')} · {_size(target.get('capacity'))}", target)
            self.targets.setCurrentIndex(0)
            self.status.setText(text("ready" if self._targets else "no_drives") if self._info.get("available")
                                else text("unavailable", self._info.get("error") or text("unavailable_reason")))
        elif operation == "plan":
            self._confirm_plan(result)
        elif operation == "execute":
            if not isinstance(result, dict) or result.get("status") != "complete":
                self.status.setText(text("failed", str(result)))
            else:
                self._complete = True
                self.status.setText(text("success", result["root"], result["sha256"]))
        self._update_controls()

    def _confirm_plan(self, plan: dict) -> None:
        reason = blocked_reason(self.ctl)
        if reason:
            self.status.setText(reason)
            return
        dialog = _FirmwareConfirmation(plan, self)
        self._confirmation = dialog
        self._update_controls()
        confirmed = dialog.exec() == QDialog.Accepted
        self._confirmation = None
        export_requested = dialog.export_requested
        dialog.deleteLater()
        if export_requested:
            self.reject()
            self.ctl.show_page("export")
            return
        if not confirmed:
            self.status.setText(text("cancelled"))
            return
        reason = blocked_reason(self.ctl)
        if reason:
            self.status.setText(reason)
            return
        self.status.setText(text("working"))
        self._start("execute", lambda progress: self._service.execute(plan["token"], confirmed=True, progress=progress))

    def _progress(self, value) -> None:
        if not isinstance(value, dict):
            return
        phase = {"clear": "phase_clear", "delete": "phase_clear", "copy": "phase_copy",
                 "verify": "phase_verify", "prepare": "phase_prepare", "validate": "phase_prepare"}.get(value.get("phase"))
        if phase:
            self.status.setText(text(phase) + "\n" + text("working"))
        completed, total = value.get("completed"), value.get("total")
        if isinstance(completed, (int, float)) and isinstance(total, (int, float)) and total > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(max(0, min(1000, int(completed / total * 1000))))
        else:
            self.progress.setRange(0, 0)

    def reject(self) -> None:
        if self.running():
            self.status.setText(text("wait"))
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self.running():
            self.status.setText(text("wait"))
            event.ignore()
        else:
            super().closeEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.ctl and event.type() == QEvent.Close and self.running():
            self.status.setText(text("wait"))
            event.ignore()
            return True
        return super().eventFilter(watched, event)
