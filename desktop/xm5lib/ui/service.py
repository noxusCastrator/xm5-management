"""The integration backend as seen from the GUI: one owned child process, requests off the UI thread.

The backend (``xm5lib.automation.server``) owns the library identities, playlists, favourites, notes,
PEQ presets, card export/return jobs and playback. The GUI never opens the backend's database and never
creates its own ``Application`` writer (docs/FRONTEND-HANDOFF.md). It:

* starts the server as its own child (Windows: in a kill-on-close job object, so it cannot outlive the
  window), or attaches to the one already serving this profile -- never two per profile;
* connects with ``automation.client.Client`` (token stays inside that object; nothing logs it);
* sends every request from a worker thread ("lane") and delivers the reply on the UI thread;
* freezes each mutation's payload and idempotency key before dispatch, so a retry after an unclear
  transport failure repeats exactly the same request. Playback commands are never retried.
"""
from __future__ import annotations

import http.client
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, QCoreApplication, QEvent

from .i18n import tr
from ..core.portable import PortablePaths

APP = Path(__file__).resolve().parents[2]
BACKEND_SITE = APP / "backend" / "site-packages"      # the backend's own packages (fastapi, uvicorn, ...)

TRANSPORT_ERRORS = (OSError, urllib.error.URLError, http.client.HTTPException, RuntimeError, ValueError)


class ServiceError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def backend_home(gui_home: Path | None = None) -> Path:
    """Durable backend state is portable; no implicit AppData fallback."""
    return PortablePaths.discover(APP).backend


@dataclass
class BackendConfig:
    music_roots: list[str] = field(default_factory=list)
    export_roots: list[str] = field(default_factory=list)
    import_roots: list[str] = field(default_factory=list)
    mpv: str | None = None
    allow_null_playback: bool = False       # tests only: silent mpv output

    def args(self) -> list[str]:
        out: list[str] = []
        for flag, values in (("--music-root", self.music_roots), ("--export-root", self.export_roots),
                             ("--import-root", self.import_roots)):
            for v in dict.fromkeys(str(x) for x in values if x):
                out += [flag, v]
        if self.mpv:
            out += ["--mpv", str(self.mpv)]
        if self.allow_null_playback:
            out.append("--allow-null-playback")
        return out


