"""Playback through the backend's own mpv session, behind the engine interface ``Player`` drives.

The backend owns playback (docs/FRONTEND-HANDOFF.md): the GUI never runs its own mpv while this engine
is in use. ``Player`` keeps the queue and the XM5 play modes; this engine loads one song at a time
(``playback.command play_tracks`` with the song's library ID), polls ``playback.status`` for transport and
position, and turns the end of a song into ``ended``.

* Output: the backend never picks the computer's default speaker by itself. Until an output has been
  chosen, the first play asks (``MainWindow.choose_output``); "Windows 默认输出设备" is offered there as an
  explicit choice.
* Playback commands are sent once and never retried automatically (an unclear IPC outcome must be
  inspected, not repeated).
* PEQ preview: ``set_peq(name)`` applies a saved preset to desktop playback (``peq.set``).
"""
from __future__ import annotations

import os

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout

from .player import Player, mpv_exe
from .i18n import tr

STATE = {"playing": "playing", "play_requested": "playing", "paused": "paused", "stopped": "stopped",
         "unknown": "paused"}
LOST = ("PLAYBACK_UNAVAILABLE", "SESSION_CLOSED", "PLAYBACK_SESSION_CHANGED", "BACKEND_UNCERTAIN", "IPC_TIMEOUT",
        "IPC_DISCONNECTED", "BACKEND_UNAVAILABLE")
NULL_DEVICE = "@null"          # tests: the backend's silent output (needs --allow-null-playback)


