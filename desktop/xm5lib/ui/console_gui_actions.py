"""Semantic equivalents of mouse actions on this window and its player mode.

No second state owner, SQL, Python evaluation, shell command, or geometric click
injection. Player-mode handles address existing controller callbacks and expire
when their inspected page/library/configuration changes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import uuid

from PySide6.QtCore import Qt

from ..console.language import ScriptError
from ..core import peq
from . import catalog, tagfile


class ConsoleGuiActions:
    def _select_albums(self, items, mode="replace"):
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        from .models import ALBUM_ROLE
        page = self.win.pages["albums"]
        self.win.show_page("albums")
        page.stack.setCurrentIndex(0)
        wanted = items.get("items", []) if isinstance(items, dict) and items.get("kind") == "albums" else items
        if not isinstance(wanted, list): wanted = [wanted]
        identities = {a["id"] if isinstance(a, dict) else str(a) for a in wanted}
        available = {str(page.model.index(i, 0).data(ALBUM_ROLE).key): i for i in range(page.model.rowCount())}
        if identities - set(available):
            raise ScriptError("STALE_REFERENCE", "Use exact album IDs returned by albums()", {"choices": list(available)})
        flags = {"replace": QItemSelectionModel.ClearAndSelect, "add": QItemSelectionModel.Select,
                 "remove": QItemSelectionModel.Deselect}.get(mode)
        if flags is None: raise ScriptError("ARGUMENTS", "mode: replace, add, remove")
        selection = QItemSelection()
        for identity in identities:
            index = page.model.index(available[identity], 0)
            selection.select(index, index)
        page.grid.selectionModel().select(selection, flags)
        return self.c_selected()
    def c_resolutions(self, query=""):
        items = [{"name": n, "tracks": len(rows)} for n, rows in catalog.resolutions(self.view)
                 if str(query).casefold() in n.casefold()]
        return {"kind": "resolutions", "items": items, "count": len(items)}

    def c_set_player_mode(self, enabled=True):
        self.win.toggle_player_mode(enabled)
        if self.win.player_mode != enabled:
            raise ScriptError("PLAYER_MODE_UNAVAILABLE", "The original player-mode resources could not be loaded")
        return self.c_device_state() if enabled else self.c_view_state()

    def _device(self):
        if not self.win.player_mode or self.win.device is None:
            raise ScriptError("PLAYER_MODE_REQUIRED", "Enter the existing window's player mode with set_player_mode(true)")
        return self.win.device

    def c_device_state(self):
        d = self._device()
        return {"kind": "device_view", "player_mode": True, "powered": d.powered,
                "screen_on": d.screen_on, "pages": [type(p).__name__ for p in d.stack],
                "page": type(d.stack[-1]).__name__, "dialogs": [type(p).__name__ for p in d.dialogs],
                "overlay": type(d.overlay).__name__ if d.overlay else None,
                "panel": d.panel is not None, "config": copy.deepcopy(d.config),
                "desktop_audio_note": "Hardware gain/filter/DSD/balance settings are remembered by the player-mode simulation; desktop audio support follows the actual playback engine"}

    def _device_stamp(self):
        d, s, p = self._device(), self.win.state, self.win.player
        # Page identity, full settings and stable backend resource revisions protect
        # virtual row handles against equal-length replacement/reorder races.
        value = [id(d.stack[-1]), [id(x) for x in d.screen.layers], d.config, d.powered, d.screen_on,
                 s.revision, s.fav_revision, s.presets_revision,
                 [(i, pl.get("revision"), pl.get("items")) for i, pl in s.playlists.items()],
                 [t.get("id", t.get("path")) for t in p.queue], p.index, p.mode,
                 [(id(n.rows), n.rows.count) for l in d.screen.layers for n in l.root.walk() if n.rows],
                 [(id(n), getattr(n, "page", 0)) for l in d.screen.layers for n in l.root.walk() if n.type == "slide_view"]]
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    def c_device_controls(self, start=1, count=100):
        """Expose controls, including offscreen virtual rows without geometric scrolling."""
        d = self._device()
        start, count = int(start), int(count)
        if start < 1 or not 0 <= count <= 200:
            raise ScriptError("ARGUMENTS", "start >= 1; count 0..200")
        stamp = self._device_stamp()
        handles, result, virtual = {}, [], []

        def walk(n, route, inherited=True, resolve=None):
            resolve = resolve or (lambda: n)
            visible = inherited and n.visible
            if not visible:
                return
            slide = n.type == "slide_view" and len([c for c in n.children if c.visible]) > 1
            if n.clickable or slide:
                label = " · ".join(x.text or d.s(x.tr_text) for x in n.walk() if x.text or x.tr_text)
                signature = json.dumps([route, label, n.type, bool(n.enabled)], ensure_ascii=False)
                token = hashlib.sha256((stamp + signature).encode()).hexdigest()[:32]
                handles[token] = (stamp, n, signature, resolve)
                result.append({"id": token, "route": route, "label": label, "type": n.type,
                               "actions": [name for name, fn in (("activate", n.on_click), ("long", n.on_long), ("value", n.on_change), ("page", slide)) if fn],
                               **({"page": getattr(n, "page", 0) + 1, "pages": len([c for c in n.children if c.visible])} if slide else {}),
                               **({"value": n.value, "min": n.min, "max": n.max} if n.on_change else {})})
            for index, child in enumerate(n.children):
                if slide:
                    visible_children = [c for c in n.children if c.visible]
                    if child is not visible_children[int(getattr(n, "page", 0))]:
                        continue
                walk(child, route + "/" + (child.name or child.type) + ":" + str(index), visible and n.enabled,
                     lambda get=resolve, i=index: get().children[i])
            if n.rows:
                virtual.append({"route": route, "count": n.rows.count})
                for i in range(start - 1, min(n.rows.count, start - 1 + count)):
                    def get_row(get=resolve, index=i):
                        owner = get()
                        return owner.rows.row(index, owner)
                    walk(n.rows.row(i, n), route + "/row:" + str(i + 1), visible and n.enabled, get_row)

        # Modal layers own touch input. The same restriction applies to scripts.
        layers = d.screen.layers
        modal = next((i for i in range(len(layers) - 1, -1, -1) if layers[i].modal), 0)
        for index, layer in enumerate(layers[modal:], modal):
            if not layer.passive:
                walk(layer.root, str(index) + "/" + (layer.name or layer.root.name or layer.root.type))
        self._device_handles = handles
        return {"kind": "device_controls", "snapshot": stamp, "start": start, "virtual_lists": virtual, "items": result}

    def c_device_activate(self, control, gesture="activate", value=None):
        handle = getattr(self, "_device_handles", {}).get(control)
        if not handle or handle[0] != self._device_stamp():
            raise ScriptError("STALE_CONTROL", "Inspect device_controls() again and use an exact returned control ID")
        _, node, _, resolve = handle
        try:
            current = resolve()
        except (IndexError, AttributeError, KeyError):
            current = None
        if current is not node or not (node.clickable or node.type == "slide_view"):
            raise ScriptError("STALE_CONTROL", "This virtual row/control was rebuilt; inspect device_controls() again")
        d = self._device()
        if not d.powered or not d.screen_on:
            raise ScriptError("SCREEN_INACTIVE", "Wake the player with device_key(\"power\") first")
        d.reset_idle()
        if gesture == "page":
            pages = [c for c in node.children if c.visible]
            if node.type != "slide_view" or type(value) is not int or not 1 <= value <= len(pages):
                raise ScriptError("ARGUMENTS", "Use an exact 1-based slide page number")
            d.screen.set_page(node, value - 1, animate=False)
        elif gesture == "value":
            if node.on_change is None or not isinstance(value, (float, int)) or not math.isfinite(value) or not node.min <= value <= node.max:
                raise ScriptError("ARGUMENTS", "Supply a finite value inside this control's advertised range")
            node.value = float(value)
            node.on_change(node.value, True)
        elif gesture in ("activate", "long"):
            callback = node.on_click if gesture == "activate" else node.on_long
            if callback is None:
                raise ScriptError("ARGUMENTS", "This control does not support that gesture")
            callback()
        else:
            raise ScriptError("ARGUMENTS", "gesture: activate, long, value, page")
        d.screen.update()
        self._device_handles = {}
        return self.c_device_state()

    def c_device_key(self, key, long=False):
        d = self._device()
        if key == "power":
            d.power_key(long)
        elif key in ("prev", "play", "next", "fn"):
            d.key(key, long)
        elif key == "back":
            d.back()
        elif key == "home":
            d.home_page()
        else:
            raise ScriptError("ARGUMENTS", "key: power, prev, play, next, fn, back, home")
        return self.c_device_state()

    def c_device_panel(self, visible=True):
        d = self._device()
        (d.open_panel if visible else d.close_panel)()
        return self.c_device_state()

    def c_device_type(self, text, replace=False, submit=False):
        d = self._device()
        edit = d.live_edit()
        if edit is None:
            raise ScriptError("NO_TEXT_FIELD", "Activate a player-mode text field first")
        if replace:
            edit.set_text("")
        if not d.type_text(str(text)):
            raise ScriptError("NO_TEXT_FIELD", "No active player-mode text field")
        if submit:
            d.key_enter()
        return self.c_device_state()

    def c_device_knob(self, notches):
        if not isinstance(notches, int) or not -100 <= notches <= 100:
            raise ScriptError("ARGUMENTS", "notches must be an integer from -100 to 100")
        self._device().knob(notches)
        return {"volume": self.win.player.volume}

    def c_set_device_scale(self, scale):
        from .device.body import SCALES
        self._device()
        if scale not in SCALES:
            raise ScriptError("ARGUMENTS", "Use an advertised player scale", {"choices": list(SCALES)})
        self.win.device_frame.set_scale(scale)
        self.win.prefs.set("device_scale", scale)
        self.win._fit_player_mode(True)
        return {"scale": self.win.device_frame.s}

    def _table(self, name="current"):
        if name in (None, "current", "auto"):
            table = self._visible_view()
        elif name == "finder":
            table = self.win.pages["playlist"].results
        else:
            page = self.win.pages.get(name)
            table = getattr(page, "view", None) if page else None
            if name == "albums":
                table = self.win.pages["albums"].tracks
        if table is None or not hasattr(table, "apply_columns"):
            raise ScriptError("NO_TRACK_TABLE", "Choose a visible track table or its stable view key")
        return table

    def c_sort_view(self, column, descending=False):
        table = self._table()
        logical = table.model().col(column)
        if logical < 0:
            raise ScriptError("ARGUMENTS", "Unknown column", {"choices": table.model().IDS})
        if table.reorderable:
            raise ScriptError("PLAYLIST_ORDER", "Playlist order is edited with move_playlist_items or replace_playlist")
        page = self.win.current_page()
        if not hasattr(page, "_sorted") or column not in page.SORT_KEYS:
            raise ScriptError("ARGUMENTS", "This view has no mouse sorting action for that column; use tracks(sort=...) for a result")
        page.sort = (column, descending)
        page.model.set_rows(page._sorted(page.model.rows))
        return {"column": column, "descending": descending}

    def c_set_columns(self, columns, view="current"):
        table = self._table(view)
        ids = table.model().IDS
        if not isinstance(columns, list) or not columns or len(set(columns)) != len(columns) or set(columns) - set(ids):
            raise ScriptError("ARGUMENTS", "Supply distinct column IDs", {"choices": ids})
        config = copy.deepcopy(self.win.prefs.get("columns") or {})
        config.setdefault(table.config_key, {})["visible"] = columns
        self.win.prefs.set("columns", config)
        table.apply_columns()
        return {"view": table.config_key, "columns": columns}

    def c_set_column_width(self, column, width, view="current"):
        table = self._table(view)
        logical = table.model().col(column)
        if logical < 0 or column == "title" or not 20 <= int(width) <= 3000:
            raise ScriptError("ARGUMENTS", "Use a non-title column and width 20..3000; title fills the remaining width")
        table._auto = False
        table.setColumnWidth(logical, int(width))
        table.save_columns()
        return {"column": column, "width": table.columnWidth(logical)}

    def c_reset_columns(self, view="current"):
        table = self._table(view)
        table.reset_columns()
        return {"view": table.config_key, "reset": True}

    def c_set_search_visible(self, visible=True):
        (self.win.open_search if visible else lambda: self.win.close_search(force=True))()
        return self.c_view_state()

    def c_set_playlist_finder(self, visible=True, text=None):
        page = self.win.pages["playlist"]
        if not page.pid:
            raise ScriptError("NO_PLAYLIST", "Open a playlist first")
        page.show_finder(visible)
        if text is not None:
            page.find.setText(str(text))
            page._find_timer.stop()
            page._search()
        return {"visible": page.finder_box.isVisible(), "text": page.find.text()}

    def c_clear_error(self):
        self.win._clear_error()
        return {"cleared": True}

    def c_set_window_state(self, state):
        action = {"normal": self.win.showNormal, "minimized": self.win.showMinimized,
                  "maximized": self.win.showMaximized, "fullscreen": self.win.showFullScreen}.get(state)
        if action is None:
            raise ScriptError("ARGUMENTS", "state: normal, minimized, maximized, fullscreen")
        action()
        return {"state": state, "player_mode": self.win.player_mode}

    def c_refresh_outputs(self):
        engine = self.win.player.backend
        if hasattr(engine, "refresh_outputs"):
            engine.refresh_outputs()
        from .console_live import Wait
        before = self.win.backend.activity_serial - 1
        return Wait(lambda: not any(n > before for n in self.win.backend.pending_activity), self.c_outputs)

    def c_set_output(self, output, exclusive=None):
        ids = {i for i, _, _ in self.win.player.outputs()} | {"auto"}
        if output not in ids:
            raise ScriptError("NOT_FOUND", "Use an exact output ID from outputs()", {"choices": sorted(ids)})
        self.win.player.set_output(output)
        self.win.prefs.set("player_device", output)
        if exclusive is not None:
            self.c_set_exclusive(exclusive)
        self.win.pages["settings"].refresh()
        return {"output": output, "exclusive": bool(self.win.prefs.get("player_exclusive"))}

    def c_set_output_choice(self, output):
        return self.c_set_output(output)

    def c_test_output(self):
        from .console_live import Wait
        engine = self.win.player.backend
        if not hasattr(engine, "_ensure") or not getattr(engine, "device_id", None):
            raise ScriptError("OUTPUT_REQUIRED", "Choose an explicit output in this window before a test tone")
        state = {}
        before = self.win.backend.activity_serial
        def connected():
            # The temporary tone is private to the same Session. Do not let
            # polling mistake its short end for the queued song ending.
            engine._poll.stop()
            state["connected"] = True
        engine._ensure(connected)
        def ready():
            errors = [e for serial, e in self.win.backend.activity_errors if serial > before]
            if errors:
                state["error"] = errors[0]
                return True
            if state.get("connected") and not engine._polling and "wait" not in state:
                state["wait"] = self._console_request("playback.command", {"action": "test_tone", "arguments": {}}, mutation=True)
            return "wait" in state and state["wait"].ready()
        def result():
            try:
                if "error" in state: raise self._service_error(state["error"])
                receipt = state["wait"].result()
                engine._apply(receipt["playback"], True)
                return receipt
            finally:
                if engine.connected: engine._poll.start()
        return Wait(ready, result)

    def _queue_positions(self, positions):
        n = len(self.win.player.queue)
        if isinstance(positions, dict) and positions.get("kind") == "queue":
            identities = [r["entry_id"] for r in positions.get("entries", [])]
            current = {r["entry_id"]: i + 1 for i, r in enumerate(self._queue_refs)}
            if any(i not in current for i in identities):
                raise ScriptError("STALE_REFERENCE", "A queue entry is no longer present")
            positions = [current[i] for i in identities]
        elif isinstance(positions, list) and positions and all(isinstance(i, str) for i in positions):
            current = {r["entry_id"]: i + 1 for i, r in enumerate(self._queue_refs)}
            if any(i not in current for i in positions):
                raise ScriptError("STALE_REFERENCE", "A queue entry is no longer present")
            positions = [current[i] for i in positions]
        if not isinstance(positions, list) or not positions or len(set(positions)) != len(positions) or any(type(i) is not int or not 1 <= i <= n for i in positions):
            raise ScriptError("ARGUMENTS", "Use distinct 1-based queue positions", {"count": n})
        return [i - 1 for i in positions]

    def _replace_queue(self, order):
        p = self.win.player
        current, old_index, playing = p.current, p.index, p.state == "playing"
        p.queue = [p.queue[i] for i in order]
        self._queue_refs = [self._queue_refs[i] for i in order]
        if old_index in order:
            p.index = order.index(old_index)
        else:
            p.stop()
            p.index = min(max(old_index, 0), len(p.queue) - 1)
            if p.queue:
                p._load_current(play=playing)
        p._reorder()
        p.queueChanged.emit()
        if not p.queue:
            p.trackChanged.emit(None)
        return self.c_queue()

    def c_remove_from_queue(self, positions):
        drop = set(self._queue_positions(positions))
        return self._replace_queue([i for i in range(len(self.win.player.queue)) if i not in drop])

    def c_move_queue_items(self, positions, before="end"):
        rows = self._queue_positions(positions)
        n = len(self.win.player.queue)
        target = n if before in (None, "end") else int(before) - 1
        if not 0 <= target <= n:
            raise ScriptError("ARGUMENTS", "before is a 1-based queue position or end")
        remaining = [i for i in range(n) if i not in rows]
        at = target - sum(i < target for i in rows)
        return self._replace_queue(remaining[:at] + rows + remaining[at:])

    def c_clear_queue(self):
        return self._replace_queue([])

    def c_replace_playlist(self, playlist, items):
        pl = self._playlist(playlist)
        tracks = self._tracks(items, allow_empty=True)
        previous = {}
        for entry in pl.get("items", []):
            previous.setdefault(entry["track_id"], []).append(entry)
        rows = [previous[t["id"]].pop(0) if previous.get(t["id"]) else {"track_id": t["id"]} for t in tracks]
        self.win.state.set_items(pl["id"], rows)
        return self._wait_playlist(pl["id"], lambda: self.c_playlist_entries({"kind": "playlist", "id": pl["id"]}))

    def c_set_playlist_cover(self, playlist, file):
        pl = self._playlist(playlist)
        self.win.state.set_cover(pl["id"], str(Path(file).resolve()))
        return self._wait_playlist(pl["id"], lambda: {"playlist_id": pl["id"], "cover": self.win.state.playlist(pl["id"]).get("cover")})

    def c_clear_playlist_cover(self, playlist):
        pl = self._playlist(playlist)
        self.win.state.set_cover(pl["id"], None)
        return self._wait_playlist(pl["id"], lambda: {"playlist_id": pl["id"], "cover": None})

    def c_edit_playlist_details(self, name=None, cover=None):
        pid = self.win.pages["playlist"].pid
        if not pid:
            raise ScriptError("NO_PLAYLIST", "Open a playlist first")
        pl = self.win.state.playlist(pid)
        if name is not None:
            self.win.state.rename_playlist(pid, str(name))
        if cover is not None:
            self.win.state.set_cover(pid, str(Path(cover).resolve()) if cover else None)
        return self._wait_playlist(pid, lambda: {"playlist_id": pid, "name": self.win.state.playlist(pid)["name"]})

    def c_set_library_tags(self, fields, items="selection"):
        paths = [t["path"] for t in self._tracks(items)]
        self.win.state.annotate(paths, fields, None)
        return {"tracks": len(paths), "fields": fields, "scope": "library"}

    def c_clear_library_tags(self, fields, items="selection"):
        return self.c_set_library_tags({name: None for name in fields}, items)

    def c_set_note(self, note, items="selection"):
        paths = [t["path"] for t in self._tracks(items)]
        self.win.state.annotate(paths, None, str(note))
        return {"tracks": len(paths), "note": str(note)}

    def c_refresh_tracks(self, items="selection"):
        return self._console_request("library.refresh", {"paths": [t["path"] for t in self._tracks(items)]}, mutation=True, refresh=True)

    def _inspector(self, items="selection"):
        from .console_live import Wait
        tracks = self._tracks(items)
        inspector = self.win.inspector
        self._show_selection([t["path"] for t in tracks])
        inspector.show_tab(0)
        return inspector

    def c_edit_tags(self, fields, items="selection"):
        from .console_live import Wait
        inspector = self._inspector(items)
        unknown = set(fields) - set(inspector.fields)
        if unknown:
            raise ScriptError("ARGUMENTS", "Unknown tag field", {"choices": sorted(inspector.fields)})
        def edit():
            if not any(f.writable for f in inspector.files):
                raise ScriptError("READ_ONLY", "The actual inspector cannot write this file format")
            for name, value in fields.items():
                field = inspector.fields[name]
                field.setEditText("" if value is None else str(value))
            inspector._dirty()
            return {"draft": inspector.changes(), "tracks": len(inspector.tracks)}
        return Wait(lambda: len(inspector.files) == len(inspector.tracks), edit)

    def c_clear_tag_field(self, field, items="selection"):
        return self.c_edit_tags({field: ""}, items)

    def c_keep_tag_field(self, field):
        inspector = self.win.inspector
        if field not in inspector.fields:
            raise ScriptError("ARGUMENTS", "Unknown tag field")
        f = inspector.fields[field]
        f.load([x.fields.get(field, "") for x in inspector.files])
        inspector._dirty()
        return {"kept": field}

    def c_discard_tag_edits(self):
        self.win.inspector._fill()
        return {"discarded": True}

    def c_save_tag_edits(self):
        from .console_live import Wait
        inspector = self.win.inspector
        if not inspector.dirty():
            raise ScriptError("NO_TAG_DRAFT", "No tag or cover edits are pending")
        from .inspector import NUMBER
        number = inspector.changes().get("tracknumber")
        if number and not NUMBER.match(number):
            raise ScriptError("INVALID_TAG", "Track number must be digits or number/total, as required by the inspector Save")
        # Same writer as the inspector's Save, with explicit per-file completion.
        rows = [(t["path"], inspector.changes(), inspector.cover_change) for t in inspector.tracks]
        done = {}
        self.win.state.write_tags(rows, "Script inspector Save", ok=lambda written, failed: done.update(written=written, failed=failed))
        def result():
            inspector.reload()
            if done["failed"]:
                raise ScriptError("PARTIAL_RESULT", "Some inspector writes failed", done)
            return done
        return Wait(lambda: bool(done), result)

    def c_set_track_cover(self, file, items="selection", save=True):
        from .console_live import Wait
        inspector = self._inspector(items)
        def edit():
            previous = inspector.cover_change
            inspector.choose_cover(str(Path(file).resolve()))
            if inspector.cover_change is previous:
                raise ScriptError("INVALID_COVER", "The inspector rejected this JPEG/PNG cover or the file format is read-only")
            return {"draft_cover": str(file), "tracks": len(inspector.tracks)}
        wait = Wait(lambda: len(inspector.files) == len(inspector.tracks), edit)
        return self._then_wait(wait, self.c_save_tag_edits) if save else wait

    def c_remove_track_cover(self, items="selection", save=True):
        from .console_live import Wait
        inspector = self._inspector(items)
        wait = Wait(lambda: len(inspector.files) == len(inspector.tracks), lambda: (inspector.remove_cover(), {"draft_cover": None})[1])
        return self._then_wait(wait, self.c_save_tag_edits) if save else wait

    @staticmethod
    def _then_wait(first, then):
        from .console_live import Wait
        stage = {}
        def ready():
            if "next" not in stage:
                if not first.ready():
                    return False
                first.result()
                stage["next"] = then()
            return stage["next"].ready() if isinstance(stage["next"], Wait) else True
        return Wait(ready, lambda: stage["next"].result() if isinstance(stage["next"], Wait) else stage["next"], cancel=first.cancel)

    def c_presets(self):
        return {"kind": "presets", "items": copy.deepcopy(self.win.state.presets)}

    def c_open_eq(self, preset):
        p = self.win.state.preset(str(preset))
        if p is None:
            raise ScriptError("NOT_FOUND", "Use an exact preset name from presets()")
        page = self.win.pages["peq"]
        self.win.show_page("peq")
        page.reload(p["name"])
        return {"preset": p, "draft": False}

    def c_new_eq(self, name="New preset"):
        page = self.win.pages["peq"]
        self.win.show_page("peq")
        if self.win.state.preset(name):
            raise ScriptError("NAME_CONFLICT", "Preset already exists")
        page.current = None
        page.load(peq.Preset(name))
        return {"preset": name, "draft": True}

    def c_edit_eq(self, name=None, preamp=None, bands=None):
        page = self.win.pages["peq"]
        p, errors = page.preset()
        if errors:
            raise ScriptError("INVALID_PRESET", "Fix or discard the current preset", {"issues": errors})
        if name is not None: p.name = str(name)
        if preamp is not None: p.preamp = float(preamp)
        if bands is not None:
            p.bands = [peq.Band(**{("fc" if k == "frequency" else k): v for k, v in b.items()}) for b in bands]
        issues = peq.validate(p)
        if issues:
            raise ScriptError("INVALID_PRESET", "Preset validation failed", {"issues": issues})
        page.load(p)
        return {"preset": p.to_dict(), "draft": True}

    def c_add_eq_band(self, type="PK", frequency=1000, gain=0, q=1, enabled=True):
        p, errors = self.win.pages["peq"].preset()
        p.bands.append(peq.Band(type, frequency, gain, q, enabled))
        return self.c_edit_eq(bands=[b.__dict__ for b in p.bands])

    def c_edit_eq_band(self, band, type=None, frequency=None, gain=None, q=None, enabled=None, channel=None):
        p, errors = self.win.pages["peq"].preset()
        if not 1 <= int(band) <= len(p.bands):
            raise ScriptError("ARGUMENTS", "Use a 1-based band number")
        for k, v in dict(type=type, fc=frequency, gain=gain, q=q, enabled=enabled, channel=channel).items():
            if v is not None: setattr(p.bands[int(band) - 1], k, v)
        return self.c_edit_eq(bands=[b.__dict__ for b in p.bands])

    def c_remove_eq_band(self, bands):
        p, errors = self.win.pages["peq"].preset()
        if not isinstance(bands, list) or not bands or any(type(i) is not int or not 1 <= i <= len(p.bands) for i in bands):
            raise ScriptError("ARGUMENTS", "Supply exact 1-based band numbers")
        return self.c_edit_eq(bands=[b.__dict__ for i, b in enumerate(p.bands, 1) if i not in bands])

    def c_validate_eq(self):
        p, issues = self.win.pages["peq"].preset()
        issues += peq.validate(p)
        return {"valid": not issues, "issues": issues, "preset": p.to_dict(), "text": peq.to_apo_text(p) if not issues else None}

    def c_save_eq(self, name=None, as_copy=False):
        page = self.win.pages["peq"]
        p, issues = page.preset()
        if name is not None: p.name = str(name)
        issues += peq.validate(p)
        if issues:
            raise ScriptError("INVALID_PRESET", "Preset is invalid", {"issues": issues})
        if p.name != page.current and self.win.state.preset(p.name):
            raise ScriptError("NAME_CONFLICT", "Preset already exists; choose a new exact name")
        from .console_live import Wait
        done = {}
        self.win.state.save_preset(p.to_dict(), ok=lambda: (page.reload(p.name), done.update(saved=True)),
                                   replace_name=None if as_copy else page.current)
        return Wait(lambda: bool(done), lambda: {"name": p.name, "saved": True})

    def c_discard_eq_edits(self):
        page = self.win.pages["peq"]
        page.reload(page.current)
        return {"discarded": True, "preset": page.current}

    def c_delete_eq(self, preset):
        self.c_open_eq(preset)
        from .console_live import Wait
        done = {}
        self.win.state.delete_preset(str(preset), ok=lambda: done.update(deleted=True))
        return Wait(lambda: bool(done), lambda: {"deleted": str(preset)})

    def c_import_eq(self, file, name=None):
        name = name or Path(file).stem
        if self.win.state.preset(name):
            raise ScriptError("NAME_CONFLICT", "Preset already exists")
        first = self._console_local(lambda: self._read_console_document(file)[1])
        return self._then_wait(first, lambda: self._console_request("presets.import", {"name": name, "text": first.result()}, mutation=True, refresh=True))

    def c_export_eq(self, preset, file, source="saved"):
        if source == "saved":
            p = self.win.state.preset(str(preset))
            if p is None: raise ScriptError("NOT_FOUND", "Preset does not exist")
            p = peq.Preset.from_dict(p)
        elif source == "editor":
            p, issues = self.win.pages["peq"].preset()
            if issues: raise ScriptError("INVALID_PRESET", "Editor preset is invalid", {"issues": issues})
        else:
            raise ScriptError("ARGUMENTS", "source: saved, editor")
        data = peq.to_apo_text(p).encode("utf-8")
        return self._console_local(lambda: self._write_new(file, data))

    @staticmethod
    def _write_new(file, data):
        path = Path(file).resolve()
        with path.open("xb") as output:
            output.write(data)
        return {"file": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def c_use_eq(self, preset):
        self.c_open_eq(preset)
        return self.c_enable_eq(True)

    def c_enable_eq(self, enabled=True):
        page = self.win.pages["peq"]
        engine = page._engine()
        if engine is None:
            raise ScriptError("UNSUPPORTED_CAPABILITY", "The actual playback engine does not support PEQ preview")
        if not getattr(engine, "device_id", None):
            raise ScriptError("OUTPUT_REQUIRED", "Choose an explicit desktop output before enabling PEQ")
        if enabled and not page.current:
            raise ScriptError("NO_PRESET", "Open a saved preset first")
        page._listen(enabled)
        from .console_live import Wait
        return Wait(lambda: engine.peq_name == (page.current if enabled else None), lambda: {"enabled": enabled, "preset": engine.peq_name})

    def c_set_eq_preview_rate(self, rate):
        from .peq_page import RATES
        if rate not in RATES:
            raise ScriptError("ARGUMENTS", "Unsupported preview rate", {"choices": RATES})
        self.win.pages["peq"].rate.setCurrentIndex(RATES.index(rate))
        return {"rate": rate}

    def c_history(self):
        return {"kind": "history", "items": [{"receipt": rid if isinstance(rid, str) else None, "label": label, "kind": kind}
                                                for rid, label, kind in self.win.state.undo_stack]}

    def c_set_inspector_tab(self, tab="tags"):
        if tab not in ("tags", "console"):
            raise ScriptError("ARGUMENTS", "tab: tags, console")
        self.win.inspector.show_tab(0 if tab == "tags" else 1)
        return {"tab": tab}

    def c_job_status(self, job):
        return self._console_request("jobs.get", {"id": job.get("job_id", job.get("id")) if isinstance(job, dict) else str(job)})

    def c_jobs(self):
        state = self.win.state
        return {"kind": "jobs", "library": {"kind": state.busy, "job_id": getattr(state.job, "job_id", None)},
                "export": {"running": self.win.pages["export"].running(), "job_id": getattr(self.win.pages["export"].watch, "job_id", None)}}

    def c_cancel(self, job):
        identity = job.get("job_id", job.get("id")) if isinstance(job, dict) else str(job)
        return self._console_request("jobs.cancel", {"id": identity}, mutation=True)

