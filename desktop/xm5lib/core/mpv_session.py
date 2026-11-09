"""Explicitly owned mpv process, acknowledged JSON IPC and decoder-format bridge.

No process or audio starts on import. Real output requires an explicitly selected
device when constructing Session; all automated tests use test_null=True.
"""
from __future__ import annotations

import copy
import json
import os
import queue
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import peq
from .playback_control import BackendCapabilities, PlaybackController, PlaybackError, null_mpv_args


class _Pipe:
    """Windows overlapped named-pipe client, with bounded reads and cancellation."""
    def __init__(self, path: str, timeout: float):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.k = c.WinDLL("kernel32", use_last_error=True)
        pointer = c.c_size_t

        class OVERLAPPED(c.Structure):
            _fields_ = [("Internal", pointer), ("InternalHigh", pointer), ("Offset", w.DWORD),
                        ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]
        self.OVERLAPPED = OVERLAPPED
        self.k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        self.k.CreateFileW.restype = w.HANDLE
        self.k.CreateEventW.argtypes = [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
        self.k.CreateEventW.restype = w.HANDLE
        for name in ("ReadFile", "WriteFile"):
            getattr(self.k, name).argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.POINTER(OVERLAPPED)]
            getattr(self.k, name).restype = w.BOOL
        self.k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.k.WaitForSingleObject.restype = w.DWORD
        self.k.GetOverlappedResult.argtypes = [w.HANDLE, c.POINTER(OVERLAPPED), c.POINTER(w.DWORD), w.BOOL]
        self.k.GetOverlappedResult.restype = w.BOOL
        self.k.CancelIoEx.argtypes = [w.HANDLE, c.POINTER(OVERLAPPED)]
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.handle = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            handle = self.k.CreateFileW(path, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle != c.c_void_p(-1).value:
                self.handle = handle
                return
            error = c.get_last_error()
            if error not in (2, 231):
                raise OSError(error, "Cannot open the owned mpv pipe")
            time.sleep(.025)
        raise TimeoutError("mpv pipe did not become available")

    def _io(self, data: bytes | None, timeout: float) -> bytes | int:
        c, w = self.c, self.w
        if self.handle is None:
            raise EOFError("mpv pipe is closed")
        buffer = c.create_string_buffer(data) if data is not None else c.create_string_buffer(65536)
        size = len(data) if data is not None else 65536
        overlapped, transferred = self.OVERLAPPED(), w.DWORD()
        overlapped.hEvent = self.k.CreateEventW(None, True, False, None)
        if not overlapped.hEvent:
            raise OSError(c.get_last_error(), "Cannot create pipe I/O event")
        try:
            function = self.k.WriteFile if data is not None else self.k.ReadFile
            success = function(self.handle, buffer, size, c.byref(transferred), c.byref(overlapped))
            if not success:
                error = c.get_last_error()
                if error != 997:  # ERROR_IO_PENDING
                    raise OSError(error, "mpv pipe I/O failed")
                result = self.k.WaitForSingleObject(overlapped.hEvent, max(1, int(timeout * 1000)))
                if result != 0:
                    self.k.CancelIoEx(self.handle, c.byref(overlapped))
                    # Keep buffer/OVERLAPPED alive until cancellation completes.
                    self.k.GetOverlappedResult(self.handle, c.byref(overlapped), c.byref(transferred), True)
                    if result == 258:
                        raise TimeoutError("mpv pipe I/O timed out")
                    raise OSError(c.get_last_error(), "mpv pipe wait failed")
                if not self.k.GetOverlappedResult(self.handle, c.byref(overlapped), c.byref(transferred), False):
                    raise OSError(c.get_last_error(), "mpv pipe completion failed")
            if data is not None:
                return transferred.value
            if not transferred.value:
                raise EOFError("mpv pipe disconnected")
            return bytes(buffer.raw[:transferred.value])
        finally:
            self.k.CloseHandle(overlapped.hEvent)

    def read(self) -> bytes:
        return self._io(None, .1)

    def write(self, data: bytes):
        if self._io(data, 5) != len(data):
            raise OSError("Short write to mpv pipe")

    def close(self):
        if self.handle is not None:
            self.k.CancelIoEx(self.handle, None)
            self.k.CloseHandle(self.handle)
            self.handle = None


class _Socket:
    def __init__(self, path: str, timeout: float):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.socket.connect(path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    self.socket.close()
                    raise TimeoutError("mpv socket did not become available")
                time.sleep(.025)
        self.socket.settimeout(.1)

    def read(self) -> bytes:
        data = self.socket.recv(65536)
        if not data:
            raise EOFError("mpv socket disconnected")
        return data

    def write(self, data: bytes):
        self.socket.sendall(data)

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


class Session:
    """Own one headless mpv, with independent reply and decoder-event threads.

    Use command()/play_tracks() to serialize operations. controller is exposed for
    queries/inspection; bypassing Session for competing mutations is unsupported.
    An externally supplied metadata result is never needed for normal playback.
    """
    def __init__(self, mpv_path: str | os.PathLike, output_device: str | None = None, *, test_null: bool = False,
                 timeout: float = 5.0):
        executable = Path(mpv_path).resolve()
        if not executable.is_file():
            raise PlaybackError("missing_engine", "Configured mpv executable does not exist")
        if not isinstance(test_null, bool):
            raise PlaybackError("invalid_test_mode", "test_null must be a Boolean")
        if not test_null and (not isinstance(output_device, str) or not output_device or "\x00" in output_device):
            raise PlaybackError("output_required", "Select an output device explicitly before starting a real-output session")
        if not isinstance(timeout, (float, int)) or isinstance(timeout, bool) or not .1 <= timeout <= 60:
            raise PlaybackError("invalid_timeout", "IPC timeout must be from 0.1 to 60 seconds")
        self.timeout, self.test_null = float(timeout), bool(test_null)
        self._closed = threading.Event()
        self._closing = threading.Event()
        self._pending_lock, self._write_lock = threading.Lock(), threading.RLock()
        self._wire_lock, self._operations = threading.RLock(), threading.RLock()
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._next_id = 0
        self._events: queue.Queue = queue.Queue(maxsize=1024)
        self._expected: tuple[int, str, int] | None = None
        self._source_kinds: dict[str, str] = {}
        self._bridge_error: dict | None = None
        # Frontend addition: playback position for the GUI's progress bar. Filled by the reply
        # thread from observed properties, never by a request, so reading it cannot time out.
        self._progress: dict = {"position_ms": None, "duration_ms": None}
        self._temp = tempfile.TemporaryDirectory(prefix="xm5-mpv-ipc-")
        self.endpoint = rf"\\.\pipe\xm5-{uuid.uuid4().hex}" if os.name == "nt" else str(Path(self._temp.name) / "mpv.sock")
        args = null_mpv_args(executable, self.endpoint)
        if not test_null:
            args = [arg for arg in args if arg != "--ao=null"]
            args.append(f"--audio-device={output_device}")
        self.process, self._io = None, None
        self._reader = self._worker = None
        self.argv = tuple(args)
        caps = BackendCapabilities("mpv-null", True, False, False) if test_null else BackendCapabilities()
        self.controller = PlaybackController(self._send, caps)
        if not test_null:
            self.controller._output["device"] = output_device
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._io = (_Pipe if os.name == "nt" else _Socket)(self.endpoint, self.timeout)
            self._reader = threading.Thread(target=self._read_loop, name="xm5-mpv-replies", daemon=True)
            self._reader.start()
            self._worker = threading.Thread(target=self._event_loop, name="xm5-mpv-formats", daemon=True)
            self._worker.start()
            self._raw(["observe_property", 1, "audio-params"])
            self._raw(["observe_property", 2, "pause"])
            self._raw(["observe_property", 3, "idle-active"])
            self._raw(["observe_property", 4, "time-pos"])
            self._raw(["observe_property", 5, "duration"])
            self._raw(["get_property", "mpv-version"])
            self.controller.get_volume()
        except Exception:
            self.close()
            raise

    def _raw(self, command: list) -> dict:
        if self._closed.is_set():
            raise PlaybackError("session_closed", "mpv session is closed")
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            event, result = threading.Event(), []
            self._pending[request_id] = event, result
        try:
            payload = json.dumps({"command": command, "request_id": request_id}, ensure_ascii=False,
                                 allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
            with self._write_lock:
                self._io.write(payload)
            if not event.wait(self.timeout):
                # Late replies are not reused; an indeterminate mutation poisons the session.
                self._closed.set()
                self._terminate_owned_process()
                raise PlaybackError("ipc_timeout", "mpv did not acknowledge the command in time")
            if isinstance(result[0], Exception):
                raise result[0]
            return result[0]
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _get(self, property_name: str):
        result = self._raw(["get_property", property_name])
        return result.get("data") if result.get("error") == "success" else None

    def _send(self, command: list) -> dict:
        with self._wire_lock:
            if self.test_null and command[:2] in (["set_property", "audio-device"], ["set_property", "audio-exclusive"]):
                return {"error": "output changes are disabled in a null test session"}
            if command[0] != "loadfile":
                return self._raw(command)
            # Remove the previous decoder before loading. A stale old rate cannot
            # satisfy the new generation's metadata gate during a track transition.
            self._expected = None
            self._bridge_error = None
            stop = self._raw(["stop"])
            if stop.get("error") != "success":
                return stop
            deadline = time.monotonic() + self.timeout
            while self._get("idle-active") is not True:
                if time.monotonic() >= deadline:
                    raise PlaybackError("unload_timeout", "Previous decoder did not reach idle")
                time.sleep(.01)
            generation = self.controller.status()["generation"]
            result = self._raw(command)
            if result.get("error") == "success":
                entries = self._get("playlist") or []
                matching = [e for e in entries if e.get("filename") == command[1]]
                if len(matching) != 1 or not isinstance(matching[0].get("id"), int):
                    raise PlaybackError("load_identity", "Cannot bind decoder events to the requested playlist entry")
                self._expected = generation, command[1], matching[0]["id"]
                self._events.put_nowait({"event": "xm5-check-format"})
            return result

    def _read_loop(self):
        buffer = b""
        try:
            while not self._closed.is_set():
                try:
                    buffer += self._io.read()
                except TimeoutError:
                    continue
                if len(buffer) > 1024 * 1024:
                    raise PlaybackError("ipc_oversize", "mpv IPC line exceeds one MiB")
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise PlaybackError("ipc_protocol", "mpv sent an invalid JSON message")
                    if "request_id" in message:
                        with self._pending_lock:
                            pending = self._pending.get(message["request_id"])
                            if pending:
                                pending[1].append(message)
                                pending[0].set()
                    elif "event" in message:
                        if message.get("event") == "property-change" and message.get("name") in ("time-pos", "duration"):
                            value = message.get("data")
                            key = "position_ms" if message["name"] == "time-pos" else "duration_ms"
                            self._progress[key] = int(value * 1000) if isinstance(value, (int, float)) and value >= 0 else None
                            continue
                        self._events.put_nowait(message)
        except Exception as exc:
            if not self._closed.is_set() and not self._closing.is_set():
                self._bridge_error = {"code": "ipc_disconnected", "message": str(exc)}
            self._closed.set()
            self._terminate_owned_process()
            with self._pending_lock:
                for event, result in self._pending.values():
                    if not result:
                        result.append(PlaybackError("ipc_disconnected", str(exc)))
                        event.set()

    def _format_snapshot(self):
        with self._wire_lock:
            expected = self._expected
            if not expected:
                return None
            generation, path, entry_id = expected
            entries = self._get("playlist") or []
            current = [e for e in entries if e.get("current")]
            params = self._get("audio-params")
            actual_path = self._get("path")
            if expected != self._expected or len(current) != 1 or current[0].get("id") != entry_id or actual_path != path:
                return None
            if not isinstance(params, dict) or not isinstance(params.get("samplerate"), int):
                return None
            layout = "stereo" if params.get("channel-count") == 2 and params.get("channels") == "stereo" else str(params.get("channels", "unknown"))
            sample_format = str(params.get("format", ""))
            # DoP is indistinguishable from PCM by amplitude/format alone. Explicit
            # metadata policy must mark it; session never promises DoP passthrough.
            source_kind = "dsd" if sample_format.startswith("dsd") else "pcm"
            if Path(path).suffix.lower() in (".dsf", ".dff") and source_kind == "pcm":
                source_kind = "decoded-dsd"
            source_kind = self._source_kinds.get(path, source_kind)
            return generation, {"sample_rate": params["samplerate"], "channels": layout, "source_kind": source_kind}

    def _event_loop(self):
        while not self._closed.is_set():
            try:
                event = self._events.get(timeout=.05)
            except queue.Empty:
                event = {"event": "xm5-check-format"}
            try:
                with self._operations:
                    status = self.controller.status()
                    if self._expected and (status["awaiting_format"] or event.get("event") == "audio-reconfig" or
                            (event.get("event") == "property-change" and event.get("name") == "audio-params")):
                        snapshot = self._format_snapshot()
                        if snapshot:
                            generation, fmt = snapshot
                            # Repeated unchanged property events must not rebuild
                            # filters or change an explicit pause into playback.
                            if status["format"] != fmt:
                                self.controller.confirm_format(generation, **fmt)
                    if event.get("event") == "property-change" and event.get("name") == "pause":
                        state = self.controller.status()
                        if state["format"] is not None and state["play_requested"] and event.get("data") is False:
                            if self._get("pause") is False:
                                self.controller.observe_transport("playing", state["generation"])
                    elif event.get("event") == "end-file" and event.get("reason") == "error":
                        self.controller.transport("pause")
                        self._bridge_error = {"code": "decode_error", "message": event.get("file_error", "mpv decode failed")}
                    elif event.get("event") == "end-file" and event.get("reason") == "eof" and self._expected:
                        if event.get("playlist_entry_id") == self._expected[2]:
                            state = self.controller.status()
                            if state["play_requested"] and state["index"] + 1 < len(state["queue"]):
                                self.controller.transport("next")
                            else:
                                self._expected = None
                                self.controller.transport("stop")
            except PlaybackError as exc:
                self._bridge_error = exc.to_dict()
                # Preserve the paused failure; do not retry a rejected format forever.
                self._expected = None
            except Exception as exc:
                self._bridge_error = {"code": "format_bridge", "message": str(exc)}
                self._expected = None
                try:
                    self.controller.transport("pause")
                except PlaybackError:
                    pass

    def status(self) -> dict:
        result = self.controller.status()
        result.update(self._progress)
        result.update({"session_closed": self._closed.is_set(), "bridge_error": copy.deepcopy(self._bridge_error),
                       "test_null": self.test_null, "pid": self.process.pid if self.process else None})
        return result

    def play_tracks(self, paths: list, start: int = 0, *, source_kinds: dict[str, str] | None = None) -> dict:
        with self._operations:
            self._bridge_error = None
            if source_kinds is not None:
                if not isinstance(source_kinds, dict) or any(k not in ("pcm", "decoded-dsd", "dsd", "dop", "unknown") for k in source_kinds.values()):
                    raise PlaybackError("invalid_source_kind", "Invalid explicit source-kind mapping")
                self._source_kinds = {str(Path(p).resolve()): kind for p, kind in source_kinds.items()}
            self.controller.replace_queue(paths, start)
            if paths:
                self.controller.transport("play")
            return self.status()

    def output_inventory(self):
        """Read devices without granting output mutation to a NULL discovery session."""
        with self._operations:
            reply=self._raw(['get_property','audio-device-list'])
            if reply.get('error')!='success' or not isinstance(reply.get('data'),list):
                raise PlaybackError('output_inventory_failed','mpv did not return an output inventory')
            return copy.deepcopy(reply['data'])

    def command(self, action: str, arguments: dict | None = None) -> dict | list:
        if arguments is not None and not isinstance(arguments, dict):
            raise PlaybackError("invalid_arguments", "Playback arguments must be an object")
        args = arguments or {}
        required = {"play_tracks": "paths", "queue.replace": "paths", "queue.enqueue": "paths", "peq.set": "preset"}
        if action in required and required[action] not in args:
            raise PlaybackError("invalid_arguments", f"Missing playback argument: {required[action]}")
        with self._operations:
            if self._closed.is_set():
                raise PlaybackError("session_closed", "mpv session is closed")
            if action == "status":
                return self.status()
            if action == "capabilities":
                return self.controller.capabilities()
            if action in ("play_tracks", "queue.replace"):
                return self.play_tracks(args["paths"], args.get("start", 0), source_kinds=args.get("source_kinds")) if action == "play_tracks" else self.controller.replace_queue(args["paths"], args.get("start", 0))
            if action == "queue.enqueue":
                return self.controller.enqueue(args["paths"])
            if action in ("play", "pause", "stop", "next", "previous", "seek"):
                if action == "stop":
                    self._expected = None
                elif action == "play" and self.controller.status()["awaiting_format"] and self._expected is None:
                    # Explicit retry after a rejected format/cleared PEQ must load
                    # a fresh decoder rather than unpause an unconfirmed format.
                    self.controller.transport("stop")
                return self.controller.transport(action, position_ms=args.get("position_ms"))
            if action == "peq.set":
                preset = args["preset"]
                return self.controller.set_peq(peq.Preset.from_dict(preset) if isinstance(preset, dict) else preset)
            if action == "peq.clear":
                return self.controller.set_peq(None)
            if action == "volume.get":
                return {"value": self.controller.get_volume()}
            if action == "volume.set":
                if "value" not in args:
                    raise PlaybackError("invalid_arguments", "Missing playback argument: value")
                return self.controller.set_volume(args["value"])
            if action == "output.list":
                return self.controller.outputs()
            if action == "output.set":
                return self.controller.set_output(args.get("device", "auto"), exclusive=args.get("exclusive", False))
            raise PlaybackError("unsupported_action", f"Unknown playback command: {action}")

    def _terminate_owned_process(self):
        process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            except OSError:
                pass

    def close(self):
        self._closing.set()
        process = self.process
        if process and process.poll() is None:
            try:
                if not self._closed.is_set() and self._io is not None:
                    self._raw(["quit"])
            except Exception:
                pass
        self._closed.set()
        if process and process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        # Process termination completes/cancels outstanding pipe reads first.
        for worker in (self._reader, self._worker):
            if worker and worker is not threading.current_thread():
                worker.join(timeout=1)
        if self._io:
            self._io.close()
            self._io = None
        self._temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
