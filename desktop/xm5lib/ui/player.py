"""A basic built-in player: one play queue, transport, volume 0-100 and the XM5 play modes.

This is the simple coupling asked for until the XM5-DAC-specific player exists. The UI only talks to
``Player`` (signals + the methods below); a later player only has to provide the same surface.

    Player.play_tracks(tracks, start)   replace the queue (track dicts with "path") and play from ``start``
    Player.enqueue(tracks) / play_next(tracks)
    toggle() play() pause() stop() next() previous() seek(ms)
    set_volume(0..100) step_volume(+-n) set_mode(mode) cycle_mode()
    signals: stateChanged(str "playing"|"paused"|"stopped"), trackChanged(object track|None),
             positionChanged(int ms), durationChanged(int ms), volumeChanged(int), modeChanged(str),
             queueChanged(), failed(str)

Audio engines, first one that starts wins:
1. ``MpvBackend``: mpv (open source, GPL) as a separate, windowless process -- ``app/vendor/mpv/mpv.exe``
   started with ``--idle --no-video --force-window=no``, driven over its JSON IPC pipe (QLocalSocket).
   It decodes with FFmpeg (FLAC, ALAC/AAC, MP3, WAV/AIFF, DSF/DFF ...) and plays through Windows audio
   (WASAPI, shared or exclusive) to the default or a chosen output.
2. ``QtBackend``: Qt Multimedia in-process (PySide6-Addons, or the files in ``app/vendor/qtmultimedia``).
``XM5LIB_PLAYER=mpv|qt|none`` forces one (tests). Without any, ``Player.available`` is False and
``reason`` says why.
"""
from __future__ import annotations
from .i18n import tr

import importlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QTimer, QUrl, Signal, qVersion
from PySide6.QtNetwork import QLocalSocket

MODES = ("order", "repeat_all", "repeat_one", "shuffle")
MODE_NAMES = {"order": tr("顺序播放"), "repeat_all": tr("列表循环"), "repeat_one": tr("单曲循环"), "shuffle": tr("随机播放")}
VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "qtmultimedia"
MPV_DIR = Path(__file__).resolve().parents[2] / "vendor" / "mpv"

_QTMM = None
_REASON = ""
SOURCE = ""              # "PySide6" or "vendor" once loaded
LOG: list[str] = []      # how the audio stack was found (Settings shows it; tools/check_player.py prints it)

# vendored DLLs in dependency order; loading them by full path first means neither the module nor the
# FFmpeg plugin has to find them through a search path
_PRELOAD = ("avutil-59.dll", "swresample-5.dll", "swscale-8.dll", "avcodec-61.dll", "avformat-61.dll",
            "Qt6Multimedia.dll")


def qtmultimedia():
    """PySide6.QtMultimedia, from PySide6 itself or from the vendored files; None if neither loads."""
    global _QTMM, _REASON, SOURCE
    if _QTMM is not None or _REASON:
        return _QTMM
    try:
        from PySide6 import QtMultimedia
        _QTMM, SOURCE = QtMultimedia, "PySide6"
        LOG.append(f"QtMultimedia from PySide6: {QtMultimedia.__file__}")
        return _QTMM
    except ImportError as exc:
        LOG.append(f"PySide6 has no QtMultimedia ({exc})")
    if sys.platform != "win32" or not (VENDOR / "QtMultimedia.pyd").exists():
        _REASON = tr("没有 Qt Multimedia：PySide6 不带，也没有 {0}", VENDOR)
        LOG.append(_REASON)
        return None
    step = ""
    try:
        import ctypes
        import PySide6
        step = "add_dll_directory"
        os.add_dll_directory(str(VENDOR))
        os.environ["PATH"] = str(VENDOR) + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
        for dll in _PRELOAD:
            step = f"load {dll}"
            ctypes.WinDLL(str(VENDOR / dll))
        step = "import QtMultimedia.pyd"
        if str(VENDOR) not in PySide6.__path__:
            PySide6.__path__.append(str(VENDOR))
        QCoreApplication.addLibraryPath(str(VENDOR / "plugins"))
        importlib.invalidate_caches()
        from PySide6 import QtMultimedia
        _QTMM, SOURCE = QtMultimedia, "vendor"
        LOG.append(f"QtMultimedia from {VENDOR} (plugins: {VENDOR / 'plugins'})")
    except Exception as exc:                               # noqa: BLE001 - any failure means: no player
        _REASON = tr("加载 vendor/qtmultimedia 失败（{0}）：{1}: {2}", step, type(exc).__name__, exc)
        LOG.append(_REASON)
    return _QTMM