class ServiceEngine(QObject):
    stateChanged = Signal(str)
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    ended = Signal()
    error = Signal(str)            # this song cannot be played (Player skips it)
    problem = Signal(str)          # any other failed command (shown, nothing skipped)
    outputsChanged = Signal()
    peqChanged = Signal()

    def __init__(self, win, parent=None):
        super().__init__(parent or win)
        self.win, self.backend = win, win.backend
        self.connected = self.connecting = False
        self._after: list = []
        self.state = "stopped"
        self.transport = "stopped"
        self.path: str | None = None
        self.need_load = False
        self.gain = 1.0
        self._vol_sent = None
        self._vol_busy = False
        self.devices: list[dict] = []
        self.device_id = str(win.prefs.get("player_device") or "")
        self.exclusive = bool(win.prefs.get("player_exclusive"))
        self.status: dict = {}
        self.last_error = ""
        self.peq_name: str | None = None
        self._dur = 0
        self._last_bridge = None
        self._polling = False
        self._poll = QTimer(self, interval=250, timeout=self._tick)
        self.backend.stateChanged.connect(self._backend_state)

    # ------------------------------------------------------------------ session
    def _backend_state(self, st: str) -> None:
        if st != "ready" and (self.connected or self.connecting):
            self.connected = self.connecting = False
            self._after.clear()
            self._poll.stop()
            self.peq_name = None
            self.peqChanged.emit()
            if self.state != "stopped":
                self._set_state("stopped")
                self.need_load = True

    def _ensure(self, then) -> None:
        if self.connected:
            then()
            return
        self._after.append(then)
        if self.connecting:
            return
        if not self.backend.ready:
            self._after.clear()
            self.problem.emit(tr("后端服务没有运行，不能播放。"))
            return
        if not self.device_id:
            device = self.win.choose_output(self)
            if not device:
                self._after.clear()
                self.problem.emit(tr("没有选择输出设备，没有播放。"))
                return
            self.set_device_pref(device)
        self.connecting = True
        args = {"test_null": True} if self.device_id == NULL_DEVICE else {"output_device": self.device_id}

        def ok(status):
            self.connecting, self.connected = False, True
            self._vol_sent = None
            self._apply(status, mine=True)
            if self.exclusive and self.device_id != NULL_DEVICE:
                self._cmd("output.set", {"device": self.device_id, "exclusive": True}, ok=lambda s: self._apply(s, True))
            self._push_volume()
            self._poll.start()
            waiting, self._after = self._after, []
            for fn in waiting:
                fn()

        def failed(e):
            if e.code == "PLAYBACK_CONNECTED":            # a session from before (attached backend): use it
                ok(self.status or {})
                return
            self.connecting = False
            self._after.clear()
            self.problem.emit(tr("连接播放输出失败：{0}", e.message))
        self.backend.command("playback.connect", args, ok=ok, err=failed, lane="playback")

    def _cmd(self, action: str, arguments: dict | None = None, ok=None, err=None) -> None:
        def failed(e):
            if e.code in LOST:
                self.connected = False
                self._poll.stop()
            (err or (lambda x: self.problem.emit(tr("播放命令没有完成：{0}", x.message))))(e)
        self.backend.command("playback.command", {"action": action, "arguments": arguments or {}},
                             ok=ok, err=failed, lane="playback")

    def set_device_pref(self, device: str) -> None:
        self.device_id = device
        if device != NULL_DEVICE:
            self.win.prefs.set("player_device", device)

    # ------------------------------------------------------------------ status
    def _tick(self) -> None:
        if not self.connected or self._polling:
            return
        self._polling = True
        self.backend.query("playback.status", ok=self._polled, err=self._poll_failed, lane="playback")
        self._poll.setInterval(250 if self.state == "playing" else 1000)

    def _polled(self, st) -> None:
        self._polling = False
        self._apply(st, mine=False)

    def _poll_failed(self, e) -> None:
        self._polling = False
        if e.code in LOST or not self.backend.ready:
            self.connected = False
            self._poll.stop()
            if self.state != "stopped":
                self._set_state("stopped")

    def _set_state(self, s: str) -> None:
        if s != self.state:
            self.state = s
            self.stateChanged.emit(s)

    def _apply(self, st, mine: bool) -> None:
        """Mirror a status snapshot. ``mine``: the reply to our own command (no end-of-song guess)."""
        if not isinstance(st, dict) or "transport" not in st:
            return
        self.status = st
        if st.get("session_closed"):
            self.connected = False
            self._poll.stop()
            self._set_state("stopped")
            self.need_load = True
            self.problem.emit(tr("播放进程断开了；再按播放会重新连接。"))
            return
        pos, dur = st.get("position_ms"), st.get("duration_ms")
        if isinstance(pos, int):
            self.positionChanged.emit(pos)
        if isinstance(dur, int) and dur > 0 and dur != self._dur:
            self._dur = dur
            self.durationChanged.emit(dur)
        bridge = st.get("bridge_error")
        if bridge != self._last_bridge:
            self._last_bridge = bridge
            if bridge:
                self.last_error = bridge.get("message") or bridge.get("code") or tr("播放出错")
                if bridge.get("code") in ("decode_error",):
                    self.error.emit(self.last_error)
                    return
                self.problem.emit(tr("播放出错：{0}", self.last_error))
        tport = st.get("transport")
        before, self.transport = self.transport, tport
        if not mine and tport == "stopped" and before in ("playing", "play_requested"):
            self._set_state("stopped")
            self.need_load = True                      # the song is unloaded at its end
            self.ended.emit()
            return
        if tport == "awaiting_format":
            new = "playing" if st.get("play_requested") else "paused"
        else:
            new = STATE.get(tport, "stopped")
        if tport == "stopped":
            self.need_load = True
        self._set_state(new)

    # ------------------------------------------------------------------ transport (Player's engine API)
    def load(self, path: str) -> None:
        self.path = path
        self.need_load = True
        self._dur = 0

    def play(self) -> None:
        if self.path is None:
            return

        def go():
            if self.need_load:
                tid = self.win.state.id_for(self.path)
                if tid is None:
                    self.error.emit(tr("这首歌不在曲库里（后端只播放曲库中的歌曲）"))
                    return
                self.need_load = False
                self._cmd("play_tracks", {"track_ids": [tid], "start": 0}, ok=lambda s: self._apply(s, True),
                          err=self._load_failed)
            else:
                self._cmd("play", ok=lambda s: self._apply(s, True))
        self._ensure(go)

    def _load_failed(self, e) -> None:
        self.need_load = True
        if e.code in LOST:
            self.problem.emit(tr("播放输出断开了：{0}", e.message))
            self._set_state("stopped")
            return
        self.error.emit(e.message)

    def pause(self) -> None:
        if self.connected:
            self._cmd("pause", ok=lambda s: self._apply(s, True))

    def stop(self) -> None:
        if self.connected:
            self._cmd("stop", ok=lambda s: self._apply(s, True))
        self._set_state("stopped")

    def seek(self, ms: int) -> None:
        if self.connected and not self.need_load:
            self._cmd("seek", {"position_ms": max(0, int(ms))}, ok=lambda s: self._apply(s, True))
            self.positionChanged.emit(max(0, int(ms)))

    def set_gain(self, g: float) -> None:
        self.gain = g
        self._push_volume()

    def _push_volume(self) -> None:
        if not self.connected or self._vol_busy:
            return
        # mpv's volume is cubic, and kept as float32: send a value it stores exactly (1/64 steps)
        value = round(100 * max(0.0, self.gain) ** (1 / 3) * 64) / 64
        if value == self._vol_sent:
            return
        self._vol_busy = True

        def done(_s):
            self._vol_busy = False
            self._vol_sent = value
            self._push_volume()

        def failed(e):
            self._vol_busy = False
            self.problem.emit(tr("音量没有设置：{0}", e.message))
        self._cmd("volume.set", {"value": value}, ok=done, err=failed)

    # ------------------------------------------------------------------ outputs
    def refresh_outputs(self) -> None:
        if not self.backend.ready:
            return

        def got(devices):
            self.devices = [d for d in devices if isinstance(d, dict) and d.get("name")] if isinstance(devices, list) else []
            self.outputsChanged.emit()
        self.backend.query("playback.outputs", ok=got, err=lambda e: None, lane="playback")

    def outputs(self) -> list[tuple[str, str, bool]]:
        return [(d["name"], d.get("description") or d["name"], False) for d in self.devices if d["name"] != "auto"]

    def set_device(self, device_id: str) -> None:
        device_id = device_id or "auto"
        self.set_device_pref(device_id)
        if self.connected and device_id != NULL_DEVICE:
            self._cmd("output.set", {"device": device_id, "exclusive": self.exclusive},
                      ok=lambda s: self._apply(s, True))

    def device_name(self) -> str:
        if not self.device_id:
            return tr("（未选择，第一次播放时询问）")
        if self.device_id == "auto":
            return tr("Windows 默认输出设备")
        if self.device_id == NULL_DEVICE:
            return tr("静音测试输出")
        return next((d.get("description") or d["name"] for d in self.devices if d["name"] == self.device_id),
                    self.device_id)

    def set_exclusive(self, on: bool) -> None:
        self.exclusive = bool(on)
        if self.connected and self.device_id not in ("", NULL_DEVICE):
            self._cmd("output.set", {"device": self.device_id, "exclusive": self.exclusive},
                      ok=lambda s: self._apply(s, True))

    # ------------------------------------------------------------------ PEQ preview
    def set_peq(self, name: str | None, ok=None, err=None) -> None:
        def go():
            def done(st):
                self.peq_name = name
                self._apply(st, True)
                self.peqChanged.emit()
                if ok:
                    ok()

            def failed(e):
                self.peqChanged.emit()
                (err or (lambda x: self.problem.emit(tr("PEQ 没有应用：{0}", x.message))))(e)
            if name:
                self._cmd("peq.set", {"preset_name": name}, ok=done, err=failed)
            else:
                self._cmd("peq.clear", ok=done, err=failed)
        self._ensure(go)

    # ------------------------------------------------------------------ misc
    def describe(self) -> str:
        if not self.backend.ready:
            return tr("后端 mpv：后端没有运行（{0}）", self.backend.describe())
        mode = tr("独占") if self.exclusive else tr("共享")
        link = tr("已连接") if self.connected else tr("未连接（播放时连接）")
        peq = tr(" · 电脑端 PEQ：{0}", self.peq_name) if self.peq_name else ""
        return (tr("后端 mpv（独立进程，无界面）· FFmpeg 解码 → Windows 音频（WASAPI {0}模式）· {1} → {2}{3}", mode, link, self.device_name(), peq))

    def shutdown(self) -> None:
        self._poll.stop()