def backend_python() -> str:
    """The interpreter for the child: the GUI's base interpreter, console build (valid std streams)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def backend_env() -> dict:
    env = dict(os.environ)
    parts = [str(APP)] + ([str(BACKEND_SITE)] if BACKEND_SITE.is_dir() else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONUSERBASE", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("QT_QPA_PLATFORM", None)
    return env


class _Bridge(QObject):
    deliver = Signal(object)

    def __init__(self):
        super().__init__()
        self.deliver.connect(lambda fn: fn(), )


class _Lane(threading.Thread):
    """One serial worker: requests on a lane run in submission order."""

    def __init__(self, name: str):
        super().__init__(name=f"xm5-gui-{name}", daemon=True)
        self.jobs: queue.Queue = queue.Queue()
        self.start()

    def run(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception:                                   # noqa: BLE001 - a job reports its own errors
                pass


class Backend(QObject):
    """Process ownership + transport. ``state``: "stopped" | "starting" | "ready" | "failed"."""

    stateChanged = Signal(str)
    unhandled = Signal(object)          # ServiceError nobody asked to handle
    commandAcknowledged = Signal(object)  # exact args + accepted receipt, delivered on Qt
    jobFinished = Signal(object)          # after the owning page/state callback has applied its result

    START_TIMEOUT = 45.0

    def __init__(self, home: Path, log_dir: Path, parent=None):
        super().__init__(parent)
        self.home = Path(home)
        self.log_path = Path(log_dir) / "backend.log"
        self.state = "stopped"
        self.reason = ""
        self.client = None
        self.proc: subprocess.Popen | None = None
        self.owned = False
        self.config = BackendConfig()
        self.capabilities: dict = {}
        self._closing = False
        self.shutdown_incomplete_lanes = []
        self.activity_serial = 0
        self.pending_activity = set()
        self.activity_errors = []
        self._bridge = _Bridge()
        self._lanes = {n: _Lane(n) for n in ("data", "jobs", "playback")}
        self._generation = 0
        self._log = None

    # ------------------------------------------------------------------ lifecycle
    def _set_state(self, state: str, reason: str = "") -> None:
        self.state, self.reason = state, reason
        self.stateChanged.emit(state)

    @property
    def ready(self) -> bool:
        return self.state == "ready" and self.client is not None

    def start(self, config: BackendConfig) -> None:
        """Start (or attach to) the profile's backend in the background; stateChanged reports the result."""
        if self.state == "starting":
            return
        self.config = config
        self._generation += 1
        gen = self._generation
        self._set_state("starting")
        threading.Thread(target=self._start_worker, args=(gen, config), daemon=True, name="xm5-gui-start").start()

    def _start_worker(self, gen: int, config: BackendConfig) -> None:
        try:
            client, caps = self._connect_or_spawn(config)
        except Exception as exc:                                # noqa: BLE001 - shown in the UI
            msg = str(exc)
            self._post(lambda: gen == self._generation and self._started(None, {}, msg))
            return
        self._post(lambda: self._started(client, caps, "") if gen == self._generation else None)

    def _connect_or_spawn(self, config: BackendConfig):
        from ..automation.client import Client
        from ..automation.runtime import runtime_path
        discovery = runtime_path(self.home) / "connection.json"
        if discovery.exists():
            try:                                                # someone already serves this profile
                client = Client(self.home)
                self.owned = False
                return client, client.query("capabilities")["result"]
            except Exception:                                   # noqa: BLE001 - stale discovery: start our own
                pass
        self.home.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self._log is not None:
            self._log.close()
        self._log = open(self.log_path, "a", encoding="utf-8", errors="replace")
        self._log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting backend\n")
        self._log.flush()
        cmd = [backend_python(), "-B", "-s", "-m", "xm5lib.automation.server", "--home", str(self.home)] + config.args()
        spawned = time.time()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, env=backend_env(), stdin=subprocess.DEVNULL, stdout=self._log,
                                     stderr=self._log, creationflags=flags, cwd=str(self.home))
        from .player import _kill_with_parent
        _kill_with_parent(self.proc)
        self.owned = True
        deadline = time.monotonic() + self.START_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(tr("后端进程退出（代码 {0}）：{1}", self.proc.returncode, self.log_tail()))
            try:
                record = json.loads(discovery.read_text(encoding="utf-8"))
                # our child (a venv launcher's pid differs from the interpreter's: then go by the time)
                if record.get("pid") == self.proc.pid or discovery.stat().st_mtime >= spawned - 1:
                    client = Client(self.home)
                    return client, client.query("capabilities")["result"]
            except (OSError, ValueError, RuntimeError, PermissionError):
                pass
            time.sleep(0.1)
        self._kill()
        raise RuntimeError(tr("后端在 {0:.0f} 秒内没有就绪：{1}", self.START_TIMEOUT, self.log_tail()))

    def _started(self, client, caps: dict, error: str) -> None:
        if client is None:
            self.client = None
            self._set_state("failed", error)
            return
        self.client, self.capabilities = client, caps or {}
        self._set_state("ready")

    def log_tail(self, lines: int = 6) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        except OSError:
            return ""
        return "\n".join(text[-lines:])

    def describe(self) -> str:
        if self.state == "ready":
            who = f"pid {self.proc.pid}" if self.owned and self.proc else tr("已在运行的实例")
            return tr("运行中（{0}）· 数据目录 {1}", who, self.home)
        if self.state == "starting":
            return tr("启动中…")
        if self.state == "failed":
            return tr("没有启动：{0}", self.reason)
        return tr("已停止")

    def stop(self, wait: float = 6.0) -> None:
        """Shut our child down (its mpv goes with it). An attached backend is left running."""
        self._generation += 1
        client, self.client = self.client, None
        if self.owned and self.proc is not None and self.proc.poll() is None:
            if client is not None:
                try:
                    client.request("POST", "/v1/shutdown")
                except TRANSPORT_ERRORS:
                    pass
            try:
                self.proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                self._kill()
        self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None
        if self.state != "stopped":
            self._set_state("stopped")

    def _kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    def restart(self, config: BackendConfig) -> None:
        self.stop()
        self.start(config)

    def shutdown(self) -> None:
        self._closing = True
        self.stop()
        for lane in self._lanes.values():
            lane.jobs.put(None)
        deadline = time.monotonic() + 3
        for name, lane in self._lanes.items():
            if lane is not threading.current_thread():
                lane.join(max(0, deadline - time.monotonic()))
            if lane.is_alive():
                self.shutdown_incomplete_lanes.append(name)
        QCoreApplication.removePostedEvents(self._bridge, QEvent.MetaCall)

    # ------------------------------------------------------------------ requests
    def _post(self, fn) -> None:
        if not self._closing:
            self._bridge.deliver.emit(lambda: None if self._closing else fn())

    def _fail(self, err, exc: ServiceError) -> None:
        if err is not None:
            self._post(lambda: err(exc))
        else:
            self._post(lambda: self.unhandled.emit(exc))

    def run(self, fn, ok=None, err=None, lane: str = "data") -> None:
        """Run ``fn(client)`` on a worker lane; ``ok(result)`` / ``err(ServiceError)`` on the UI thread."""
        if self._closing:
            return
        client = self.client
        self.activity_serial += 1
        serial = self.activity_serial
        self.pending_activity.add(serial)

        def delivered(value=None, error=None):
            try:
                if error is not None:
                    self.activity_errors.append((serial, error))
                    self.activity_errors[:] = self.activity_errors[-2048:]
                    if err is not None:
                        err(error)
                    else:
                        self.unhandled.emit(error)
                elif ok is not None:
                    ok(value)
            finally:
                self.pending_activity.discard(serial)

        def job():
            if client is None or client is not self.client:
                self._post(lambda: delivered(error=ServiceError("BACKEND_UNAVAILABLE", tr("后端服务没有运行"))))
                return
            try:
                result = fn(client)
            except ServiceError as exc:
                self._post(lambda e=exc: delivered(error=e))
            except TRANSPORT_ERRORS as exc:
                self._post(lambda e=str(exc): delivered(error=ServiceError("TRANSPORT", tr("无法连接后端：{0}", e))))
                self._post(self._check_alive)
            except Exception as exc:
                self._post(lambda e=str(exc): delivered(error=ServiceError("INTERNAL_ERROR", e)))
            else:
                self._post(lambda: delivered(value=result))
        self._lanes[lane].jobs.put(job)

    @staticmethod
    def unwrap(envelope: dict):
        if envelope.get("ok"):
            return envelope.get("result")
        e = envelope.get("error") or {}
        raise ServiceError(str(e.get("code") or "FAILED"), str(e.get("message") or tr("后端拒绝了这个操作")),
                           e.get("details") if isinstance(e.get("details"), dict) else {})

    def query(self, name: str, args: dict | None = None, ok=None, err=None, lane: str = "data") -> None:
        body = json.loads(json.dumps(args or {}))

        def fn(client):
            last = None
            for attempt in range(2):
                try:
                    return self.unwrap(client.query(name, body))
                except TRANSPORT_ERRORS as exc:
                    last = exc
                    time.sleep(0.3)
            raise last
        self.run(fn, ok, err, lane)

    def command(self, name: str, args: dict | None = None, *, revision: int | None = None, ok=None, err=None,
                lane: str = "data", key: str | None = None) -> str:
        """Send a mutation. Payload and key are frozen now; an unclear transport failure is retried with
        exactly the same request (the backend replays its receipt). Playback commands are sent once."""
        body = json.loads(json.dumps(args or {}))
        key = key or uuid.uuid4().hex
        tries = 1 if name.startswith("playback.") else 3

        def fn(client):
            last = None
            for attempt in range(tries):
                try:
                    return self.unwrap(client.command(name, body, expected_revision=revision, idempotency_key=key))
                except TRANSPORT_ERRORS as exc:
                    last = exc
                    time.sleep(0.4 * (attempt + 1))
            raise last
        def acknowledged(value):
            self.commandAcknowledged.emit({"name": name, "arguments": body, "key": key, "result": value})
            if ok is not None:
                ok(value)
        self.run(fn, acknowledged, err, lane)
        return key

    def _check_alive(self) -> None:
        if self.owned and self.proc is not None and self.proc.poll() is not None and self.state == "ready":
            self.client = None
            self._set_state("failed", tr("后端进程意外退出（代码 {0}）：{1}", self.proc.returncode, self.log_tail(3)))

    # ------------------------------------------------------------------ jobs
    def watch(self, job_id: str, progress=None, done=None, failed=None, interval: int = 350) -> "JobWatch":
        return JobWatch(self, job_id, progress, done, failed, interval)

    def call_sync(self, name: str, args: dict | None = None, *, command: bool = False, timeout: float = 10.0):
        """Blocking request for tests and shutdown paths only (never from ordinary UI code)."""
        if self.client is None:
            raise ServiceError("BACKEND_UNAVAILABLE", tr("后端服务没有运行"))
        if command:
            return self.unwrap(self.client.command(name, args or {}, idempotency_key=uuid.uuid4().hex))
        return self.unwrap(self.client.query(name, args or {}))


