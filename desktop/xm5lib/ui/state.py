"""The library as the backend reports it, mirrored for the pages, and the edits they make through it.

Pages keep working with track dicts and paths (drag data, selections); this layer maps paths to the
backend's stable track IDs. Every edit is a backend command with its resource revision:

* playlist edits of one playlist run strictly one after another; each uses the revision the previous one
  returned. Re-orders are shown at once and sent in the background; a burst of drags is collapsed into
  one ``playlists.replace`` with the exact occurrence list (repeated songs stay separate entries);
* a ``REVISION_CONFLICT`` (someone else changed it, e.g. ``xm5ctl``) re-reads the playlist and says so --
  a stale edit is never forced;
* undoable receipts are kept so Ctrl+Z can ask the backend to undo them (conditional undo).
"""
from __future__ import annotations

import collections
import os
import shutil
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from .libview import View
from . import tagfile
from .service import Backend, ServiceError
from .i18n import tr

PAGE = 1000


def norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


class FavoritesView:
    """The four methods the pages use (is_fav / paths / set / toggle), backed by the backend."""

    def __init__(self, state: "LibraryState"):
        self.state = state

    def is_fav(self, path: str) -> bool:
        tid = self.state.id_for(path)
        return tid is not None and tid in self.state.fav_set

    def paths(self) -> list[str]:
        by = self.state.by_id
        return [by[i]["path"] for i in self.state.fav_ids if i in by]

    def set(self, paths: list[str], on: bool) -> None:
        self.state.set_favorites(paths, on)

    def toggle(self, paths: list[str]) -> bool:
        on = not all(self.is_fav(p) for p in paths)
        self.set(paths, on)
        return on


