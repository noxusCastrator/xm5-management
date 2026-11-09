"""CLI client. Never imports Qt or starts a window/backend/profile."""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import re
import sys
import time
import uuid

from ..core.portable import PortablePaths
from .session_constants import SCHEMA, TERMINAL

MAX_SOURCE = 1024 * 1024


class InputRejected(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(code + ": " + message)


class ApiRejected(RuntimeError):
    def __init__(self, status, error):
        self.http_status = status
        self.code = str(error.get("code", status))
        super().__init__(self.code + ": " + str(error.get("message", "Request rejected")))


def source_bytes(data, *, file=False):
    if len(data) > MAX_SOURCE:
        raise InputRejected("INPUT_TOO_LARGE", "A script is limited to 1 MiB")
    encoding = "utf-16" if file and data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        return data.decode(encoding, errors="strict")
    except UnicodeError as exc:
        raise InputRejected("INPUT_ENCODING_ERROR", "Script input must be strict UTF-8 (files may also have a UTF-16 BOM); no request was submitted") from exc


def validate_source(source):
    try:
        raw = source.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise InputRejected("INPUT_ENCODING_ERROR", "Script contains an unpaired surrogate; no request was submitted") from exc
    if not source.strip():
        raise InputRejected("EMPTY_SCRIPT", "Supply a nonempty function-language script")
    if len(raw) > MAX_SOURCE:
        raise InputRejected("INPUT_TOO_LARGE", "A script is limited to 1 MiB")
    return source


class GuiClient:
    def __init__(self, root=None):
        paths = PortablePaths(Path(root).resolve()) if root is not None else PortablePaths.discover()
        self.path = paths.runtime / "gui-control/session.json"
        try:
            from ..automation.runtime import check_private
            check_private(self.path)
            data = json.loads(self.path.read_text(encoding="utf-8"))
            match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", data["url"])
            if data["schema"] != SCHEMA or not match or not isinstance(data["token"], str):
                raise ValueError("Invalid GUI discovery record")
            self.port, self.token, self.instance = int(match[1]), data["token"], data["instance_id"]
            if not 1 <= self.port <= 65535:
                raise ValueError("Invalid port")
            value = self.request("GET", "/v1/session")
            if value.get("instance_id") != self.instance:
                raise ValueError("GUI session changed")
        except (OSError, KeyError, ValueError, http.client.HTTPException) as exc:
            raise RuntimeError("NO_OPEN_GUI: Open XM5 Library normally. Stale discovery is not an active session; this command never starts one.") from exc

    def request(self, method, path, body=None):
        try:
            payload = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
        except (UnicodeError, ValueError) as exc:
            raise InputRejected("INPUT_ENCODING_ERROR", "Request is not strict UTF-8 JSON; no HTTP request was submitted") from exc
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        try:
            connection.request(method, path, payload, {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
            response = connection.getresponse()
            value = json.loads(response.read(32 * 1024 * 1024))
            if response.status >= 400:
                error = value.get("error", {})
                raise ApiRejected(response.status, error)
            return value
        finally:
            connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Control the existing XM5 desktop window with its function language")
    parser.add_argument("script", nargs="?", help="Function-style script; quote it in PowerShell")
    parser.add_argument("--file", type=Path, help="UTF-8/UTF-16 script file")
    parser.add_argument("--stdin", action="store_true", help="Read function-style text from stdin")
    parser.add_argument("--root", type=Path, help="Existing portable application root, never created")
    parser.add_argument("--request-id", help="Idempotency key; reuse the same key and exact script after a transport failure")
    parser.add_argument("--status", help="Inspect an accepted request ID in this GUI session")
    parser.add_argument("--cancel", help="Request cancellation of this request ID")
    parser.add_argument("--catalog", action="store_true")
    parser.add_argument("--dialog", action="store_true", help="Inspect the active desktop modal dialog, including while a script is waiting")
    parser.add_argument("--answer-dialog", help="JSON with exact dialog_id, choice button ID/close and optional values map")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    rid = args.request_id or uuid.uuid4().hex
    admission = "not_submitted"
    operation = "status" if args.status else "cancel" if args.cancel else "catalog" if args.catalog else "dialog" if args.dialog else "answer_dialog" if args.answer_dialog else "script"
    try:
        client = GuiClient(args.root)
        if args.catalog:
            result = client.request("GET", "/v1/catalog")
        elif args.dialog:
            result = client.request("GET", "/v1/dialog")
        elif args.answer_dialog:
            body = json.loads(args.answer_dialog)
            if not isinstance(body, dict): raise ValueError("--answer-dialog expects a JSON object")
            body["request_id"] = rid
            print(json.dumps({"request_id": rid, "instance_id": client.instance}), file=sys.stderr, flush=True)
            admission = "unknown"
            result = client.request("POST", "/v1/dialog", body)
            admission = "accepted"
        elif args.status or args.cancel:
            rid = args.status or args.cancel
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", rid):
                raise ValueError("Invalid request ID")
            if args.cancel: admission = "unknown"
            result = client.request("POST" if args.cancel else "GET", "/v1/runs/" + rid + ("/cancel" if args.cancel else ""), {} if args.cancel else None)
            if args.cancel: admission = "accepted"
        else:
            if sum(bool(x) for x in (args.script is not None, args.file, args.stdin)) != 1:
                raise ValueError("Supply one script, --file, or --stdin")
            if args.file:
                with args.file.open("rb") as file:
                    source = source_bytes(file.read(MAX_SOURCE + 1), file=True)
            else:
                # Embedded Windows Python may default to GBK + surrogateescape.
                # PowerShell 7 pipes UTF-8, so decoding through sys.stdin.read()
                # corrupts valid Unicode before JSON encoding. Read strict bytes.
                source = source_bytes(sys.stdin.buffer.read(MAX_SOURCE + 1)) if args.stdin else args.script
            source = validate_source(source)
            print(json.dumps({"request_id": rid, "instance_id": client.instance}, ensure_ascii=False), file=sys.stderr, flush=True)
            admission = "unknown"
            result = client.request("POST", "/v1/runs", {"request_id": rid, "source": source})
            admission = "accepted"
        deadline = time.monotonic() + args.timeout
        while not args.no_wait and not args.status and not args.cancel and result.get("status") in ("queued", "running"):
            if time.monotonic() >= deadline:
                raise TimeoutError("OUTCOME_UNKNOWN: Query --status " + rid + "; do not submit a new request ID")
            time.sleep(.05)
            result = client.request("GET", "/v1/runs/" + rid)
        # JSON escapes preserve Unicode without relying on a PowerShell 5
        # terminal/native-output code page to decode UTF-8 characters correctly.
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0 if result.get("status") not in {"failed", "outcome_unknown", "cancelled"} else 1
    except (RuntimeError, OSError, ValueError, http.client.HTTPException) as exc:
        if isinstance(exc, InputRejected) or admission == "not_submitted":
            status, effect = "not_accepted", "none"
            retry = "No new request was accepted. Correct the input/encoding or connection and submit again; a status lookup is unnecessary for this local rejection."
        elif isinstance(exc, ApiRejected) and admission != "accepted" and (exc.http_status < 500 or exc.code == "INTENT_RECORD_FAILED"):
            status, effect = "rejected", "none"
            retry = "The server rejected this submission before admission. Correct the reported input; for a reused ID inspect its existing receipt."
        else:
            status, effect = admission, "unknown"
            retry = "Inspect --status for this ID in the same session; never blindly issue a new mutation ID."
        print(json.dumps({"error": str(exc), "code": getattr(exc, "code", type(exc).__name__), "operation": operation,
                          "request_id": rid, "admission_status": status, "effect_of_submission": effect, "retry": retry}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
