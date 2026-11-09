"""Batch inspection and explicit intake of ordinary inline-card M3U8 files."""
from hashlib import sha256
import json
from pathlib import Path
import threading
import uuid

from ..application import DomainError
from ..core.inline_card import library
from .inline_library_actions import InlineLibraryActions


class InlineDeviceActions:
    def __init__(self, engine):
        self.engine = engine
        self.cards = InlineLibraryActions(engine)

    def inspect(self, args):
        e = self.engine
        root, manifest = library.load(e._under(args["root"], e.export_roots))
        associations = args.get("associations") or {}
        if not isinstance(associations, dict):
            raise DomainError("INVALID_ARGUMENT", "Associations map exact card-relative M3U8 paths to PC playlist IDs", {})
        rows = []
        found = set()
        for path in library.playlist_paths(root):
            relative = path.relative_to(root).as_posix()
            parameters = {"root": str(root), "m3u": str(path)}
            if relative in associations:
                parameters["playlist_id"] = associations[relative]
                found.add(relative)
            proposal = self.cards.dispatch("cards.reconcile", parameters, None, threading.Event())
            key = "favorites" if proposal["favorites"] else relative
            rows.append({"key": key, "kind": "favorites" if proposal["favorites"] else "playlist",
                         "source": relative, "name": proposal["name"], "proposal": proposal,
                         "suggested": "retain" if proposal["status"] in ("unresolved", "missing_playlist", "already_absent") else
                            None if proposal["status"] == "conflict" else
                            "pc" if proposal["status"] in ("unchanged", "pc_only") else "device"})
        if found != set(associations):
            raise DomainError("NOT_FOUND", "An associated M3U8 file is not present on the selected card", {})
        return {"schema": "xm5.device-state-inspection", "version": 1, "format": "XM5I",
                "root": str(root), "build_id": manifest["build_id"], "current": True,
                "application_revision": e.application.query("tracks.all", {})["revision"],
                "rows": rows, "snapshot": "ordinary_relative_m3u8", "warnings": [], "problems": []}

    def plan(self, args):
        inspection = self.inspect(args)
        decisions = args.get("decisions") or {}
        if not isinstance(decisions, dict):
            raise DomainError("INVALID_ARGUMENT", "Decisions map inspection row keys to device, pc, keep_both or retain", {})
        known = {row["key"] for row in inspection["rows"]}
        if set(decisions) - known:
            raise DomainError("NOT_FOUND", "A decision does not match an inspected row", {})
        names = {row["name"] for row in self.engine.application.query("playlists.list", {})["playlists"]}
        operations = []
        for row in inspection["rows"]:
            decision = decisions.get(row["key"], row["suggested"])
            if decision not in ("device", "pc", "keep_both", "retain"):
                raise DomainError("IMPORT_CONFLICT", "Choose device, pc, keep_both or retain for " + row["key"], {})
            proposal = row["proposal"]
            if proposal["unresolved"] and decision not in ("retain", "pc"):
                raise DomainError("UNRESOLVED_ENTRIES", "Resolve unavailable PC tracks or retain the card row", proposal)
            if decision == "retain" or (decision == "pc" and proposal["status"] in ("unchanged", "pc_only")):
                continue
            parameters = {"root": inspection["root"], "m3u": str(Path(inspection["root"]) / row["source"]),
                          "input_sha256": proposal["input_sha256"]}
            if proposal["playlist_id"]:
                parameters["playlist_id"] = proposal["playlist_id"]
            if decision == "pc":
                parameters["decision"] = "pc"
            elif decision == "keep_both":
                parameters["decision"] = "copy"
            elif proposal["status"] in ("conflict", "missing_playlist"):
                parameters["decision"] = "device"
            new = decision == "keep_both" or (proposal["current_snapshot"] is None and not proposal["favorites"])
            if new:
                base = proposal["name"] + (" (XM5)" if decision == "keep_both" else "")
                name, number = base, 2
                while name in names:
                    name = f"{base} ({number})"
                    number += 1
                names.add(name)
                parameters["new_name"] = name
            operations.append({"key": row["key"], "arguments": parameters,
                "revision": None if new else (proposal["current_snapshot"] or {}).get("revision")})
        plan = {"schema": "xm5.inline.device-plan", "version": 1, "root": inspection["root"],
                "build_id": inspection["build_id"], "application_revision": inspection["application_revision"],
                "operations": operations}
        from .engine import digest
        identifier, hashed = uuid.uuid4().hex, digest(plan)
        with self.engine.connect() as db:
            db.execute("INSERT INTO plans VALUES(?,?,?)", (identifier, hashed, json.dumps(plan)))
        return {"plan_id": identifier, "digest": hashed, "plan": plan}

    def apply(self, args, key):
        e = self.engine
        stored = e.query("plans.get", {"id": args["plan_id"]})
        plan = stored["plan"]
        if stored["digest"] != args.get("digest") or plan.get("schema") != "xm5.inline.device-plan":
            raise DomainError("PLAN_STALE", "Supply the complete reviewed inline device plan", {})
        root = e._under(plan["root"], e.export_roots)
        _, manifest = library.load(root)
        if manifest["build_id"] != plan["build_id"] or e.application.query("tracks.all", {})["revision"] != plan["application_revision"]:
            raise DomainError("PLAN_STALE", "Card or PC library changed after device-state planning", {})
        for operation in plan["operations"]:
            source = e._under(operation["arguments"]["m3u"], [root])
            if library.input_digest(source) != operation["arguments"]["input_sha256"]:
                raise DomainError("PLAN_STALE", "A card playlist changed after planning", {})
        receipts = []
        for operation in plan["operations"]:
            receipt_key = sha256((key + ":" + operation["key"]).encode()).hexdigest()
            try:
                result = self.cards.dispatch("cards.import", operation["arguments"], None, threading.Event(),
                                             operation["revision"], receipt_key)
            except (OSError, ValueError, DomainError) as exc:
                return {"status": "partial", "results": receipts,
                        "error": {"code": getattr(exc, "code", "INTAKE_FAILED"), "message": str(exc)}}
            receipts.append({"key": operation["key"], **result})
        return {"status": "complete", "results": receipts}

    def dispatch(self, name, args, key=None):
        return self.inspect(args) if name == "device_state.inspect" else self.plan(args) if name == "device_state.plan" else self.apply(args, key)
