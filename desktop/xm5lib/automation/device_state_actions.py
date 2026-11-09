"""Exact-byte, revision-guarded native favorites/playlist return.

Only normal Application commands mutate PC state. Native files and original
audio/tags are read-only. Durable per-side baselines never imply a device write.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import uuid

from ..application import DomainError
from ..core import device_state as state, track_info
from ..core.restore_card import no_links, load_manifest, detect_layout
from .script_actions import _save


def fail(code, message, **details):
    raise DomainError(code, message, details)


def digest(value):
    return hashlib.sha256(state.json_bytes(value)).hexdigest()


def pc_value(playlist):
    return None if playlist is None else {"name": playlist["name"], "track_ids": playlist["track_ids"]}


def suggestion(old_pc, old_device, pc, device):
    p, d = pc != old_pc, device != old_device
    if p and d:
        return None, "two_sided"
    return ("device" if d else "pc"), ("device_only" if d else "pc_only" if p else "unchanged")


class DeviceStateActions:
    def __init__(self, engine):
        self.engine = engine
        with engine.connect() as connection:
            connection.executescript("""CREATE TABLE IF NOT EXISTS device_state_baselines(
                card TEXT,build TEXT,body TEXT NOT NULL,PRIMARY KEY(card,build));
                CREATE TABLE IF NOT EXISTS device_state_receipts(
                id TEXT PRIMARY KEY,body TEXT NOT NULL);""")

    @staticmethod
    def handles(name):
        return name in {"device_state.inspect", "device_state.plan", "device_state.apply"}

    def dispatch(self, name, args, revision=None, key=None):
        try:
            with self.engine.lock, self.engine.application._write_lock:
                from ..core.inline_card import library as inline_library
                inline = False
                if args.get("root"):
                    root = self.engine._under(args["root"], self.engine.export_roots + self.engine.import_roots)
                    inline = (root / inline_library.MANIFEST).is_file() or (root / "Struct/library.json").is_file()
                elif name == "device_state.apply":
                    inline = self.engine.query("plans.get", {"id": args["plan_id"]})["plan"].get("schema") == "xm5.inline.device-plan"
                if inline:
                    from .inline_device_actions import InlineDeviceActions
                    return InlineDeviceActions(self.engine).dispatch(name, args, key)
                if name == "device_state.inspect":
                    return self.inspect(args)
                if name == "device_state.plan":
                    return self.plan(args, key)
                if name == "device_state.apply":
                    return self.apply(args, key)
                fail("UNKNOWN_COMMAND", "Unknown device-state command")
        except state.DeviceStateError as exc:
            fail(exc.code, str(exc))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            fail("INVALID_STATE", str(exc))

    def _input(self, args):
        root = self.engine._under(args["root"], self.engine.export_roots + self.engine.import_roots)
        manifest = load_manifest(root)
        descriptor = manifest.get("device_state")
        if not isinstance(descriptor, dict) or descriptor.get("schema") != "xm5.state-baseline" or descriptor.get("version") != 1:
            fail("STATE_BASELINE_MISSING", "Export lacks a durable card identity and state baseline; inspect legacy files separately")
        if descriptor.get("build_id") != manifest["build_id"]:
            fail("STATE_GENERATION", "Manifest state baseline belongs to another generation")
        try:
            uuid.UUID(descriptor["card_id"])
        except (ValueError, TypeError, KeyError):
            fail("STATE_CARD_ID", "Invalid card identity")
        catalog = manifest.get("native_info_index", {})
        catalog_path = catalog.get("path")
        catalog_entry = next((entry for entry in manifest["files"] if entry["path"] == catalog_path), None)
        if not catalog_entry or catalog_entry.get("kind") != "native_info_index" or catalog_path != "Struct/indexes/info.bin":
            fail("STATE_GENERATION", "An owned native information catalog is required")
        catalog_file = no_links(root / catalog_path)
        if catalog_file.stat().st_size != catalog_entry["size"] or catalog_file.stat().st_size > 16*1024*1024:
            fail("STATE_GENERATION", "Native catalog size differs from its manifest")
        catalog_raw = catalog_file.read_bytes()
        parsed_catalog = track_info.parse_index(catalog_raw)
        if parsed_catalog["sha256"] != catalog_entry["sha256"] or parsed_catalog["build_id"] != uuid.UUID(manifest["build_id"]).hex:
            fail("STATE_GENERATION", "Native catalog bytes/generation differ from the manifest")
        snapshot_path = no_links(root / "Struct/device/state.xms")
        snapshot = None
        if snapshot_path.exists():
            if snapshot_path.stat().st_size > state.MAX_BYTES:
                fail("STATE_SIZE", "Snapshot exceeds 16 MiB")
            raw = snapshot_path.read_bytes()
            snapshot = state.parse_snapshot(raw)
        manifest_path = no_links(root / detect_layout(root)["manifest"])
        return root, manifest, descriptor, snapshot, hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    def _baseline(self, descriptor):
        with self.engine.connect() as connection:
            record = connection.execute("SELECT body FROM device_state_baselines WHERE card=? AND build=?",
                                        (descriptor["card_id"], descriptor["build_id"])).fetchone()
            prior = connection.execute("SELECT build FROM device_state_baselines WHERE card=? AND build<>?",
                                       (descriptor["card_id"], descriptor["build_id"])).fetchall()
        value = json.loads(record[0]) if record else {
            "card_id": descriptor["card_id"], "build_id": descriptor["build_id"], "sequence": -1,
            "snapshot_sha256": None, "favorites": {"pc": descriptor["favorites"], "device": None}, "bindings": {}}
        return value, [row[0] for row in prior]

    def inspect(self, args):
        root, manifest, descriptor, snapshot, manifest_hash = self._input(args)
        baseline, previous_generations = self._baseline(descriptor)
        playlists = self.engine.application.query("playlists.list", {})
        favorites = self.engine.application.query("favorites.list", {})
        import_path = no_links(root / "Struct/device/import.status")
        native_import, warnings = None, []
        if import_path.exists():
            with import_path.open("rb") as stream:
                status_raw = stream.read(4097)
            native_import={"bytes":import_path.stat().st_size,"sha256":hashlib.sha256(status_raw).hexdigest(),"current_generation":False}
            try:
                if len(status_raw)>4096: raise ValueError("Status exceeds4 KiB")
                document=json.loads(status_raw.decode("utf-8"))
                if not isinstance(document,dict) or document.get("schema")!="xm5.native-import" or document.get("version")!=1:
                    raise ValueError("Unsupported native import status schema")
                native_import["document"]=document
                native_import["current_generation"]=document.get("build_id")==uuid.UUID(manifest["build_id"]).hex
                if not native_import["current_generation"]: warnings.append("native_import_status_other_generation")
                elif document.get("favorites")=="failed" or document.get("status")=="failed" or document.get("favorites_applied") is False:
                    warnings.append("native_favorites_import_failed_scan_library_then_import_again")
            except (ValueError,UnicodeError) as exc:
                native_import["diagnostic"]=str(exc)
                warnings.append("native_import_status_invalid")
        pending_path = no_links(root / "Struct/device/state.pending")
        pending = None
        if pending_path.exists():
            with pending_path.open("rb") as stream:
                raw_pending = stream.read(4097)
            pending = {"path": "Struct/device/state.pending", "bytes": pending_path.stat().st_size,
                       "sha256": hashlib.sha256(raw_pending).hexdigest(), "bounded_prefix": raw_pending[:4096].decode("utf-8", "replace"),
                       "oversized": len(raw_pending)>4096}
        if snapshot is None:
            return {"schema":"xm5.device-state-inspection","version":1,"root":str(root),"card_id":descriptor["card_id"],
                    "build_id":manifest["build_id"],"manifest_sha256":manifest_hash,"application_revision":playlists["revision"],
                    "snapshot":None,"current":False,"problems":["missing_snapshot"]+(["pending_export"] if pending else []),
                    "pending":pending,"rows":[],"baseline":baseline,"baseline_sha256":digest(baseline),
                    "native_import":native_import,"warnings":warnings,
                    "previous_generations":previous_generations,"requires_generation_confirmation":bool(previous_generations and baseline["sequence"]<0)}
        actual = {item["id"]: item for item in playlists["playlists"]}
        known_tracks = {item["id"] for item in manifest["tracks"]}
        fav_resolved, fav_unknown = state.resolve_entries(snapshot["payload"]["favorites"], manifest)
        device_favorites = sorted({entry["track_id"] for entry in fav_resolved})
        pc_favorites = sorted(set(favorites["track_ids"]) & known_tracks)
        if baseline["favorites"]["device"] is None:
            selected, change = None, "initial_device_state"
        else:
            selected, change = suggestion(baseline["favorites"]["pc"], baseline["favorites"]["device"], pc_favorites, device_favorites)
        rows = [{"key": "favorites", "kind": "favorites", "pc": pc_favorites, "device": device_favorites,
                 "pc_revision": favorites["revision"], "unresolved": fav_unknown, "suggested": selected, "change": change}]
        exports = {item["pc_id"]: item for item in descriptor["playlists"]}
        bound = copy.deepcopy(baseline["bindings"])
        associations = args.get("associations", {})
        if not isinstance(associations, dict):
            fail("STATE_ASSOCIATION", "associations must map native ID strings to exported PC IDs")
        device_lists = {str(item["native_id"]): item for item in snapshot["payload"]["playlists"]}
        native_binding_file = no_links(root / "Struct/device/playlists.bind")
        confirmed = None
        if native_binding_file.exists():
            if native_binding_file.stat().st_size > state.MAX_BYTES:
                fail("STATE_SIZE", "Native bindings exceed 16 MiB")
            binding_raw = native_binding_file.read_bytes()
            confirmed = state.parse_playlist_bindings(binding_raw)
            confirmed["sha256"] = hashlib.sha256(binding_raw).hexdigest()
            if confirmed["build_id"] != snapshot["build_id"]:
                fail("STATE_GENERATION", "Native binding generation differs from snapshot")
            native_ids = [row["native_id"] for row in confirmed["playlists"] if "native_id" in row]
            if len(native_ids) != len(set(native_ids)):
                fail("STATE_ASSOCIATION", "Native binding IDs must be distinct")
        for identity, target in associations.items():
            if not isinstance(identity, str) or not identity.isdigit() or str(int(identity)) != identity:
                fail("STATE_ASSOCIATION", "Association keys must be canonical native ID strings")
            native = device_lists.get(str(identity))
            exported = exports.get(target)
            if identity in bound and bound[identity]["pc_id"] != target:
                fail("STATE_ASSOCIATION", "Existing durable native binding cannot be reassigned")
            if identity in bound:
                continue
            matches = [item for item in descriptor["playlists"] if native and item["exported_name"] == native["name"]]
            native_matches = [item for item in device_lists.values() if exported and item["name"] == exported["exported_name"]]
            hint = native and exported and native.get("pc_id") == target and native.get("source_export_name") == exported["exported_name"]
            binding_hint = confirmed and exported and any(item.get("native_id") == int(identity) and item["pc_id"] == target
                              and item["exported_name"] == exported["exported_name"] for item in confirmed["playlists"])
            name_match = len(matches) == 1 and len(native_matches) == 1 and matches[0]["pc_id"] == target
            if not native or not exported or not (hint or binding_hint or name_match):
                fail("STATE_ASSOCIATION", "Initial association must uniquely match an exact exported playlist name")
            if any(value["pc_id"] == target for key, value in bound.items() if key != identity):
                fail("STATE_ASSOCIATION", "A PC playlist is already bound to another native ID")
            bound[identity] = {"pc_id": target,
                               "pc": {"name": exported["name"], "track_ids": exported["track_ids"]},
                               "device": {"name": exported["exported_name"], "track_ids": exported["track_ids"], "unresolved": []}}
        for identity in sorted(set(device_lists) | set(bound), key=int):
            native, binding = device_lists.get(identity), bound.get(identity)
            resolved, unknown = state.resolve_entries(native["entries"], manifest) if native else ([], [])
            device = {"name": native["name"], "track_ids": [row["track_id"] for row in resolved], "unresolved": unknown} if native else None
            target = actual.get(binding["pc_id"]) if binding else None
            pc = pc_value(target)
            selected, change = suggestion(binding["pc"], binding["device"], pc, device) if binding else (None, "unassociated")
            candidates = [item["pc_id"] for item in descriptor["playlists"] if native and item["exported_name"] == native["name"]]
            rows.append({"key": "native:" + identity, "kind": "playlist", "native_id": int(identity),
                         "pc_id": binding["pc_id"] if binding else None, "pc_revision": target["revision"] if target else None,
                         "pc": pc, "device": device, "unresolved": unknown, "suggested": selected,
                         "change": change, "association_candidates": candidates, "binding": binding})
        problems = []
        if pending: problems.append("pending_export")
        if snapshot["build_id"] != manifest["build_id"].replace("-", "").lower(): problems.append("generation_mismatch")
        if not snapshot["payload"]["complete"]: problems.append("incomplete")
        if snapshot["sequence"] < baseline["sequence"] or (snapshot["sequence"] == baseline["sequence"] and snapshot["sha256"] != baseline["snapshot_sha256"]): problems.append("stale_sequence")
        return {"schema": "xm5.device-state-inspection", "version": 1, "root": str(root),
                "card_id": descriptor["card_id"], "build_id": manifest["build_id"], "manifest_sha256": manifest_hash,
                "application_revision": playlists["revision"], "snapshot": snapshot,
                "current": not problems, "problems": problems, "previous_generations": previous_generations,
                "pending": pending,
                "native_import":native_import,"warnings":warnings,
                "requires_generation_confirmation": bool(previous_generations and baseline["sequence"] < 0),
                "rows": rows, "baseline": baseline, "bindings": bound,
                "native_bindings": confirmed,
                "all_favorites": favorites["track_ids"], "all_playlists": playlists["playlists"],
                "baseline_sha256": digest(baseline)}

    def plan(self, args, key=None):
        inspection = self.inspect(args)
        if not inspection["current"]:
            fail("STATE_NOT_CURRENT", "Snapshot cannot be applied", problems=inspection["problems"])
        if inspection["requires_generation_confirmation"] and args.get("allow_new_generation") is not True:
            fail("STATE_NEW_GENERATION", "A new catalog generation requires explicit acknowledgement")
        decisions = args.get("decisions", {})
        if not isinstance(decisions, dict) or set(decisions) - {row["key"] for row in inspection["rows"]}:
            fail("STATE_DECISION", "Decisions must name exact inspection row keys")
        operations, acknowledgements = [], []
        used_names = {item["name"]: item["id"] for item in inspection["all_playlists"]}
        for row in inspection["rows"]:
            choice = decisions.get(row["key"], row["suggested"])
            if choice is None:
                fail("STATE_CHOICE_REQUIRED", "Choose device, pc, keep_both or retain for this row", row=row["key"], change=row["change"])
            if choice not in {"device", "pc", "keep_both", "retain"}:
                fail("STATE_DECISION", "Unknown reconciliation choice")
            row = copy.deepcopy(row)
            row["choice"] = choice
            if choice == "retain":
                acknowledgements.append(row)
                continue
            if row["kind"] == "favorites":
                desired = row["device"] if choice == "device" else sorted(set(row["pc"]) | set(row["device"])) if choice == "keep_both" else row["pc"]
                additions, removals = sorted(set(desired) - set(row["pc"])), sorted(set(row["pc"]) - set(desired))
                for favorite, ids in ((False, removals), (True, additions)):
                    if ids: operations.append({"row": row["key"], "command": "favorites.set", "args": {"track_ids": ids, "favorite": favorite}})
            elif choice != "pc":
                device, pc, target = row["device"], row["pc"], row["pc_id"]
                name = device["name"] if device is not None else None
                binding = row.get("binding")
                if (device is not None and binding and binding.get("device") and binding.get("pc")
                        and device["name"] == binding["device"]["name"]):
                    # Export filenames carry an identity suffix. An unchanged native name
                    # represents the bound PC name, including when the PC side was renamed.
                    name = binding["pc"]["name"]
                if device is None:
                    if pc is not None and choice == "device": operations.append({"row": row["key"], "command": "playlists.delete", "args": {"id": target}})
                elif pc != {"name": name, "track_ids": device["track_ids"]}:
                    create = pc is None or choice == "keep_both"
                    if choice == "keep_both" and pc is not None:
                        name += " (device " + str(row["native_id"]) + ")"
                    if name in used_names and (create or used_names[name] != target):
                        fail("STATE_NAME_CONFLICT", "Chosen device name conflicts with an existing PC playlist", name=name)
                    used_names[name] = target or row["key"]
                    if create:
                        operations.append({"row": row["key"], "command": "playlists.create", "args": {"name": name, "track_ids": device["track_ids"]}})
                    else:
                        if pc["name"] != name: operations.append({"row": row["key"], "command": "playlists.rename", "args": {"id": target, "name": name}})
                        if pc["track_ids"] != device["track_ids"]: operations.append({"row": row["key"], "command": "playlists.replace", "args": {"id": target, "track_ids": device["track_ids"]}})
            acknowledgements.append(row)
        plan = {"schema": "xm5.device-state-plan", "version": 1, "request": copy.deepcopy(args),
                "inspection": inspection, "operations": operations, "acknowledgements": acknowledgements,
                "source_writes": False, "device_writes": False}
        identity = uuid.uuid5(uuid.UUID("d7439cac-6968-4de7-b8ab-7903805a4073"), key).hex if key else uuid.uuid4().hex
        fingerprint = digest(plan)
        with self.engine.connect() as connection:
            previous = connection.execute("SELECT digest,body FROM plans WHERE id=?", (identity,)).fetchone()
            if previous and json.loads(previous[1]).get("request") != args:
                fail("IDEMPOTENCY_CONFLICT", "Plan key belongs to another request")
            if previous:
                return {"plan_id": identity, "digest": previous[0], "plan": json.loads(previous[1]), "replayed": True}
            connection.execute("INSERT INTO plans VALUES(?,?,?)", (identity, fingerprint, state.json_bytes(plan).decode()))
        return {"plan_id": identity, "digest": fingerprint, "plan": plan}

    def export_acknowledgement(self, root):
        """Private Engine seam: recheck the accepted receipt and actual PC side."""
        current=self.inspect({"root":str(root)})
        baseline=current["baseline"]
        if not current["current"] or baseline.get("snapshot_sha256")!=current["snapshot"]["sha256"]:
            fail("STATE_RECONCILIATION_REQUIRED","Inspect/plan/apply the current native state before replacing its generation")
        identity=baseline.get("receipt_id")
        with self.engine.connect() as connection:
            record=connection.execute("SELECT body FROM device_state_receipts WHERE id=?",(identity,)).fetchone()
            plan_record=connection.execute("SELECT digest,body FROM plans WHERE id=?",(identity,)).fetchone()
        if not record or not plan_record:
            fail("STATE_RECONCILIATION_REQUIRED","A durable acknowledged state receipt is required")
        receipt=json.loads(record[0]);plan=json.loads(plan_record[1]);before=plan.get("inspection",{})
        if (digest(plan)!=plan_record[0] or receipt.get("status")!="complete" or receipt.get("snapshot",{}).get("sha256")!=current["snapshot"]["sha256"]
                or before.get("manifest_sha256")!=current["manifest_sha256"] or any(row.get("choice")=="retain" for row in receipt["acknowledgements"])):
            fail("STATE_RECONCILIATION_REQUIRED","The exact accepted receipt has unresolved or changed state")
        if current["rows"][0]["pc"]!=baseline["favorites"]["pc"]:
            fail("STATE_RECONCILIATION_REQUIRED","PC favorites changed after the last acknowledged state decision")
        playlists={item["id"]:item for item in current["all_playlists"]}
        for binding in baseline["bindings"].values():
            if pc_value(playlists.get(binding["pc_id"]))!=binding["pc"]:
                fail("STATE_RECONCILIATION_REQUIRED","A bound PC playlist changed after its state decision")
        for key in ("native_bindings","native_import"):
            old,new=before.get(key),current.get(key)
            if (old or {}).get("sha256")!=(new or {}).get("sha256"):
                fail("STATE_RECONCILIATION_REQUIRED","Native binding/import evidence changed after state acknowledgement")
        files=[{"path":"Struct/device/state.xms","size":current["snapshot"]["bytes"],"sha256":current["snapshot"]["sha256"]}]
        for key,path in (("native_bindings","Struct/device/playlists.bind"),("native_import","Struct/device/import.status")):
            item=current.get(key)
            if item:
                actual=no_links(Path(current["root"])/path)
                files.append({"path":path,"size":actual.stat().st_size,"sha256":item["sha256"]})
        return {"schema":"xm5.device-state-export-ack","version":1,"root":current["root"],"card_id":current["card_id"],
                "build_id":current["build_id"],"manifest_sha256":current["manifest_sha256"],
                "snapshot_sha256":current["snapshot"]["sha256"],"receipt_id":identity,
                "application_revision":current["application_revision"],"files":files}

    def apply(self, args, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            fail("INVALID_ARGUMENT", "State apply requires a bounded idempotency key")
        with self.engine.connect() as connection:
            record = connection.execute("SELECT digest,body FROM plans WHERE id=?", (args["plan_id"],)).fetchone()
            previous = connection.execute("SELECT body FROM device_state_receipts WHERE id=?", (args["plan_id"],)).fetchone()
        if not record:
            fail("PLAN_NOT_FOUND", "Unknown state plan")
        plan = json.loads(record[1])
        if record[0] != args["digest"] or digest(plan) != args["digest"] or plan.get("schema") != "xm5.device-state-plan":
            fail("PLAN_STALE", "Exact device-state plan digest required")
        if previous:
            receipt = json.loads(previous[0])
            if receipt["key"] != key: fail("IDEMPOTENCY_CONFLICT", "Plan already applied with another key")
            return dict(receipt, replayed=True)
        before = plan["inspection"]
        current = self.inspect(plan["request"])
        if current != before:
            fail("PLAN_STALE", "Snapshot bytes, manifest generation, application state or baselines changed")
        directory = self.engine.home / "device-state" / args["plan_id"]
        directory.mkdir(parents=True, exist_ok=True)
        journal_path = directory / "apply.json"
        if journal_path.exists():
            fail("STATE_RECOVERY_REQUIRED", "Interrupted state intake is retained for explicit inspection; it is not replayed", journal=str(journal_path))
        journal = {"schema": "xm5.device-state-journal", "version": 1, "key": key, "digest": args["digest"], "status": "applying", "receipts": []}
        _save(journal_path, journal)
        targets = {row["key"]: row["pc_id"] for row in before["rows"] if row["kind"] == "playlist"}
        try:
            for position, operation in enumerate(plan["operations"]):
                command, values = operation["command"], operation["args"]
                if command == "favorites.set":
                    revision = self.engine.application.query("favorites.list", {})["revision"]
                elif command == "playlists.create":
                    revision = None
                else:
                    revision = self.engine.application.query("playlists.get", {"id": values["id"]})["revision"]
                receipt = self.engine.application.execute(command, values, expected_revision=revision,
                                                          idempotency_key="device-state:" + args["plan_id"] + ":" + str(position))
                journal["receipts"].append(receipt)
                if command == "playlists.create": targets[operation["row"]] = receipt["result"]["id"]
                _save(journal_path, journal)
            baselines = copy.deepcopy(before["baseline"])
            playlists = {item["id"]: item for item in self.engine.application.query("playlists.list", {})["playlists"]}
            favorites = self.engine.application.query("favorites.list", {})["track_ids"]
            exported = set(before["rows"][0]["pc"]) | set(before["rows"][0]["device"])
            # Scope is the full manifest catalog, including tracks with no prior favorite.
            _, manifest, _, _, _ = self._input(plan["request"])
            exported = {item["id"] for item in manifest["tracks"]}
            for row in plan["acknowledgements"]:
                if row["choice"] == "retain": continue
                if row["kind"] == "favorites":
                    baselines["favorites"] = {"pc": sorted(set(favorites) & exported), "device": row["device"]}
                else:
                    target = targets[row["key"]]
                    if target is not None:
                        baselines["bindings"][str(row["native_id"])] = {"pc_id": target, "pc": pc_value(playlists.get(target)),
                            "device": row["device"], "tombstone": row["device"] is None or target not in playlists}
            baselines.update(sequence=before["snapshot"]["sequence"], snapshot_sha256=before["snapshot"]["sha256"],receipt_id=args["plan_id"])
            receipt = {"schema": "xm5.device-state-receipt", "version": 1, "key": key, "status": "complete", "plan_id": args["plan_id"],
                       "card_id": before["card_id"], "build_id": before["build_id"], "snapshot": before["snapshot"],
                       "acknowledgements": plan["acknowledgements"], "receipts": journal["receipts"],
                       "source_writes": False, "device_writes": False, "baseline": baselines}
            with self.engine.connect() as connection:
                connection.execute("INSERT INTO device_state_baselines VALUES(?,?,?) ON CONFLICT(card,build) DO UPDATE SET body=excluded.body",
                                   (before["card_id"], before["build_id"], state.json_bytes(baselines).decode()))
                connection.execute("INSERT INTO device_state_receipts VALUES(?,?)", (args["plan_id"], state.json_bytes(receipt).decode()))
            journal.update(status="complete", result=receipt)
            _save(journal_path, journal)
            return receipt
        except Exception as exc:
            journal.update(status="recovery_required", error=str(exc))
            _save(journal_path, journal)
            raise
