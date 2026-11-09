"""Existing source, playlist, lyric and export controller workflows for scripts."""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QAbstractButton

from ..console.language import ScriptError, Parser
from ..core import peq, peace, playlists
from . import tagfile


class ConsoleWorkflowActions:
    def c_run_script(self, file, arguments=None):
        raise ScriptError("SCRIPT_INCLUDE_LITERAL", "run_script must be a standalone literal include, expanded during whole-script preflight")

    def c_save_script(self, file):
        source = getattr(self, "last_source", "")
        return self._console_local(lambda: self._write_new(file, source.encode("utf-8")))

    def c_open_in_folder(self, item="selection"):
        import subprocess
        rows = self._tracks(item)
        if len(rows) != 1:
            raise ScriptError("ARGUMENTS", "Choose exactly one indexed track to reveal")
        if os.name != "nt":
            raise ScriptError("UNSUPPORTED_CAPABILITY", "This Windows application reveals files through Explorer")
        path = str(Path(rows[0]["path"]).resolve())
        subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)])
        return {"requested": "reveal", "path": path, "completion": "Explorer launch only; external window is not controlled"}

    def c_quit_app(self, jobs="ask", drafts="ask"):
        if jobs not in ("ask", "cancel", "wait") or drafts not in ("ask", "discard"):
            raise ScriptError("ARGUMENTS", "jobs: ask, cancel, wait; drafts: ask, discard")
        if self.win.inspector.dirty() and drafts != "discard":
            raise ScriptError("UNSAVED_DRAFTS", "Save/discard tag drafts or explicitly choose drafts=discard")
        if self.win.state.busy or self.win.pages["export"].running():
            if jobs == "wait":
                from .console_live import Wait
                return Wait(lambda: not self.win.state.busy and not self.win.pages["export"].running(), self._close_window, timeout=6 * 3600)
            if jobs != "cancel":
                raise ScriptError("ACTIVE_JOBS", "Wait for jobs, or explicitly choose jobs=cancel")
            self.win.state.cancel_job()
            self.win.pages["export"].cancel()
            from .console_live import Wait
            return Wait(lambda: not self.win.state.busy and not self.win.pages["export"].running(),
                        lambda: self._close_window(), timeout=6 * 3600)
        return self._close_window()

    def _close_window(self):
        self.win.close()
        return {"closed": self.win.isHidden(), "session_invalidated": not self.win.gui_control.live}
    def _source(self, ref, kind="music"):
        kind = "peq" if kind in ("peq", "eq") else kind
        if kind not in ("music", "peq"):
            raise ScriptError("ARGUMENTS", "kind: music, peq")
        records = self.win.prefs.store.sources(kind, include_disabled=True)
        hits = [r for r in records if ref == r["id"] or Path(str(ref)).resolve() == Path(r["path"]).resolve()]
        if len(hits) != 1:
            raise ScriptError("NOT_FOUND", "Use an exact source ID/path", {"choices": records})
        return kind, hits[0]

    def c_eq_folders(self):
        return {"kind": "sources", "items": self.win.prefs.store.sources("peq", include_disabled=True)}

    def _sources_changed(self, kind):
        from .console_live import Wait
        self.win.state.reindex()
        self.win.pages["peq"]._show_peace_dir()
        self.win.pages["export"].refresh_sources()
        done = {}
        self.win.restart_backend(lambda: done.update(ready=True))
        return Wait(lambda: bool(done) and self.win.state.is_loaded and not self.win.state.busy,
                    lambda: {"kind": "sources", "items": self.win.prefs.store.sources(kind, include_disabled=True)})

    def c_add_music_folder(self, path, name=None):
        from .console_live import Wait
        path = str(Path(path).resolve())
        if not Path(path).is_dir():
            raise ScriptError("NOT_FOUND", "Select an existing music folder")
        if path in self.win.music_roots():
            return {"already_registered": True, "path": path}
        if name is not None:
            raise ScriptError("ARGUMENTS", "Sources use their existing directory label; rename on disk is a separate explicit operation")
        if not self.win.add_music_folder(path):
            raise ScriptError("SOURCE_CONFLICT", "Source registration was refused", {"message": self.win.last_error})
        return Wait(lambda: self.win.backend.ready and not self.win._when_ready and not self.win.state.busy,
                    lambda: {"kind": "sources", "items": self.win.prefs.store.sources("music", include_disabled=True)}, timeout=6 * 3600)

    def c_add_eq_folder(self, path, name=None):
        path = str(Path(path).resolve())
        if not Path(path).is_dir():
            raise ScriptError("NOT_FOUND", "Select an existing PEQ source folder")
        if name is not None:
            raise ScriptError("ARGUMENTS", "PEQ source labels come from their directories")
        self.win.prefs.register_source("peq", path)
        return self._sources_changed("peq")

    def c_relink_folder(self, folder, path, kind="music"):
        from .console_live import Wait
        kind, source = self._source(folder, kind)
        path = str(Path(path).resolve())
        if not Path(path).is_dir():
            raise ScriptError("NOT_FOUND", "Select an existing replacement directory")
        if kind == "music":
            if not self.win.relink_music_folder(source["path"], path):
                raise ScriptError("SOURCE_CONFLICT", "The normal controller refused this relink")
            return Wait(lambda: self.win.backend.ready and not self.win._when_ready and not self.win.state.busy,
                        lambda: {"source_id": source["id"], "path": path}, timeout=6 * 3600)
        self.win.prefs.relink_source(kind, source["id"], path)
        return self._sources_changed(kind)

    def c_enable_folder(self, folder, enabled=True, kind="music"):
        kind, source = self._source(folder, kind)
        if enabled:
            self.win.prefs.register_source(kind, source["path"])
        else:
            self.win.prefs.remove_source(kind, source["id"])
        return self._sources_changed(kind)

    def c_remove_folder(self, folder, kind="music"):
        return self.c_enable_folder(folder, False, kind)

    def c_import_playlist(self, file, name=None, base_folder=None, playlist=None, mode="new", missing="preserve"):
        from .console_live import Wait
        if mode not in ("new", "append", "replace") or missing not in ("preserve", "skip", "error"):
            raise ScriptError("ARGUMENTS", "mode: new, append, replace; missing: preserve, skip, error")
        if base_folder is not None and Path(base_folder).resolve() != Path(file).resolve().parent:
            raise ScriptError("ARGUMENTS", "Ordinary M3U references resolve against the playlist's own directory")
        frozen = {t["path"]: t["id"] for t in self._all()}
        first = self._console_local(lambda: asdict(playlists.read_m3u(Path(file), list(frozen))))
        def apply():
            result = first.result()
            problems = result["missing"] + result["outside"] + result["unsupported"]
            if problems and missing != "skip":
                raise ScriptError("UNRESOLVED_ENTRIES", "Ordinary GUI playlists cannot retain unindexed/stream entries; inspect or explicitly choose missing=skip", result)
            ids = [frozen[p] for p in result["paths"]]
            ref = {"kind": "tracks", "ids": ids, "count": len(ids)}
            if mode == "new":
                if playlist is not None:
                    raise ScriptError("ARGUMENTS", "A target playlist requires append or replace mode")
                return self.c_create_playlist(name or result["name"], ref)
            if playlist is None:
                raise ScriptError("ARGUMENTS", "Supply an exact target playlist")
            return self.c_add_to_playlist(playlist, ref) if mode == "append" else self.c_replace_playlist(playlist, ref)
        return self._then_wait(first, apply)

    def c_export_playlist(self, playlist, file, paths="relative", overwrite="refuse"):
        if paths not in ("relative", "absolute") or overwrite not in ("refuse", "replace"):
            raise ScriptError("ARGUMENTS", "paths: relative, absolute; overwrite: refuse, replace")
        pl = self._playlist(playlist)
        rows, missing = self.win.state.playlist_rows(pl["id"])
        if missing:
            raise ScriptError("UNRESOLVED_ENTRIES", "Restore or remove unresolved playlist occurrences before export", {"missing": missing})
        target = Path(file).resolve()
        entries = []
        for t in rows:
            ref = t["path"]
            if paths == "relative":
                try: ref = os.path.relpath(ref, target.parent)
                except ValueError: pass
            entries.append((ref, t.get("duration"), playlists.display_title(t)))
        payload = playlists.m3u8_text(entries).encode("utf-8")
        def write():
            with target.open("xb" if overwrite == "refuse" else "wb") as output:
                output.write(payload)
            return {"playlist_id": pl["id"], "file": str(target), "entries": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}
        return self._console_local(write)

    def c_export_track_cover(self, file, items="selection", source="files", overwrite="refuse"):
        tracks = self._tracks(items)
        if len(tracks) != 1 or source not in ("files", "file") or overwrite not in ("refuse", "replace"):
            raise ScriptError("ARGUMENTS", "Select exactly one file cover; overwrite: refuse, replace")
        def work():
            cover = tagfile.read(tracks[0]["path"]).cover
            if not cover:
                raise ScriptError("NO_COVER", "This file has no embedded cover")
            data = cover.data
            with Path(file).open("xb" if overwrite == "refuse" else "wb") as output:
                output.write(data)
            return {"file": str(Path(file).resolve()), "bytes": len(data)}
        return self._console_local(work)

    def c_preview_cover(self, item="selection"):
        tracks = self._tracks(item)
        if len(tracks) != 1:
            raise ScriptError("ARGUMENTS", "Select exactly one track")
        def read():
            cover = tagfile.read(tracks[0]["path"]).cover
            if cover is None: return {"cover": None}
            import base64
            return {"mime": cover.mime, "bytes": len(cover.data), "data_b64": base64.b64encode(cover.data).decode("ascii")}
        return self._console_local(read)

    def c_inspect_peace(self, folder):
        return self._console_request("peace.inspect", {"directory": str(Path(folder).resolve())})

    def c_open_peace(self, folder=None):
        self.win.open_peace()
        return {"page": "peq", "editor": "internal", "optional_source_folder": folder}

    def c_set_peace_sources(self, folders):
        sources = [self._source(f, "peq")[1]["id"] for f in folders]
        page = self.win.pages["peq"]
        for i in range(page.peace_sources.count()):
            item = page.peace_sources.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in sources else Qt.Unchecked)
        return {"source_ids": sources}

    def c_select_peace_profiles(self, profiles):
        available = getattr(self, "_peace_profiles", {})
        if not isinstance(profiles, list) or any(p not in available for p in profiles):
            raise ScriptError("NOT_FOUND", "Inspect Peace sources first, then supply exact returned profile IDs", {"choices": list(available)})
        self._checked_peace = list(profiles)
        return {"profiles": profiles}

    def c_import_peace(self, profiles="checked", folders="checked"):
        from .console_live import Wait
        page = self.win.pages["peq"]
        if folders == "checked":
            ids = {page.peace_sources.item(i).data(Qt.UserRole) for i in range(page.peace_sources.count()) if page.peace_sources.item(i).checkState() == Qt.Checked}
            roots = [s["path"] for s in self.win.source_records("peq") if s["id"] in ids]
        else:
            roots = [self._source(f, "peq")[1]["path"] for f in folders]
        if not roots:
            raise ScriptError("EMPTY_SELECTION", "Choose explicit Peace source folders")
        first = self._console_local(lambda: [r for root in roots for r in peace.read_folder(Path(root))])
        def apply():
            rows = first.result()
            available = {hashlib.sha256((r.name + str(r.preset.source if r.preset else r.reasons)).encode()).hexdigest()[:32]: r for r in rows}
            self._peace_profiles = available
            wanted = getattr(self, "_checked_peace", []) if profiles == "checked" else profiles
            if not isinstance(wanted, list) or not wanted:
                # Explicit inspect stage; does not silently import every profile.
                return {"kind": "peace_profiles", "items": [{"id": i, "name": r.name, "ok": r.ok, "reasons": r.reasons} for i, r in available.items()], "imported": 0}
            if any(i not in available or not available[i].ok for i in wanted):
                raise ScriptError("INVALID_PRESET", "Every selected profile must be present and convertible")
            page.apply_peace([available[i] for i in wanted])
            return {"profiles": wanted, "imported": len(wanted)}
        return self._then_wait(first, apply)

    def _lyric_track(self, items):
        rows = self._tracks(items)
        if len(rows) != 1:
            raise ScriptError("ARGUMENTS", "Choose exactly one track for this lyric operation")
        return rows[0]["id"]

    def c_open_lyrics(self, items="selection"):
        from .lyrics_dialog import LyricsDialog
        dlg = LyricsDialog(self.win, self._lyric_track(items))
        self.win.last_dialog = dlg
        dlg.open()
        return {"dialog": "lyrics", "track_id": dlg.track_id}

    def c_inspect_lyrics(self, items="selection"):
        return self._console_request("lyrics.inspect", {"track_id": self._lyric_track(items)}, mutation=True)

    def c_find_lyrics(self, items="selection"):
        return self._console_request("lyrics.search", {"track_id": self._lyric_track(items)}, mutation=True)

    def c_import_lyrics(self, file, items="selection"):
        return self._console_request("lyrics.import", {"track_id": self._lyric_track(items), "path": str(Path(file).resolve())}, mutation=True)

    def c_use_lyrics(self, source, items="selection"):
        values = source if isinstance(source, dict) else {"candidate_id": str(source)}
        return self._console_request("lyrics.attach", {"track_id": self._lyric_track(items), **values}, mutation=True)

    def c_remove_lyrics(self, items="selection", source=None):
        return self._console_request("lyrics.remove", {"track_id": self._lyric_track(items), "source_id": source}, mutation=True)

    def c_read_lyrics(self, items="selection"):
        return self._console_request("lyrics.get", {"track_id": self._lyric_track(items)})

    def c_preview_lyrics(self, source, items="selection"):
        return self._console_request("lyrics.get", {"track_id": self._lyric_track(items), "source_id": source})

    def c_fill_missing_lyrics(self, items="selection", budget=20):
        rows = self._tracks(items)
        if not isinstance(budget, int) or not 1 <= budget <= 200 or len(rows) > budget:
            raise ScriptError("ARGUMENTS", "Select at most budget explicit tracks, budget 1..200")
        return self._console_request("lyrics.fetch_missing", {"track_ids": [t["id"] for t in rows], "enabled": True}, mutation=True)

    def c_open_export(self, tab="export"):
        page = self.win.pages["export"]
        self.win.show_page("export")
        if tab not in ("export", "return", "restore", "organize"):
            raise ScriptError("ARGUMENTS", "tab: export, return, restore, organize")
        page.tabs.setCurrentIndex(0 if tab == "export" else 2 if tab == "organize" else 1)
        return {"page": "export", "tab": tab}

    def c_set_export(self, destination=None, music=None, eq_folders=None, playlists=None, presets=None, auto_lyrics=None):
        page = self.win.pages["export"]
        if page.running():
            raise ScriptError("BUSY", "Wait for or cancel the active export")
        self.c_open_export("export")
        page.refresh_sources()
        if destination is not None:
            target = Path(destination).resolve()
            if not target.is_dir(): raise ScriptError("NOT_FOUND", "destination is the existing parent of the generated xm5 folder")
            page.parent_edit.setText(str(target))
        for widget, values, kind in ((page.music_sources, music, "music"), (page.peq_sources, eq_folders, "peq")):
            if values is not None:
                wanted = {self._source(v, kind)[1]["id"] for v in values}
                for i in range(widget.count()):
                    item = widget.item(i)
                    # Export source list data is the full source record.
                    data = item.data(Qt.UserRole)
                    identity = data["id"] if isinstance(data, dict) else data
                    item.setCheckState(Qt.Checked if identity in wanted else Qt.Unchecked)
        if playlists is not None:
            wanted = {self._playlist(p)["id"] for p in playlists}
            self._check_list(page.lists, wanted)
        if presets is not None:
            if any(self.win.state.preset(p) is None for p in presets): raise ScriptError("NOT_FOUND", "Unknown preset name")
            self._check_list(page.presets, set(presets))
        if auto_lyrics is not None:
            page.auto_lyrics.setChecked(auto_lyrics)
        page._invalidate()
        return self.c_export_settings()

    @staticmethod
    def _check_list(widget, wanted):
        for i in range(widget.count()):
            item = widget.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in wanted else Qt.Unchecked)

    def c_export_settings(self):
        page = self.win.pages["export"]
        return {"destination_parent": page.parent_edit.text(), "music": page._selected_sources(page.music_sources),
                "eq_folders": page._selected_sources(page.peq_sources), "playlist_ids": sorted(page._checked(page.lists)),
                "preset_names": sorted(page._checked(page.presets)), "auto_lyrics": page.auto_lyrics.isChecked(),
                "format": "XM5I", "midpoint_dbfs": page.midpoint.value(),
                "correction_limit_db": page.gain_limit.value(), "peak_correction_enabled": page.peak_enabled.isChecked(),
                "plan": copy.deepcopy(page.plan), "running": page.running()}

    def c_preview_export(self):
        from .console_live import Wait
        page = self.win.pages["export"]
        page.plan = None
        backend, state = self.win.backend, {}
        before = backend.activity_serial
        def accepted(event):
            if event["name"] == "cards.plan":
                state["job_id"] = event["result"].get("job_id")
                if state.get("cancel_requested"):
                    backend.command("jobs.cancel", {"id": state["job_id"]}, lane="jobs")
        def completed(job):
            if job.get("id") == state.get("job_id"):
                state["job"] = copy.deepcopy(job)
        backend.commandAcknowledged.connect(accepted)
        backend.jobFinished.connect(completed)
        page.preview()
        def result():
            backend.commandAcknowledged.disconnect(accepted)
            backend.jobFinished.disconnect(completed)
            job = state.get("job")
            if job:
                error = self._outcome_error(job.get("result"), job)
                if error: raise self._service_error(error)
            if page.plan is None:
                raise ScriptError("EXPORT_PLAN_FAILED", page.summary.text(), {"details": page.details.toPlainText()})
            return {"kind": "card_plan", **copy.deepcopy(page.plan)}
        def cancel():
            state["cancel_requested"] = True
            page.cancel()
        return Wait(lambda: "job" in state or (not any(n > before for n in backend.pending_activity) and
                    backend.ready and not page.running() and page.preview_btn.isEnabled()),
                    result, timeout=6 * 3600, cancel=cancel)

    def c_export_card(self, destination=None, plan=None):
        record = self.last if plan in (None, "last") else plan
        if not isinstance(record, dict) or not record.get("plan_id") or not record.get("digest"):
            raise ScriptError("INVALID_PLAN", "Preview first and pass the exact card plan receipt")
        page = self.win.pages["export"]
        if destination is not None and str(Path(destination).resolve()) != str(Path((record.get("plan") or {}).get("destination", "")).resolve()):
            raise ScriptError("PLAN_STALE", "The execution destination must match the frozen plan")
        from .console_live import Wait
        if page.running(): raise ScriptError("BUSY", "An export job is already active")
        page.plan = {k: copy.deepcopy(record[k]) for k in ("plan_id", "digest", "plan")}
        state = {}
        before = self.win.backend.activity_serial
        def accepted(event):
            if event["name"] == "cards.execute" and event["arguments"].get("plan_id") == record["plan_id"]:
                state["job_id"] = event["result"].get("job_id")
        def completed(job):
            if job.get("id") == state.get("job_id"):
                state["job"] = copy.deepcopy(job)
        self.win.backend.commandAcknowledged.connect(accepted)
        self.win.backend.jobFinished.connect(completed)
        page.run()
        def result():
            self.win.backend.commandAcknowledged.disconnect(accepted)
            self.win.backend.jobFinished.disconnect(completed)
            job = state.get("job")
            if not job:
                raise ScriptError("EXPORT_NOT_STARTED", page.summary.text())
            error = self._outcome_error(job.get("result"), job)
            if error: raise self._service_error(error)
            return job["result"]
        return Wait(lambda: "job" in state or (not any(n > before for n in self.win.backend.pending_activity) and
                    not page.running() and page.preview_btn.isEnabled()), result, timeout=6 * 3600, cancel=page.cancel)

    def _card_request(self, name, folder, values=None, refresh=False):
        from .console_live import Wait
        folder = str(Path(folder).resolve())
        stage = {}
        def start():
            stage["wait"] = self._console_request(name, {"root": folder, **(values or {})}, mutation=True, refresh=refresh)
        self.win.ensure_roots(export=[folder], then=start)
        return Wait(lambda: "wait" in stage and stage["wait"].ready(), lambda: stage["wait"].result(), timeout=6 * 3600,
                    cancel=lambda: stage.get("wait") and stage["wait"].cancel and stage["wait"].cancel())

    def c_verify_card(self, folder):
        return self._card_request("cards.verify", folder)

    def c_card_inbox(self, folder):
        return self._card_request("cards.inbox", folder)

    def c_card_playlists(self, folder):
        from ..core.card_layout import playlist_files
        from ..core.inline_card import library
        def listing():
            path = Path(folder).resolve()
            inline = (path / library.MANIFEST).is_file() or (path / "Struct/library.json").is_file()
            files = library.playlist_paths(path) if inline else playlist_files(path)
            return {"kind": "card_playlists", "root": str(path), "files": [str(p) for p in files]}
        return self._console_local(listing)

    def c_inspect_return(self, file, folder):
        first = self._card_request("cards.reconcile", folder, {"m3u": str(Path(file).resolve())})
        from .console_live import Wait
        return Wait(first.ready, lambda: {"kind": "return_inspection", "root": str(Path(folder).resolve()),
                                         "m3u": str(Path(file).resolve()), "proposal": first.result()}, timeout=6 * 3600, cancel=first.cancel)

    def c_import_return(self, inspection, decision=None, name=None):
        if not isinstance(inspection, dict) or inspection.get("kind") != "return_inspection":
            raise ScriptError("INVALID_PLAN", "Pass the exact inspect_return receipt")
        proposal = inspection["proposal"]
        args = {"root": inspection["root"], "m3u": inspection["m3u"], "input_sha256": proposal["input_sha256"]}
        if proposal.get("playlist_id"): args["playlist_id"] = proposal["playlist_id"]
        if decision is not None: args["decision"] = decision
        if name is not None: args["new_name"] = name
        revision = None if decision == "copy" else (proposal.get("current_snapshot") or {}).get("revision")
        return self._console_revision_request("cards.import", args, revision, refresh=True)

    def _console_revision_request(self, name, args, revision, refresh=False):
        # The regular request helper also tracks async jobs and mirror acknowledgement.
        def continuation(result, done, failed, active): done(result)
        previous = getattr(self, "_request_revision", None)
        self._request_revision = revision
        try:
            return self._console_request(name, args, mutation=True, refresh=refresh)
        finally:
            self._request_revision = previous

    def c_restore_library(self, folder, destination):
        from .console_live import Wait
        stage = {}
        self.win.ensure_roots(export=[folder, str(Path(destination).resolve().parent)],
                              then=lambda: stage.update(wait=self._console_request("cards.restore", {"root": str(Path(folder).resolve()), "destination": str(Path(destination).resolve())}, mutation=True)))
        return Wait(lambda: "wait" in stage and stage["wait"].ready(), lambda: stage["wait"].result(), timeout=6 * 3600)

    def c_recover_export(self, transaction):
        directory = transaction.get("control_directory") if isinstance(transaction, dict) else str(transaction)
        return self._console_request("cards.recover_update", {"control_directory": directory}, mutation=True)

    def c_set_return_root(self, folder):
        page = self.win.pages["export"]
        page.card_edit.setText(str(Path(folder).resolve()))
        if page._card_root() is None:
            raise ScriptError("INVALID_ROOT", "The folder has no recognized exported application manifest")
        return {"root": page.card_edit.text()}

    def c_set_setting(self, name, value):
        routes = {"language": self.c_set_language, "theme": self.c_set_theme,
                  "volume": self.c_set_volume, "exclusive": self.c_set_exclusive,
                  "album_ignore_artist": self.c_set_album_grouping, "player_mode": self.c_set_play_mode,
                  "player_device": self.c_set_output}
        if name in routes: return routes[name](value)
        if name in ("midpoint_dbfs", "correction_limit_db", "peak_correction_enabled"):
            options = {k: self.win.prefs.get(k, d) for k, d in (("midpoint_dbfs", 0), ("correction_limit_db", 4), ("peak_correction_enabled", True))}
            options[name] = value
            return self.c_set_peak_options(**options)
        if name == "lyrics.auto_search" and type(value) is bool:
            self.win.set_setting(name, value)
            return {name: value}
        raise ScriptError("ARGUMENTS", "Use a supported setting name; player-mode hardware settings are exposed through device_controls()", {"choices": list(routes) + ["midpoint_dbfs", "correction_limit_db", "peak_correction_enabled", "lyrics.auto_search"]})

    def c_copy_info(self, what="paths", items="selection"):
        rows = self._tracks(items)
        if what not in ("paths", "titles", "metadata"):
            raise ScriptError("ARGUMENTS", "what: paths, titles, metadata")
        text = json.dumps(rows, ensure_ascii=False, default=str) if what == "metadata" else "\n".join(t["path"] if what == "paths" else t.get("title", "") for t in rows)
        QGuiApplication.clipboard().setText(text)
        return {"copied": len(rows), "what": what}

    def c_screenshot(self, region="window"):
        import base64
        if region not in ("window", "player"):
            raise ScriptError("ARGUMENTS", "region: window, player")
        widget = self.win.device_frame if region == "player" and self.win.player_mode else self.win
        data = QBuffer()
        data.open(QIODevice.WriteOnly)
        if not widget.grab().save(data, "PNG"):
            raise ScriptError("SCREENSHOT_FAILED", "Qt could not capture this window")
        return {"mime": "image/png", "data_b64": base64.b64encode(bytes(data.data())).decode("ascii")}

    def c_close_dialog(self):
        dlg = QApplication.activeModalWidget() or self.win.last_dialog
        if dlg is None: raise ScriptError("NO_DIALOG", "No dialog is open")
        dlg.reject()
        self.win.last_dialog = None
        return {"closed": True}

    def c_answer_dialog(self, choice, values=None):
        dlg = QApplication.activeModalWidget() or self.win.last_dialog
        if dlg is None: raise ScriptError("NO_DIALOG", "No desktop dialog is open; player-mode dialogs use device_controls()")
        values = values or {}
        edits = {w.objectName(): w for w in dlg.findChildren(QLineEdit) if w.objectName()}
        if set(values) - set(edits): raise ScriptError("ARGUMENTS", "Use exact dialog field IDs", {"choices": list(edits)})
        for name, value in values.items(): edits[name].setText(str(value))
        if choice in ("accept", "ok", "yes"): dlg.accept()
        elif choice in ("reject", "cancel", "no"): dlg.reject()
        else:
            buttons = [b for b in dlg.findChildren(QAbstractButton) if b.text() == choice or b.objectName() == choice]
            if len(buttons) != 1: raise ScriptError("ARGUMENTS", "Choose a unique exact button text/ID")
            buttons[0].click()
        return {"choice": choice}

    def c_show_dialog(self, dialog, item=None):
        if dialog == "lyrics": return self.c_open_lyrics(item or "selection")
        if dialog in ("playlist", "playlist_details"):
            if item is not None: self.c_open_playlist(item)
            page = self.win.pages["playlist"]
            if not page.pid: raise ScriptError("NO_PLAYLIST", "Open a playlist first")
            from .playlist_page import EditDetails
            pl = self.win.state.playlist(page.pid)
            pix, custom = page.cover_pixmap(180)
            dlg = EditDetails(page, pl["name"], pix, custom, self.win.settings_value("cover_dir") or "")
            dlg.accepted.connect(lambda: page.apply_details(pl["id"], dlg))
            self.win.last_dialog = dlg
            dlg.open()
            return {"dialog": "playlist_details", "playlist_id": pl["id"]}
        raise ScriptError("ARGUMENTS", "dialog: lyrics, playlist_details; player-mode dialogs use device_controls()")

    def c_wait(self, timeout=60, **values):
        job = values.get("for", "last")
        job = self.last if job == "last" else job
        if not isinstance(job, dict) or not (job.get("job_id") or job.get("id")):
            raise ScriptError("ARGUMENTS", "wait expects an exact job receipt")
        from .console_live import Wait
        state = {}
        watch = self.win.backend.watch(job.get("job_id") or job["id"], done=lambda r: state.update(result=r), failed=lambda r: state.update(result=r))
        def result():
            error = self._outcome_error(state["result"].get("result"), state["result"])
            if error: raise self._service_error(error)
            return state["result"]
        return Wait(lambda: bool(state), result, timeout=float(timeout), cancel=watch.cancel)

