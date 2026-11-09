"""The 指令 tab's live dispatcher: GPT's script language (``xm5lib.console.language``, catalog
``COMMANDS.json``) bound to this window.

* The whole input is parsed and every command name/argument checked before the first step runs
  (``Parser`` + ``Registry.bind``, unchanged from the design delivery).
* Registered commands dispatch through existing window controllers and backend services.
  Unsupported arguments/capabilities fail explicitly; catalog presence is not test evidence.
* Queries read the window's own mirror of the library (what the pages show). Changes go through the same
  controller methods the mouse uses, so the pages, the play bar and the tag panel follow. A step that
  waits for the backend (playlists, tags, scans) finishes only when the window shows its result; the
  next step starts after that. Stop ends the script after the current step (and cancels a running scan).
* Steps run on the Qt thread, one at a time; nothing runs a shell or Python.
"""
from __future__ import annotations

import copy
import re
import time
import unicodedata
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ..console.language import Parser, Registry, ScriptError
from . import catalog, tagfile
from .i18n import tr
from .console_backend_actions import ConsoleBackendActions
from .console_gui_actions import ConsoleGuiActions
from .console_workflow_actions import ConsoleWorkflowActions

# Page and mode names; the sidebar's and the play bar's own labels (in the window's language) are added
# at start, so a page can be named as it is shown.
PAGES = {"folders": "folders", "folder": "folders", "albums": "albums", "album": "albums", "artists": "artists",
         "artist": "artists", "genres": "genres", "genre": "genres", "resolution": "resolution",
         "resolutions": "resolution", "hires": "resolution", "favorites": "favorites", "favourites": "favorites",
         "fav": "favorites", "myfav": "favorites", "tracks": "tracks", "songs": "tracks", "allsongs": "tracks",
         "playlists": "playlists", "playlist": "playlists", "peq": "peq", "eq": "peq", "export": "export",
         "card": "export", "tfcard": "export", "settings": "settings", "systemsetting": "settings"}
MODES = {"order": "order", "list": "order", "listplay": "order", "repeatall": "repeat_all", "repeat": "repeat_all",
         "loop": "repeat_all", "repeatone": "repeat_one", "repeattrack": "repeat_one", "single": "repeat_one",
         "shuffle": "shuffle", "random": "shuffle"}
SHOWN = 8           # rows of a result printed in the log


def folded(value) -> str:
    return unicodedata.normalize("NFC", str(value or "")).replace("\\", "/").casefold().strip()


def key(value) -> str:
    return re.sub(r"[_\-\s]", "", str(value)).casefold()


class Wait:
    """A step that finishes later: ``ready()`` is polled; then ``result()`` is the step's value."""

    def __init__(self, ready, result, timeout: float = 120.0, cancel=None):
        self.ready, self.result, self.deadline, self.cancel = ready, result, time.monotonic() + timeout, cancel


