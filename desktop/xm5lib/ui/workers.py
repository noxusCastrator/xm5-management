"""Background jobs: scanning, cover thumbnails, export, foobar hand-off. Results arrive as Qt signals."""
from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class JobSignals(QObject):
    progress = Signal(int, int, str)
    item = Signal(object)          # incremental results (e.g. one cover)
    done = Signal(object)
    failed = Signal(str)


class Job(QRunnable):
    """Runs ``fn(job)``; ``fn`` may call job.report()/job.emit_item() and check job.cancel."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = JobSignals()
        self.cancel = threading.Event()
        self.setAutoDelete(False)

    def report(self, i: int, n: int, label: str = "") -> None:
        self.signals.progress.emit(i, n, label)

    def emit_item(self, obj) -> None:
        self.signals.item.emit(obj)

    def run(self) -> None:
        try:
            result = self.fn(self)
        except Exception as exc:  # shown to the user, never swallowed
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=4)}")
        else:
            self.signals.done.emit(result)


class Jobs(QObject):
    """Keeps references to running jobs so they are not garbage collected."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool.globalInstance()
        self.running: dict[str, Job] = {}

    def start(self, name: str, fn, on_done=None, on_failed=None, on_progress=None, on_item=None) -> Job:
        if name in self.running:
            raise RuntimeError(f"'{name}' is already running")
        job = Job(fn)
        if on_progress:
            job.signals.progress.connect(on_progress)
        if on_item:
            job.signals.item.connect(on_item)

        def finish(*_):
            self.running.pop(name, None)
            self.changed.emit()

        job.signals.done.connect(finish)
        job.signals.failed.connect(finish)
        if on_done:
            job.signals.done.connect(on_done)
        if on_failed:
            job.signals.failed.connect(on_failed)
        self.running[name] = job
        self.changed.emit()
        self.pool.start(job)
        return job

    def busy(self, name: str) -> bool:
        return name in self.running

    def cancel(self, name: str) -> None:
        if name in self.running:
            self.running[name].cancel.set()