class LibraryState(QObject):
    loaded = Signal()                 # everything (re)read: tracks, playlists, favourites, presets
    tracksChanged = Signal()
    playlistsChanged = Signal()       # names / covers / membership of the list of playlists
    playlistChanged = Signal(str)     # one playlist's items or details
    favoritesChanged = Signal()
    presetsChanged = Signal()
    notice = Signal(str)
    problem = Signal(str)
    undoChanged = Signal()
    tagsWritten = Signal(list, bool)  # paths written by the tag editor, whether a cover changed
    busyChanged = Signal(str)         # "" | "scan" | "import"
    jobProgress = Signal(object)      # the running job's progress record ({phase, current, total, path})

    def __init__(self, backend: Backend, staging: Path, roots=lambda: [], ignore_artist=lambda: False, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.staging = Path(staging)      # inside an import root: picked cover images are copied here first
        self.roots = roots                # configured music roots, original letter case (for display paths)
        self.ignore_artist = ignore_artist  # 专辑无视歌手: albums grouped by title alone
        self.tracks: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self._ids: dict[str, str] = {}
        self.view = View([])
        self.revision = None
        self.playlists: dict[str, dict] = {}
        self.fav_ids: list[str] = []
        self.fav_set: set[str] = set()
        self.fav_revision = None
        self.presets: list[dict] = []
        self.presets_revision = None
        self.undo_stack: list[tuple[str, str, str]] = []     # (receipt_id, label, kind)
        self.busy = ""
        self.job = None
        self.is_loaded = False
        self.favorites = FavoritesView(self)
        self._pl_ops: dict[str, collections.deque] = {}
        self._pl_busy: set[str] = set()
        self._view_timer = QTimer(self, singleShot=True, interval=60, timeout=self._rebuild_view)

    # ------------------------------------------------------------------ identity
    def id_for(self, path) -> str | None:
        return self._ids.get(norm(path)) if path else None

    def ids_for(self, paths) -> list[str]:
        out = []
        for p in paths:
            tid = self.id_for(p)
            if tid is not None:
                out.append(tid)
        return out

    def track(self, tid: str) -> dict | None:
        return self.by_id.get(tid)

    def _ingest(self, raw: dict, roots: dict | None = None) -> dict:
        t = dict(raw)
        t["key"] = raw.get("path")
        if roots is None:
            roots = {norm(r): r for r in self.roots()}
        locs = t.get("locations") or []
        # A still-present old copy must not hide the user's selected source.
        loc = next((l for l in locs if l.get("present") and norm(l.get("root", "")) in roots), None)
        if loc is None:
            loc = next((l for l in locs if l.get("present")), locs[0] if locs else None)
        if loc and loc.get("path"):
            t["path"] = loc["path"]
            if loc.get("root"):
                try:
                    t["rel"] = Path(loc["path"]).relative_to(Path(loc["root"])).as_posix()
                except ValueError:
                    pass
        root = roots.get(norm(loc["root"])) if loc and loc.get("root") else None
        if root is None and t.get("key"):                      # scanned under another root spelling
            k = norm(t["key"])
            root = next((r for n, r in roots.items() if k.startswith(n.rstrip("\\/") + os.sep)), None)
        t["root"] = root                                       # None: not in a music folder of the window
        if root and t.get("rel") and not t.get("unresolved"):
            t["path"] = str(Path(root) / t["rel"])           # the file's own letter case, for display
        if not t.get("path"):
            t["path"] = t["key"] or f"<{t.get('id')}>"
        return t

    def _set_tracks(self, raw: list[dict], revision) -> None:
        self._raw = raw
        roots = {norm(r): r for r in self.roots()}
        self.tracks = [self._ingest(t, roots) for t in raw]
        self.by_id = {t["id"]: t for t in self.tracks}
        self._ids = {}
        for t in self.tracks:
            self._ids[norm(t["path"])] = t["id"]
            if t.get("key"):
                self._ids[norm(t["key"])] = t["id"]
            for loc in t.get("locations") or []:
                self._ids.setdefault(norm(loc["path"]), t["id"])
        self.revision = revision
        self._rebuild_view()

    def _rebuild_view(self) -> None:
        """The library the pages show: present files inside the window's music folders."""
        self.view = View([t for t in self.tracks if not t.get("unresolved") and t.get("root")], self.ignore_artist())
        self.tracksChanged.emit()

    def regroup(self) -> None:
        """The album grouping changed (专辑无视歌手)."""
        self._rebuild_view()

    def reindex(self) -> None:
        """The music folders changed: re-sort the tracks already known into them."""
        self._set_tracks(getattr(self, "_raw", []), self.revision)

    # ------------------------------------------------------------------ loading
    def load_all(self) -> None:
        def fetch(client):
            tracks, revision, restarts = [], None, 0
            offset = 0
            while True:
                page = Backend.unwrap(client.query("tracks.search", {"limit": PAGE, "offset": offset}))
                if revision is None:
                    revision = page["revision"]
                elif page["revision"] != revision and restarts < 5:      # library changed while paging
                    tracks, offset, revision, restarts = [], 0, page["revision"], restarts + 1
                    continue
                tracks += page["tracks"]
                offset += len(page["tracks"])
                if not page["tracks"] or offset >= page["total"]:
                    break
            pls = Backend.unwrap(client.query("playlists.list"))
            favs = Backend.unwrap(client.query("favorites.list"))
            presets = Backend.unwrap(client.query("presets.list"))
            return tracks, revision, pls, favs, presets

        def done(result):
            tracks, revision, pls, favs, presets = result
            self._set_tracks(tracks, revision)
            self._set_playlists(pls["playlists"])
            self._set_favorites(favs)
            self.presets, self.presets_revision = presets["presets"], presets["revision"]
            self.presetsChanged.emit()
            self.is_loaded = True
            self.loaded.emit()
        self.backend.run(fetch, ok=done, err=lambda e: self.problem.emit(tr("读取曲库失败：{0}", e.message)))

    def refresh_playlists(self) -> None:
        self.backend.query("playlists.list", ok=lambda r: self._set_playlists(r["playlists"]),
                           err=lambda e: self.problem.emit(tr("读取歌单失败：{0}", e.message)))

    def refresh_playlist(self, pid: str, why: str = "") -> None:
        def got(r):
            self.playlists[pid] = r["playlist"]
            self.playlistChanged.emit(pid)
            self.playlistsChanged.emit()
            if why:
                self.problem.emit(why)

        def gone(e: ServiceError):
            if e.code == "NOT_FOUND":
                self.playlists.pop(pid, None)
                self.playlistsChanged.emit()
            if why:
                self.problem.emit(why)
        self.backend.query("playlists.get", {"id": pid}, ok=got, err=gone)

    def refresh_favorites(self) -> None:
        self.backend.query("favorites.list", ok=self._set_favorites,
                           err=lambda e: self.problem.emit(tr("读取最爱失败：{0}", e.message)))

    def refresh_presets(self, done=None) -> None:
        def got(r):
            self.presets, self.presets_revision = r["presets"], r["revision"]
            self.presetsChanged.emit()
            if done:
                done()
        self.backend.query("presets.list", ok=got, err=lambda e: self.problem.emit(tr("读取 PEQ 预设失败：{0}", e.message)))

    def _set_playlists(self, rows: list[dict]) -> None:
        self.playlists = {p["id"]: p for p in rows}
        self.playlistsChanged.emit()
        for pid in self.playlists:
            self.playlistChanged.emit(pid)

    def _set_favorites(self, r: dict) -> None:
        self.fav_ids = list(r.get("track_ids") or [])
        self.fav_set = set(self.fav_ids)
        self.fav_revision = r.get("revision")
        self.favoritesChanged.emit()

    # ------------------------------------------------------------------ playlists (read)
    def playlist_list(self) -> list[dict]:
        return sorted(self.playlists.values(), key=lambda p: (p["name"].casefold(), p["id"]))

    def playlist(self, pid) -> dict | None:
        return self.playlists.get(pid)

    def playlist_rows(self, pid) -> tuple[list[dict], set[str]]:
        """Display rows in play order; an unresolved entry stays a (red) row and keeps its place."""
        pl = self.playlists.get(pid)
        rows, missing = [], set()
        for item in (pl or {}).get("items", []):
            t = self.by_id.get(item["track_id"])
            row = dict(t) if t else {"path": item.get("path") or "?", "id": item["track_id"], "unresolved": True}
            row["occurrence"] = item
            if row.get("unresolved") or row["path"] not in self.view.by_path:
                missing.add(row["path"])
            rows.append(row)
        return rows, missing

    def unique_name(self, base: str = tr("新歌单"), numbered: bool = True) -> str:
        names = {p["name"] for p in self.playlists.values()}
        if not numbered and base not in names:
            return base
        n = 1 if numbered else 2
        while f"{base} {n}" in names:
            n += 1
        return f"{base} {n}"

    # ------------------------------------------------------------------ playlists (edit)
    def create_playlist(self, name: str, paths=(), ok=None) -> None:
        ids = self.ids_for(paths)

        def done(receipt):
            pl = receipt["result"]
            self.playlists[pl["id"]] = pl
            self._remember(receipt, tr("新建歌单「{0}」", pl['name']), "playlists")
            self.playlistsChanged.emit()
            if ok:
                ok(pl["id"])
        self.backend.command("playlists.create", {"name": name.strip(), "track_ids": ids}, ok=done,
                             err=lambda e: self.problem.emit(self._why(e, name)))

    def _why(self, e: ServiceError, name: str = "") -> str:
        if e.code == "NAME_CONFLICT":
            return tr("已经有叫「{0}」的歌单了，换个名字吧。", name.strip())
        if e.code == "INVALID_ARGUMENT" and "name" in e.message:
            return tr("歌单名不能为空。")
        if e.code == "INVALID_COVER":
            return tr("这张图片不能用作封面：{0}", e.message)
        if e.code == "PATH_NOT_ALLOWED":
            return tr("这个位置不在后端允许读取的文件夹里。")
        return tr("操作没有完成：{0}（{1}）", e.message, e.code)

    def _submit(self, pid: str, kind: str, make, done=None) -> None:
        self._pl_ops.setdefault(pid, collections.deque()).append((kind, make, done))
        self._pump(pid)

    def _pump(self, pid: str) -> None:
        if pid in self._pl_busy:
            return
        q = self._pl_ops.get(pid)
        while q and q[0][0] == "items" and any(k == "items" for k, _, _ in list(q)[1:]):
            q.popleft()                                         # a later re-order supersedes this one
        if not q:
            return
        kind, make, done = q.popleft()
        pl = self.playlists.get(pid)
        if pl is None:
            q.clear()
            return
        name, args, context = make(pl)
        self._pl_busy.add(pid)

        def ok(receipt):
            self._pl_busy.discard(pid)
            current = self.playlists.get(pid)
            if kind == "delete":
                self.playlists.pop(pid, None)
                self._pl_ops.pop(pid, None)
                self.playlistsChanged.emit()
            elif current is not None:
                current["revision"] = receipt["revision"]
                result = receipt.get("result") or {}
                if kind == "items":
                    for obj, item in zip(context, result.get("items", [])):
                        if not obj.get("occurrence_id"):
                            obj["occurrence_id"] = item["occurrence_id"]    # new entries get their IDs
                    if not any(k == "items" for k, _, _ in self._pl_ops.get(pid, ())):
                        current.update({k: result[k] for k in ("items", "count", "track_ids", "tracks") if k in result})
                else:
                    current.update({k: result[k] for k in ("name", "cover") if k in result})
                self.playlistsChanged.emit()
                self.playlistChanged.emit(pid)
            if done:
                done(receipt)
            self._pump(pid)

        def err(e: ServiceError):
            self._pl_busy.discard(pid)
            self._pl_ops.pop(pid, None)
            label = pl.get("name", "")
            if e.code == "REVISION_CONFLICT":
                self.refresh_playlist(pid, tr("歌单「{0}」刚在别处被修改过，已换成最新内容；你刚才这一步没有保存。", label))
            elif e.code in ("NAME_CONFLICT", "INVALID_COVER", "INVALID_ARGUMENT", "PATH_NOT_ALLOWED"):
                self.problem.emit(self._why(e, args.get("name", "")))
                self.refresh_playlist(pid)
            else:
                self.refresh_playlist(pid, tr("歌单「{0}」的修改没有保存：{1}（{2}）", label, e.message, e.code))
        self.backend.command(name, args, revision=pl["revision"], ok=ok, err=err)

    def set_items(self, pid: str, items: list[dict], label: str = tr("调整歌单顺序")) -> None:
        """Show ``items`` (occurrence dicts in play order) now; persist it in the background."""
        pl = self.playlists.get(pid)
        if pl is None:
            return
        pl["items"] = items
        pl["count"] = len(items)
        self.playlistChanged.emit(pid)
        self.playlistsChanged.emit()

        def make(current):
            objs = list(current["items"])
            body = [{"occurrence_id": o["occurrence_id"], "track_id": o["track_id"]} if o.get("occurrence_id")
                    else {"track_id": o["track_id"]} for o in objs]
            return "playlists.replace", {"id": pid, "items": body}, objs
        self._submit(pid, "items", make, lambda r: self._remember(r, tr("{0}「{1}」", label, pl['name']), "playlists"))

    def append(self, pid: str, paths, at: int | None = None) -> int:
        pl = self.playlists.get(pid)
        if pl is None:
            return 0
        new = [{"occurrence_id": None, "track_id": tid, "path": self.by_id[tid]["key"], "unresolved": False}
               for tid in self.ids_for(paths) if tid in self.by_id]
        if not new:
            return 0
        items = list(pl.get("items", []))
        at = len(items) if at is None else max(0, min(at, len(items)))
        items[at:at] = new
        self.set_items(pid, items, tr("加入歌单"))
        return len(new)

    def remove_rows(self, pid: str, rows: list[int]) -> None:
        pl = self.playlists.get(pid)
        if pl is None:
            return
        drop = set(rows)
        self.set_items(pid, [o for i, o in enumerate(pl.get("items", [])) if i not in drop], tr("从歌单移除"))

    def move_rows(self, pid: str, rows: list[int], target: int) -> list[int]:
        """Move ``rows`` (any selection) so they land before row ``target``; returns their new rows."""
        pl = self.playlists.get(pid)
        if pl is None or not rows:
            return []
        items = list(pl.get("items", []))
        rows = sorted(set(r for r in rows if 0 <= r < len(items)))
        moving = [items[r] for r in rows]
        keep = [o for i, o in enumerate(items) if i not in set(rows)]
        target -= sum(1 for r in rows if r < target)
        target = max(0, min(target, len(keep)))
        new = keep[:target] + moving + keep[target:]
        if [id(o) for o in new] != [id(o) for o in items]:
            self.set_items(pid, new)
        return list(range(target, target + len(moving)))

    def rename_playlist(self, pid: str, name: str, ok=None) -> None:
        name = name.strip()
        pl = self.playlists.get(pid)
        if pl is None or not name or name == pl["name"]:
            if ok:
                ok(None)
            return
        old = pl["name"]
        self._submit(pid, "rename", lambda cur: ("playlists.rename", {"id": pid, "name": name}, None),
                     lambda r: (self._remember(r, tr("重命名歌单「{0}」", old), "playlists"), ok and ok(r)))

    def set_cover(self, pid: str, image: str | None, ok=None) -> None:
        path = None
        if image:
            try:
                folder = self.staging / "covers" / uuid.uuid4().hex
                folder.mkdir(parents=True, exist_ok=True)
                path = str(folder / Path(image).name)
                shutil.copyfile(image, path)                 # the backend may read only its import roots
            except OSError as exc:
                self.problem.emit(tr("读不了这张图片：{0}", exc))
                return
        pl = self.playlists.get(pid)
        if pl is None:
            return
        self._submit(pid, "cover", lambda cur: ("playlists.cover", {"id": pid, "path": path}, None),
                     lambda r: (self._remember(r, tr("更换「{0}」的封面", pl['name']), "playlists"), ok and ok(r)))

    def delete_playlist(self, pid: str, ok=None) -> None:
        pl = self.playlists.get(pid)
        if pl is None:
            return
        name = pl["name"]
        self._submit(pid, "delete", lambda cur: ("playlists.delete", {"id": pid}, None),
                     lambda r: (self._remember(r, tr("删除歌单「{0}」", name), "playlists"), ok and ok(r)))

    # ------------------------------------------------------------------ favourites
    def set_favorites(self, paths, on: bool) -> None:
        ids = [i for i in self.ids_for(paths) if i in self.by_id]
        if not ids:
            return
        if on:                                                  # shown at once; the reply is authoritative
            self.fav_ids += [i for i in ids if i not in self.fav_set]
        else:
            drop = set(ids)
            self.fav_ids = [i for i in self.fav_ids if i not in drop]
        self.fav_set = set(self.fav_ids)
        self.favoritesChanged.emit()

        def done(receipt):
            self._set_favorites({"track_ids": receipt["result"]["track_ids"], "revision": receipt["revision"]})
            self._remember(receipt, tr("加入最爱 {0} 首", len(ids)) if on else tr("移出最爱 {0} 首", len(ids)), "favorites")

        def failed(e):
            self.problem.emit(tr("最爱没有保存：{0}", e.message))
            self.refresh_favorites()
        self.backend.command("favorites.set", {"track_ids": ids, "favorite": bool(on)}, ok=done, err=failed)

    # ------------------------------------------------------------------ notes / tag overrides
    def annotate(self, paths, overrides: dict | None, note: str | None, ok=None, remember: bool = True) -> None:
        """One ``tracks.annotate`` per track, each with that track's revision; source files untouched."""
        ids = [i for i in self.ids_for(paths) if i in self.by_id]
        pending = {"n": len(ids), "failed": []}
        if not ids:
            return

        def finish():
            pending["n"] -= 1
            if pending["n"] == 0:
                self._rebuild_view()
                if pending["failed"]:
                    self.problem.emit(tr("有 %d 首的修改没有保存：%s") % (len(pending["failed"]), pending["failed"][0]))
                elif ok:
                    ok()

        for tid in ids:
            args = {"id": tid}
            if overrides:
                args["overrides"] = overrides
            if note is not None:
                args["note"] = note

            def done(receipt, tid=tid):
                raw = getattr(self, "_raw", [])
                for i, r in enumerate(raw):
                    if r.get("id") == tid:
                        raw[i] = receipt["result"]
                        break
                fresh = self._ingest(receipt["result"])
                old = self.by_id.get(tid)
                if old is not None:
                    old.clear()
                    old.update(fresh)
                if remember:
                    self._remember(receipt, tr("修改标签/笔记"), "tracks")
                finish()

            def failed(e, tid=tid):
                pending["failed"].append(e.message)
                if e.code == "REVISION_CONFLICT":
                    self.backend.query("tracks.get", {"id": tid},
                                       ok=lambda r, tid=tid: self.by_id.get(tid, {}).update(self._ingest(r["track"])))
                finish()
            self.backend.command("tracks.annotate", args, revision=self.by_id[tid]["revision"], ok=done, err=failed)

    # ------------------------------------------------------------------ file tags (the tag editor)
    def write_tags(self, jobs: list, label: str, ok=None, remember: bool = True) -> None:
        """``jobs``: [(path, {field: text}, cover)] -- written into the files themselves (``tagfile``), then
        the backend reads exactly those files again (``library.refresh``: same track IDs, so playlists and
        favourites stay). Undo writes the previous values back. Library-only tag overrides of a written
        track are cleared: the file is the truth from then on."""
        jobs = [(p, dict(c), cov) for p, c, cov in jobs]

        def fn(client):
            written, undo, failed, tracks, refresh_error = [], [], [], [], None
            for path, changes, cover in jobs:
                try:
                    old = tagfile.read(path)
                    if old.error or not old.writable:
                        raise tagfile.TagError(old.error or tr("这种格式不能写标签"))
                    prev = {k: old.fields.get(k, "") for k in changes}
                    prev_cover = tagfile.KEEP if cover is tagfile.KEEP else old.cover
                    tagfile.write(path, changes, cover)
                except Exception as exc:       # one file failing never stops the others
                    failed.append((path, str(exc)))
                    continue
                written.append(path)
                undo.append((path, prev, prev_cover))
            if written:
                try:
                    receipt = Backend.unwrap(client.command("library.refresh", {"paths": written},
                                                            idempotency_key=uuid.uuid4().hex))
                    tracks = receipt["result"]["tracks"]
                except ServiceError as exc:
                    refresh_error = exc.message
                except Exception as exc:
                    refresh_error = str(exc)
            return written, undo, failed, tracks, refresh_error

        def done(result):
            written, undo, failed, tracks, refresh_error = result
            raw = getattr(self, "_raw", [])
            index = {r.get("id"): i for i, r in enumerate(raw)}
            stale = []
            for t in tracks:
                if t["id"] in index:
                    raw[index[t["id"]]] = t
                fresh = self._ingest(t)
                old = self.by_id.get(t["id"])
                if old is not None:
                    old.clear()
                    old.update(fresh)
                if t.get("overrides"):
                    stale.append(fresh["path"])
            if tracks:
                self._rebuild_view()
            if stale:
                self.annotate(stale, {f: None for f in tagfile.FIELDS if f != "composer"}, None, remember=False)
            if undo and remember:
                self.undo_stack.append((undo, label, "tags"))
                del self.undo_stack[:-40]
                self.undoChanged.emit()
            if failed:
                self.problem.emit(tr("{0} 首没有写入：{1}", len(failed), failed[0][1]) if len(failed) > 1 else
                                  tr("没有写入「{0}」：{1}", Path(failed[0][0]).name, failed[0][1]))
            if refresh_error:
                self.problem.emit(tr("标签已写入文件，但曲库没有更新（{0}）。按 F5 更新曲库。", refresh_error))
            if written:
                self.tagsWritten.emit(written, any(cov is not tagfile.KEEP for _p, _c, cov in jobs))
            if ok:
                ok(written, failed)
        self.backend.run(fn, ok=done, err=lambda e: self.problem.emit(tr("标签没有写入：{0}", e.message)))

    # ------------------------------------------------------------------ presets
    def save_preset(self, data: dict, ok=None, replace_name: str | None = None) -> None:
        def saved(receipt):
            self._remember(receipt, tr("保存预设「{0}」", data['name']), "presets")
            if replace_name and replace_name != data["name"]:
                self.backend.command("presets.delete", {"name": replace_name},
                                     ok=lambda r: self.refresh_presets(ok), err=lambda e: self.refresh_presets(ok))
            else:
                self.refresh_presets(ok)
        self.backend.command("presets.save", {"preset": data}, ok=saved,
                             err=lambda e: self.problem.emit(tr("预设没有保存：{0}", e.message)))

    def delete_preset(self, name: str, ok=None) -> None:
        self.backend.command("presets.delete", {"name": name},
                             ok=lambda r: (self._remember(r, tr("删除预设「{0}」", name), "presets"), self.refresh_presets(ok)),
                             err=lambda e: self.problem.emit(tr("预设没有删除：{0}", e.message)))

    def preset(self, name: str) -> dict | None:
        return next((p for p in self.presets if p.get("name") == name), None)

    # ------------------------------------------------------------------ jobs: scan / legacy import
    def scan(self, roots: list[str]) -> None:
        if self.busy:
            return
        self._job("scan", "library.scan", {"roots": roots}, tr("扫描"))

    def import_legacy(self, db: str) -> None:
        if self.busy:
            return
        self._job("import", "library.import_legacy", {"path": db}, tr("导入旧数据"))

    def _job(self, kind: str, name: str, args: dict, label: str) -> None:
        self.busy = kind
        self.busyChanged.emit(kind)

        def end():
            self.busy = ""
            self.job = None
            self.busyChanged.emit("")

        def accepted(r):
            self.job = self.backend.watch(r["job_id"], done=succeeded, failed=failed,
                                          progress=lambda job: self.jobProgress.emit(job.get("progress")))

        def succeeded(job):
            end()
            res = job.get("result") or {}
            res = res.get("result", res) if isinstance(res.get("result"), dict) else res     # the receipt's result
            if kind == "scan":
                bits = [tr("{0} 个文件", res.get('found', 0)), tr("新增 {0}", res.get('added', 0))]
                if res.get("relocated"):
                    bits.append(tr("移动 {0}", res['relocated']))
                if res.get("missing"):
                    bits.append(tr("不见了 {0}", res['missing']))
                if res.get("errors"):
                    bits.append(tr("{0} 个无法读取", len(res['errors'])))
                self.notice.emit(tr("扫描完成：") + tr("，").join(bits))
            elif res.get("already_imported"):
                self.notice.emit(tr("这个旧数据库已经导入过了。"))
            else:
                self.notice.emit(tr("已导入旧数据：{0} 首、{1} 个歌单、{2} 个预设", res.get('tracks', 0), res.get('playlists', 0), res.get('presets', 0)))
            self.load_all()

        def failed(job):
            end()
            if job.get("status") == "cancelled":
                self.notice.emit(tr("{0}已取消，曲库保持原样。", label))
            else:
                self.problem.emit(tr("{0}没有完成：{1}", label, job.get('error') or job.get('status')))
                self.load_all()

        def refused(e):
            end()
            self.problem.emit(tr("{0}没有开始：{1}", label, e.message))
        self.backend.command(name, args, ok=accepted, err=refused, lane="jobs")

    def cancel_job(self) -> None:
        if self.job is not None:
            self.job.cancel()

    # ------------------------------------------------------------------ undo
    def _remember(self, receipt, label: str, kind: str) -> None:
        if receipt and receipt.get("undoable") and receipt.get("receipt_id"):
            self.undo_stack.append((receipt["receipt_id"], label, kind))
            del self.undo_stack[:-40]
            self.undoChanged.emit()

    def undo(self) -> None:
        if not self.undo_stack:
            self.notice.emit(tr("没有可以撤销的操作。"))
            return
        rid, label, kind = self.undo_stack.pop()
        self.undoChanged.emit()
        if kind == "tags":                 # file tags: write the previous values back
            self.write_tags(rid, label, remember=False,
                            ok=lambda written, failed: written and self.notice.emit(tr("已撤销：{0}", label)))
            return

        def done(_r):
            self.notice.emit(tr("已撤销：{0}", label))
            if kind == "playlists":
                self.refresh_playlists()
            elif kind == "favorites":
                self.refresh_favorites()
            elif kind == "presets":
                self.refresh_presets()
            else:
                self.load_all()

        def failed(e):
            why = tr("之后又有别的修改") if e.code == "UNDO_CONFLICT" else e.message
            self.problem.emit(tr("没能撤销「{0}」：{1}", label, why))
        self.backend.command("undo", {"receipt_id": rid}, ok=done, err=failed)


__all__ = ["LibraryState", "FavoritesView", "norm"]
