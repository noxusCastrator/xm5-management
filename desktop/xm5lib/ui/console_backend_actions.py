"""Data commands for the existing window console and its existing Backend lanes.

No window/profile/server is created here. Plans are immutable backend records;
source backups are backend receipts, never entries in the GUI undo stack.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
import math
import json
from pathlib import Path
import re

from ..console.language import ScriptError
from .service import Backend, ServiceError


class ConsoleBackendActions:
    ASYNC_COMMANDS = frozenset({
        "capture_tracks", "format_text", "preview_names", "import_actions", "export_metadata",
        "library_health", "storage_transactions", "storage_plan", "set_peak_options",
        "preview_cover", "read_lyrics", "preview_lyrics", "inspect_peace", "card_playlists",
        "read_tags", "inspect_device_library",
    })

    @staticmethod
    def _service_error(exc, **details):
        code = str(getattr(exc, "code", "FAILED"))
        return ScriptError(code, f"{getattr(exc, 'message', str(exc))} [{code}]",
                           {**getattr(exc, "details", {}), **details})

    def _exact_selection(self, items):
        """Resolve a frozen list now; keep playlist occurrence IDs and reject stale entries."""
        if isinstance(items, dict) and items.get("kind") == "entries":
            if not isinstance(items.get("playlist_id"), str) or not isinstance(items.get("entries"), list):
                raise ScriptError("INVALID_SELECTION", "Supply a playlist-entry result with its stable playlist and occurrence IDs")
            playlist = self.win.state.playlist(items.get("playlist_id"))
            if playlist is None:
                raise ScriptError("STALE_REFERENCE", "Playlist no longer exists")
            by_id = {row.get("occurrence_id"): row for row in playlist.get("items", [])}
            selection = []
            for occurrence in items.get("entries", []):
                if not isinstance(occurrence, str):
                    raise ScriptError("INVALID_SELECTION", "Occurrence IDs must be text")
                row = by_id.get(occurrence)
                track = self._by_id(row.get("track_id")) if row else None
                if track is None:
                    raise ScriptError("STALE_REFERENCE", "A selected playlist occurrence is missing")
                selection.append({"track_id": track["id"], "path": track["path"], "occurrence_id": occurrence})
        elif isinstance(items, list):
            selection = [row for item in items for row in self._exact_selection(item)]
        else:
            try:
                selection = [{"track_id": row["id"], "path": row["path"]} for row in self._tracks(items)]
            except (KeyError, TypeError, ValueError) as exc:
                raise ScriptError("INVALID_SELECTION", "Use explicit stable track or playlist-occurrence results") from exc
        if not 1 <= len(selection) <= 10000:
            raise ScriptError("EMPTY_SELECTION" if not selection else "LIMIT", "Select 1–10000 explicit tracks; empty never means all")
        return selection

    def _script_context(self, selection, capture_values=True):
        """Freeze the actual table's displayed column order and each selected row.

        Repeated playlist occurrences retain separate values (e.g. their position
        column). A track-only reference is omitted when its rendered rows differ;
        the formatter then asks for an exact occurrence instead of guessing.
        """
        from PySide6.QtCore import Qt
        table = self._visible_view() or self.win.pages["tracks"].view
        model, header = table.model(), table.horizontalHeader()
        logical = [header.logicalIndex(i) for i in range(header.count()) if not table.isColumnHidden(header.logicalIndex(i))]
        names = [str(model.headerData(i, Qt.Horizontal, Qt.DisplayRole) or "") for i in logical]
        common = {"columns": [{"name": name, "value": ""} for name in names]}
        if not capture_values:
            return common
        wanted = {r["track_id"] for r in selection}
        by_track, by_occurrence = {}, {}
        for index, track in enumerate(model.rows):
            tid = track.get("id") or track.get("track_id")
            if tid not in wanted:
                continue
            columns = []
            for name, i in zip(names, logical):
                value = model.index(index, i).data(Qt.DisplayRole)
                columns.append({"name": name, "value": "" if value is None else str(value)})
            if any(len(c["value"]) > 32768 for c in columns):
                raise ScriptError("CONTEXT_LIMIT", "A displayed column exceeds the captured-value limit; choose a smaller visible field set")
            by_track.setdefault(tid, []).append(columns)
            occurrence = (track.get("occurrence") or {}).get("occurrence_id")
            if occurrence:
                by_occurrence[occurrence] = columns
        tracks = {tid: rows[0] for tid, rows in by_track.items() if all(row == rows[0] for row in rows)}
        selected_occurrences = {r.get("occurrence_id") for r in selection if r.get("occurrence_id")}
        context = {**common, "columns_by_track": tracks,
                   "columns_by_occurrence": {oid: row for oid, row in by_occurrence.items() if oid in selected_occurrences}}
        if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > 4 * 1024 * 1024:
            raise ScriptError("CONTEXT_LIMIT", "Captured GUI column values exceed 4 MiB; split the explicit selection")
        return context

    @staticmethod
    def _column_functions(payload):
        # Literal native tag writes/numbering/conversion masks are not formatter
        # programs, even if their text happens to contain a dollar function name.
        if payload.get("kind") in {"tags", "number_tracks", "filename_to_tags", "text_to_tags", "covers"}:
            return set()
        found = set()
        from ..scripting.formatter import parse, ScriptError as FormatError
        def nodes(tree):
            for node in tree:
                if isinstance(node, (tuple, list)):
                    nodes(node)
                    continue
                if node.kind == "call" and node.value.casefold() in {"columnnames", "columnvalues"}:
                    found.add(node.value.casefold())
                nodes(node.children)
        def visit(value):
            if isinstance(value, str):
                try:
                    nodes(parse(value))
                except FormatError:
                    # An imported MTA document wraps its formatter expressions
                    # in INI syntax. The backend compiler still owns its verdict.
                    if "mta_text" in payload and value is payload["mta_text"]:
                        found.update(m.group(1).casefold() for m in re.finditer(r"(?<!\$)\$(columnNames|columnValues)\s*\(", value, re.I))
            elif isinstance(value, dict):
                for item in value.values(): visit(item)
            elif isinstance(value, (list, tuple)):
                for item in value: visit(item)
        visit(payload)
        return found

    def _with_script_context(self, payload, selection):
        args = {**payload, "selection": selection}
        functions = self._column_functions(payload)
        if functions:
            args["context"] = self._script_context(selection, capture_values="columnvalues" in functions)
            # The authenticated backend admits a 1 MiB envelope. Refuse an
            # oversized exact capture here, before acceptance; never truncate it.
            if len(json.dumps(args, allow_nan=False).encode("utf-8")) > 1_040_000:
                raise ScriptError("CONTEXT_LIMIT", "Exact displayed column values exceed the bounded request envelope; split the explicit selection")
        return args

    def _refresh_console_mirrors(self, receipt, done, failed):
        """Use a fresh acknowledged snapshot, applying it on Qt before reporting completion."""
        retries = {"apply": 0}
        def fetch(client):
            # Every resource belongs to one global application generation. A
            # per-page track check alone cannot protect the following resources.
            for _ in range(6):
                tracks, revision, offset, drift = [], None, 0, False
                while True:
                    page = Backend.unwrap(client.query("tracks.search", {"limit": 1000, "offset": offset}))
                    if revision is not None and page["revision"] != revision:
                        drift = True
                        break
                    revision = page["revision"]
                    tracks.extend(page["tracks"])
                    offset += len(page["tracks"])
                    if not page["tracks"] or offset >= page["total"]:
                        break
                if drift:
                    continue
                playlists, favorites, presets = (Backend.unwrap(client.query(name, {})) for name in
                                                ("playlists.list", "favorites.list", "presets.list"))
                head = Backend.unwrap(client.query("tracks.search", {"limit": 1, "offset": 0}))["revision"]
                if all(value == revision for value in (playlists["revision"], favorites["state_revision"], presets["revision"], head)):
                    return {"tracks": tracks, "revision": revision}, playlists, favorites, presets
            raise ServiceError("REVISION_CONFLICT", "Application kept changing across the complete window snapshot")

        def apply(snapshot):
            try:
                tracks, playlists, favorites, presets = snapshot
                state = self.win.state
                known = [state.revision or 0, state.presets_revision or 0, state.fav_revision or 0]
                known.extend(p.get("revision", 0) for p in state.playlists.values())
                if tracks["revision"] < max(known):
                    retries["apply"] += 1
                    if retries["apply"] > 5:
                        raise ServiceError("REVISION_CONFLICT", "A newer GUI generation kept overtaking the fetched snapshot")
                    self.win.backend.run(fetch, ok=apply, err=error)
                    return
                selected_ids = state.ids_for(self.win.selection)
                state._set_tracks(tracks["tracks"], tracks["revision"])
                state._set_playlists(playlists["playlists"])
                state._set_favorites(favorites)
                state.presets, state.presets_revision = presets["presets"], presets["revision"]
                state.presetsChanged.emit()
                state.is_loaded = True
                state.loaded.emit()
                if selected_ids:
                    self._show_selection([state.by_id[i]["path"] for i in selected_ids if i in state.by_id])
            except Exception as exc:
                failed(self._service_error(ServiceError("MIRROR_REFRESH_FAILED", str(exc)), receipt=receipt))
            else:
                done(receipt)

        def error(exc):
            failed(self._service_error(ServiceError("MIRROR_REFRESH_FAILED", exc.message,
                                                   {"cause_code": exc.code}), receipt=receipt))
        self.win.backend.run(fetch, ok=apply, err=error)

    @staticmethod
    def _outcome_error(result, job=None):
        """Keep structured errors, and faithfully decode Engine's legacy CODE: message jobs."""
        result = result if isinstance(result, dict) else {}
        error = (job or {}).get("error") or result.get("error")
        status = (job or {}).get("status") or result.get("status")
        incomplete = {"failed", "partial", "incomplete", "interrupted", "recovery_required", "cancelled", "unknown"}
        if not error and status not in incomplete:
            return None
        if isinstance(error, dict):
            code, message, details = error.get("code", "FAILED"), error.get("message", str(error)), error.get("details", {})
        else:
            message, details = str(error or status or "Backend operation failed"), {}
            match = re.match(r"^([A-Z][A-Z0-9_]+):\s*(.*)$", message, re.S)
            code = match[1] if match else {"cancelled": "CANCELLED", "interrupted": "INTERRUPTED",
                                           "recovery_required": "RECOVERY_REQUIRED", "partial": "PARTIAL_RESULT",
                                           "unknown": "OUTCOME_UNKNOWN"}.get(status, "FAILED")
            if match:
                message = match[2]
        return ServiceError(code, message, {**details, "receipt": result, **({"job": job} if job else {})})

    def _send_console_request(self, name, args, mutation, done, failed, active):
        def result(value):
            if isinstance(value, dict) and value.get("job_id"):
                active["watch"] = self.win.backend.watch(value["job_id"], done=job_done, failed=job_failed)
                if active.get("cancel_requested"):
                    active["watch"].cancel()
                return
            error = self._outcome_error(value)
            if error:
                failed(error)
            else:
                done(value)

        def job_done(job):
            value = job.get("result") or {}
            error = self._outcome_error(value)
            if error:
                failed(error)
            else:
                done(value)

        def job_failed(job):
            failed(self._outcome_error(job.get("result"), job) or ServiceError("FAILED", "Job failed", {"job": job}))

        if mutation:
            self.win.backend.command(name, args, revision=getattr(self, "_request_revision", None), lane="jobs", ok=result, err=failed)
        else:
            self.win.backend.query(name, args, ok=result, err=failed)

    def _console_request(self, name, args, *, mutation=False, refresh=False, continuation=None):
        from .console_live import Wait
        if not self.win.backend.ready:
            raise ScriptError("BACKEND_UNAVAILABLE", "Backend service is not ready")
        state, active = {}, {}

        def fail(exc):
            state["error"] = exc if isinstance(exc, ScriptError) else self._service_error(exc)

        def finish(value):
            if refresh:
                self._refresh_console_mirrors(value, lambda r: state.update(result=r), fail)
            else:
                state["result"] = value

        def backend_error(exc):
            # An accepted job may have changed some files before failing. Reconcile even on errors.
            if refresh and getattr(exc, "details", {}).get("receipt") is not None:
                self._refresh_console_mirrors(exc.details["receipt"], lambda _: fail(exc), fail)
            else:
                fail(exc)

        def done(value):
            try:
                if continuation:
                    continuation(value, finish, backend_error, active)
                else:
                    finish(value)
            except Exception as exc:
                fail(exc)

        def value():
            if "error" in state:
                raise state["error"]
            return state["result"]

        def cancel():
            active["cancel_requested"] = True
            if active.get("watch"):
                active["watch"].cancel()

        self._send_console_request(name, args, mutation, done, backend_error, active)
        return Wait(lambda: "result" in state or "error" in state, value, timeout=6 * 3600, cancel=cancel)

    def _remember_console_plan(self, result):
        if not isinstance(result, dict) or not isinstance(result.get("plan"), dict):
            raise ScriptError("INVALID_PLAN", "Supply the complete frozen backend plan receipt")
        result = copy.deepcopy(result)
        payload = result.get("plan") or {}
        schema = payload.get("schema")
        route = {"xm5.script-plan": "scripts.execute", "xm5.file-sync-plan": "storage.sync.apply",
                 "xm5.device-state-plan": "device_state.apply", "xm5.inline.device-plan": "device_state.apply",
                 "xm5.inline.library.plan": "cards.execute"}.get(schema) if isinstance(schema, str) else None
        identity = result.get("plan_id") or result.get("id")
        if (not route or not isinstance(identity, str) or not identity or
                not isinstance(result.get("digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", result["digest"])):
            raise ScriptError("INVALID_PLAN", "Expected an intact script or storage plan receipt")
        result.update(kind="plan", plan_id=identity, route=route)
        plans = getattr(self, "_console_plans", {})
        plans[identity] = copy.deepcopy(result)
        if len(plans) > 128:
            plans.pop(next(iter(plans)))
        self._console_plans = plans
        return result

    def _execute_console_plan(self, plan, done, failed, active):
        args = {"plan_id": plan["plan_id"], "digest": plan["digest"]}
        if plan["route"] == "scripts.execute":
            args["approve_source_writes"] = True
        self._send_console_request(plan["route"], args, True, done, failed, active)

    def _script_plan(self, payload, items, preview=True):
        selection = self._exact_selection(items)
        args = self._with_script_context(payload, selection)

        def planned(result, done, failed, active):
            result = self._remember_console_plan(result)
            if preview or active.get("cancel_requested"):
                done(result)
            else:
                self._execute_console_plan(result, done, failed, active)
        return self._console_request("scripts.plan", args, mutation=True, refresh=not preview, continuation=planned)

    def c_capture_tracks(self, items="selection"):
        return self._console_request("scripts.capture", {"selection": self._exact_selection(items)})

    def c_format_text(self, text, items="selection"):
        selection = self._exact_selection(items)
        return self._console_request("scripts.format", self._with_script_context({"text": text}, selection))

    def c_preview_tags(self, fields, items="selection"):
        changes = copy.deepcopy(fields)
        return self._script_plan({"kind": "tags", "changes": changes}, items)

    def c_format_tags(self, fields, items="selection", preview=True):
        actions = [{"kind": "format", "options": {"field": field.upper(), "value": text}}
                   for field, text in fields.items()]
        return self._script_plan({"kind": "actions", "actions": actions}, items, preview)

    def c_tags_from_names(self, pattern, items="selection", preview=True):
        return self._script_plan({"kind": "filename_to_tags", "pattern": pattern}, items, preview)

    def c_preview_names(self, pattern, items="selection"):
        selection = self._exact_selection(items)
        return self._console_request("scripts.convert", self._with_script_context({"direction": "tags_to_filename", "pattern": pattern}, selection))

    def c_rename_files(self, pattern, items="selection", preview=True):
        return self._script_plan({"kind": "rename", "pattern": pattern}, items, preview)

    def c_number_tracks(self, items="selection", start=1, include_total=False, reset_per_folder=False, pad=2, preview=True):
        return self._script_plan({"kind": "number_tracks", "start": start, "pad": pad,
                                  "total": include_total, "reset_per_folder": reset_per_folder}, items, preview)

    def c_tag_to_tag(self, field, value, items="selection", preview=True):
        return self._script_plan({"kind": "tag_to_tag", "field": field, "value": value}, items, preview)

    def c_text_to_tags(self, pattern, text=None, input_file=None, encoding="utf-8", items="selection", preview=True):
        payload = {"kind": "text_to_tags", "pattern": pattern}
        if text is not None: payload["text"] = text
        if input_file is not None: payload.update(input_file=input_file, encoding=encoding)
        return self._script_plan(payload, items, preview)

    def c_plan_covers(self, operation, options=None, items="selection", preview=True):
        return self._script_plan({"kind": "covers", "options": {**(options or {}), "operation": operation}}, items, preview)

    def c_plan_cover_export(self, output_directory, pattern=None, indexes=None, items="selection"):
        values = {"kind": "cover_export", "output_directory": output_directory}
        if pattern is not None: values["pattern"] = pattern
        if indexes is not None: values["indexes"] = indexes
        return self._script_plan(values, items, True)

    def c_inspect_device_library(self, folder):
        return self._console_request("device_state.inspect", {"root": folder})

    def c_plan_device_library(self, folder, associations=None, decisions=None, allow_new_generation=False):
        def planned(result, done, failed, active):
            record = copy.deepcopy(result)
            record.update(kind="plan", route="device_state.apply")
            record["plan_id"] = record.get("plan_id") or record.get("id")
            self._console_plans = {**getattr(self, "_console_plans", {}), record["plan_id"]: record}
            done(record)
        return self._console_request("device_state.plan", {"root": folder, "associations": associations or {},
                                     "decisions": decisions or {}, "allow_new_generation": allow_new_generation}, mutation=True, continuation=planned)

    def c_apply_plan(self, plan="last"):
        plan = self.last if plan == "last" else plan
        if isinstance(plan, str) and plan in getattr(self, "_console_plans", {}):
            plan = self._console_plans[plan]
        if isinstance(plan, dict):
            record = self._remember_console_plan(plan)
            args = {"plan_id": record["plan_id"], "digest": record["digest"]}
            if record["route"] == "scripts.execute":
                args["approve_source_writes"] = True
            return self._console_request(record["route"], args, mutation=True, refresh=True)
        if not isinstance(plan, str) or not plan:
            raise ScriptError("INVALID_PLAN", "Supply a preview receipt or exact persisted plan ID")

        def loaded(result, done, failed, active):
            self._execute_console_plan(self._remember_console_plan(result), done, failed, active)
        return self._console_request("plans.get", {"id": plan}, refresh=True, continuation=loaded)

    def _console_local(self, work, accepted=None):
        """Explicit document/config IO also runs on the existing data lane."""
        from .console_live import Wait
        if not self.win.backend.ready:
            raise ScriptError("BACKEND_UNAVAILABLE", "Backend service is not ready")
        state = {}

        def run(_):
            try:
                return work()
            except Exception as exc:
                raise ServiceError(getattr(exc, "code", "IO_FAILED"), str(exc), getattr(exc, "details", {})) from exc

        def done(result):
            try:
                state["result"] = accepted(result) if accepted else result
            except Exception as exc:
                state["error"] = self._service_error(exc)

        def value():
            if "error" in state:
                raise state["error"]
            return state["result"]
        self.win.backend.run(run, ok=done, err=lambda exc: state.update(error=self._service_error(exc)))
        return Wait(lambda: bool(state), value)

    @staticmethod
    def _read_console_document(file):
        from ..scripting.actions import decode_document
        path = Path(file)
        with path.open("rb") as source:
            raw = source.read(4_000_001)
        document, text = decode_document(raw, path.name)
        return document, text

    def c_import_actions(self, file, name=None):
        def read():
            from ..scripting import import_mta
            document, text = self._read_console_document(file)
            group = import_mta(document.raw_bytes, document.name)
            return {"name": str(name or group.name), "sha256": document.sha256, "text": text,
                    "steps": len(group.actions), "diagnostics": [asdict(d) for d in group.diagnostics]}

        def remember(result):
            groups = getattr(self, "_console_action_groups", {})
            groups[result["name"]] = result
            self._console_action_groups = groups
            return {k: v for k, v in result.items() if k != "text"}
        return self._console_local(read, remember)

    def c_action_groups(self):
        return {"items": [{k: v for k, v in group.items() if k != "text"}
                          for group in getattr(self, "_console_action_groups", {}).values()], "session_only": True}

    def c_run_actions(self, group, items="selection", preview=True):
        if not isinstance(group, str):
            raise ScriptError("INVALID_ARGUMENT", "Use an explicitly imported action-group name")
        source = getattr(self, "_console_action_groups", {}).get(group)
        if source is None:
            raise ScriptError("NOT_FOUND", "Import this named action group first")
        return self._script_plan({"kind": "actions", "mta_text": source["text"], "name": group}, items, preview)

    def c_preview_template(self, text, directory, name="export.txt", items="selection"):
        return self._script_plan({"kind": "template", "text": text, "output_directory": directory,
                                  "default_name": name}, items)

    def c_export_metadata(self, template, file, items="selection"):
        # Existing catalog takes a template path. Reading precedes a second ordinary parser step.
        if not isinstance(template, str) or not isinstance(file, str) or not template or not file:
            raise ScriptError("INVALID_ARGUMENT", "Supply explicit template and new output file paths")
        selection = self._exact_selection(items)

        def read(_):
            try:
                _, text = self._read_console_document(template)
            except Exception as exc:
                raise ServiceError(getattr(exc, "code", "IO_FAILED"), str(exc)) from exc
            return text

        def start(result, done, failed, active):
            output = Path(file).absolute()
            try:
                args = self._with_script_context({"kind": "template", "text": result, "output_directory": str(output.parent),
                                                  "default_name": output.name}, selection)
            except Exception as exc:
                failed(exc)
                return

            def planned(value):
                try:
                    plan = self._remember_console_plan(value)
                    # A $filename directive may choose outputs; every path remains in this plan.
                    if active.get("cancel_requested"):
                        done(plan)
                    else:
                        self._execute_console_plan(plan, done, failed, active)
                except Exception as exc:
                    failed(exc)
            self._send_console_request("scripts.plan", args, True, planned, failed, active)

        # Reuse the ordinary acknowledgement machinery for local read + backend plan/apply.
        from .console_live import Wait
        if not self.win.backend.ready:
            raise ScriptError("BACKEND_UNAVAILABLE", "Backend service is not ready")
        state, active = {}, {}
        def fail(exc):
            state["error"] = self._service_error(exc)
        def value():
            if "error" in state:
                raise state["error"]
            return state["result"]
        self.win.backend.run(read, ok=lambda text: start(text, lambda r: self._refresh_console_mirrors(
            r, lambda receipt: state.update(result=receipt), fail), fail, active), err=fail)
        return Wait(lambda: bool(state), value, timeout=6 * 3600,
                    cancel=lambda: (active.update(cancel_requested=True), active.get("watch") and active["watch"].cancel()))

    def c_plan_playlists(self, recipe, items="selection"):
        return self._script_plan({"kind": "playlists", "recipe": recipe}, items)

    def c_library_health(self, folder, sources=None, find_relinks=False):
        return self._console_request("storage.health", {"root": folder, "source_roots": sources or {},
                                    "find_relinks": find_relinks})

    def c_export_inline_card(self, destination, music, eq=None, analyze_peak=True):
        return self._console_request("storage.inline.export", {"parent": destination, "music_roots": music,
                                    "eq_roots": eq or [], "analyze_peak": analyze_peak},
                                    mutation=True, refresh=True)

    def c_organize_card(self, folder, files, analyze_peak=True):
        return self._console_request("storage.inline.organize", {"root": folder, "tracks": files,
                                    "analyze_peak": analyze_peak}, mutation=True, refresh=True)

    def c_rebuild_card_index(self, folder):
        return self._console_request("storage.inline.rebuild", {"root": folder}, mutation=True, refresh=True)

    def c_storage_transactions(self, folder):
        return self._console_request("storage.transactions", {"root": folder})

    def c_storage_plan(self, plan):
        def loaded(result, done, failed, active):
            done(self._remember_console_plan(result))
        return self._console_request("storage.plan", {"plan_id": plan}, continuation=loaded)

    def c_plan_sync(self, folder, sources, updates=None, choices=None, metadata=None):
        def planned(result, done, failed, active):
            done(self._remember_console_plan(result))
        return self._console_request("storage.sync.plan", {"root": folder, "source_roots": sources,
                    "updates": updates or [], "choices": choices, "manifest_metadata": metadata}, mutation=True, continuation=planned)

    def c_plan_repair(self, folder, sources, choices=None):
        def planned(result, done, failed, active):
            done(self._remember_console_plan(result))
        return self._console_request("storage.repair.plan", {"root": folder, "source_roots": sources,
                    "choices": choices}, mutation=True, continuation=planned)

    def c_recover_storage(self, folder, transaction, action="rollback"):
        return self._console_request("storage.recover", {"root": folder, "transaction_id": transaction,
                                    "action": action}, mutation=True, refresh=True)

    def c_set_peak_options(self, midpoint_dbfs=0, correction_limit_db=4, peak_correction_enabled=True):
        from ..core.portable import ConfigError
        for name, value in (("midpoint_dbfs", midpoint_dbfs), ("correction_limit_db", correction_limit_db)):
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ScriptError("INVALID_ARGUMENT", name + " must be a finite number")
        if not -60 <= midpoint_dbfs <= 0 or not 0 <= correction_limit_db <= 12 or type(peak_correction_enabled) is not bool:
            raise ScriptError("INVALID_ARGUMENT", "Peak midpoint must be -60..0 dBFS, limit 0..12 dB, enabled true/false")
        options = {"midpoint_dbfs": float(midpoint_dbfs), "correction_limit_db": float(correction_limit_db),
                   "peak_correction_enabled": peak_correction_enabled}

        def save():
            try:
                result = self.win.prefs.store.update(lambda config: config["ui"].update(options))
            except ConfigError as exc:
                raise ServiceError("CONFIG_FAILED", str(exc)) from exc
            page = self.win.pages["export"]
            page.midpoint.setValue(options["midpoint_dbfs"])
            page.gain_limit.setValue(options["correction_limit_db"])
            page.peak_enabled.setChecked(options["peak_correction_enabled"])
            page._invalidate()
            return {**options, "config_revision": result["revision"]}
        return self._console_local(save)
