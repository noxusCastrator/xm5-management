"""Main visual/console card workflow backed by the inline format owner."""
from hashlib import sha256
import json
from pathlib import Path
import time
import uuid

from ..application import DomainError
from ..core.bundled_runtime import require_tool
from ..core.inline_card import library


class InlineLibraryActions:
    def __init__(self, engine):
        self.engine = engine

    def dispatch(self, name, args, progress, cancel, revision=None, key=None):
        e = self.engine
        if name == "cards.plan":
            destination = e._under(args["destination"], e.export_roots)
            if destination.name != "xm5":
                raise DomainError("INVALID_ARGUMENT", "The managed folder must be named xm5", {})
            roots = [e._under(path, e.music_roots) for path in args["roots"]]
            eq = [e._under(path, e.import_roots + e.music_roots) for path in args.get("peq_roots", [])]
            snapshot = e.application.query("tracks.all", {})
            paths = {}
            references = {}
            selected_tracks = []
            for track in snapshot["tracks"]:
                if track.get("card_reference"):
                    references[track["id"]] = track["card_reference"]["path"]
                    if track.get("unresolved"):
                        continue
                candidates = ([track["path"]] if track.get("path") else []) + [
                    item["path"] for item in track.get("locations", []) if item.get("present")]
                path = next((str(Path(path).absolute()) for path in candidates
                             if any(Path(path).resolve().is_relative_to(root) for root in roots)), None)
                if path:
                    paths[track["id"]] = path
                    selected_tracks.append(dict(track, path=path))
            selected = [e.application.query("playlists.get", {"id": pid})["playlist"]
                        for pid in args.get("playlist_ids", [])]
            covers = {}
            for row in selected:
                if row.get("cover"):
                    covers[row["id"]] = row["cover"]["path"]
            # Managed application originals are trusted retained inputs. Public
            # replacements still require registration like any other file input.
            for pid, path in args.get("playlist_covers", {}).items():
                covers[pid] = str(e._under(path, e.music_roots + e.import_roots))
            from ..core.preset_sources import collect_presets
            from ..core import peq
            converted = collect_presets(eq, args.get("peq_source_ids"), cancel)
            available = {p["name"]: p for p in e.application.query("presets.list", {})["presets"]}
            presets = dict(converted["presets"])
            for preset in args.get("preset_names", []):
                if preset not in available:
                    raise DomainError("NOT_FOUND", "PEQ preset no longer exists", {})
                presets[preset] = peq.to_apo_text(peq.Preset.from_dict(available[preset]))
            with e.lyrics_lock:
                lyric_entries, _, _ = e.lyrics().for_export(selected_tracks, auto=False, progress=progress, cancel=cancel)
                lyrics = {}
                for tid, path in paths.items():
                    preferred = e.lyrics().store._preferred(tid)
                    if preferred:
                        lyrics[tid] = str(e.lyrics().store._path(preferred["normalized_rel"]))
                lyric_revision = e.lyrics().store.state_fingerprint()
            plan = library.make_plan(destination.parent, roots, eq,
                options=library.gain_options(args.get("midpoint_dbfs", 0), args.get("correction_limit_db", 4),
                                             args.get("peak_correction_enabled", True)),
                selected_playlists=selected, favorites=e.application.query("favorites.list", {})["track_ids"],
                track_paths=paths, card_references=references, presets=presets, covers=covers, lyrics=lyrics,
                existing=args.get("existing", "refuse"), cancel=cancel)
            plan["lyrics"] = {plan["track_paths"][tid]: source for tid, source in lyrics.items()
                              if tid in plan["track_paths"]}
            plan["lyric_originals"] = [row for row in lyric_entries if row["kind"] == "lyrics-original"]
            if plan["previous_build"]:
                with e.connect() as db:
                    receipts = [json.loads(row[0]) for row in db.execute(
                        "SELECT body FROM sync_baselines WHERE build=?", (plan["previous_build"],))]
                for receipt in sorted(receipts, key=lambda row: row.get("imported_at", 0)):
                    if receipt.get("input_path") and receipt.get("input_sha256"):
                        plan["acknowledged_playlists"][receipt["input_path"]] = receipt["input_sha256"]
                        for playlist in plan["playlists"]:
                            if playlist["id"] == receipt.get("pc_id") and receipt.get("bind_input", True):
                                playlist["path"] = receipt["input_path"]
            plan["warnings"].extend(converted["warnings"])
            if args.get("lyrics_auto_search"):
                plan["warnings"].append("Export uses retained local lyrics. Use explicit lyric search before export.")
            if plan["previous_build"]:
                from ..core.inline_card import delta
                delta.annotate(plan)
            from .engine import digest
            record = {"inline_plan": plan, "application_revision": snapshot["revision"], "lyrics_revision": lyric_revision}
            identifier, hashed = uuid.uuid4().hex, digest(record)
            with e.connect() as db:
                db.execute("INSERT INTO plans VALUES(?,?,?)", (identifier, hashed, json.dumps(record)))
            return {"plan_id": identifier, "digest": hashed, "plan": plan}
        if name == "cards.execute":
            stored = e.query("plans.get", {"id": args["plan_id"]})
            record = stored["plan"]
            if stored["digest"] != args["digest"] or e.application.query("tracks.all", {})["revision"] != record["application_revision"]:
                raise DomainError("PLAN_STALE", "Library changed after preview", {})
            if e.lyrics().store.state_fingerprint() != record["lyrics_revision"]:
                raise DomainError("PLAN_STALE", "Lyrics changed after preview", {})
            plan = record["inline_plan"]
            e._under(plan["parent"], e.export_roots)
            for root in plan["roots"]:
                e._under(root["source"], e.music_roots if root["category"] == "music" else e.import_roots + e.music_roots)
            return library.execute(plan, ffmpeg=require_tool("ffmpeg"), progress=progress, cancel=cancel)
        root = e._under(args["root"], e.export_roots)
        if name == "cards.verify":
            return library.verify(root, cancel=cancel)
        if name == "cards.inbox":
            return {"entries": []}
        if name == "cards.restore":
            target = e._under(args["destination"], e.export_roots)
            return library.restore(root, target)
        if name not in ("cards.reconcile", "cards.import"):
            raise DomainError("UNKNOWN_COMMAND", "Unsupported inline card action", {})
        source = e._under(args["m3u"], [library.card_root(root)])
        proposal = library.reconcile(root, source)
        # Every valid M3U path is retained, even when the PC has never seen its audio.
        tracks = e.application.query("tracks.all", {})["tracks"]
        known = {row["id"] for row in tracks}
        reference_ids = {row["card_reference"]["path"]: row["id"] for row in tracks if row.get("card_reference")}
        entries, returned = [], []
        for entry in proposal["entries"]:
            tid = entry.get("track_id")
            if tid not in known:
                tid = reference_ids.get(entry["card_path"])
            entries.append({"track_id": tid} if tid else {"card_path": entry["card_path"]})
            returned.append(tid or "@card:" + entry["card_path"])
        proposal["entries"], proposal["proposed_tracks"] = entries, returned
        proposal["card_only_references"] = proposal.pop("unresolved")
        proposal["unresolved"] = []  # unsafe/escaping paths already fail in the M3U parser
        before = proposal["input_sha256"]
        if library.input_digest(source) != before:
            raise DomainError("SOURCE_CHANGED", "Playlist changed during inspection", {})
        favorite = proposal["favorites"]
        pid = proposal["playlist_id"]
        if not pid and not favorite:
            relative = source.relative_to(library.card_root(root)).as_posix()
            with e.connect() as db:
                receipts = [json.loads(row[0]) for row in db.execute(
                    "SELECT body FROM sync_baselines WHERE build=?", (proposal["build_id"],))]
            found = [row for row in receipts if row.get("input_path") == relative and row.get("pc_id") != "@favorites"
                     and row.get("bind_input", True)]
            if found:
                pid = proposal["playlist_id"] = max(found, key=lambda row: row.get("imported_at", 0))["pc_id"]
        if args.get("playlist_id") and not favorite:
            if pid and pid != args["playlist_id"]:
                raise DomainError("IMPORT_CONFLICT", "The exported playlist association cannot be changed implicitly", {})
            pid = proposal["playlist_id"] = args["playlist_id"]
        current = e.application.query("favorites.list", {}) if favorite else None
        if not favorite and pid:
            try:
                current = e.application.query("playlists.get", {"id": pid})["playlist"]
            except DomainError as exc:
                if exc.code != "NOT_FOUND":
                    raise
        with e.connect() as db:
            stored = db.execute("SELECT body FROM sync_baselines WHERE build=? AND playlist=?",
                (proposal["build_id"], "@favorites" if favorite else pid)).fetchone()
        base = json.loads(stored[0]) if stored else proposal["base_snapshot"]
        if favorite and base:
            proposal["scope_track_ids"] = list(dict.fromkeys(proposal["scope_track_ids"] + base["tracks"]))
        if favorite:
            # A path catalogued on another card can already be a PC favorite.
            # Its presence in this input puts it in this card's editable scope.
            proposal["scope_track_ids"] = list(dict.fromkeys(proposal["scope_track_ids"] +
                [entry["track_id"] for entry in entries if "track_id" in entry]))
        now = current["track_ids"] if current else None
        if favorite and now is not None:
            now = [tid for tid in now if tid in proposal["scope_track_ids"]]
        returned = proposal["proposed_tracks"]
        if favorite:
            returned = proposal["proposed_tracks"] = list(dict.fromkeys(returned))
            seen = set()
            proposal["entries"] = []
            for entry in entries:
                token = entry.get("track_id") or "@card:" + entry["card_path"]
                if token not in seen:
                    seen.add(token)
                    proposal["entries"].append(entry)
        current_name = ("Favorites" if favorite else current["name"]) if current else None
        base_name = base.get("name", current_name) if base else None
        name_changed = not favorite and current_name is not None and proposal["name"] != current_name
        name_conflict = name_changed and base_name is not None and current_name != base_name and proposal["name"] != base_name
        # The import applies a whole playlist, not an automatic field merge.
        # Independent name/order edits on opposite sides are a conflict too:
        # applying the device's whole snapshot would otherwise lose PC changes.
        pc_changed = bool(base and (now != base["tracks"] or current_name != base_name))
        device_changed = bool(base and (returned != base["tracks"] or proposal["name"] != base_name))
        versions_differ = now != returned or name_changed
        conflict = name_conflict or bool(current and (not base or
            (pc_changed and device_changed and versions_differ)))
        proposal.update(current_snapshot={"tracks": now, "revision": current["revision"], "name": current_name} if current else None,
                        base_snapshot=base, conflict={"reason": "both_pc_and_device_changed"} if conflict else None)
        proposal["status"] = ("unresolved" if proposal["unresolved"] else "conflict" if conflict else
            "new_playlist" if current is None else "unchanged" if returned == now and not name_changed else
            "pc_only" if base and returned == base["tracks"] and (not name_changed or proposal["name"] == base_name) else "changed")
        if proposal["missing"]:
            proposal["status"] = "missing_playlist" if current else "already_absent"
        if name == "cards.reconcile":
            return proposal
        if args.get("input_sha256") != before:
            raise DomainError("PLAN_STALE", "Inspect these exact playlist bytes before importing", {})
        if args.get("decision") == "pc":
            if current is None:
                raise DomainError("NOT_FOUND", "There is no PC version to retain", {})
            baseline = {"tracks": now, "name": current_name, "pc_id": "@favorites" if favorite else pid,
                        "revision": current["revision"], "input_sha256": before,
                        "input_path": source.relative_to(library.card_root(root)).as_posix(), "imported_at": time.time()}
            with e.connect() as db:
                db.execute("INSERT OR REPLACE INTO sync_baselines VALUES(?,?,?)",
                           (proposal["build_id"], baseline["pc_id"], json.dumps(baseline)))
            return {"applied": False, "acknowledged": True, "reason": "pc_version_retained", "proposal": proposal}
        if proposal["unresolved"]:
            raise DomainError("UNRESOLVED_ENTRIES", "Card entries cannot be matched by their exported paths", proposal)
        if proposal["missing"]:
            if current is None:
                return {"applied": False, "reason": "already_absent", "proposal": proposal}
            if args.get("decision") != "device":
                raise DomainError("IMPORT_CONFLICT", "A missing card playlist needs an explicit device-deletion or PC-retain choice", proposal)
        if conflict and args.get("decision") not in ("device", "copy"):
            raise DomainError("IMPORT_CONFLICT", "Choose the device version or save a separate playlist", proposal)
        if proposal["status"] in ("unchanged", "pc_only") and args.get("decision") not in ("device", "copy"):
            return {"applied": False, "reason": proposal["status"], "proposal": proposal}
        if proposal["deleted"]:
            receipt = e.application.execute("playlists.delete", {"id": pid}, expected_revision=revision, idempotency_key=key)
            stored_id = pid
        elif args.get("decision") == "copy" or (current is None and not favorite):
            receipt = e.application.execute("library.intake_card", {
                "name": args.get("new_name") or proposal["name"], "entries": proposal["entries"]},
                expected_revision=revision, idempotency_key=key)
            stored_id = receipt["result"]["id"]
        elif favorite:
            kept = [tid for tid in current["track_ids"] if tid not in proposal["scope_track_ids"]]
            receipt = e.application.execute("library.intake_card", {"favorites": True,
                "preserve_track_ids": kept, "entries": proposal["entries"]},
                expected_revision=revision, idempotency_key=key)
            stored_id = "@favorites"
        else:
            receipt = e.application.execute("library.intake_card", {"id": pid, "entries": proposal["entries"], "name": proposal["name"]},
                expected_revision=revision, idempotency_key=key)
            stored_id = pid
        if not proposal["deleted"]:
            resolved = receipt["result"]["track_ids"]
            returned = [tid for tid in resolved if tid not in kept] if favorite and stored_id == "@favorites" else resolved
        baseline = {"tracks": returned, "name": proposal["name"], "pc_id": stored_id,
                    "revision": receipt["revision"], "input_sha256": before,
                    "bind_input": args.get("decision") != "copy",
                    "input_path": source.relative_to(library.card_root(root)).as_posix(), "imported_at": time.time()}
        with e.connect() as db:
            db.execute("INSERT OR REPLACE INTO sync_baselines VALUES(?,?,?)",
                       (proposal["build_id"], stored_id, json.dumps(baseline)))
        return {"applied": True, "receipt": receipt, "proposal": proposal}