class JobWatch(QObject):
    """Polls ``jobs.get`` until a job leaves queued/running. An accepted job is not a finished job."""

    ACTIVE = ("queued", "running", "cancel_requested")

    def __init__(self, backend: Backend, job_id: str, progress, done, failed, interval: int):
        super().__init__(backend)
        self.backend, self.job_id = backend, job_id
        self.progress, self.done, self.failed = progress, done, failed
        self.status = "queued"
        self.timer = QTimer(self, singleShot=True, interval=interval, timeout=self._poll)
        self.timer.start(60)

    def _poll(self) -> None:
        self.backend.query("jobs.get", {"id": self.job_id}, ok=self._got, err=self._error, lane="jobs")

    def _got(self, job: dict) -> None:
        self.status = job.get("status", "")
        if self.status in self.ACTIVE:
            if self.progress is not None:
                self.progress(job)
            self.timer.start()
            return
        cb = self.done if self.status == "succeeded" else self.failed
        if cb is not None:
            cb(job)
        self.backend.jobFinished.emit(job)
        self.deleteLater()

    def _error(self, exc: ServiceError) -> None:
        if exc.code in ("TRANSPORT", "BACKEND_UNAVAILABLE") and self.backend.ready:
            self.timer.start(1000)
            return
        if self.failed is not None:
            self.failed({"id": self.job_id, "status": "unknown", "error": str(exc)})
        self.deleteLater()

    def cancel(self) -> None:
        self.backend.command("jobs.cancel", {"id": self.job_id}, lane="jobs", err=lambda e: None)


__all__ = ["Backend", "BackendConfig", "JobWatch", "ServiceError", "backend_home"]
