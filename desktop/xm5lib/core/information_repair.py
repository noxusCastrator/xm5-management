"""Retained-input information repair with one frozen XMI/index/manifest plan.

Only explicitly selected source roots and owned card-relative manifest entries
are read. Stored absolute source paths never authorize filesystem access.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import mutagen

from . import card_library, covers, lyrics, peak_scan, tags, track_info, device_state, native_art_prepare
from .restore_card import RestoreError, checked_path, safe_relative

INFO_KINDS = {"track_info", "information_file", "info_file"}


def _fail(code, message, **details):
    raise RestoreError(code, message, details)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return track_info.canonical_json(value)


class _Retained:
    def __init__(self, report):
        from . import library_health as health
        self.health, self.report = health, report
        self.root = Path(report["root"])
        self.entries = {entry["path"]: entry for entry in report["manifest"]["files"]}
        self.records = {record["path"]: record for record in report["records"]}
        self.inputs = {}
        self.generated = {}

    def path(self, entry):
        relative = safe_relative(entry["path"])
        expected = self.health.baseline_version(entry)
        observation = self.records[relative]["source"]
        if observation["status"] == "present" and observation["version"] != expected:
            _fail("source_changed", "Declared retained source differs from the accepted manifest; sync/reconcile the original first", path=relative)
        card = checked_path(self.root, relative)
        observed = self.health.version(card)
        if observed is not None:
            if observed != expected:
                _fail("original_changed", "Retained original on the card differs from the accepted manifest", path=relative)
            return card
        identity, source_relative = entry.get("root_id"), entry.get("source_relative")
        selected = self.report["sources"].get(identity)
        if selected and selected["status"] == "connected" and source_relative:
            source = checked_path(selected["path"], safe_relative(source_relative))
            version = self.health.version(source)
            if version != expected:
                _fail("source_changed" if version else "retained_input_missing", "Exact retained source is missing or changed", path=relative)
            self.inputs[(identity, source_relative)] = {"root_id": identity, "relative": source_relative, "version": version}
            return source
        _fail("retained_input_missing", "No exact owned original or explicitly selected source can rebuild this artifact", path=relative)

    def read(self, entry, maximum=track_info.MAX_BYTES):
        if entry["path"] in self.generated:
            return self.generated[entry["path"]]
        path = self.path(entry)
        before = self.health.version(path)
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            _fail("retained_input_too_large", "Retained metadata asset exceeds the bounded reader", path=entry["path"])
        if self.health.version(path) != before or {"size": len(data), "sha256": _sha(data)} != before:
            _fail("source_changed", "Retained input changed during generation", path=entry["path"])
        return data

    def named(self, relative):
        entry = self.entries.get(safe_relative(relative))
        if entry is None:
            _fail("unowned_input", "Repair metadata refers to a path outside owned file units", path=relative)
        return entry


class InformationRepairGenerator:
    """Trusted callable adapter for StorageActions(repair_generator=...).

    StorageActions must use plan_repair(), since a changed XMI requires coupled
    index/readiness and top-level manifest updates in the same frozen plan.
    """

    def __init__(self, *, selected_paths=None, ffmpeg_path=None, peak_cache_dir=None, peak_scanner=None):
        self._art_cache = {}
        self.selected_paths = None if selected_paths is None else tuple(safe_relative(path) for path in selected_paths)
        self.scanner = peak_scanner or peak_scan.PeakScanner(ffmpeg_path=ffmpeg_path, cache_dir=peak_cache_dir)

    def __call__(self, entry, report):
        _fail("coupled_repair_required", "Information repair requires generator.plan_repair so XMI/index/readiness/metadata freeze together", path=entry.get("path"))

    @staticmethod
    def _valid_info(entry, report):
        from . import library_health as health
        path = checked_path(report["root"], entry["path"])
        if health.version(path) != health.baseline_version(entry):
            return None
        try:
            return track_info.parse_info(path)
        except (track_info.InfoError, OSError):
            return None

    @staticmethod
    def _recipe(manifest, metadata, options):
        recipe = {"format": "xm5-information-v1", "art": manifest["recipe"], "peak_options": options,
                  "peak_analysis": metadata.get("peak_analysis", {}).get("recipe"), "lyrics": "selected-local-bytes-v1"}
        return "xm5-information-v1-" + _sha(_json(recipe))

    def _art(self, art_id, retained, track=None):
        manifest = retained.report["manifest"]
        if art_id is None:
            return None
        if art_id in self._art_cache:
            return self._art_cache[art_id]
        artwork = manifest.get("artworks", {}).get(art_id)
        if not artwork:
            _fail("artwork_unresolved", "Chosen artwork identity is absent from the manifest", art_id=art_id)
        if manifest.get("recipe") != card_library.RECIPE:
            _fail("recipe_unavailable", "Retained artwork recipe differs from the installed compiler")
        expected = artwork["source_sha256"]
        originals = [entry for entry in retained.entries.values() if entry.get("kind") in {"original_image", "original_playlist_cover"}
                     and entry.get("sha256") == expected]
        # Equality of exact chosen-cover bytes makes content dedup unambiguous;
        # no stored absolute art path, basename heuristic or fresh cover search.
        if originals:
            data = retained.read(sorted(originals, key=lambda entry: entry["path"])[0])
        else:
            candidates = [track] if track else []
            candidates += [item for item in manifest.get("tracks", []) if item.get("playback_art") == art_id and item is not track]
            data = None
            for item in candidates:
                if not item:
                    continue
                source = retained.path(retained.named(item["path"]))
                audio = mutagen.File(source)
                picture = tags.embedded_picture(audio) if audio is not None else None
                if picture and _sha(picture) == expected:
                    data = picture
                    break
            if data is None:
                _fail("retained_artwork_missing", "Chosen original cover bytes are unavailable; no fallback cover was invented", art_id=art_id)
        result = native_art_prepare.prepare(data)
        for side, bitmap in result.items():
            identity = artwork["derivatives"].get(str(side))
            if identity != {"sha256": _sha(bitmap), "size": len(bitmap)}:
                _fail("recipe_unavailable", "Compiler output differs from frozen artwork identity", art_id=art_id, side=side)
        if len(self._art_cache)>=4: self._art_cache.pop(next(iter(self._art_cache)))
        self._art_cache[art_id]=result
        return result

    @staticmethod
    def _lyrics(track, retained, old_metadata):
        if track is None:
            return None, None, None
        manifest = retained.report["manifest"]
        selected = None
        old = old_metadata.get("lyrics")
        if isinstance(old, dict) and old.get("original_path") and old.get("normalized_path"):
            selected = copy.deepcopy(old)
        else:
            maps = [item for item in manifest.get("lyrics", []) if item.get("kind") == "lyrics-map"]
            if len(maps) > 1:
                _fail("lyric_selection_ambiguous", "Multiple retained lyric association maps")
            if maps:
                document = json.loads(retained.read(retained.named(maps[0]["path"])))
                if document.get("schema") != "xm5.lyrics-associations" or document.get("version") != 1:
                    _fail("lyric_mapping_invalid", "Retained association map has an unsupported schema")
                matches = [row for row in document.get("associations", []) if row.get("selected") and row.get("track_id") == track["id"]]
                if len(matches) > 1:
                    _fail("lyric_selection_ambiguous", "Multiple selected lyric sources")
                selected = copy.deepcopy(matches[0]) if matches else None
        if selected:
            original = retained.read(retained.named(selected["original_path"]))
            normalized = retained.read(retained.named(selected["normalized_path"]))
            return normalized, original, selected
        records = [item for item in manifest.get("lyrics", []) if item.get("track_id") == track["id"]]
        originals = [item for item in records if item.get("kind") == "lyrics-original"]
        normalized = [item for item in records if item.get("kind") == "lyrics-rendered"]
        if len(originals) > 1 or len(normalized) > 1:
            _fail("lyric_selection_ambiguous", "Several retained lyrics require their selected association map")
        raw = retained.read(retained.named(originals[0]["path"])) if originals else None
        rendered = retained.read(retained.named(normalized[0]["path"])) if normalized else None
        provenance = originals[0].get("provenance", {}) if originals else normalized[0].get("provenance", {}) if normalized else None
        return rendered, raw, provenance

    def _rebuild_info(self, entry, relation, retained, old):
        manifest = retained.report["manifest"]
        identity = relation.get("id", entry.get("info_id"))
        if track_info.info_path(identity) != entry["path"]:
            _fail("identity_mismatch", "Existing XMI UUID/path identity is inconsistent")
        role, subject = relation.get("role", entry.get("role")), relation.get("subject", entry.get("subject"))
        old_metadata = json.loads(old.payload(3)) if old and old.payload(3) else {}
        track = None
        if role == "playback":
            tid = relation.get("track_id", entry.get("track_id"))
            track = next((item for item in manifest["tracks"] if item["id"] == tid), None)
            if track is None or subject != "xm5/" + track["path"]:
                _fail("track_unresolved", "Information subject does not match its ordinary original track")
            original_entry = retained.named(track["path"])
            source = retained.path(original_entry)
            source_hash, source_size = original_entry["sha256"], original_entry["size"]
            art_id = track.get("playback_art")
            options = {"midpoint_dbfs": old.midpoint_dbfs, "correction_limit_db": old.correction_limit_db,
                       "peak_correction_enabled": bool(old.flags & 2)} if old and old.peak is not None else copy.deepcopy(manifest.get("peak_options", {}))
            if set(options) != {"midpoint_dbfs", "correction_limit_db", "peak_correction_enabled"}:
                _fail("peak_settings_missing", "No exact retained peak settings are available")
            if old and old.source_sha256 == source_hash and old.source_size == source_size and old.peak is not None:
                peak = old.peak
                analysis = copy.deepcopy(old_metadata.get("peak_analysis", {}))
            else:
                try:
                    result = self.scanner.scan(source, expected_sha256=source_hash, expected_size=source_size)
                except peak_scan.SourceChangedError as exc:
                    _fail("source_changed", str(exc))
                peak = result.peak
                analysis = {"status": result.status, "diagnostic": result.diagnostic, "recipe": result.recipe, "recipe_sha256": result.recipe_sha256}
            normalized, raw, provenance = self._lyrics(track, retained, old_metadata)
            metadata = copy.deepcopy(old_metadata) if old_metadata else {"schema": "xm5.track-information", "version": 1, "track_id": track["id"]}
            metadata.update(source={"root_id": track["root_id"], "relative": track["source_relative"], "sha256": source_hash, "size": source_size},
                            cover={"art_id": art_id, "origin": track.get("art_origin", "no_art"), "recipe": manifest["recipe"]}, peak_analysis=analysis, lyrics=provenance)
            source_path = subject
        elif role in ("directory", "playlist"):
            records = manifest["directories"] if role == "directory" else manifest["playlists"]
            item = next((item for item in records if ("xm5/" + item["path"] if role == "directory" else item["id"]) == subject), None)
            if item is None:
                _fail("auxiliary_unresolved", "Auxiliary information subject is absent from the manifest")
            art_id = item.get("preview_art" if role == "directory" else "cover_art")
            original = next((entry for entry in retained.entries.values() if entry.get("kind") in {"original_image", "original_playlist_cover"}
                             and entry.get("sha256") == manifest.get("artworks", {}).get(art_id, {}).get("source_sha256")), None)
            if art_id and original is None:
                # Embedded artwork still has a hash/size; the exact source bytes
                # are checked by _art below and length comes from that picture.
                candidates = [track for track in manifest["tracks"] if track.get("playback_art") == art_id]
                picture = None
                for candidate in candidates:
                    audio = mutagen.File(retained.path(retained.named(candidate["path"])))
                    data = tags.embedded_picture(audio) if audio is not None else None
                    if data and _sha(data) == manifest["artworks"][art_id]["source_sha256"]:
                        picture = data
                        break
                if picture is None:
                    _fail("retained_artwork_missing", "Auxiliary original artwork is missing")
                source_hash, source_size = _sha(picture), len(picture)
            else:
                data = retained.read(original) if original else b""
                source_hash, source_size = _sha(data), len(data)
            options = copy.deepcopy(manifest.get("peak_options", {}))
            if not options:
                _fail("peak_settings_missing", "Auxiliary header options are not retained")
            peak, normalized, raw, source_path = None, None, None, ""
            metadata = copy.deepcopy(old_metadata) if old_metadata else {"schema": "xm5.aux-information", "version": 1, "role": role, "subject": subject}
            metadata["cover"] = {"art_id": art_id, "recipe": manifest["recipe"]}
        else:
            _fail("role_unsupported", "Unknown information role")
        bitmaps = self._art(art_id, retained, track)
        data = track_info.build_info(identity=identity, source_sha256=source_hash, source_size=source_size, source_path=source_path,
                                     peak=peak, bitmaps=bitmaps, normalized_lyrics=normalized, original_lyrics=raw, metadata=metadata, **options)
        return data, {"source_sha256": source_hash, "recipe_id": self._recipe(manifest, metadata, options)}

    def plan_repair(self, card_root, source_roots=None, *, choices=None, expected_recipes=None):
        from . import differential_sync as sync, library_health as health
        self._art_cache = {}
        report = health.inspect_library(card_root, source_roots, expected_recipes=expected_recipes)
        retained = _Retained(report)
        manifest = report["manifest"]
        relations = manifest.get("information")
        if not isinstance(relations, list) or not relations:
            _fail("information_topology_missing", "This manifest does not retain information-file relations")
        by_relation = {row["path"]: row for row in relations}
        if len(by_relation) != len(relations):
            _fail("identity_collision", "Duplicate information path relations")
        selected = set(self.selected_paths or ())
        if not selected <= retained.entries.keys():
            _fail("unowned_selection", "Repair selection must name existing owned file units")
        updates, final_relations, blobs = [], copy.deepcopy(relations), {}
        # Regenerate genuine derived lyric units first so XMI generation reads
        # exactly the same frozen bytes that this transaction will publish.
        for entry in manifest["files"]:
            if entry.get("kind") != "normalized_lyrics" or not (entry["path"] in selected or retained.records[entry["path"]]["repairable"]):
                continue
            relation = next((row for row in manifest.get("lyrics", []) if row.get("path") == entry["path"] or row.get("normalized_path") == entry["path"]), None)
            original_path = entry.get("original_path") or (relation or {}).get("original_path")
            if not original_path or original_path == entry["path"]:
                _fail("lyric_original_unresolved", "Normalized lyrics need a distinct retained original relation")
            original = retained.read(retained.named(original_path))
            parsed = lyrics.parse_lyrics(original, entry.get("original_format", Path(original_path).suffix[1:] or "txt"), encoding=entry.get("original_encoding"))
            data = parsed["normalized_text"].encode("utf-8-sig" if parsed["cues"] else "utf-8")
            if {"size": len(data), "sha256": _sha(data)} != health.baseline_version(entry):
                _fail("recipe_unavailable", "Normalized lyric regeneration differs from its retained identity")
            retained.generated[entry["path"]] = data
            updates.append({"path": entry["path"], "kind": entry["kind"], "managed_id": health.managed_id(entry, manifest), "data": data})
        for relation in final_relations:
            path = relation["path"]
            entry = retained.named(path)
            if entry.get("kind") not in INFO_KINDS:
                _fail("identity_mismatch", "Information relation is not an information file unit")
            old = self._valid_info(entry, report)
            original = next((item for item in manifest.get("tracks", []) if item["id"] == relation.get("track_id")), None)
            # Exact currently accepted audio is required even for a healthy XMI.
            # It prevents an unchanged container from reusing a stale peak while
            # an explicitly selected PC source has drifted.
            if original:
                source = retained.named(original["path"])
                retained.path(source)
            stale = original and old and (old.source_sha256 != source["sha256"] or old.source_size != source["size"])
            if old is None or stale or path in selected or retained.records[path]["stale_reasons"]:
                data, metadata = self._rebuild_info(entry, relation, retained, old)
                updates.append({"path": path, "kind": entry["kind"], "managed_id": health.managed_id(entry, manifest), "data": data, "metadata": metadata})
                blobs[path] = data
                relation.update(size=len(data), sha256=_sha(data), **metadata)
            else:
                blobs[path] = checked_path(report["root"], path).read_bytes()
                relation.update(size=len(blobs[path]), sha256=_sha(blobs[path]))
                # Retrofit freshness metadata on old, otherwise valid exports.
                metadata = {"source_sha256": old.source_sha256,
                            "recipe_id": entry.get("recipe_id") or self._recipe(manifest, json.loads(old.payload(3)) if old.payload(3) else {},
                                            {"midpoint_dbfs": old.midpoint_dbfs, "correction_limit_db": old.correction_limit_db,
                                             "peak_correction_enabled": bool(old.flags & 2) if old.peak is not None else manifest["peak_options"]["peak_correction_enabled"]})}
                if any(entry.get(key) != value for key, value in metadata.items()):
                    updates.append({"path": path, "kind": entry["kind"], "managed_id": health.managed_id(entry, manifest), "data": blobs[path], "metadata": metadata})
                relation.update(**metadata)
        # Compatibility BMPs are rebuilt from exact chosen originals; canonical
        # content paths cannot be retargeted to another cover or recipe.
        art_cache = {}
        for entry in manifest["files"]:
            if entry.get("kind") == "derived_art" and (entry["path"] in selected or retained.records[entry["path"]]["repairable"]):
                match = next(((identity, int(side)) for identity, artwork in manifest.get("artworks", {}).items()
                              for side, path in artwork["roles"].items() if path == entry["path"]), None)
                if match is None:
                    _fail("artwork_unresolved", "Derived bitmap has no retained artwork relation")
                art_id, side = match
                if art_id not in art_cache:
                    art_cache[art_id] = self._art(art_id, retained)
                updates.append({"path": entry["path"], "kind": entry["kind"], "managed_id": health.managed_id(entry, manifest), "data": art_cache[art_id][side]})
        descriptor = copy.deepcopy(manifest.get("native_info_index", {}))
        descriptor["compatibility_flat_assets"] = not manifest.get("native_only_art", False)
        if descriptor.get("path") != "Struct/indexes/info.bin" or descriptor.get("ready_path") != "Struct/indexes/info.ready":
            _fail("index_topology_missing", "Exact information index/readiness relations are required")
        rows = [{"role": row["role"], "subject": row["subject"], "info_id": row["id"], "sha256": row["sha256"]} for row in final_relations]
        index = track_info.compile_index(rows, build_id=manifest["build_id"])
        asset_rows = [{"side": entry.key, "sha256": entry.sha256, "info_id": row["id"], "info_sha256": row["sha256"]}
                      for row in final_relations for entry in track_info.parse_info(blobs[row["path"]]).entries if entry.kind == 1]
        asset_index = track_info.compile_asset_index(asset_rows, build_id=manifest["build_id"])
        asset_descriptor = {"schema": "xm5.native-asset-index", "version": 1, "path": "Struct/indexes/assets.bin",
                            "sha256": _sha(asset_index), "size": len(asset_index), "record_count": (len(asset_index) - 128) // 96,
                            "record_bytes": 96, "owner_policy": "minimum-uuid-per-payload-side"}
        asset_entry = retained.entries.get(asset_descriptor["path"])
        updates.append({"path": asset_descriptor["path"], "kind": asset_entry["kind"] if asset_entry else "native_asset_index",
                        "managed_id": health.managed_id(asset_entry, manifest) if asset_entry else "file:" + asset_descriptor["path"], "data": asset_index})
        ready = _json({"schema": "xm5.info-readiness", "version": 1, "build_id": manifest["build_id"], "index_sha256": _sha(index),
                       "index_size": len(index), "record_count": len(rows),
                       "compatibility_flat_assets": descriptor["compatibility_flat_assets"],
                       "assets": asset_descriptor,
                       "information": [{key: row[key] for key in ("id", "path", "size", "sha256")} for row in final_relations]}) + b"\n"
        for path, data in ((descriptor["path"], index), (descriptor["ready_path"], ready)):
            entry = retained.named(path)
            updates.append({"path": path, "kind": entry["kind"], "managed_id": health.managed_id(entry, manifest), "data": data})
        descriptor.update(sha256=_sha(index), size=len(index), ready_sha256=_sha(ready), ready_size=len(ready), record_count=len(rows), information_count=len(rows))
        descriptor.update(assets_path=asset_descriptor["path"], assets_sha256=asset_descriptor["sha256"],
                          assets_size=asset_descriptor["size"], assets_record_count=asset_descriptor["record_count"])
        binding_descriptor=manifest.get("playlist_binding_index")
        if binding_descriptor is not None:
            binding=retained.named(binding_descriptor["path"])
            binding_data=device_state.compile_playlist_bindings(manifest["device_state"])
            if {"size":len(binding_data),"sha256":_sha(binding_data)}!=health.baseline_version(binding):
                _fail("playlist_binding_identity","Prepared playlist binding bytes differ from the frozen manifest state baseline")
            if retained.records[binding["path"]]["card"]!=health.baseline_version(binding) or binding["path"] in selected:
                updates.append({"path":binding["path"],"kind":binding["kind"],"managed_id":health.managed_id(binding,manifest),"data":binding_data})
        for update in updates:
            update["inputs"] = list(retained.inputs.values())
            expected = (expected_recipes or {}).get(update["kind"])
            if expected is not None and update.get("metadata", {}).get("recipe_id", retained.named(update["path"]).get("recipe_id")) != expected:
                _fail("recipe_unavailable", "Requested repair recipe is unavailable", path=update["path"])
        plan = sync.plan_sync(card_root, source_roots, updates=updates, choices=choices, repair_only=True,
                              manifest_metadata={"information": final_relations, "native_info_index": descriptor, "native_asset_index": asset_descriptor})
        before = {row["path"]: row["card"] for row in report["records"]}
        after = {row["path"]: row["version"] for row in plan["preconditions"]}
        if before != {path: version for path, version in after.items() if path in before} or _sha(base64.b64decode(plan["baseline_manifest_b64"])) != report["manifest_version"]["sha256"]:
            _fail("destination_changed", "Owned retained inputs or manifest changed during repair generation")
        if plan["sources"] != report["sources"]:
            _fail("source_changed", "Declared source roots changed during repair generation")
        observed_sources = {(row["source"]["root_id"], row["source"]["relative"]): row["source"]["version"]
                            for row in report["records"] if row["source"]["status"] in {"present", "missing"}}
        for dependency in plan["source_checks"]:
            key = dependency["root_id"], dependency["relative"]
            if key in observed_sources and observed_sources[key] != dependency["version"]:
                _fail("source_changed", "Declared retained source changed during repair generation", root_id=key[0], relative=key[1])
        return plan


def plan_information_repair(card_root, source_roots=None, *, selected_paths=None, choices=None, expected_recipes=None,
                            ffmpeg_path=None, peak_cache_dir=None, peak_scanner=None):
    """Complete plan builder; does not apply it or mutate original/user state."""
    return InformationRepairGenerator(selected_paths=selected_paths, ffmpeg_path=ffmpeg_path, peak_cache_dir=peak_cache_dir,
                                      peak_scanner=peak_scanner).plan_repair(card_root, source_roots, choices=choices, expected_recipes=expected_recipes)