class LiveConsole(ConsoleWorkflowActions, ConsoleGuiActions, ConsoleBackendActions, QObject):
    line = Signal(str, str)          # (kind: input | result | error | info, text)
    busyChanged = Signal(bool)
    finished = Signal(object)      # terminal, structured, after every step's real acknowledgement

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.registry = Registry()
        self.variables: dict = {}
        self.last = None
        self.busy = False
        self._steps: list = []
        self._index = 0
        self._stop = False
        self._wait: Wait | None = None
        self._results = []
        self._queue_refs = []
        self.win.player.queueChanged.connect(self._refresh_queue_refs)
        self._refresh_queue_refs()
        self._poll = QTimer(self, interval=50, timeout=self._check)
        from .main_window import NAV, TOOLS
        from .player import MODE_NAMES
        self.page_names = dict(PAGES, **{key(label): k for label, k, _ in NAV + TOOLS})
        self.modes = dict(MODES, **{key(label): m for m, label in MODE_NAMES.items()})

    # ------------------------------------------------------------------ running
    def connected(self, name: str) -> bool:
        return hasattr(self, "c_" + name)

    def run(self, source: str) -> bool:
        if self.busy:
            self.line.emit("error", tr("上一条还在执行（「停止」可以结束它）"))
            return False
        self.line.emit("input", source.strip())
        self.last_source = source
        self._results = []
        try:
            stripped = source.strip()
            if stripped.startswith("```") and stripped.endswith("```"):
                source = "\n".join(stripped.splitlines()[1:-1])
            steps = self._expand_includes(Parser(source).script())
            if len(steps) > 1000:
                raise ScriptError("SCRIPT_TOO_LONG", tr("一次最多 1000 步"))
            for _, node in steps:
                self._preflight(node)
        except ScriptError as e:
            self._error(e)
            self.finished.emit({"status": "failed", "completed": 0,
                                "error": {"code": e.code, "message": e.message, "details": e.details}})
            return False
        except Exception as e:
            error = ScriptError("SCRIPT_INPUT_ERROR", str(e))
            self._error(error)
            self.finished.emit({"status": "failed", "completed": 0, "error": {"code": error.code, "message": error.message}})
            return False
        self._steps, self._index, self._stop = steps, 0, False
        self._set_busy(True)
        QTimer.singleShot(0, self._next)
        return True

    def _expand_includes(self, steps, depth=0, budget=None):
        """Read and preflight all explicit text files before any script side effect."""
        budget = [0] if budget is None else budget
        if depth > 8:
            raise ScriptError("SCRIPT_DEPTH", "At most 8 nested script files")
        output = []
        def literal(node):
            if node[0] == "call" or node[0] == "word" and node[1] in self.variables:
                raise ScriptError("SCRIPT_INCLUDE_LITERAL", "run_script paths/arguments must be literal so all files can be checked before effects")
            if node[0] == "list":
                for child in node[1]: literal(child)
            elif node[0] == "map":
                for _, child in node[1]: literal(child)
            return self._eval(node)
        for assignment, node in steps:
            if node[0] != "call" or self.registry.resolve(node[1]["name"])["name"] != "run_script":
                output.append((assignment, node))
                continue
            if assignment:
                raise ScriptError("ARGUMENTS", "run_script cannot be assigned; assign values inside its script")
            _, args = self.registry.bind(node[1], literal)
            path = Path(args["file"])
            if not path.is_file():
                raise ScriptError("SCRIPT_INPUT_ERROR", "An included script must be an existing ordinary text file")
            with path.open("rb") as file:
                data = file.read(1_048_577)
            budget[0] += len(data)
            if budget[0] > 1_048_576:
                raise ScriptError("SCRIPT_TOO_LONG", "Included scripts total at most 1 MiB")
            source = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
            for name, value in (args.get("arguments") or {}).items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
                    raise ScriptError("ARGUMENTS", "Script argument names must be variable identifiers")
                output.append((name, ("literal", value)))
            output += self._expand_includes(Parser(source).script(), depth + 1, budget)
        return output

    def stop(self) -> None:
        if not self.busy:
            return
        self._stop = True
        if self._wait is not None and self._wait[0].cancel:
            self._wait[0].cancel()
        self.line.emit("info", tr("已请求停止：当前这一步结束后不再继续"))

    def _set_busy(self, on: bool) -> None:
        if self.busy != on:
            self.busy = on
            self.busyChanged.emit(on)

    def _preflight(self, node, nested=False, depth=0):
        if depth > 48:
            raise ScriptError("SCRIPT_DEPTH", tr("嵌套太深"))
        kind, value = node
        if kind == "call":
            row = self.registry.resolve(value["name"])
            if not self.connected(row["name"]):
                raise ScriptError("NOT_CONNECTED", "Command has no connected application service", {"command": row["name"]})
            if nested and row["name"] in self.ASYNC_COMMANDS:
                raise ScriptError("NESTED_ASYNC_QUERY", "Assign this backend query to a variable on its own line before using it")
            if nested and row["effects"]:
                raise ScriptError("NESTED_CHANGE", tr("会改动东西的指令要单独一行；嵌套的只能是查询"), {"command": row["name"]})
            self.registry.bind(value, lambda child: self._preflight(child, True, depth + 1), validate_values=False)
        elif kind in ("list", "map"):
            children = value if kind == "list" else [c for _, c in value]
            if kind == "map" and len({k for k, _ in value}) != len(value):
                raise ScriptError("SYNTAX", tr("同一个字段写了两次"))
            for child in children:
                self._preflight(child, True, depth + 1)

    def _next(self) -> None:
        if self._stop or self._index >= len(self._steps):
            if self._stop and self._index < len(self._steps):
                self.line.emit("info", tr("已停止：完成了 {0} 步，没有执行的 {1} 步", self._index, len(self._steps) - self._index))
            self._set_busy(False)
            self.finished.emit({"status": "cancelled" if self._stop else "succeeded", "completed": len(self._results),
                                "result": copy.deepcopy(self.last), "results": copy.deepcopy(self._results)})
            return
        assignment, node = self._steps[self._index]
        self._index += 1
        self._name = node[1]["name"] if node[0] == "call" else None
        try:
            value = self._step(node)
        except ScriptError as e:
            self._fail(e)
            return
        except Exception as e:
            self._fail(ScriptError("INTERNAL_ERROR", str(e)))
            return
        if isinstance(value, Wait):
            self._wait = (value, assignment)
            self._poll.start()
            self._check()
            return
        self._done(value, assignment)

    def _check(self) -> None:
        if self._wait is None:
            self._poll.stop()
            return
        w, assignment = self._wait
        try:
            ready = w.ready()
            if not ready and time.monotonic() > w.deadline:
                raise ScriptError("TIMEOUT", tr("等了太久没有结果；这一步可能还会在后台完成"))
            if not ready:
                return
            value = w.result()
        except ScriptError as e:
            self._wait = None
            self._poll.stop()
            self._fail(e)
            return
        except Exception as e:
            self._wait = None
            self._poll.stop()
            self._fail(ScriptError("INTERNAL_ERROR", str(e)))
            return
        self._wait = None
        self._poll.stop()
        self._done(value, assignment)

    def _done(self, value, assignment) -> None:
        if assignment:
            self.variables[assignment] = copy.deepcopy(value)
        self.last = copy.deepcopy(value)
        self._results.append({"step": self._index, "assignment": assignment, "value": copy.deepcopy(value)})
        name = getattr(self, "_name", None)
        try:
            name = self.registry.resolve(name)["name"] if name else None
        except ScriptError:
            name = None
        text = self.sentence(name, value) or self.render(value)
        if assignment:
            text = f"{assignment} = " + text
        self.line.emit("result", text)
        QTimer.singleShot(0, self._next)

    def _fail(self, e: ScriptError) -> None:
        done = self._index - 1
        self._error(e, step=self._index if len(self._steps) > 1 else None, completed=done)
        self._set_busy(False)
        self.finished.emit({"status": "outcome_unknown" if e.code in ("TIMEOUT", "TRANSPORT", "OUTCOME_UNKNOWN", "PLAYBACK_OUTCOME_UNKNOWN", "BACKEND_UNCERTAIN", "IPC_TIMEOUT") else "failed",
                            "completed": len(self._results), "results": copy.deepcopy(self._results),
                            "error": {"code": e.code, "message": e.message, "details": e.details}})

    @staticmethod
    def _human(e: ScriptError) -> str:
        """The parser's English messages, in the window's language (what went wrong, then how to fix it)."""
        m, d = e.message, e.details or {}
        line = d.get("line")
        for prefix, text in (("Unknown command: ", tr("没有这个指令：{0}")), ("Missing arguments: ", tr("少了参数：{0}")),
                             ("Too many arguments for ", tr("{0} 的参数太多了")), ("Unknown argument: ", tr("没有这个参数：{0}")),
                             ("Argument supplied twice: ", tr("参数写了两次：{0}"))):
            if m.startswith(prefix):
                return text.format(m[len(prefix):])
        if e.code == "SYNTAX":
            if m == "Unclosed quoted text":
                return tr("引号没有配对（第 {0} 行）", line)
            if m == "Unexpected character":
                return tr("看不懂的字符「{0}」（第 {1} 行）", d.get("character"), line)
            if m.startswith("Expected"):
                return tr("写法不对（第 {0} 行）：缺少 {1}", line, m.removeprefix("Expected ").strip()) if line else \
                    tr("写法不对：缺少 {0}", m.removeprefix("Expected ").strip())
            return tr("写法不对：{0}", m)
        return m

    def _error(self, e: ScriptError, step=None, completed=0) -> None:
        text = "✗ " + self._human(e)
        if step:
            text = "✗ " + tr("第 {0} 步：", step) + self._human(e) + (tr("（前 {0} 步已完成）", completed) if completed else "")
        d = e.details or {}
        if d.get("suggestions"):
            text += "\n  " + tr("是不是：") + ", ".join(d["suggestions"])
        if d.get("choices"):
            text += "\n" + "\n".join("  · " + self._choice(c) for c in d["choices"][:SHOWN])
        if d.get("accepted"):
            text += "\n  " + tr("可用参数：") + ", ".join(d["accepted"])
        receipt = d.get("receipt") or {}
        if isinstance(receipt, dict):
            text += "".join(f"\n  {name}: {receipt[name]}" for name in
                             ("status", "transaction_id", "journal", "recovery_required") if name in receipt)
        if isinstance(d.get("job"), dict) and d["job"].get("id"):
            text += f"\n  job_id: {d['job']['id']}"
        self.line.emit("error", text)

    @staticmethod
    def _choice(c) -> str:
        if isinstance(c, dict):
            return " — ".join(str(c[k]) for k in ("title", "name", "artist", "album", "path") if c.get(k))
        return str(c)

    # ------------------------------------------------------------------ values
    def _step(self, node):
        kind, value = node
        if kind != "call":
            return self._eval(node)
        name, args = self.registry.bind(value, self._eval)
        handler = getattr(self, "c_" + name, None)
        if handler is None:
            raise ScriptError("NOT_CONNECTED", tr("「{0}」还没接到窗口上", name))
        before = self.win.backend.activity_serial
        value = handler(**args)
        row = self.registry.commands[name]
        if not row["effects"]:
            return value
        return self._acknowledge_effect(value, before, name)

    def _acknowledge_effect(self, value, before, name):
        """Finish after actual transport callbacks and queued mirror work, never a posted event."""
        backend, state = self.win.backend, self.win.state
        waiting = value if isinstance(value, Wait) else None

        def ready():
            if waiting is not None and not waiting.ready():
                return False
            return (not any(n > before for n in backend.pending_activity) and
                    not state._pl_busy and not any(state._pl_ops.values()) and
                    not state._view_timer.isActive() and not state.busy)

        def result():
            errors = [e for n, e in backend.activity_errors if n > before]
            if errors:
                raise self._service_error(errors[0])
            output = waiting.result() if waiting is not None else value
            if name in {"play", "pause", "stop", "seek", "next_track", "previous_track", "play_view"}:
                output = self.c_now_playing()
            return output
        if waiting is None and ready():
            return result()
        return Wait(ready, result, timeout=max(1, waiting.deadline - time.monotonic()) if waiting else 120,
                    cancel=waiting.cancel if waiting else None)

    def _eval(self, node):
        kind, value = node
        if kind == "literal":
            return value
        if kind == "word":
            if value in self.variables:
                return copy.deepcopy(self.variables[value])
            low = value.casefold()
            if low in ("true", "yes", "on"):
                return True
            if low in ("false", "no", "off"):
                return False
            if low in ("null", "none"):
                return None
            if low in ("selected", "selection"):
                return self.c_selected()
            if low == "last":
                return copy.deepcopy(self.last)
            return value
        if kind == "list":
            return [self._eval(c) for c in value]
        if kind == "map":
            return {k: self._eval(c) for k, c in value}
        result = self._step(node)
        if isinstance(result, Wait):
            raise ScriptError("NESTED_CHANGE", tr("嵌套的只能是查询"))
        return result

    # ------------------------------------------------------------------ resolving
    @property
    def view(self):
        return self.win.view

    def _ref(self, tracks: list[dict]) -> dict:
        return {"kind": "tracks", "ids": [t["id"] for t in tracks], "count": len(tracks)}

    def _all(self) -> list[dict]:
        return list(self.view.tracks)

    def _by_id(self, tid):
        t = self.win.state.by_id.get(tid)
        return self.view.by_path.get(t["path"]) if t else None

    def one_track(self, title, artist=None, album=None) -> dict:
        if isinstance(title, dict):
            rows = self._tracks(title)
        else:
            k = folded(title)
            rows = [t for t in self._all() if k in (folded(t.get("title")), folded(t.get("path")), folded(t.get("id")))]
        if artist is not None:
            rows = [t for t in rows if folded(artist) in (folded(t.get("artist")), folded(t.get("albumartist")))]
        if album is not None:
            rows = [t for t in rows if folded(album) == folded(t.get("album"))]
        if len(rows) != 1:
            raise ScriptError("AMBIGUOUS" if rows else "NOT_FOUND",
                              tr("要一首歌：加上 artist= 或 album=；要很多首请用 tracks()") if rows else
                              tr("曲库里没有「{0}」", title if not isinstance(title, dict) else "…"),
                              {"choices": [{k: t.get(k) for k in ("title", "artist", "album", "path")} for t in rows[:20]]})
        return self._ref(rows)

    def _tracks(self, value, allow_empty: bool = False) -> list[dict]:
        """A song result, a playlist-entry result, a list of them, a title, or the selection -> tracks."""
        if value is None or value == "selection":
            out = [self.view.by_path[p] for p in self.win.selection if p in self.view.by_path]
            if not out and not allow_empty:
                raise ScriptError("EMPTY_SELECTION", tr("现在没有选中的歌（空选择不代表整个曲库；整库请用 tracks()）"))
            return out
        if isinstance(value, dict):
            kind = value.get("kind")
            if kind == "page":
                return self._tracks(value.get("result"), allow_empty)
            if kind in ("tracks", "queue"):
                out = []
                for tid in value.get("ids", []):
                    t = self._by_id(tid)
                    if t is None:
                        raise ScriptError("STALE_REFERENCE", tr("结果里有一首歌已经不在曲库里了"))
                    out.append(t)
                return out
            if kind == "entries":
                rows, missing = self.win.state.playlist_rows(value.get("playlist_id"))
                by_occ = {r["occurrence"].get("occurrence_id"): r for r in rows}
                if any(o not in by_occ or by_occ[o]["path"] not in self.view.by_path for o in value.get("entries", [])):
                    raise ScriptError("STALE_REFERENCE", "A playlist occurrence is missing; inspect the exact current occurrence IDs")
                return [self.view.by_path[by_occ[o]["path"]] for o in value.get("entries", [])]
            raise ScriptError("INVALID_RESULT", tr("这里要歌曲（tracks()/track()/selected() 的结果）"))
        if isinstance(value, list):
            out = []
            for v in value:
                out += self._tracks(v, True)
            if not out and not allow_empty:
                raise ScriptError("EMPTY_RESULT", tr("一首歌也没有"))
            return out
        if isinstance(value, str):
            return self._tracks(self.one_track(value))
        raise ScriptError("INVALID_RESULT", tr("这里要歌曲（tracks()/track()/selected() 的结果）"))

    def _playlist(self, ref, must=True) -> dict | None:
        st = self.win.state
        if isinstance(ref, dict) and ref.get("kind") in ("playlist", "entries"):
            pl = st.playlist(ref.get("id") or ref.get("playlist_id"))
        else:
            pl = st.playlist(str(ref)) or next((p for p in st.playlist_list() if folded(p["name"]) == folded(ref)), None)
        if pl is None and must:
            raise ScriptError("NOT_FOUND", tr("没有叫「{0}」的歌单", ref if not isinstance(ref, dict) else "…"),
                              {"choices": [{"name": p["name"]} for p in st.playlist_list()]})
        return pl

    def _wait_playlist(self, pid: str, result) -> Wait:
        st = self.win.state
        return Wait(lambda: pid not in st._pl_busy and not st._pl_ops.get(pid), result)

    def _visible_view(self):
        page = self.win.current_page()
        albums = self.win.pages["albums"]
        if page is albums:
            return albums.tracks if albums.stack.currentIndex() == 1 else None
        v = getattr(page, "view", None)
        return v if v is not None and hasattr(v.model(), "rows") else None

    # ------------------------------------------------------------------ help
    def c_help(self, topic="start"):
        r = self.registry
        if key(topic) in r.lookup:
            row = r.resolve(topic)
            params = ", ".join(p["name"] + ("" if p["required"] else "=" + repr(p.get("default")).replace("'", '"'))
                               for p in row["parameters"])
            live = tr("已接到窗口") if self.connected(row["name"]) else tr("还没接到窗口")
            lines = [f"{row['name']}({params})  [{live}]", row["summary"]]
            lines += ["  " + e for e in row.get("examples", [])[:3]]
            return {"text": "\n".join(lines)}
        if topic in ("start", "", None):
            return {"text": "Function scripts control this existing window. Start with status() or help(\"command\").\n"
                            "Manual router: xm5lib/console/manuals/START.md\n"
                            "Player/UI: PLAYER-UI.md · Library/playlists: LIBRARY-PLAYLISTS.md · Tags: TAGS.md\n"
                            "PEQ: PEQ.md · Export/sync: EXPORT-SYNC.md\n"
                            "commands(\"topic\") lists exact names. Empty selection never means the whole library. "
                            "Preview source/output plans, then apply_plan(exact_receipt)."}
        rows = [row for row in r.commands.values()
                if str(topic).casefold().rstrip("s") in (row["group"] + " " + row["name"] + " " + row["summary"]).casefold()]
        return {"text": "\n".join(("  " if self.connected(row["name"]) else "· ") + row["name"] + " — " + row["summary"]
                                  for row in rows) or tr("没有和「{0}」有关的指令", topic)}

    def c_commands(self, topic=""):
        t = str(topic or "").casefold()
        rows = [row for row in self.registry.commands.values() if not t or t in (row["group"] + " " + row["name"]).casefold()]
        return {"text": "\n".join(("  " if self.connected(row["name"]) else "· ") + row["name"] for row in rows)
                + "\n" + tr("（前面带 · 的还没接到窗口）")}

    def c_last_result(self):
        return copy.deepcopy(self.last)

    # ------------------------------------------------------------------ queries
    def c_track(self, title, artist=None, album=None):
        return self.one_track(title, artist, album)

    def c_tracks(self, query="", artist=None, album=None, genre=None, where=None, sort="title", descending=False,
                 scope="library", include_missing=False):
        if key(scope) not in ("library", "all", ""):
            raise ScriptError("NOT_CONNECTED", tr("scope 现在只支持 library（整个曲库）"))
        rows = self.view.search_tracks(query or "")
        if artist is not None:
            rows = [t for t in rows if folded(artist) in (folded(t.get("artist")), folded(t.get("albumartist")))]
        if album is not None:
            rows = [t for t in rows if folded(album) == folded(t.get("album"))]
        if genre is not None:
            rows = [t for t in rows if folded(genre) in {folded(g) for g in catalog.split_genre(t.get("genre"))}]
        for field, cond in (where or {}).items():
            rows = [t for t in rows if self._match(t.get(field), cond)]
        if key(sort) not in ("none", "order"):
            f = {"track": "tracknumber", "disc": "discnumber", "year": "date"}.get(key(sort), sort)
            rows = sorted(rows, key=lambda t: (t.get(f) is None, folded(t.get(f)) if isinstance(t.get(f), str) else t.get(f) or 0),
                          reverse=bool(descending))
        return self._ref(rows)

    @staticmethod
    def _match(value, cond) -> bool:
        if not isinstance(cond, dict):
            return folded(value) == folded(cond) if isinstance(cond, str) else value == cond
        for op, want in cond.items():
            op = key(op)
            try:
                if op == "eq" and not (folded(value) == folded(want) if isinstance(want, str) else value == want):
                    return False
                if op in ("min", "gte") and not (value is not None and value >= want):
                    return False
                if op in ("max", "lte") and not (value is not None and value <= want):
                    return False
                if op == "gt" and not (value is not None and value > want):
                    return False
                if op == "lt" and not (value is not None and value < want):
                    return False
                if op == "contains" and folded(want) not in folded(value):
                    return False
            except TypeError:
                return False
        return True

    def c_albums(self, query=""):
        albums = self.view.search_albums(query or "")
        return {"kind": "albums", "count": len(albums),
                "items": [{"id": str(a.key), "name": a.title, "artist": a.artist, "tracks": len(a.tracks), "year": a.year,
                           "track_ids": [t["id"] for t in a.tracks]} for a in albums]}

    def c_artists(self, query=""):
        q = folded(query)
        items = [{"name": n, "albums": c} for n, c in self.view.artists() if not q or q in folded(n)]
        return {"kind": "artists", "count": len(items), "items": items}

    def c_genres(self, query=""):
        q = folded(query)
        items = [{"name": g, "tracks": len(ts)} for g, ts in catalog.genre_tracks(self.view.tracks) if not q or q in folded(g)]
        return {"kind": "genres", "count": len(items), "items": items}

    def c_playlists(self):
        items = [{"name": p["name"], "tracks": p.get("count", len(p.get("items", []))), "id": p["id"]}
                 for p in self.win.state.playlist_list()]
        return {"kind": "playlists", "count": len(items), "items": items}

    def c_playlist_entries(self, playlist):
        pl = self._playlist(playlist)
        occ = [i.get("occurrence_id") for i in pl.get("items", []) if i.get("occurrence_id")]
        return {"kind": "entries", "playlist_id": pl["id"], "playlist": pl["name"], "entries": occ, "count": len(occ)}

    def c_favorites(self):
        st = self.win.state
        return self._ref([t for t in (self._by_id(i) for i in st.fav_ids) if t is not None])

    def c_selected(self):
        return self._ref(self._tracks(None, allow_empty=True))

    def c_now_playing(self):
        p = self.win.player
        t = p.current
        return {"kind": "now", "track_id": t.get("id") if t else None,
                "queue_entry_id": self._queue_refs[p.index]["entry_id"] if 0 <= p.index < len(self._queue_refs) else None,
                "title": t.get("title") if t else None, "artist": t.get("artist") if t else None,
                "album": t.get("album") if t else None, "state": p.state, "position": round(p.position / 1000, 1),
                "duration": round(p.duration / 1000, 1), "volume": p.volume, "mode": p.mode}

    def c_queue(self):
        p = self.win.player
        out = self._ref([t for t in p.queue if t.get("path") in self.view.by_path])
        out["current"] = p.index + 1 if p.current else None
        out["kind"] = "queue"
        out["entries"] = [{"entry_id": r["entry_id"], "track_id": r["track_id"], "position": i + 1} for i, r in enumerate(self._queue_refs)]
        return out

    def _refresh_queue_refs(self):
        import uuid
        previous = {}
        for r in self._queue_refs:
            previous.setdefault(r["track_id"], []).append(r)
        self._queue_refs = [previous[t.get("id", t.get("path"))].pop(0) if previous.get(t.get("id", t.get("path")))
                            else {"entry_id": uuid.uuid4().hex, "track_id": t.get("id", t.get("path"))} for t in self.win.player.queue]

    def c_status(self):
        w, st = self.win, self.win.state
        return {"kind": "status", "tracks": len(w.view.tracks), "albums": len(w.view.albums),
                "playlists": len(st.playlists), "favorites": len(st.fav_ids), "page": self._page_key(),
                "search": w.search.text(), "selected": len(w.selection), "player": w.player.state,
                "volume": w.player.volume, "mode": w.player.mode, "backend": w.backend.state, "busy": st.busy or None}

    def c_music_folders(self):
        roots = self.win.music_roots()
        return {"kind": "folders", "count": len(roots),
                "items": self.win.prefs.store.sources("music", include_disabled=True)}

    def c_outputs(self):
        return {"kind": "outputs", "items": [{"name": n, "id": i, "default": d} for i, n, d in self.win.player.outputs()]}

    def c_view_state(self):
        w = self.win
        page = self._page_key()
        out = {"kind": "view", "page": page, "search": w.search.text(), "selected": len(w.selection),
               "selected_track_ids": self.c_selected()["ids"], "player_mode": w.player_mode,
               "window_state": "minimized" if w.isMinimized() else "fullscreen" if w.isFullScreen() else "maximized" if w.isMaximized() else "normal"}
        if page == "albums" and w.pages["albums"].stack.currentIndex() == 1 and w.pages["albums"].album:
            out["album"] = w.pages["albums"].album.title
        if page == "playlist" and w.pages["playlist"].pid:
            out["playlist"] = (w.state.playlist(w.pages["playlist"].pid) or {}).get("name")
        return out

    def _page_key(self) -> str:
        cur = self.win.current_page()
        return next((k for k, p in self.win.pages.items() if p is cur), "")

    def c_read_results(self, result="last", start=1, count=50):
        ref = self.last if result == "last" else result
        if isinstance(ref, dict) and ref.get("kind") in ("plan", "card_plan"):
            rows = self._console_plan_rows(ref)
            start, count = max(1, int(start)), min(200, max(0, int(count)))
            return {"kind": "plan_page", "plan_id": ref.get("plan_id"), "digest": ref.get("digest"),
                    "start": start, "total": len(rows), "rows": rows[start - 1:start - 1 + count]}
        if isinstance(ref, dict) and ref.get("kind") not in ("tracks", "queue", "entries", "page"):
            field = next((name for name in ("tracks", "items", "values", "records", "profiles", "files", "entries")
                          if isinstance(ref.get(name), list)), None)
            if field is not None:
                rows = ref[field]
                start, count = max(1, int(start)), min(200, max(0, int(count)))
                return {"kind": "result_page", "source_kind": ref.get("kind") or field, "start": start,
                        "total": len(rows), "rows": copy.deepcopy(rows[start - 1:start - 1 + count])}
        rows = self._tracks(ref, allow_empty=True)
        start = max(1, int(start))
        page = rows[start - 1:start - 1 + max(0, int(count))]
        return {"kind": "page", "result": ref, "start": start, "total": len(rows),
                "rows": [{k: t.get(k) for k in ("id", "title", "artist", "album", "path")} for t in page]}

    def c_read_tags(self, items="selection", **_from):
        tracks = self._tracks(items)
        if len(tracks) > 200:
            raise ScriptError("TOO_MANY", tr("一次最多读 200 首的文件标签"))
        def read():
            out = []
            for t in tracks:
                f = tagfile.read(t["path"])
                out.append({"track_id": t["id"], "title": t.get("title"), "path": t["path"], "fields": f.fields, "error": f.error})
            return {"kind": "tags", "count": len(out), "items": out}
        return self._console_local(read)

    def c_settings(self, name=None):
        w, p = self.win, self.win.prefs
        values = {"language": p.get("language") or "zh", "theme": w.theme, "album_ignore_artist": bool(p.get("albums_ignore_artist")),
                  "exclusive": bool(p.get("player_exclusive")), "volume": w.player.volume, "music_folders": w.music_roots(),
                  "midpoint_dbfs": p.get("midpoint_dbfs", 0.0), "correction_limit_db": p.get("correction_limit_db", 4.0),
                  "peak_correction_enabled": p.get("peak_correction_enabled", True)}
        if name:
            if name not in values:
                raise ScriptError("NOT_FOUND", tr("没有这个设置"), {"choices": list(values)})
            return {name: values[name]}
        return values

    # ------------------------------------------------------------------ playback
    def _ready(self):
        if not self.win.player.available:
            raise ScriptError("PLAYER_UNAVAILABLE", tr("播放器没有加载起来"))

    def c_set_volume(self, volume):
        v = str(volume).strip().rstrip("%").strip()
        try:
            v = round(float(v))
        except ValueError:
            raise ScriptError("ARGUMENTS", tr("音量是 0–100 的数字"))
        if not 0 <= v <= 100:
            raise ScriptError("ARGUMENTS", tr("音量是 0–100 的数字"))
        self.win.player.set_volume(v)
        return {"volume": self.win.player.volume}

    def c_pause(self):
        self.win.player.pause()
        return {"player": "paused"}

    def c_stop(self):
        self.win.player.stop()
        return {"player": "stopped"}

    def c_next_track(self):
        self.win.player.next()
        return self.c_now_playing()

    def c_previous_track(self):
        self.win.player.previous()
        return self.c_now_playing()

    def c_seek(self, position):
        s = str(position).strip()
        try:
            parts = [float(x) for x in s.split(":")]
        except ValueError:
            raise ScriptError("ARGUMENTS", tr("位置写成秒或 m:ss"))
        secs = 0.0
        for x in parts:
            secs = secs * 60 + x
        self.win.player.seek(int(secs * 1000))
        return {"position": secs}

    def c_set_play_mode(self, mode):
        m = self.modes.get(key(mode))
        if m is None:
            raise ScriptError("ARGUMENTS", tr("播放模式："), {"choices": ["order", "repeat_all", "repeat_one", "shuffle"]})
        self.win.player.set_mode(m)
        return {"mode": m}

    def c_play(self, items=None, start=1, shuffle=False):
        self._ready()
        if items is None:
            self.win.player.play()
            return self.c_now_playing()
        tracks = self._tracks(items)
        if shuffle:
            self.win.player.set_mode("shuffle")
        self.win.play_rows(tracks, max(0, int(start) - 1))
        return self.c_now_playing()

    def c_play_view(self, start=1):
        self._ready()
        v = self._visible_view()
        rows = v.model().rows if v is not None else []
        if not rows:
            raise ScriptError("EMPTY_VIEW", tr("当前页面没有歌曲列表"))
        self.win.play_rows(rows, max(0, int(start) - 1))
        return self.c_now_playing()

    def c_add_to_queue(self, items="selection", next=False):
        self._ready()
        paths = [t["path"] for t in self._tracks(items)]
        (self.win.play_next_paths if next else self.win.enqueue_paths)(paths)
        return {"queued": len(paths)}

    def c_set_exclusive(self, enabled=True):
        self.win.prefs.set("player_exclusive", bool(enabled))
        self.win.player.set_exclusive(bool(enabled))
        self.win.pages["settings"].refresh()
        box = self.win.pages["settings"].exclusive
        box.blockSignals(True)
        box.setChecked(bool(enabled))
        box.blockSignals(False)
        return {"exclusive": bool(enabled)}

    # ------------------------------------------------------------------ view
    def c_show_page(self, page):
        k = self.page_names.get(key(page))
        if k is None:
            raise ScriptError("NOT_FOUND", tr("没有这个页面"), {"choices": sorted(set(PAGES.values()))})
        if self.win.player_mode:
            self.win.toggle_player_mode(False)
        self.win.show_page(k)
        return self.c_view_state()

    def c_open_album(self, album, artist=None):
        hits = [a for a in self.view.albums if folded(a.title) == folded(album)]
        if artist is not None:
            hits = [a for a in hits if folded(artist) in {folded(n) for n in a.artists}]
        if len(hits) != 1:
            raise ScriptError("AMBIGUOUS" if hits else "NOT_FOUND", tr("加上 artist= 指明是哪张") if hits else tr("没有这张专辑"),
                              {"choices": [{"name": a.title, "artist": a.artist} for a in hits]})
        self.win.open_album(hits[0])
        if self.win.player_mode:
            self.win.toggle_player_mode(False)
        return self.c_view_state()

    def _open_group(self, page_key, name):
        self.win.show_page(page_key)
        page = self.win.pages[page_key]
        for i in range(page.list.count()):
            if folded(page.list.item(i).data(0x0100)) == folded(name):
                page.list.setCurrentRow(i)
                return dict(self.c_view_state(), group=page.list.item(i).data(0x0100))
        raise ScriptError("NOT_FOUND", tr("没有「{0}」", name),
                          {"choices": [page.list.item(i).data(0x0100) for i in range(min(page.list.count(), 20))]})

    def c_open_artist(self, artist):
        return self._open_group("artists", artist)

    def c_open_genre(self, genre):
        return self._open_group("genres", genre)

    def c_open_resolution(self, resolution):
        return self._open_group("resolution", resolution)

    def c_open_playlist(self, playlist):
        pl = self._playlist(playlist)
        self.win.open_playlist(pl["id"])
        return self.c_view_state()

    def c_open_folder(self, folder):
        w = self.win
        page = w.pages["folders"]
        text = str(folder).replace("\\", "/").strip("/")
        for name, node in page.root.folders.items():          # a music folder by name or path, then sub-folders
            root = (node.root or "").replace("\\", "/").rstrip("/")
            if folded(text) == folded(root) or folded(text).startswith(folded(root) + "/"):
                rest = text[len(root):].strip("/")
                path = (name,) + tuple(p for p in rest.split("/") if p)
                break
            if folded(text) == folded(name) or folded(text).startswith(folded(name) + "/"):
                path = (name,) + tuple(p for p in text[len(name):].split("/") if p)
                break
        else:
            raise ScriptError("NOT_FOUND", tr("不在音乐文件夹里"), {"choices": list(page.root.folders)})
        w.show_page("folders")
        if catalog.find_folder(page.root, path) is None:
            raise ScriptError("NOT_FOUND", tr("没有这个文件夹"))
        page.open(path)
        return dict(self.c_view_state(), folder="/".join(path))

    def c_search(self, text=""):
        w = self.win
        w.open_search()
        w.search.setText(str(text))
        w._apply_search()
        return {"search": str(text), "tracks": len(w.view.search_tracks(str(text)))}

    def c_clear_search(self):
        self.win.close_search(force=True)
        self.win._apply_search()
        return {"search": ""}

    def c_go_back(self):
        self.win._back()
        return self.c_view_state()

    def c_select(self, items, mode="replace", target="tracks"):
        if key(target) == "albums":
            return self._select_albums(items, mode)
        if key(target) != "tracks":
            raise ScriptError("ARGUMENTS", "target: tracks, albums")
        paths = [t["path"] for t in self._tracks(items)]
        m = key(mode)
        cur = list(self.win.selection)
        if m == "add":
            paths = cur + [p for p in paths if p not in cur]
        elif m == "remove":
            drop = set(paths)
            paths = [p for p in cur if p not in drop]
        elif m != "replace":
            raise ScriptError("ARGUMENTS", "mode", {"choices": ["replace", "add", "remove"]})
        self._show_selection(paths)
        return self.c_selected()

    def _show_selection(self, paths: list[str]) -> None:
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        v = self._visible_view()
        if paths and (v is None or set(paths) - {r.get("path") for r in v.model().rows}):
            self.win.close_search(force=True)
            self.win._apply_search()
            self.c_show_page("tracks")
            v = self._visible_view()
        if v is not None:
            m, sm, want = v.model(), v.selectionModel(), set(paths)
            sel = QItemSelection()
            for r, row in enumerate(m.rows):
                if row.get("path") in want:
                    sel.select(m.index(r, 0), m.index(r, m.columnCount() - 1))
            sm.blockSignals(True)
            sm.select(sel, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            sm.blockSignals(False)
            v.viewport().update()
        self.win.set_selection_paths(paths)

    def c_select_all(self):
        page = self.win.pages["albums"]
        if self.win.current_page() is page and page.stack.currentIndex() == 0:
            page.grid.selectAll()
            return self.c_selected()
        v = self._visible_view()
        if v is None:
            raise ScriptError("EMPTY_VIEW", tr("当前页面没有歌曲列表"))
        self._show_selection([r["path"] for r in v.model().rows if r.get("path") in self.view.by_path])
        return self.c_selected()

    def c_clear_selection(self):
        page = self.win.pages["albums"]
        if self.win.current_page() is page and page.stack.currentIndex() == 0:
            page.grid.clearSelection()
        self._show_selection([])
        return self.c_selected()

    def c_set_album_grouping(self, ignore_artist=False):
        self.win.set_albums_ignore_artist(bool(ignore_artist))
        self.win.pages["settings"].refresh()
        return {"ignore_artist": bool(ignore_artist), "albums": len(self.view.albums)}

    def c_set_theme(self, theme):
        t = {"titanium": "titanium", "ti": "titanium", "aluminum": "aluminum", "aluminium": "aluminum",
             "al": "aluminum", "dark": "aluminum", "light": "titanium"}.get(key(theme))
        if t is None:
            raise ScriptError("ARGUMENTS", "theme", {"choices": ["titanium", "aluminum"]})
        self.win.apply_theme(t, save=True)
        return {"theme": t}

    def c_set_language(self, language):
        lang = {"zh": "zh", "chinese": "zh", key("简体中文"): "zh", "en": "en", "english": "en"}.get(key(language))
        if lang is None:
            raise ScriptError("ARGUMENTS", "language", {"choices": ["zh", "en"]})
        self.win.prefs.set("language", lang)
        page = self.win.pages["settings"]
        for code, button in page.language.items():
            button.blockSignals(True)
            button.setChecked(code == lang)
            button.blockSignals(False)
        page.lang_note.setText(tr("重新打开程序后生效"))
        return {"language": lang, "restart": tr("重新打开程序后生效")}

    # ------------------------------------------------------------------ library changes
    def c_set_favorite(self, items="selection", enabled=True):
        paths = [t["path"] for t in self._tracks(items)]
        self.win.state.set_favorites(paths, bool(enabled))
        return {"favorite": bool(enabled), "tracks": len(paths)}

    def c_create_playlist(self, name="auto", items=None, open=False, edit=False):
        st = self.win.state
        if not self.win.backend.ready:
            raise ScriptError("BACKEND_UNAVAILABLE", tr("后端服务没有运行"))
        name = st.unique_name() if name in (None, "", "auto") else str(name)
        if any(p["name"] == name for p in st.playlist_list()):
            raise ScriptError("NAME_CONFLICT", tr("已经有叫「{0}」的歌单", name))
        paths = [t["path"] for t in self._tracks(items)] if items is not None else []
        got = {}
        st.create_playlist(name, paths, ok=lambda pid: got.setdefault("pid", pid))

        def result():
            self.win.reload_playlists()
            if open or edit:
                self.win.open_playlist(got["pid"])
            return {"kind": "playlist", "id": got["pid"], "name": name, "tracks": len(paths)}
        return Wait(lambda: "pid" in got, result)

    def c_add_to_playlist(self, playlist, items="selection", before=None, create=False):
        tracks = self._tracks(items)
        pl = self._playlist(playlist, must=not create)
        if pl is None:
            return self.c_create_playlist(str(playlist), self._ref(tracks))
        at = None if before in (None, "end") else max(0, int(before) - 1)
        n = self.win.state.append(pl["id"], [t["path"] for t in tracks], at)
        return self._wait_playlist(pl["id"], lambda: {"playlist": pl["name"], "added": n,
                                                      "tracks": len(self.win.state.playlist(pl["id"])["items"])})

    def _positions(self, pl, positions, song, occurrence, artist, album) -> list[int]:
        items = pl.get("items", [])
        if song is not None or occurrence is not None:
            if song is None or occurrence is None or positions:
                raise ScriptError("ARGUMENTS", tr("song 和 occurrence 一起给，或者只给 positions"))
            tid = self._tracks(self.one_track(song, artist, album))[0]["id"]
            hits = [i for i, o in enumerate(items, 1) if o.get("track_id") == tid]
            if not 1 <= int(occurrence) <= len(hits):
                raise ScriptError("NOT_FOUND", tr("这首歌在歌单里出现了 {0} 次", len(hits)), {"choices": hits})
            positions = [hits[int(occurrence) - 1]]
        rows = []
        for p in positions if isinstance(positions, list) else [positions]:
            if not isinstance(p, int) or not 1 <= p <= len(items):
                raise ScriptError("ARGUMENTS", tr("位置从 1 开始，这个歌单有 {0} 首", len(items)))
            rows.append(p - 1)
        if not rows:
            raise ScriptError("ARGUMENTS", tr("要给出位置（positions=[3]）或 song= 和 occurrence="))
        return rows

    def c_remove_from_playlist(self, playlist, positions=None, song=None, occurrence=None, artist=None, album=None):
        pl = self._playlist(playlist)
        rows = self._positions(pl, positions or [], song, occurrence, artist, album)
        self.win.state.remove_rows(pl["id"], rows)
        return self._wait_playlist(pl["id"], lambda: {"playlist": pl["name"], "removed": len(rows),
                                                      "tracks": len(self.win.state.playlist(pl["id"])["items"])})

    def c_move_playlist_items(self, playlist, positions=None, before=None, song=None, occurrence=None, artist=None, album=None):
        pl = self._playlist(playlist)
        rows = self._positions(pl, positions or [], song, occurrence, artist, album)
        n = len(pl.get("items", []))
        target = n if before in (None, "end") else int(before) - 1
        if not 0 <= target <= n:
            raise ScriptError("ARGUMENTS", tr("before 是 1–{0} 或 end", n))
        new = self.win.state.move_rows(pl["id"], rows, target)
        return self._wait_playlist(pl["id"], lambda: {"playlist": pl["name"], "positions": [r + 1 for r in new]})

    def c_rename_playlist(self, playlist, name):
        pl = self._playlist(playlist)
        done = {}
        self.win.state.rename_playlist(pl["id"], str(name), ok=lambda r: done.setdefault("ok", True))
        return Wait(lambda: "ok" in done or (self.win.state.playlist(pl["id"]) or {}).get("name") == str(name),
                    lambda: {"playlist": str(name)})

    def c_delete_playlist(self, playlist):
        pl = self._playlist(playlist)
        pid, name = pl["id"], pl["name"]
        if self.win.pages["playlist"].pid == pid:
            self.win.pages["playlist"].pid = None
            self.win.show_playlists()
        self.win.state.delete_playlist(pid)
        return Wait(lambda: self.win.state.playlist(pid) is None, lambda: {"deleted": name, "undo": "undo()"})

    def c_set_tags(self, fields, items="selection", preview=False):
        if not isinstance(fields, dict) or not fields:
            raise ScriptError("ARGUMENTS", "Supply a nonempty native tag field map")
        changes = copy.deepcopy(fields)
        return self._script_plan({"kind": "tags", "changes": changes}, items, preview)

    def c_scan(self, folders=None, full=False):
        w, st = self.win, self.win.state
        roots = None
        if folders is not None:
            if not folders:
                raise ScriptError("EMPTY_SELECTION", "An empty folder list never means all roots; omit folders to scan the enabled roots")
            norm = lambda p: folded(p).rstrip("/")
            roots = []
            for f in folders:
                r = next((r for r in w.music_roots() if norm(r) == norm(f) or folded(Path(r).name) == folded(f)), None)
                if r is None:
                    raise ScriptError("NOT_FOUND", tr("不是已添加的音乐文件夹：{0}", f), {"choices": w.music_roots()})
                roots.append(r)
        if st.busy:
            raise ScriptError("BUSY", tr("正在{0}，等它结束再来", st.busy))
        w.start_scan(roots)
        seen = {"started": False}

        def ready():
            seen["started"] = seen["started"] or bool(st.busy)
            return seen["started"] and not st.busy
        return Wait(ready, lambda: {"tracks": len(self.view.tracks), "albums": len(self.view.albums)}, timeout=6 * 3600,
                    cancel=st.cancel_job)

    def c_undo(self, receipt="last"):
        st = self.win.state
        if not st.undo_stack:
            raise ScriptError("NOTHING_TO_UNDO", tr("没有可以撤销的操作"))
        label = st.undo_stack[-1][1]
        st.undo()
        return {"undo": label}

    # ------------------------------------------------------------------ printing
    def sentence(self, name, v) -> str | None:
        """One short line for a command that changed something: what changed and what it is now."""
        if not isinstance(v, dict) or not name:
            return None
        onoff = lambda b: tr("开") if b else tr("关")
        from .player import MODE_NAMES
        now = lambda: (tr("▶ {0} — {1}", v.get("title"), v.get("artist") or "") if v.get("title") else tr("没有在播放"))
        pages = {k: label for label, k, _ in self._nav()}
        def where():
            out = pages.get(v.get("page"), v.get("page") or "")
            for k in ("album", "playlist", "group", "folder"):
                if v.get(k):
                    out += " · " + tr("「{0}」", v[k])
            return tr("页面：{0}", out)
        table = {
            "set_volume": lambda: tr("音量 {0}", v.get("volume")),
            "pause": lambda: tr("已暂停"), "stop": lambda: tr("已停止"),
            "next_track": now, "previous_track": now, "play": now, "play_view": now, "now_playing": now,
            "seek": lambda: tr("跳到 {0}", f"{int(v['position']) // 60}:{int(v['position']) % 60:02d}"),
            "set_play_mode": lambda: tr("播放模式：{0}", MODE_NAMES.get(v.get("mode"), v.get("mode"))),
            "add_to_queue": lambda: tr("已加入播放队列：{0} 首", v.get("queued")),
            "set_exclusive": lambda: tr("独占模式：{0}", onoff(v.get("exclusive"))),
            "show_page": where, "open_album": where, "open_artist": where, "open_genre": where,
            "open_resolution": where, "open_playlist": where, "open_folder": where, "go_back": where, "view_state": where,
            "search": lambda: tr("搜索「{0}」：{1} 首", v.get("search"), v.get("tracks")),
            "clear_search": lambda: tr("已清除搜索"),
            "set_album_grouping": lambda: tr("专辑无视歌手：{0} · {1} 张专辑", onoff(v.get("ignore_artist")), v.get("albums")),
            "set_theme": lambda: tr("外观：{0}", v.get("theme")),
            "set_language": lambda: tr("语言：{0}（重新打开程序后生效）", v.get("language")),
            "set_favorite": lambda: (tr("已加入最爱：{0} 首", v.get("tracks")) if v.get("favorite")
                                     else tr("已取消最爱：{0} 首", v.get("tracks"))),
            "create_playlist": lambda: tr("已建歌单「{0}」：{1} 首", v.get("name"), v.get("tracks")),
            "add_to_playlist": lambda: tr("「{0}」加了 {1} 首，现在 {2} 首", v.get("playlist"), v.get("added"), v.get("tracks")),
            "remove_from_playlist": lambda: tr("「{0}」删了 {1} 首，现在 {2} 首", v.get("playlist"), v.get("removed"), v.get("tracks")),
            "move_playlist_items": lambda: tr("「{0}」：移到第 {1} 位", v.get("playlist"), tr("、").join(str(p) for p in v.get("positions", []))),
            "rename_playlist": lambda: tr("歌单已改名为「{0}」", v.get("playlist")),
            "delete_playlist": lambda: tr("已删除歌单「{0}」（undo() 可以恢复）", v.get("deleted")),
            "scan": lambda: tr("曲库已更新：{0} 首 · {1} 张专辑", v.get("tracks"), v.get("albums")),
            "undo": lambda: tr("已撤销：{0}", v.get("undo")),
        }
        if name == "set_tags":
            if v.get("kind") == "plan":
                return None  # Preserve the complete frozen plan and digest in the normal result renderer.
            return None      # Actual source-write journal/backup receipt; not ordinary GUI undo.
        if name == "status":
            return "\n".join([tr("曲库 {0} 首 · {1} 张专辑 · {2} 个歌单 · 最爱 {3} 首", v.get("tracks"), v.get("albums"),
                                 v.get("playlists"), v.get("favorites")),
                              tr("播放器 {0} · 音量 {1} · {2}", v.get("player"), v.get("volume"),
                                 MODE_NAMES.get(v.get("mode"), v.get("mode"))),
                              tr("页面：{0} · 已选 {1} 首", pages.get(v.get("page"), v.get("page")), v.get("selected"))])
        fn = table.get(name)
        return fn() if fn else None

    def _nav(self):
        from .main_window import NAV, TOOLS
        return list(NAV) + list(TOOLS) + [(tr("歌单"), "playlist", "")]

    def render(self, value) -> str:
        if value is None:
            return tr("完成")
        if isinstance(value, dict):
            if "text" in value and len(value) == 1:
                return value["text"]
            kind = value.get("kind")
            if kind in ("plan", "plan_page"):
                rows = self._console_plan_rows(value) if kind == "plan" else value["rows"]
                start, total = value.get("start", 1), value.get("total", len(rows))
                lines = [f"plan_id: {value.get('plan_id')}  digest: {value.get('digest')}"]
                if kind == "plan":
                    payload = value.get("plan") or {}
                    lines.append(f"{payload.get('kind', payload.get('schema'))}: {total} proposed units; writes have not run")
                    if "ready" in payload:
                        lines.append(f"ready: {payload['ready']}  root: {payload.get('root')}")
                shown = rows[:SHOWN] if kind == "plan" else rows
                lines += [f"  {number}. {str(row)[:2000]}" for number, row in enumerate(shown, start)]
                if start - 1 + len(shown) < total:
                    lines.append(f"  … {total - (start - 1 + len(shown))} more; read_results(plan, start={start + len(shown)})")
                return "\n".join(lines)
            if kind == "tracks":
                rows = [self._by_id(i) for i in value.get("ids", [])]
                lines = [tr("{0} 首", value.get("count", len(rows)))]
                lines += [f"  {i}. {t.get('title') or Path(t['path']).stem} — {t.get('artist') or ''}"
                          for i, t in enumerate([r for r in rows if r][:SHOWN], 1)]
                if len(rows) > SHOWN:
                    lines.append(tr("  …还有 {0} 首", len(rows) - SHOWN))
                if value.get("current"):
                    lines.append(tr("  正在播放第 {0} 首", value["current"]))
                return "\n".join(lines)
            if kind == "entries":
                return tr("歌单「{0}」：{1} 首", value.get("playlist"), value.get("count"))
            if kind == "page":
                return "\n".join([tr("第 {0}–{1} 首，共 {2} 首", value["start"], value["start"] + len(value["rows"]) - 1, value["total"])]
                                 + [f"  {i}. {r.get('title')} — {r.get('artist') or ''}" for i, r in enumerate(value["rows"], value["start"])])
            if kind == "tags":
                out = []
                for it in value["items"][:SHOWN]:
                    f = {k: v for k, v in (it.get("fields") or {}).items() if v}
                    out.append(f"  {Path(it['path']).name}: " + ("; ".join(f"{k}={v}" for k, v in f.items()) or it.get("error") or "—"))
                return "\n".join([tr("{0} 首", value["count"])] + out)
            if kind in ("albums", "artists", "genres", "playlists", "folders", "outputs"):
                items = value.get("items", [])
                lines = [f"{len(items)}"] + ["  " + " · ".join(str(v) for k, v in it.items() if k != "id" and v not in (None, ""))
                                             for it in items[:SHOWN * 2]]
                if len(items) > SHOWN * 2:
                    lines.append(tr("  …还有 {0} 个", len(items) - SHOWN * 2))
                return "\n".join(lines)
            return "  ".join(f"{k}: {self._short(v)}" for k, v in value.items()
                             if k not in ("kind", "id", "playlist_id") and v not in (None, "", []))
        if isinstance(value, list):
            return "\n".join(self.render(v) for v in value[:SHOWN])
        return str(value)

    @staticmethod
    def _short(v) -> str:
        if isinstance(v, list):
            return ", ".join(str(x) for x in v[:4]) + (" …" if len(v) > 4 else "")
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    @staticmethod
    def _console_plan_rows(value):
        payload = value.get("plan") or {}
        rows = []
        for group in ("writes", "moves", "files", "playlists", "actions", "issues", "operations", "acknowledgements", "warnings", "roots", "presets"):
            for row in payload.get(group, []):
                if not isinstance(row, dict):
                    rows.append({"unit": group, "value": row})
                    continue
                rows.append({"unit": group, **{k: v for k, v in row.items() if k not in
                             ("data_base64", "data_b64", "audio_identity", "untouched", "target")}})
        return rows


__all__ = ["LiveConsole", "Wait"]
