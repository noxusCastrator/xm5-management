"""Literal-loopback HTTP bridge to the existing Qt script owner.

There is no application/backend constructor or executable input here. A request
is a bounded function-language script, passed to the one LiveConsole on Qt.
"""
from __future__ import annotations

import atexit
import copy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import uuid

from PySide6.QtCore import QObject, Signal, Slot

from ..core.portable import PortablePaths

SCHEMA = "xm5.gui-control.v1"
MAX_SOURCE = 1024 * 1024
MAX_RUNS = 1024                 # Never evict an accepted idempotency record.
TERMINAL = {"succeeded", "failed", "cancelled", "outcome_unknown"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{8,96}$")


def secure(path: Path, directory=False):
    """Owner-only ACL, also on Windows where chmod alone is insufficient."""
    from ..automation.runtime import secure_directory, check_private
    if directory:
        secure_directory(path)
    else:
        if os.name != "nt":
            path.chmod(0o600)
        check_private(path)
    return



class GuiSession(QObject):
    dispatch = Signal(object)

    def __init__(self, win):
        super().__init__(win)
        self.win, self.console = win, win.console
        self.lock = threading.RLock()
        self.runs, self.active = {}, None
        self.uncertain = []
        self.live = True
        self.instance = uuid.uuid4().hex
        self.token = secrets.token_urlsafe(32)
        # Use the actual configuration owner, including explicit portable roots.
        self.directory = PortablePaths(win.prefs.store.root).runtime / "gui-control"
        self.directory.mkdir(parents=True, exist_ok=True)
        secure(self.directory, True)
        from ..automation.runtime import ProfileLock
        self.owner_lock = ProfileLock(self.directory)
        self.discovery = self.directory / "session.json"
        self.receipts = self.directory / "requests"
        secure(self.receipts, True)
        self.dispatch.connect(self._dispatch)
        self.console.finished.connect(self._finished)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "XM5GUI/1"

            def log_message(self, *args):
                pass                    # Never log Authorization or scripts.

            def answer(self, status, value):
                data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def handle_api(self, write=False):
                self.connection.settimeout(5)
                if (self.client_address[0] != "127.0.0.1" or
                    self.headers.get("Host") != owner.host or
                    self.headers.get("Origin") is not None or
                    any(k.lower().startswith("sec-fetch-") for k in self.headers) or
                    self.headers.get("Transfer-Encoding") is not None or
                    len(self.headers.get_all("Authorization", [])) != 1 or
                    not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.token)):
                    return self.answer(403, {"error": {"code": "FORBIDDEN"}})
                if not owner.live:
                    return self.answer(410, {"error": {"code": "SESSION_CLOSED"}})
                body = {}
                if write:
                    try:
                        if self.headers.get_content_type() != "application/json" or len(self.headers.get_all("Content-Length", [])) != 1:
                            raise ValueError("Expected bounded JSON")
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 0 < size <= MAX_SOURCE + 16384:
                            raise ValueError("Request size limit")
                        body = json.loads(self.rfile.read(size).decode("utf-8"))
                        if not isinstance(body, dict):
                            raise ValueError("Expected an object")
                    except (ValueError, OSError, UnicodeError) as exc:
                        return self.answer(400, {"error": {"code": "INVALID_REQUEST", "message": str(exc)}})
                try:
                    status, value = owner.request(self.path, body, write)
                except (ValueError, TypeError) as exc:
                    status, value = 400, {"error": {"code": "INVALID_REQUEST", "message": str(exc)}}
                self.answer(status, value)

            def do_GET(self): self.handle_api()
            def do_POST(self): self.handle_api(True)
            def do_OPTIONS(self): self.answer(403, {"error": {"code": "FORBIDDEN"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.host = "127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True, name="xm5-gui-control")
        self.thread.start()
        value = {"schema": SCHEMA, "url": "http://" + self.host, "token": self.token,
                 "instance_id": self.instance, "pid": os.getpid()}
        temp = self.directory / (self.instance + ".tmp")
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            secure(temp)
            temp.write_text(json.dumps(value), encoding="utf-8", newline="\n")
            os.replace(temp, self.discovery)
        except Exception:
            self.close()
            raise
        finally:
            temp.unlink(missing_ok=True)
        atexit.register(self.close)

    def request(self, path, body, write):
        # Never hold the run ledger lock while waiting for Qt: an earlier queued
        # script submission may need that lock before it can open the dialog.
        if path == "/v1/dialog" and not write:
            result, event = {}, threading.Event()
            self.dispatch.emit(("inspect_dialog", result, event))
            if not event.wait(5):
                return 503, {"error": {"code": "GUI_UNRESPONSIVE", "message": "The Qt owner did not answer dialog inspection"}}
            return 200, result
        with self.lock:
            if path == "/v1/session" and not write:
                return 200, {"schema": SCHEMA, "instance_id": self.instance, "pid": os.getpid(), "active_run": self.active,
                             "uncertain_runs": list(self.uncertain)}
            if path == "/v1/catalog" and not write:
                return 200, self.console.registry.document
            if path == "/v1/dialog" and write:
                rid, identity, choice = body.get("request_id"), body.get("dialog_id"), body.get("choice")
                values = body.get("values", {})
                if (not isinstance(rid, str) or not IDENTIFIER.fullmatch(rid) or not isinstance(identity, str) or
                    not isinstance(choice, str) or not isinstance(values, dict) or len(values) > 64 or
                    any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 32768 for k, v in values.items()) or
                    set(body) - {"request_id", "dialog_id", "choice", "values"}):
                    raise ValueError("Supply an exact inspected dialog ID, button ID/close and bounded text field map")
                digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if rid in self.runs:
                    row = self.runs[rid]
                    return (200, copy.deepcopy(row)) if row["source_sha256"] == digest else (409, {"error": {"code": "IDEMPOTENCY_CONFLICT"}})
                if (self.receipts / (rid + ".json")).exists():
                    return 409, {"error": {"code": "PREVIOUS_GUI_SESSION"}}
                if len(self.runs) >= MAX_RUNS:
                    return 409, {"error": {"code": "SESSION_LEDGER_FULL"}}
                row = {"request_id": rid, "instance_id": self.instance, "source_sha256": digest,
                       "kind": "dialog_action", "status": "queued", "accepted_at": time.time()}
                try:
                    self._persist(row, exclusive=True)
                except OSError as exc:
                    return 503, {"error": {"code": "INTENT_RECORD_FAILED", "message": str(exc)}}
                self.runs[rid] = row
                self.dispatch.emit(("dialog_action", rid, copy.deepcopy(body)))
                return 202, copy.deepcopy(row)
            if path == "/v1/runs" and write:
                rid, source = body.get("request_id"), body.get("source")
                if not isinstance(rid, str) or not IDENTIFIER.fullmatch(rid) or not isinstance(source, str) or not source.strip() or len(source.encode("utf-8")) > MAX_SOURCE:
                    raise ValueError("Supply request_id (8–96 safe characters) and a nonempty script of at most 1 MiB")
                digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
                if rid in self.runs:
                    row = self.runs[rid]
                    return (200, copy.deepcopy(row)) if row["source_sha256"] == digest else (409, {"error": {"code": "IDEMPOTENCY_CONFLICT"}})
                if (self.receipts / (rid + ".json")).exists():
                    return 409, {"error": {"code": "PREVIOUS_GUI_SESSION", "message": "This request ID was accepted in another GUI session. Inspect actual state; it will never execute again under the same ID"}}
                if len(self.runs) >= MAX_RUNS:
                    return 409, {"error": {"code": "SESSION_LEDGER_FULL", "message": "Close and reopen the application after reconciling all runs"}}
                row = {"request_id": rid, "instance_id": self.instance, "source_sha256": digest,
                       "status": "queued", "accepted_at": time.time()}
                # Durable intent precedes scheduling; a process crash cannot turn
                # retry into another execution in the next normally opened GUI.
                try:
                    self._persist(row, exclusive=True)
                except OSError as exc:
                    return 503, {"error": {"code": "INTENT_RECORD_FAILED", "message": str(exc)}}
                self.runs[rid] = row
                self.dispatch.emit(("run", rid, source))
                return 202, copy.deepcopy(row)
            match = re.fullmatch(r"/v1/runs/([A-Za-z0-9_-]{8,96})(/cancel)?", path)
            if match:
                rid, cancel = match.groups()
                row = self.runs.get(rid)
                if row is None:
                    receipt = self.receipts / (rid + ".json")
                    if receipt.exists() and not write:
                        value = json.loads(receipt.read_text(encoding="utf-8"))
                        if value["status"] not in TERMINAL:
                            value["status"] = "outcome_unknown"
                        value["previous_session"] = True
                        return 200, value
                    return 404, {"error": {"code": "RUN_NOT_FOUND"}}
                if cancel and write:
                    if row["status"] not in TERMINAL:
                        row["cancel_requested"] = True
                        self.dispatch.emit(("cancel", rid, None))
                    return 200, copy.deepcopy(row)
                if not cancel and not write:
                    return 200, copy.deepcopy(row)
            return 404, {"error": {"code": "NOT_FOUND"}}

    @Slot(object)
    def _dispatch(self, task):
        kind, rid, source = task
        if kind == "inspect_dialog":
            try:
                rid.update(self._dialog_snapshot())
            except Exception as exc:
                rid.update(error={"code": "DIALOG_INSPECTION_FAILED", "message": str(exc)})
            finally:
                source.set()
            return
        if kind == "dialog_action":
            self._dialog_action(rid, source)
            return
        with self.lock:
            row = self.runs[rid]
            if not self.live:
                return
            if kind == "cancel":
                if self.active == rid:
                    self.console.stop()
                elif row["status"] == "queued":
                    self._terminal(row, status="cancelled", completed=0)
                return
            if row.get("cancel_requested"):
                self._terminal(row, status="cancelled", completed=0)
                return
            if self.console.busy or self.active is not None:
                self._terminal(row, status="failed", completed=0, error={"code": "BUSY", "message": "The window's script owner is already executing a script"})
                return
            if self.uncertain:
                from ..console.language import Parser, ScriptError
                try:
                    steps = Parser(source).script()
                    for _, node in steps:
                        if node[0] == "call" and self.console.registry.resolve(node[1]["name"])["effects"]:
                            self._terminal(row, status="failed", completed=0, error={"code": "OUTCOME_UNKNOWN_GUARD", "message": "Inspect actual state and restart the normal GUI before another mutation; a prior operation has an unknown outcome", "request_ids": list(self.uncertain)})
                            return
                except ScriptError:
                    pass              # Normal whole-script preflight reports precise syntax errors.
            self.active = rid
            row["status"] = "running"
        if not self.console.run(source):
            # Preflight errors emit finished synchronously. Guard unexpected rejection.
            with self.lock:
                if self.active == rid:
                    self._finished({"status": "failed", "error": {"code": "RUN_REJECTED"}, "completed": 0})

    @Slot(object)
    def _finished(self, result):
        with self.lock:
            if self.active is None:
                return
            row = self.runs[self.active]
            self._terminal(row, **copy.deepcopy(result))
            if result.get("status") == "outcome_unknown":
                self.uncertain.append(self.active)
            self.active = None

    def _terminal(self, row, **outcome):
        """Persist every terminal transition, including pre-execution rejection."""
        row.update(outcome, finished_at=time.time())
        try:
            self._persist(row)
        except OSError as exc:
            row["durable_receipt_error"] = str(exc)

    def _dialog_snapshot(self):
        from PySide6.QtWidgets import QApplication, QLineEdit, QAbstractButton, QFileDialog, QMessageBox
        dialog = QApplication.activeModalWidget() or self.win.last_dialog
        if dialog is None or not dialog.isVisible():
            self._dialog_buttons = {}
            return {"dialog": None}
        parent = dialog
        while parent is not None and parent is not self.win:
            parent = parent.parentWidget()
        if parent is None:
            self._dialog_buttons = {}
            return {"dialog": None}
        identity = hashlib.sha256((self.instance + str(id(dialog))).encode()).hexdigest()[:32]
        buttons, handles = [], {}
        for i, button in enumerate(dialog.findChildren(QAbstractButton)):
            if not button.isVisibleTo(dialog):
                continue
            handle = hashlib.sha256((identity + str(i) + button.objectName() + button.text()).encode()).hexdigest()[:32]
            handles[handle] = button
            buttons.append({"id": handle, "text": button.text(), "enabled": button.isEnabled(), "object_name": button.objectName()})
        self._dialog_buttons = handles
        self._inspected_dialog = (identity, dialog)
        fields = [{"id": w.objectName(), "value": w.text(), "read_only": w.isReadOnly()}
                  for w in dialog.findChildren(QLineEdit) if w.objectName() and w.isVisibleTo(dialog)]
        return {"dialog": {"id": identity, "type": type(dialog).__name__, "title": dialog.windowTitle(),
                           "message": dialog.text() if isinstance(dialog, QMessageBox) else None,
                           "fields": fields, "buttons": buttons, "file_dialog": isinstance(dialog, QFileDialog)}}

    def _dialog_action(self, rid, body):
        from PySide6.QtWidgets import QLineEdit, QFileDialog
        with self.lock:
            row = self.runs[rid]
            if not self.live or row.get("cancel_requested"):
                self._terminal(row, status="cancelled", completed=0)
                return
            row["status"] = "running"
        try:
            snapshot = self._dialog_snapshot()["dialog"]
            if snapshot is None or snapshot["id"] != body["dialog_id"]:
                raise ValueError("STALE_DIALOG: Inspect the current dialog again")
            _, dialog = self._inspected_dialog
            edits = {w.objectName(): w for w in dialog.findChildren(QLineEdit) if w.objectName() and w.isVisibleTo(dialog) and not w.isReadOnly()}
            values = body.get("values", {})
            allowed = set(edits) | ({"file", "directory"} if isinstance(dialog, QFileDialog) else set())
            if set(values) - allowed:
                raise ValueError("INVALID_FIELD: Use exact editable field IDs from dialog inspection")
            button = self._dialog_buttons.get(body["choice"])
            if body["choice"] not in ("close", "accept_file") and (button is None or not button.isEnabled()):
                raise ValueError("INVALID_BUTTON: Use an enabled exact inspected button ID or close")
            if body["choice"] == "accept_file" and not isinstance(dialog, QFileDialog):
                raise ValueError("INVALID_BUTTON: accept_file only applies to a file dialog")
            for name, value in values.items():
                if name == "file" and isinstance(dialog, QFileDialog): dialog.selectFile(value)
                elif name == "directory" and isinstance(dialog, QFileDialog): dialog.setDirectory(value)
                else: edits[name].setText(value)
            if body["choice"] == "close": dialog.reject()
            elif body["choice"] == "accept_file": dialog.accept()
            else: button.click()
            try:
                closed = not dialog.isVisible()
            except RuntimeError:
                closed = True     # Qt deleted the closed dialog.
            outcome = {"status": "succeeded", "completed": 1, "result": {"dialog_id": body["dialog_id"], "closed": closed}}
        except Exception as exc:
            outcome = {"status": "failed", "completed": 0, "error": {"code": "DIALOG_ACTION_FAILED", "message": str(exc)}}
        with self.lock:
            self._terminal(row, **outcome)

    def _persist(self, row, exclusive=False):
        value = {k: row[k] for k in ("request_id", "instance_id", "source_sha256", "kind", "status", "accepted_at", "finished_at", "completed") if k in row}
        if isinstance(row.get("error"), dict):
            value["error"] = {k: row["error"][k] for k in ("code", "message") if k in row["error"]}
        target = self.receipts / (row["request_id"] + ".json")
        if exclusive:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                json.dump(value, output)
                output.flush()
                os.fsync(output.fileno())
        else:
            temp = target.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8", newline="\n") as output:
                json.dump(value, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, target)

    def close(self):
        with self.lock:
            if not self.live:
                return
            self.live = False
            for row in self.runs.values():
                if row["status"] not in TERMINAL:
                    self._terminal(row, status="cancelled" if row["status"] == "queued" else "outcome_unknown",
                                   error={"code": "SESSION_CLOSED", "message": "Inspect actual state before issuing a new mutation"})
            try:
                current = json.loads(self.discovery.read_text(encoding="utf-8"))
                if current.get("instance_id") == self.instance:
                    self.discovery.unlink()
            except (OSError, ValueError):
                pass
        self.server.shutdown()
        self.server.server_close()
        atexit.unregister(self.close)
        self.owner_lock.close()