class OutputChooser(QDialog):
    """Asks once where the sound should go; the backend never picks the default speaker on its own."""

    def __init__(self, parent, backend, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle(tr("选择播放输出"))
        self.setModal(True)
        self.setMinimumWidth(520)
        self.choice: str | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        title = QLabel(tr("声音从哪里出来？"))
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        lay.addWidget(title)
        info = QLabel(tr("第一次播放前请明确选一个输出设备（例如接在电脑上的 XM5）。以后可以在「设置 › 播放器」里更改。"))
        info.setWordWrap(True)
        info.setProperty("muted", True)
        lay.addWidget(info)
        self.list = QListWidget()
        self.list.setObjectName("groupList")
        self.list.setMinimumHeight(220)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        lay.addWidget(self.list, 1)
        self.status = QLabel(tr("正在读取设备列表…"))
        self.status.setProperty("muted", True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("取消"))
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton(tr("用这个设备播放"))
        self.ok.setObjectName("primary")
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        lay.addLayout(row)
        self.current = current
        backend.query("playback.outputs", ok=self._fill, lane="playback",
                      err=lambda e: self.status.setText(tr("读不到设备列表：{0}", e.message)))

    def _fill(self, devices) -> None:
        self.list.clear()
        pick = None
        for d in devices if isinstance(devices, list) else []:
            name = d.get("name") if isinstance(d, dict) else None
            if not name:
                continue
            desc = d.get("description") or name
            label = tr("Windows 默认输出设备（跟随系统）") if name == "auto" else desc
            it = QListWidgetItem(label)
            it.setData(256, name)
            it.setToolTip(name)
            self.list.addItem(it)
            hay = f"{name} {desc}".casefold()
            if name == self.current or (pick is None and ("xm5" in hay or "onix" in hay)):
                pick = it
        if pick is not None:
            self.list.setCurrentItem(pick)
        self.status.setText(tr("{0} 个设备", self.list.count()) + (tr("（已选中看起来是 XM5 的那个）") if pick is not None else ""))
        self.ok.setEnabled(self.list.count() > 0)
        self.list.currentItemChanged.connect(lambda it, _: self.ok.setEnabled(it is not None))

    def accept(self) -> None:
        it = self.list.currentItem()
        if it is None:
            return
        self.choice = it.data(256)
        super().accept()


def make_player(win) -> Player:
    """The backend's mpv when there is one (the normal case), otherwise the local engines."""
    prefs = win.prefs
    kw = dict(volume=int(prefs.get("player_volume") or 40), mode=str(prefs.get("player_mode") or "order"), parent=win)
    force = os.environ.get("XM5LIB_PLAYER", "")
    if force in ("", "backend") and mpv_exe() is not None:
        return Player(backend=ServiceEngine(win), **kw)
    p = Player(device=str(prefs.get("player_device") or ""), **kw)
    p.set_exclusive(bool(prefs.get("player_exclusive")))
    return p


__all__ = ["ServiceEngine", "OutputChooser", "make_player", "NULL_DEVICE"]