def write_tone(path: Path, seconds: float = 1.2, hz: float = 660.0) -> Path:
    """A short stereo 48 kHz test tone (-18 dBFS, faded in/out) for checking the output chain."""
    import math
    import struct
    import wave
    rate = 48000
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / (rate * .05), (n - i) / (rate * .05))
        v = int(32767 * .125 * env * math.sin(2 * math.pi * hz * i / rate))
        frames += struct.pack("<hh", v, v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def gain(volume: int) -> float:
    """0-100 (as shown) -> linear gain; squared, so the lower half of the dial is usable."""
    v = max(0, min(100, volume)) / 100
    return v * v


class QtBackend(QObject):
    """QMediaPlayer + QAudioOutput behind a small interface (so tests can use a fake)."""

    stateChanged = Signal(str)
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    ended = Signal()
    error = Signal(str)

    def __init__(self, mm, parent=None):
        super().__init__(parent)
        self.mm = mm
        self.player = mm.QMediaPlayer(self)
        self.output = mm.QAudioOutput(self)
        self.player.setAudioOutput(self.output)
        self.devices = mm.QMediaDevices(self)
        self.device_id = ""                 # "" = follow the Windows default output
        self.devices.audioOutputsChanged.connect(self._outputs_changed)
        P = mm.QMediaPlayer
        names = {P.PlaybackState.PlayingState: "playing", P.PlaybackState.PausedState: "paused",
                 P.PlaybackState.StoppedState: "stopped"}
        self.player.playbackStateChanged.connect(lambda s: self.stateChanged.emit(names.get(s, "stopped")))
        self.player.positionChanged.connect(lambda ms: self.positionChanged.emit(int(ms)))
        self.player.durationChanged.connect(lambda ms: self.durationChanged.emit(int(ms)))
        self.player.mediaStatusChanged.connect(self._status)
        # an undecodable file reports errorOccurred and InvalidMedia; only the error is passed on
        self.player.errorOccurred.connect(lambda _e, msg: self.error.emit(msg or tr("无法解码此文件")))

    def _status(self, status) -> None:
        if status == self.mm.QMediaPlayer.MediaStatus.EndOfMedia:
            self.ended.emit()

    def load(self, path: str) -> None:
        self.player.setSource(QUrl.fromLocalFile(path))

    def play(self) -> None:
        self.player.play()

    def pause(self) -> None:
        self.player.pause()

    def stop(self) -> None:
        self.player.stop()

    def seek(self, ms: int) -> None:
        self.player.setPosition(max(0, int(ms)))

    def set_gain(self, g: float) -> None:
        self.output.setVolume(g)

    # ---- output devices (Windows audio endpoints)
    def outputs(self) -> list[tuple[str, str, bool]]:
        """[(id, name, is_default)] of the Windows audio outputs Qt sees."""
        default = self.mm.QMediaDevices.defaultAudioOutput()
        return [(bytes(d.id()).hex(), d.description(), d.id() == default.id())
                for d in self.mm.QMediaDevices.audioOutputs()]

    def set_device(self, device_id: str) -> None:
        self.device_id = device_id or ""
        self._apply_device()

    def device_name(self) -> str:
        return self.output.device().description()

    def _apply_device(self) -> None:
        dev = self.mm.QMediaDevices.defaultAudioOutput()
        if self.device_id:
            dev = next((d for d in self.mm.QMediaDevices.audioOutputs() if bytes(d.id()).hex() == self.device_id), dev)
        if dev.id() != self.output.device().id():
            self.output.setDevice(dev)

    def _outputs_changed(self) -> None:
        self._apply_device()                # e.g. the XM5 plugged in as USB DAC and made default


def mpv_exe() -> Path | None:
    """The verified bundled mpv. Installed music applications and PATH are never consulted."""
    from ..core.bundled_runtime import BundledRuntimeError, require_tool
    try:
        return require_tool("mpv")
    except BundledRuntimeError as exc:
        LOG.append(str(exc))
        return None


def _kill_with_parent(proc) -> None:
    """Windows: put mpv in a job that is killed when this process ends, so no orphan keeps playing."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.Basic.LimitFlags = 0x2000                     # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))
        proc._xm5_job = job                                # keep the handle open for our lifetime
    except Exception:                                      # noqa: BLE001 - best effort
        pass


class MpvBackend(QObject):
    """mpv as a windowless audio process, controlled over JSON IPC. Same interface as QtBackend."""

    stateChanged = Signal(str)
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    ended = Signal()
    error = Signal(str)
    outputsChanged = Signal()
    startFailed = Signal(str)          # mpv could not be started at all (the Player then falls back)

    def __init__(self, exe: Path, parent=None):
        super().__init__(parent)
        self.exe = Path(exe)
        self.name = f"xm5lib-mpv-{os.getpid()}"
        self.server = (rf"\\.\pipe\{self.name}" if sys.platform == "win32"
                       else str(Path(tempfile.gettempdir()) / f"{self.name}.sock"))
        self.proc = None
        self.sock = QLocalSocket(self)
        self.sock.connected.connect(self._connected)
        self.sock.readyRead.connect(self._read)
        self.sock.disconnected.connect(self._disconnected)
        self.buf = b""
        self.pending: list[bytes] = []
        self.callbacks: dict[int, object] = {}
        self.req = 0
        self.state = "stopped"
        self.path: str | None = None
        self.paused = False
        self.idle = True
        self.loading = False
        self.gain = 1.0
        self.device_id = ""
        self.exclusive = False
        self.devices: list[dict] = []
        self.version = ""
        self.last_error = ""
        self._tries = 0
        self._connect = QTimer(self, interval=100, timeout=self._try_connect)
        self._poll = QTimer(self, interval=250, timeout=lambda: self._send(["get_property", "time-pos"], self._pos))
        self._spawn()

    # ---- process + pipe
    def _spawn(self) -> None:
        ao = os.environ.get("XM5LIB_MPV_AO") or ("wasapi" if sys.platform == "win32" else "")
        args = [str(self.exe), "--idle=yes", "--no-video", "--force-window=no", "--no-terminal", "--config=no",
                "--load-scripts=no", "--osc=no", "--ytdl=no", "--input-default-bindings=no",
                "--input-media-keys=no", "--audio-display=no", "--keep-open=no", "--gapless-audio=weak",
                "--volume=100", "--volume-max=100", "--audio-client-name=XM5 Library",
                f"--input-ipc-server={self.server}"] + ([f"--ao={ao}"] if ao else [])
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if sys.platform == "win32":
            # no "DLL not found" message box from a broken start: the error mode is inherited by mpv
            import ctypes
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=flags)
        _kill_with_parent(self.proc)
        LOG.append(f"mpv started: {self.exe} (pid {self.proc.pid}, pipe {self.server})")
        self._tries = 0
        self._connect.start()

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _try_connect(self) -> None:
        if self.sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            return
        self._tries += 1
        if not self._alive() or self._tries > 100:          # 10 s
            self._connect.stop()
            code = self.proc.poll() if self.proc else None
            if code is None:
                self.last_error = tr("连不上 mpv 的控制管道")
            elif code & 0xFFFFFFFF == 0xC0000135:
                self.last_error = tr("mpv 没有启动：缺少 DLL（这个 mpv 版本需要显卡驱动带的 vulkan-1.dll）")
            else:
                self.last_error = tr("mpv 没有启动（退出码 {0:#x}）", code & 0xFFFFFFFF)
            LOG.append(self.last_error)
            self.startFailed.emit(self.last_error)
            return
        self.sock.connectToServer(self.name if sys.platform == "win32" else self.server)

    def _connected(self) -> None:
        self._connect.stop()
        LOG.append("mpv control pipe connected")
        lines, self.pending = self.pending, []
        for i, prop in enumerate(("pause", "duration", "idle-active", "audio-device-list"), 1):
            self._send(["observe_property", i, prop])
        self._send(["get_property", "mpv-version"], lambda v, _e: setattr(self, "version", v or ""))
        self._send(["set_property", "volume", 100 * self.gain ** (1 / 3)])
        if self.device_id:
            self._send(["set_property", "audio-device", self.device_id])
        if self.exclusive:
            self._send(["set_property", "audio-exclusive", True])
        for line in lines:
            self.sock.write(line)
        self.sock.flush()

    def _disconnected(self) -> None:
        self._poll.stop()
        if self.state != "stopped":
            self._set_state("stopped")
        if not self._alive():
            LOG.append(f"mpv exited (code {self.proc.poll() if self.proc else '?'})")

    def _ensure(self) -> None:
        if not self._alive() and not self._connect.isActive():
            self.sock.abort()
            self._spawn()

    def _send(self, cmd: list, cb=None) -> None:
        self.req += 1
        if cb is not None:
            self.callbacks[self.req] = cb
        line = (json.dumps({"command": cmd, "request_id": self.req}) + "\n").encode("utf-8")
        if self.sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            self.sock.write(line)
            self.sock.flush()
        else:
            self.pending.append(line)

    def _read(self) -> None:
        self.buf += bytes(self.sock.readAll())
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if "event" in msg:
                self._event(msg)
            elif "request_id" in msg:
                cb = self.callbacks.pop(msg["request_id"], None)
                if cb is not None:
                    cb(msg.get("data"), msg.get("error"))

    # ---- events
    def _set_state(self, st: str) -> None:
        if st != self.state:
            self.state = st
            self.stateChanged.emit(st)
        if st == "playing":
            self._poll.start()
        else:
            self._poll.stop()

    def _pos(self, value, _err) -> None:
        if isinstance(value, (int, float)):
            self.positionChanged.emit(int(value * 1000))

    def _event(self, msg: dict) -> None:
        ev = msg["event"]
        if ev == "property-change":
            name, data = msg.get("name"), msg.get("data")
            if name == "pause":
                self.paused = bool(data)
                if not self.idle and not self.loading:
                    self._set_state("paused" if self.paused else "playing")
            elif name == "duration" and isinstance(data, (int, float)):
                self.durationChanged.emit(int(data * 1000))
            elif name == "idle-active":
                self.idle = bool(data)
                if self.idle and not self.loading:
                    self._set_state("stopped")
            elif name == "audio-device-list" and isinstance(data, list):
                self.devices = data
                self.outputsChanged.emit()
        elif ev == "start-file":
            self.loading = False
        elif ev in ("file-loaded", "playback-restart"):
            self.idle = False
            self._set_state("paused" if self.paused else "playing")
        elif ev == "end-file":
            reason = msg.get("reason")
            if reason == "eof":
                self.ended.emit()
            elif reason == "error":
                self.last_error = msg.get("file_error") or tr("无法播放")
                self.error.emit(self.last_error)
            elif not self.loading:
                self._set_state("stopped")
        elif ev == "shutdown":
            LOG.append("mpv shut down")

    # ---- transport
    def load(self, path: str) -> None:
        self._ensure()
        self.path = path
        self.loading = True
        self._send(["loadfile", path, "replace"])

    def play(self) -> None:
        if self.path is None:
            return
        self._ensure()
        if self.idle and not self.loading:                  # stopped or finished: start the file again
            self.loading = True
            self._send(["loadfile", self.path, "replace"])
        self._send(["set_property", "pause", False])

    def pause(self) -> None:
        self._send(["set_property", "pause", True])

    def stop(self) -> None:
        self.loading = False
        self._send(["stop"])
        self._set_state("stopped")

    def seek(self, ms: int) -> None:
        self._send(["seek", max(0, int(ms)) / 1000, "absolute"])
        self.positionChanged.emit(max(0, int(ms)))

    def set_gain(self, g: float) -> None:
        self.gain = g                                       # mpv's volume is cubic: give it the cube root
        self._send(["set_property", "volume", 100 * g ** (1 / 3)])

    # ---- outputs
    def outputs(self) -> list[tuple[str, str, bool]]:
        return [(d.get("name", ""), d.get("description") or d.get("name", ""), False)
                for d in self.devices if d.get("name") not in (None, "auto")]

    def set_device(self, device_id: str) -> None:
        self.device_id = device_id or ""
        self._send(["set_property", "audio-device", self.device_id or "auto"])

    def device_name(self) -> str:
        if not self.device_id:
            return tr("Windows 默认输出设备")
        return next((d.get("description") or self.device_id for d in self.devices
                     if d.get("name") == self.device_id), self.device_id)

    def set_exclusive(self, on: bool) -> None:
        self.exclusive = bool(on)
        self._send(["set_property", "audio-exclusive", self.exclusive])

    def shutdown(self) -> None:
        self._connect.stop()
        self._poll.stop()
        if self._alive():
            if self.sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
                self._send(["quit"])
                self.sock.waitForBytesWritten(300)
            try:
                self.proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.sock.abort()
        if sys.platform != "win32":
            try:
                os.unlink(self.server)
            except OSError:
                pass


class Player(QObject):
    stateChanged = Signal(str)
    trackChanged = Signal(object)
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    volumeChanged = Signal(int)
    modeChanged = Signal(str)
    queueChanged = Signal()
    failed = Signal(str)
    engineChanged = Signal()

    def __init__(self, backend=None, volume: int = 40, mode: str = "order", device: str = "", parent=None):
        super().__init__(parent)
        self.reason = ""
        if backend is None:
            backend = self._make_backend(os.environ.get("XM5LIB_PLAYER", ""))
        self.backend = backend
        self.queue: list[dict] = []
        self.index = -1
        self.order: list[int] = []      # play order over the queue (shuffled in shuffle mode)
        self.state = "stopped"
        self.position = 0
        self.duration = 0
        self.volume = max(0, min(100, int(volume)))
        self.mode = mode if mode in MODES else "order"
        self._failures = 0
        # a file FFmpeg opens but cannot decode can sit at 0:00 "playing" without an error: skip it
        self._stall = 0
        self._watch = QTimer(self, interval=1000, timeout=self._watchdog)
        self.device = device or ""
        self._wire(backend)

    def _wire(self, backend) -> None:
        if backend is None:
            return
        backend.stateChanged.connect(self._state)
        backend.positionChanged.connect(self._position)
        backend.durationChanged.connect(self._duration)
        backend.ended.connect(self._ended)
        backend.error.connect(self._error)
        if hasattr(backend, "startFailed"):
            backend.startFailed.connect(self._engine_failed)
        if hasattr(backend, "problem"):
            backend.problem.connect(self.failed.emit)
        backend.set_gain(gain(self.volume))
        if self.device and hasattr(backend, "set_device"):
            backend.set_device(self.device)

    def _engine_failed(self, why: str) -> None:
        """mpv did not come up: use Qt Multimedia instead, and carry on with what was asked for."""
        old = self.backend
        self.failed.emit(why)
        fallback = self._make_backend("qt")
        self.reason = why + ("" if fallback is not None else tr("；") + self.reason)
        self.backend = fallback
        if old is not None:
            old.deleteLater()
        self.device = ""                                    # mpv device names mean nothing to Qt
        self._wire(fallback)
        self.engineChanged.emit()
        if fallback is not None and self.current is not None and self.state != "paused":
            self._load_current(play=True)

    def _make_backend(self, force: str):
        """mpv process first, Qt Multimedia second (``force``: "mpv", "qt" or "none")."""
        why = []
        if force in ("", "mpv"):
            exe = mpv_exe()
            if exe is None:
                why.append(tr("没有 mpv（应在 {0}）", MPV_DIR / 'mpv.exe'))
            else:
                try:
                    return MpvBackend(exe, self)
                except Exception as exc:                  # noqa: BLE001
                    why.append(tr("mpv 启动失败：{0}", exc))
        if force in ("", "qt"):
            mm = qtmultimedia()
            if mm is None:
                why.append(_REASON)
            else:
                try:
                    return QtBackend(mm, self)
                except Exception as exc:                  # noqa: BLE001
                    why.append(tr("Qt Multimedia 播放器创建失败：{0}", exc))
        self.reason = tr("；").join(w for w in why if w) or tr("已停用")
        LOG.append(self.reason)
        return None

    # ------------------------------------------------------------------ state
    @property
    def available(self) -> bool:
        return self.backend is not None

    @property
    def engine(self) -> str:
        return {"MpvBackend": "mpv", "QtBackend": "qt", "ServiceEngine": "backend"}.get(type(self.backend).__name__, "test")

    def set_exclusive(self, on: bool) -> None:
        if hasattr(self.backend, "set_exclusive"):
            self.backend.set_exclusive(on)

    def shutdown(self) -> None:
        if hasattr(self.backend, "shutdown"):
            self.backend.shutdown()

    def outputs(self) -> list[tuple[str, str, bool]]:
        return self.backend.outputs() if hasattr(self.backend, "outputs") else []

    def set_output(self, device_id: str) -> None:
        self.device = device_id or ""
        if hasattr(self.backend, "set_device"):
            self.backend.set_device(self.device)

    def output_name(self) -> str:
        return self.backend.device_name() if hasattr(self.backend, "device_name") else ""

    def describe(self) -> str:
        """One line for Settings: where the audio goes, or why there is no player."""
        if not self.available:
            return tr("内置播放器不可用：{0}", self.reason)
        if hasattr(self.backend, "describe"):
            return self.backend.describe()
        out = self.output_name()
        if self.engine == "mpv":
            b = self.backend
            mode = tr("独占") if b.exclusive else tr("共享")
            head = (tr("{0}（独立进程，无界面）· FFmpeg 解码 → Windows 音频（WASAPI {1}模式）", b.version or 'mpv', mode)
                    if b._alive() else tr("mpv 未运行：{0}", b.last_error or tr('启动中')))
        else:
            src = {"PySide6": "PySide6", "vendor": tr("随程序附带")}.get(SOURCE, SOURCE or tr("测试"))
            head = tr("Qt Multimedia {0}（{1}）· FFmpeg 解码 → Windows 音频引擎（WASAPI 共享模式）", qVersion(), src)
        return head + (f" → {out}" if out else "")

    @property
    def current(self) -> dict | None:
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    def _state(self, s: str) -> None:
        if s != self.state:
            self.state = s
            self.stateChanged.emit(s)
        if s == "playing" and self.position == 0:
            self._stall = 0
            self._watch.start()
        elif s != "playing":
            self._watch.stop()

    def _watchdog(self) -> None:
        if self.state != "playing" or self.position > 0:
            self._watch.stop()
            return
        self._stall += 1
        if self._stall >= 5:
            self._watch.stop()
            self._error(tr("5 秒内没有解出任何声音（文件可能已损坏或格式不受支持）"))

    def _position(self, ms: int) -> None:
        self.position = ms
        self.positionChanged.emit(ms)

    def _duration(self, ms: int) -> None:
        self.duration = ms
        self.durationChanged.emit(ms)

    # ------------------------------------------------------------------ queue
    def _reorder(self) -> None:
        n = len(self.queue)
        if self.mode == "shuffle" and n:
            rest = [i for i in range(n) if i != self.index]
            random.shuffle(rest)
            self.order = ([self.index] if 0 <= self.index < n else []) + rest
        else:
            self.order = list(range(n))

    def play_tracks(self, tracks: list[dict], start: int = 0) -> None:
        tracks = [t for t in tracks if t and t.get("path")]
        if not tracks:
            return
        self.queue = list(tracks)
        self.index = max(0, min(start, len(tracks) - 1))
        self._reorder()
        self.queueChanged.emit()
        self._failures = 0
        self._load_current(play=True)

    def enqueue(self, tracks: list[dict]) -> None:
        tracks = [t for t in tracks if t and t.get("path")]
        if not tracks:
            return
        if not self.queue:
            self.play_tracks(tracks)
            return
        start = len(self.queue)
        self.queue += tracks
        new = list(range(start, len(self.queue)))
        if self.mode == "shuffle":
            random.shuffle(new)
        self.order += new
        self.queueChanged.emit()

    def play_next(self, tracks: list[dict]) -> None:
        tracks = [t for t in tracks if t and t.get("path")]
        if not tracks:
            return
        if not self.queue:
            self.play_tracks(tracks)
            return
        at = self.index + 1
        self.queue[at:at] = tracks
        k = len(tracks)
        self.order = [i + k if i >= at else i for i in self.order]
        pos = self.order.index(self.index) + 1 if self.index in self.order else len(self.order)
        self.order[pos:pos] = list(range(at, at + k))
        self.queueChanged.emit()

    def _load_current(self, play: bool) -> None:
        t = self.current
        self.position = 0
        self.duration = int((t or {}).get("duration") or 0) * 1000
        self.trackChanged.emit(t)
        self.positionChanged.emit(0)
        self.durationChanged.emit(self.duration)
        if t is None or self.backend is None:
            return
        self.backend.load(t["path"])
        if play:
            self.backend.play()

    def _step(self, delta: int, wrap: bool) -> bool:
        if not self.queue:
            return False
        if self.index not in self.order:
            self._reorder()
        pos = self.order.index(self.index) + delta
        if not 0 <= pos < len(self.order):
            if not wrap:
                return False
            if self.mode == "shuffle" and delta > 0:
                self._reorder_after_wrap()
                pos = 0
            else:
                pos %= len(self.order)
        self.index = self.order[pos]
        return True

    def _reorder_after_wrap(self) -> None:
        n = len(self.queue)
        order = list(range(n))
        random.shuffle(order)
        if n > 1 and order[0] == self.index:            # do not repeat the last track straight away
            order[0], order[1] = order[1], order[0]
        self.order = order

    # ------------------------------------------------------------------ transport
    def toggle(self) -> None:
        if self.backend is None:
            return
        if self.state == "playing":
            self.backend.pause()
        elif self.current is not None:
            self.backend.play()

    def play(self) -> None:
        if self.backend is not None and self.current is not None:
            self.backend.play()

    def pause(self) -> None:
        if self.backend is not None:
            self.backend.pause()

    def stop(self) -> None:
        if self.backend is not None:
            self.backend.stop()

    def next(self) -> None:
        if self._step(1, wrap=True):
            self._failures = 0
            self._load_current(play=True)

    def previous(self) -> None:
        if self.position > 3000 and self.backend is not None:
            self.backend.seek(0)
            return
        if self._step(-1, wrap=self.mode in ("repeat_all", "shuffle")):
            self._failures = 0
            self._load_current(play=True)
        elif self.backend is not None:
            self.backend.seek(0)

    def seek(self, ms: int) -> None:
        if self.backend is not None and self.current is not None:
            self.backend.seek(ms)

    def _ended(self) -> None:
        if self.mode == "repeat_one":
            self._load_current(play=True)
        elif self._step(1, wrap=self.mode in ("repeat_all", "shuffle")):
            self._load_current(play=True)
        else:                                            # end of the queue in order mode: stay on the last
            if self.backend is not None:
                self.backend.stop()
            self._state("stopped")

    def _error(self, msg: str) -> None:
        t = self.current
        name = Path(t["path"]).name if t else ""
        self.failed.emit(tr("无法播放 {0}：{1}", name, msg))
        self._failures += 1
        if self._failures < len(self.queue) and self._step(1, wrap=self.mode != "order"):
            QTimer.singleShot(0, lambda: self._load_current(play=True))
        else:
            self._failures = 0
            self._state("stopped")

    # ------------------------------------------------------------------ volume / mode
    def set_volume(self, v: int) -> None:
        v = max(0, min(100, int(v)))
        if v == self.volume:
            return
        self.volume = v
        if self.backend is not None:
            self.backend.set_gain(gain(v))
        self.volumeChanged.emit(v)

    def step_volume(self, delta: int) -> None:
        self.set_volume(self.volume + delta)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES or mode == self.mode:
            return
        self.mode = mode
        self._reorder()
        self.modeChanged.emit(mode)

    def cycle_mode(self) -> str:
        self.set_mode(MODES[(MODES.index(self.mode) + 1) % len(MODES)])
        return self.mode


def format_ms(ms: int) -> str:
    s = max(0, int(ms // 1000))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


__all__ = ["Player", "MpvBackend", "QtBackend", "MODES", "MODE_NAMES", "mpv_exe", "qtmultimedia", "gain",
           "format_ms", "write_tone"]
