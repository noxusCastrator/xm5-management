"""Acknowledged mpv commands, local queue and PEQ control without a GUI dependency.

Construction/import never starts a process or audio. The sender is injected and
must return an mpv JSON reply, not a fire-and-forget success placeholder.
"""
from __future__ import annotations

import copy
import json
import math
import os
import threading
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import peq
from .desktop_dsp import CompiledPreset, DSPError, FILTER_LABEL, compile_preset


class PlaybackError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class BackendCapabilities:
    name: str = "mpv"
    peq: bool = True
    output_selection: bool = True
    exclusive_request: bool = True
    hardware_output_verified: bool = False

    @classmethod
    def qt(cls) -> "BackendCapabilities":
        # The Qt fallback has no implemented filter adapter or exclusive mode.
        return cls("qt", False, False, False, False)


class JsonLineSender:
    """Concrete synchronous JSON IPC codec over an already connected transport.

    write(bytes) and readline() must have bounded I/O timeouts in their transport.
    This class creates no endpoint/process and does not execute mpv's run command.
    Events are retained for the owner to drain; callbacks are never re-entered here.
    After timeout/protocol failure, close the connection rather than reusing late
    replies. A Qt UI must dispatch blocking requests in its worker, not the UI loop.
    """
    def __init__(self, write: Callable[[bytes], object], readline: Callable[[], bytes], *, max_lines: int = 1024):
        self.write, self.readline, self.max_lines = write, readline, max_lines
        self.events: deque[dict] = deque(maxlen=1024)
        self.request_id = 0
        self.broken = False
        self._lock = threading.Lock()

    def __call__(self, command: list) -> dict:
        with self._lock:
            if self.broken:
                raise PlaybackError("ipc_closed", "IPC connection must be replaced after an I/O failure")
            self.request_id += 1
            payload = json.dumps({"command": command, "request_id": self.request_id}, ensure_ascii=False,
                                 allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
            try:
                count = self.write(payload)
                if isinstance(count, int) and count != len(payload):
                    raise PlaybackError("ipc_short_write", "IPC request was not written in full")
                for _ in range(self.max_lines):
                    raw = self.readline()
                    if not raw or len(raw) > 1024 * 1024:
                        raise PlaybackError("ipc_read", "IPC closed or reply exceeded the limit")
                    reply = json.loads(raw)
                    if not isinstance(reply, dict):
                        raise PlaybackError("ipc_protocol", "IPC reply must be an object")
                    if "event" in reply:
                        self.events.append(reply)
                    elif reply.get("request_id") == self.request_id:
                        return reply
                    else:
                        raise PlaybackError("ipc_protocol", "Unexpected IPC request ID")
                raise PlaybackError("ipc_limit", "Too many events before an IPC reply")
            except Exception:
                self.broken = True
                raise


def null_mpv_args(executable: str | os.PathLike, ipc_endpoint: str) -> list[str]:
    """Safe argv for an explicitly started, silent mpv test process; never a shell.

    Endpoint is a local mpv IPC pipe/socket supplied by the process owner. This
    helper starts nothing and intentionally cannot select a host audio output.
    """
    if not ipc_endpoint or "\x00" in ipc_endpoint or "\n" in ipc_endpoint or "\r" in ipc_endpoint:
        raise PlaybackError("invalid_endpoint", "A local IPC endpoint is required")
    return [os.fspath(executable), "--config=no", "--load-scripts=no", "--idle=yes", "--pause=yes",
            "--no-video", "--audio-display=no", "--force-window=no", "--terminal=no", "--osc=no",
            "--ytdl=no", "--input-default-bindings=no", "--input-media-keys=no", "--ao=null",
            "--access-references=no", "--autoload-files=no",
            "--audio-fallback-to-null=no", "--volume=100", "--volume-max=100", "--replaygain=no",
            f"--input-ipc-server={ipc_endpoint}"]


class PlaybackController:
    """Serial controller for one owned mpv instance (no competing af/queue writer).

    One local queue entry is loaded at a time, always paused. The owner must call
    confirm_format() with fresh decoder metadata after each load before playback
    can resume. Format generations reject stale asynchronous file-loaded events.
    """
    def __init__(self, sender: Callable[[list], dict], capabilities: BackendCapabilities | None = None):
        self.sender = sender
        self.backend = capabilities or BackendCapabilities()
        self._lock = threading.RLock()
        self._queue: list[str] = []
        self._index: int | None = None
        self._generation = 0
        self._format: dict | None = None
        self._loaded = False
        self._want_play = False
        self._transport = "stopped"
        self._desired: peq.Preset | None = None
        self._compiled: CompiledPreset | None = None
        self._output = {"device": "auto", "exclusive": False, "verified": False}
        self._volume: float | None = None
        self._synchronized = True
        self._error: dict | None = None

    def capabilities(self) -> dict:
        return {**asdict(self.backend), "stereo_peq": self.backend.peq,
                "native_dsd_or_dop_peq": False, "forced_resampling": False,
                "confirmation": "command acknowledgements; no hardware-output validation"}

    def status(self) -> dict:
        with self._lock:
            return {"backend": self.backend.name, "queue": list(self._queue), "index": self._index,
                    "generation": self._generation, "transport": self._transport,
                    "play_requested": self._want_play, "awaiting_format": self._loaded and self._format is None,
                    "format": copy.deepcopy(self._format), "peq": {
                        "requested": self._desired is not None, "preset": self._desired.name if self._desired else None,
                        "configured": self._compiled is not None,
                        "sample_rate": self._compiled.sample_rate if self._compiled else None,
                    "audibly_verified": False}, "output": dict(self._output), "volume": self._volume,
                    "synchronized": self._synchronized, "last_error": copy.deepcopy(self._error)}

    def _command(self, command: list):
        try:
            reply = self.sender(command)
            if not isinstance(reply, dict) or not isinstance(reply.get("error"), str):
                raise PlaybackError("invalid_reply", "Sender must return an acknowledged mpv JSON reply")
            if reply["error"] != "success":
                raise PlaybackError("backend_error", reply["error"], {"command": command[:2]})
            return reply.get("data")
        except PlaybackError as exc:
            self._error = exc.to_dict()
            if exc.code in ("invalid_reply", "ipc_timeout", "ipc_disconnected", "ipc_closed", "session_closed", "unload_timeout", "load_identity"):
                self._synchronized, self._want_play = False, False
            raise
        except Exception as exc:
            error = PlaybackError("transport_error", str(exc), {"command": command[:2]})
            self._error = error.to_dict()
            self._synchronized, self._want_play = False, False
            raise error from exc

    def _require_sync(self):
        if not self._synchronized:
            raise PlaybackError("backend_uncertain", "Replace/reconcile the backend after a failed rollback")

    def _pause(self):
        self._command(["set_property", "pause", True])
        self._transport = "paused" if self._loaded else "stopped"

    def _uncertain(self):
        self._synchronized = False
        self._compiled = None
        self._want_play = False
        try:
            self._pause()
        except PlaybackError:
            self._transport = "unknown"

    def _filters(self, compiled: CompiledPreset | None):
        previous = self._command(["get_property", "af"])
        if not isinstance(previous, list) or not all(isinstance(f, dict) for f in previous):
            raise PlaybackError("invalid_filter_state", "Backend af property is not a native filter list")
        replacement = [f for f in previous if f.get("label") != FILTER_LABEL]
        if compiled:
            replacement.append(compiled.mpv_filter())
        try:
            self._command(["set_property", "af", replacement])
            actual = self._command(["get_property", "af"])
            own = [f for f in actual if f.get("label") == FILTER_LABEL] if isinstance(actual, list) else None
            expected = [compiled.mpv_filter()] if compiled else []
            if own != expected:
                raise PlaybackError("filter_not_confirmed", "Backend did not retain the requested PEQ filter")
        except PlaybackError as original:
            try:
                self._command(["set_property", "af", previous])
                restored = self._command(["get_property", "af"])
                if restored != previous:
                    raise PlaybackError("rollback_mismatch", "Backend filters did not restore")
            except PlaybackError as rollback:
                self._uncertain()
                error = PlaybackError("rollback_failed", "PEQ update failed and backend state is uncertain",
                                      {"original": original.to_dict(), "rollback": rollback.to_dict()})
                self._error = error.to_dict()
                raise error from original
            self._error = original.to_dict()
            raise

    def set_peq(self, preset: peq.Preset | None) -> dict:
        with self._lock:
            self._require_sync()
            if preset is not None and not self.backend.peq:
                raise PlaybackError("unsupported_peq", f"The {self.backend.name} backend has no PEQ implementation")
            candidate = copy.deepcopy(preset)
            compiled = None
            if candidate is not None:
                errors = peq.validate(candidate)
                if errors:
                    raise PlaybackError("invalid_preset", "; ".join(errors))
                if self._format:
                    try:
                        compiled = compile_preset(candidate, **self._format)
                    except DSPError as exc:
                        raise PlaybackError(exc.code, str(exc)) from exc
            if self.backend.peq and (self._format is not None or candidate is None):
                self._filters(compiled)
            self._desired, self._compiled = candidate, compiled
            self._error = None
            return self.status()

    def replace_queue(self, paths: list[str | os.PathLike], start: int = 0) -> dict:
        with self._lock:
            self._require_sync()
            queue = [_local_file(p) for p in paths]
            if isinstance(start, bool) or not isinstance(start, int) or (queue and not 0 <= start < len(queue)) or (not queue and start != 0):
                raise PlaybackError("invalid_index", "Queue start index is out of range")
            self._pause()
            self._command(["stop"])
            self._queue, self._index = queue, start if queue else None
            self._loaded, self._format, self._compiled, self._want_play = False, None, None, False
            self._transport = "stopped"
            if queue:
                self._load_current()
            return self.status()

    def enqueue(self, paths: list[str | os.PathLike]) -> dict:
        with self._lock:
            self._require_sync()
            self._queue.extend([_local_file(p) for p in paths])
            if self._queue and self._index is None:
                self._index = 0
            return self.status()

    def _load_current(self):
        self._pause()
        self._loaded, self._format, self._compiled = False, None, None
        try:
            # Remove rate-dependent coefficients before decoding a different track.
            if self.backend.peq:
                self._filters(None)
            self._generation += 1
            self._command(["loadfile", self._queue[self._index], "replace"])
        except PlaybackError:
            self._transport = "stopped"
            self._want_play = False
            raise
        self._loaded, self._transport = True, "awaiting_format"

    def confirm_format(self, generation: int, sample_rate: int, *, channels: str = "stereo", source_kind: str = "pcm") -> dict:
        with self._lock:
            self._require_sync()
            if not self._loaded or generation != self._generation:
                raise PlaybackError("stale_format", "Decoder metadata belongs to a different load generation")
            self._pause()
            # An observed format change invalidates the old coefficients even if
            # the backend can roll its filter setting back after a failed update.
            self._format, self._compiled = None, None
            fmt = {"sample_rate": sample_rate, "channels": channels, "source_kind": source_kind}
            compiled = None
            if self._desired:
                try:
                    compiled = compile_preset(self._desired, **fmt)
                except DSPError as exc:
                    if self.backend.peq:
                        self._filters(None)
                    self._compiled, self._format, self._want_play = None, None, False
                    self._error = PlaybackError(exc.code, str(exc)).to_dict()
                    raise PlaybackError(exc.code, str(exc)) from exc
                try:
                    self._filters(compiled)
                except PlaybackError:
                    self._want_play = False
                    raise
            self._format, self._compiled = fmt, compiled
            if self._want_play:
                self._command(["set_property", "pause", False])
                self._transport = "play_requested"
            self._error = None
            return self.status()

    def transport(self, action: str, *, position_ms: float | None = None) -> dict:
        with self._lock:
            self._require_sync()
            if action == "play":
                if self._index is None:
                    raise PlaybackError("empty_queue", "There is no selected track")
                if not self._loaded:
                    self._load_current()
                if self._format is not None:
                    self._command(["set_property", "pause", False])
                    self._transport = "play_requested"
                self._want_play = True
            elif action == "pause":
                self._pause()
                self._want_play = False
            elif action == "stop":
                self._command(["stop"])
                self._loaded, self._want_play, self._format, self._compiled = False, False, None, None
                self._transport = "stopped"
            elif action in ("next", "previous"):
                if self._index is None:
                    raise PlaybackError("empty_queue", "There is no selected track")
                new_index = self._index + (1 if action == "next" else -1)
                if not 0 <= new_index < len(self._queue):
                    raise PlaybackError("queue_boundary", "No queue entry in that direction")
                old_index = self._index
                self._index = new_index
                try:
                    self._load_current()
                except PlaybackError:
                    self._index = old_index
                    raise
            elif action == "seek":
                if not self._loaded or isinstance(position_ms, bool) or not isinstance(position_ms, (int, float)) or not math.isfinite(position_ms) or position_ms < 0:
                    raise PlaybackError("invalid_seek", "Seek needs a loaded track and a nonnegative finite position")
                self._command(["seek", position_ms / 1000, "absolute+exact"])
            else:
                raise PlaybackError("unsupported_action", f"Unsupported transport action: {action}")
            return self.status()

    def outputs(self) -> list[dict]:
        with self._lock:
            if not self.backend.output_selection:
                raise PlaybackError("unsupported_output", "Backend output selection is not implemented")
            devices = self._command(["get_property", "audio-device-list"])
            if not isinstance(devices, list) or not all(isinstance(d, dict) and isinstance(d.get("name"), str) for d in devices):
                raise PlaybackError("invalid_outputs", "Backend returned an invalid output list")
            return devices

    def get_volume(self) -> float:
        """Read mpv's native 0..100 volume percentage; no gain-curve substitution."""
        with self._lock:
            self._require_sync()
            value = self._command(["get_property", "volume"])
            if not _valid_volume(value):
                raise PlaybackError("invalid_backend_volume", "Backend volume is not a finite value from 0 to 100")
            self._volume = float(value)
            return self._volume

    def set_volume(self, value: float) -> dict:
        """Set/read back volume, restoring the previous value on a failed update."""
        with self._lock:
            self._require_sync()
            if not _valid_volume(value):
                raise PlaybackError("invalid_volume", "Volume must be a finite number from 0 to 100")
            previous = self.get_volume()
            try:
                self._command(["set_property", "volume", float(value)])
                confirmed = self.get_volume()
                if not math.isclose(confirmed, value, rel_tol=0, abs_tol=1e-3):   # mpv keeps volume as float32
                    raise PlaybackError("volume_not_confirmed", "Backend did not retain the requested volume")
            except PlaybackError as original:
                try:
                    self._command(["set_property", "volume", previous])
                    restored = self.get_volume()
                    if not math.isclose(restored, previous, rel_tol=0, abs_tol=1e-3):
                        raise PlaybackError("rollback_mismatch", "Volume did not restore")
                except PlaybackError as rollback:
                    self._volume = None
                    self._uncertain()
                    error = PlaybackError("rollback_failed", "Volume setting is uncertain",
                                          {"original": original.to_dict(), "rollback": rollback.to_dict()})
                    self._error = error.to_dict()
                    raise error from original
                self._error = original.to_dict()
                raise
            self._error = None
            return self.status()

    def set_output(self, device: str = "auto", *, exclusive: bool = False) -> dict:
        with self._lock:
            self._require_sync()
            if not self.backend.output_selection or (exclusive and not self.backend.exclusive_request):
                raise PlaybackError("unsupported_output", "Requested output capability is not implemented")
            if not isinstance(device, str) or not isinstance(exclusive, bool) or not device or "\x00" in device:
                raise PlaybackError("invalid_output", "Output name and exclusive flag are invalid")
            names = {d["name"] for d in self.outputs()} | {"auto"}
            if device not in names:
                raise PlaybackError("unknown_output", "Select an output from the backend's current device list")
            old_device = self._command(["get_property", "audio-device"])
            old_exclusive = self._command(["get_property", "audio-exclusive"])
            self._pause()
            self._want_play = False
            try:
                self._command(["set_property", "audio-device", device])
                self._command(["set_property", "audio-exclusive", exclusive])
                if self._command(["get_property", "audio-device"]) != device or self._command(["get_property", "audio-exclusive"]) != exclusive:
                    raise PlaybackError("output_not_confirmed", "Backend did not retain the requested output settings")
            except PlaybackError as original:
                try:
                    self._command(["set_property", "audio-device", old_device])
                    self._command(["set_property", "audio-exclusive", old_exclusive])
                    if self._command(["get_property", "audio-device"]) != old_device or self._command(["get_property", "audio-exclusive"]) != old_exclusive:
                        raise PlaybackError("rollback_mismatch", "Output settings did not restore")
                except PlaybackError as rollback:
                    self._uncertain()
                    raise PlaybackError("rollback_failed", "Output settings are uncertain", {"original": original.to_dict(), "rollback": rollback.to_dict()}) from original
                self._error = original.to_dict()
                raise
            self._output = {"device": device, "exclusive": exclusive, "verified": False}
            self._error = None
            return self.status()

    def observe_transport(self, state: str, generation: int) -> dict:
        """Owner forwards engine events; command acknowledgement alone is not playing."""
        with self._lock:
            if generation != self._generation:
                raise PlaybackError("stale_event", "Transport event belongs to an old generation")
            if state not in ("playing", "paused", "stopped"):
                raise PlaybackError("invalid_state", "Invalid observed transport state")
            if state == "playing" and (not self._want_play or self._format is None):
                self._pause()
                raise PlaybackError("unexpected_playback", "Engine played before the format gate was satisfied")
            self._transport = state
            return self.status()


def _local_file(path: str | os.PathLike) -> str:
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or "\x00" in raw or "://" in raw:
        raise PlaybackError("invalid_path", "Playback accepts local file paths only")
    local = Path(raw).expanduser().resolve()
    if local.suffix.lower() in (".m3u", ".m3u8", ".pls", ".cue", ".url", ".asx", ".xspf"):
        raise PlaybackError("playlist_not_audio", "Expand playlists through the library before sending audio paths to the engine")
    if not local.is_file():
        raise PlaybackError("missing_file", f"Local audio file does not exist: {local}")
    return str(local)


def _valid_volume(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (float, int))
            and 0 <= value <= 100 and math.isfinite(value))
