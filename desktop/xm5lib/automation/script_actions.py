"""Actual-state scripting dispatch with durable exact-source plans.

No profile, GUI, listener or process is started by import. The existing Engine
provides the Application, locks, configured path roots and durable plan table.
"""
from __future__ import annotations

import base64
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import uuid

import mutagen

from ..application import DomainError
from ..core.restore_card import no_links
from ..ui import tagfile
from .. import scripting
from ..core import script_tags
from ..scripting.context import capture_context

MAX_SELECTION = 10000
MAX_DOCUMENT = 4_000_000
SOURCE_KINDS = {"tags", "actions", "filename_to_tags", "tag_to_tag", "number_tracks", "text_to_tags", "covers", "rename", "filename_to_filename"}


def _fail(code, message, **details):
    raise DomainError(code, message, details)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _save(path, value):
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("xb") as output:
        output.write(_json(value) + b"\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _version(path):
    value = path.stat()
    return {"size": value.st_size, "mtime_ns": value.st_mtime_ns, "inode": value.st_ino, "device": value.st_dev}


def _copy(source, destination):
    with Path(source).open("rb") as stream, Path(destination).open("xb") as output:
        shutil.copyfileobj(stream, output, 1024 * 1024)
        output.flush()
        os.fsync(output.fileno())


def _audio_identity(path, pictures_changed=False):
    """Exact non-tag bytes for the three admitted containers, without decoding."""
    digest = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as stream:
        def consume(length):
            while length:
                block = stream.read(min(length, 1024 * 1024))
                if not block:
                    _fail("INVALID_AUDIO", "Truncated audio container")
                digest.update(block)
                length -= len(block)
        if path.suffix.lower() == ".wav":
            header = stream.read(12)
            if header[:4] != b"RIFF" or header[8:] != b"WAVE":
                _fail("FORMAT_UNSUPPORTED", "Only ordinary RIFF WAVE is admitted")
            while stream.tell() < size:
                header = stream.read(8)
                if len(header) != 8:
                    _fail("INVALID_AUDIO", "Invalid RIFF chunk")
                kind, length = struct.unpack("<4sI", header)
                if stream.tell() + length + (length & 1) > size:
                    _fail("INVALID_AUDIO", "RIFF chunk outside source bounds")
                if kind.lower() != b"id3 ":
                    digest.update(header)
                    consume(length + (length & 1))
                else:
                    stream.seek(length + (length & 1), 1)
        elif path.suffix.lower() == ".flac":
            if stream.read(4) != b"fLaC":
                _fail("FORMAT_UNSUPPORTED", "Only ordinary native FLAC is admitted")
            while True:
                header = stream.read(4)
                if len(header) != 4:
                    _fail("INVALID_AUDIO", "Truncated FLAC metadata")
                kind, length = header[0] & 127, int.from_bytes(header[1:], "big")
                if stream.tell() + length > size:
                    _fail("INVALID_AUDIO", "FLAC metadata outside source bounds")
                if kind not in ((1, 4, 6) if pictures_changed else (1, 4)):  # padding and Vorbis comment may change
                    digest.update(bytes([kind]) + header[1:])
                    consume(length)
                else:
                    stream.seek(length, 1)
                if header[0] & 128:
                    break
            consume(size - stream.tell())
        elif path.suffix.lower() == ".mp3":
            header = stream.read(10)
            offset = 0
            if header[:3] == b"ID3":
                if len(header) < 10 or any(value & 128 for value in header[6:10]):
                    _fail("INVALID_AUDIO", "Invalid ID3 size")
                offset = 10 + sum(value << (7 * (3 - index)) for index, value in enumerate(header[6:10]))
                if header[5] & 16:
                    offset += 10
            if offset > size:
                _fail("INVALID_AUDIO", "ID3 tag outside source bounds")
            if size >= 128:
                stream.seek(size - 128)
                if stream.read(3) == b"TAG":
                    _fail("FORMAT_UNSUPPORTED", "MP3 with ID3v1 needs a separate preservation policy")
            stream.seek(offset)
            consume(size - offset)
        else:
            _fail("FORMAT_UNSUPPORTED", "Source tag writes currently admit WAV, native FLAC and MP3 without ID3v1")
    return digest.hexdigest()


def _value(value):
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"type": type(value).__name__, "attributes": _value(vars(value))} if hasattr(value, "__dict__") else str(value)


def _untouched_tags(path, changed):
    audio = mutagen.File(path)
    if audio is None:
        _fail("FORMAT_UNSUPPORTED", "Tags cannot be read")
    entries = {}
    if path.suffix.lower() == ".flac":
        exclude = set(changed)
        if "comment" in changed:
            exclude.add("description")
        if "albumartist" in changed:
            exclude.add("album artist")
        if "tracknumber" in changed:
            exclude.update(("tracktotal", "totaltracks"))
        if "discnumber" in changed:
            exclude.update(("disctotal", "totaldiscs"))
        entries = {str(key): _value(value) for key, value in (audio.tags or {}).items() if key.lower() not in exclude}
    else:
        exclude = {tagfile.ID3_FRAMES[name] for name in changed if name != "comment"}
        if "date" in changed:
            exclude.update(("TYER", "TDAT", "TIME", "TRDA"))
        for key, value in (audio.tags or {}).items():
            if key.split(":", 1)[0] in exclude or "comment" in changed and key.startswith("COMM:") and not value.desc:
                continue
            # Text encoding may change during ID3 serialization. Values, frame
            # identities and binary data remain protected.
            attributes = {k: v for k, v in vars(value).items() if k != "encoding"}
            entries[key] = _value(attributes)
    pictures = [{"mime": p.mime, "kind": p.kind, "data": _value(p.data)} for p in tagfile.read(str(path)).pictures]
    return {"tags": entries, "pictures": pictures}


class ScriptActions:
    """Attach to the existing Engine; call dispatch from its acknowledged path."""

    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def handles(name):
        return name in {"scripts.capture", "scripts.format", "scripts.convert", "scripts.plan", "scripts.execute"}

    def dispatch(self, name, args, revision=None, key=None):
        try:
            if name == "scripts.capture":
                return {"tracks": self._capture(args["selection"])}
            if name == "scripts.format":
                records = self._capture(args["selection"])
                context, counters = capture_context(args.get("context")), {}
                return {"context": context, "values": [{"track_id": row["track_id"], "value": scripting.evaluate(args["text"], row["fields"], row["technical"], captured=context, counters=counters)} for row in records]}
            if name == "scripts.convert":
                records = self._capture(args["selection"])
                context = capture_context(args.get("context"))
                values = []
                for row in records:
                    if args["direction"] == "filename_to_tags":
                        result = scripting.filename_to_tags(row["path"], args["pattern"])
                    elif args["direction"] == "tags_to_filename":
                        result = scripting.tags_to_filename(args["pattern"], row["fields"], row["path"], row["technical"],captured=context)
                    else:
                        _fail("INVALID_ARGUMENT", "Unknown converter direction")
                    values.append({"track_id": row["track_id"], "proposed": result})
                return {"values": values, "writes": False}
            if name == "scripts.plan":
                with self.engine.lock, self.engine.application._write_lock:
                    return self.plan(args, key)
            if name == "scripts.execute":
                return self.execute(args, key)
            _fail("UNKNOWN_COMMAND", "Unknown script command")
        except scripting.ScriptError as exc:
            _fail(exc.code, str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            _fail("INVALID_ARGUMENT", str(exc))

    def _capture(self, selection):
        if not isinstance(selection, list) or not 1 <= len(selection) <= MAX_SELECTION:
            _fail("EXACT_SELECTION_REQUIRED", "Supply a nonempty bounded list of stable track references")
        records = []
        for position, selected in enumerate(selection):
            if not isinstance(selected, dict) or not isinstance(selected.get("track_id"), str):
                _fail("EXACT_SELECTION_REQUIRED", "Each source requires an exact track ID")
            track = self.engine.application.query("tracks.get", {"id": selected["track_id"]})["track"]
            path = self.engine._under(selected.get("path") or track["path"], self.engine.music_roots)
            present = {str(Path(item["path"]).resolve()) for item in track["locations"] if item["present"]}
            if str(path) not in present or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_nlink != 1:
                _fail("SOURCE_SCOPE", "Source must be a single-link known present track location")
            before = _version(path)
            tags = tagfile.read(str(path))
            if tags.error:
                _fail("TAG_READ_FAILED", tags.error)
            native_fields = script_tags.read_values(path)
            fingerprint = _hash(path)
            if _version(path) != before:
                _fail("SOURCE_CHANGED", "Source changed during capture")
            folder = hashlib.sha256(str(path.parent).encode()).hexdigest()[:32]
            records.append({"track_id": track["id"], "path": str(path), "revision": str(track["revision"]),
                            "fields": native_fields, "version": before,
                            "source_fingerprint": fingerprint, "folder_id": folder,
                            "occurrence_id": selected.get("occurrence_id") or f"selection:{position}:{track['id']}",
                            "technical": {"_TRACK_ID": track["id"],
                                          "_OCCURRENCE_ID": selected.get("occurrence_id") or f"selection:{position}:{track['id']}",
                                          "_PATH": str(path), "_FILENAME": path.stem, "_FILENAME_EXT": path.name,
                                          "_FOLDERPATH": str(path.parent), "_EXTENSION": path.suffix[1:],
                                          "_FILE_MOD_DATETIME_RAW": str(int(path.stat().st_mtime)), "_COUNTER": str(position+1), "_TOTAL_FILES": str(len(selection))}})
        return records

    @staticmethod
    def _tracks(records):
        return tuple(scripting.CapturedTrack(**{k: row[k] for k in ("track_id", "path", "fields", "revision", "source_fingerprint", "technical", "folder_id", "occurrence_id")}) for row in records)

    def plan(self, args, key=None):
        request_hash = hashlib.sha256(_json(args)).hexdigest()
        identity = uuid.uuid5(uuid.UUID("5ef10f81-8ea8-52b6-a6ab-a755a2953fbe"), "script-plan:" + key).hex if key else uuid.uuid4().hex
        if key:
            with self.engine.connect() as connection:
                previous = connection.execute("SELECT digest,body FROM plans WHERE id=?", (identity,)).fetchone()
            if previous:
                payload = json.loads(previous[1])
                if payload.get("request_sha256") != request_hash:
                    _fail("IDEMPOTENCY_CONFLICT", "Planning key belongs to another request")
                return {"plan_id": identity, "digest": previous[0], "plan": payload,
                        "source_writes": payload["kind"] in SOURCE_KINDS, "replayed": True}
        records = self._capture(args["selection"])
        tracks = self._tracks(records)
        context = capture_context(args.get("context"))
        kind = args["kind"]
        payload = {"schema": "xm5.script-plan", "version": 1, "kind": kind, "request_sha256": request_hash,
                   "selection": args["selection"], "capture": records, "context": context}
        if kind in SOURCE_KINDS - {"rename", "filename_to_filename", "covers"}:
            if len({row["track_id"] for row in records}) != len(records):
                _fail("DUPLICATE_TARGET", "Source mutation requires unique track IDs")
            if kind == "actions":
                if "mta_text" in args:
                    raw = args["mta_text"].encode("utf-8")
                    group = scripting.import_mta(raw)
                else:
                    group = scripting.ActionGroup(args.get("name", "Action group"), tuple(scripting.Action(item["kind"], item.get("options", {})) for item in args["actions"]))
                from .script_groups import plan_group
                group_writes = plan_group(self,group,records,payload,context,args)
                operations = []
            else:
                operations = []
                text_rows = None
                if kind == "text_to_tags":
                    if "input_file" in args:
                        source = self.engine._under(args["input_file"], self.engine.import_roots + self.engine.music_roots)
                        if source.stat().st_size > MAX_DOCUMENT: _fail("LIMIT", "Text input exceeds four MB")
                        raw = source.read_bytes()
                        payload["inputs"] = [{"path": str(source), "sha256": _hash(source), "version": _version(source)}]
                        from ..scripting.actions import decode_document
                        _, text = decode_document(raw, source.name, args.get("encoding"))
                    else: text = args["text"]
                    if len(text.encode("utf-8")) > MAX_DOCUMENT: _fail("LIMIT", "Text input exceeds four MB")
                    text_rows = text.splitlines()
                    if len(text_rows) != len(records): _fail("TEXT_ROW_COUNT", "Text import needs one exact row per selected track")
                counters, folder_positions = {}, {}
                start, pad = args.get("start", 1), args.get("pad", 0)
                if kind == "number_tracks" and (type(start) is not int or not 0<=start<=1_000_000 or type(pad) is not int or not 0<=pad<=20):
                    _fail("INVALID_ARGUMENT", "Track numbering requires bounded integer start/pad")
                for position, row in enumerate(records):
                    if kind == "tags": changes = args["changes"]
                    elif kind == "filename_to_tags": changes = scripting.filename_to_tags(row["path"], args["pattern"])
                    elif kind == "tag_to_tag": changes = {args["field"]: scripting.evaluate(args["value"],row["fields"],row["technical"],captured=context,counters=counters)}
                    elif kind == "text_to_tags": changes = scripting.match_values(text_rows[position], args["pattern"], allow_path_separators=True)
                    elif kind == "number_tracks":
                        folder=row["folder_id"] if args.get("reset_per_folder",False) else "all"
                        ordinal=folder_positions.get(folder,0);folder_positions[folder]=ordinal+1
                        count=sum(1 for item in records if item["folder_id"]==row["folder_id"]) if folder!="all" else len(records)
                        number=str(start+ordinal).zfill(pad)
                        if args.get("total",False): number+="/"+str(start+count-1).zfill(pad)
                        changes={"TRACKNUMBER":number}
                    else: _fail("KIND_UNSUPPORTED", "Unsupported source conversion")
                    operations.append({"track_id": row["track_id"], "set": changes, "remove": []})
            writes = group_writes if kind=="actions" else []
            for operation in operations:
                if isinstance(operation, dict):
                    tid, changes, removed = operation["track_id"], operation["set"], operation["remove"]
                else:
                    if operation.kind != "set_tags":
                        _fail("SOURCE_ACTION_UNSUPPORTED", "Filename changes are preview-only")
                    tid, changes, removed = operation.track_id, operation.values["set"], operation.values["remove"]
                normalized = {}
                for field, value in {**changes, **{field: "" for field in removed}}.items():
                    field = field.lower()
                    field = script_tags.canonical(field)
                    value = list(value) if isinstance(value, (list, tuple)) else [value]
                    if len(value)>1024 or any(not isinstance(v,str) or len(v)>65536 or "\0" in v for v in value):
                        _fail("INVALID_ARGUMENT", "Tag values must be bounded text values")
                    normalized[field] = [v for v in value if v]
                row = next(row for row in records if row["track_id"] == tid)
                if normalized:
                    path = Path(row["path"])
                    writes.append({"track_id": tid, "path": str(path), "changes": normalized,
                                   "audio_identity": _audio_identity(path), "untouched": script_tags.untouched(path, normalized)})
            payload["writes"] = writes
        elif kind in {"covers", "cover_export"}:
            from ..scripting import pictures
            if len({row["track_id"] for row in records}) != len(records): _fail("DUPLICATE_TARGET", "Cover operations require unique track IDs")
            options=args.get("options",{})
            imported=None;payload["writes"]=[];payload["files"]=[]
            if kind=="covers" and options.get("operation")=="import":
                source_path=self.engine._under(options["input_file"],self.engine.import_roots+self.engine.music_roots)
                if source_path.stat().st_size>16*1024*1024: _fail("LIMIT","Picture input exceeds sixteen MiB")
                before=_version(source_path);imported=source_path.read_bytes()
                if _version(source_path)!=before: _fail("SOURCE_CHANGED","Picture changed during capture")
                payload["inputs"]=[{"path":str(source_path),"version":before,"sha256":hashlib.sha256(imported).hexdigest()}]
            destinations=set()
            for row in records:
                path=Path(row["path"]);existing=script_tags.pictures(path)
                if kind=="cover_export":
                    output=self.engine._under(args["output_directory"],self.engine.export_roots)
                    if not output.is_dir(): _fail("OUTPUT_SCOPE","Choose an existing allowed export directory")
                    indexes=args.get("indexes",list(range(len(existing))))
                    for index in indexes:
                        if type(index) is not int or not 0<=index<len(existing): _fail("COVER_SELECTION","Select an existing picture index")
                        item=existing[index];extension={"image/png":"png","image/jpeg":"jpg","image/gif":"gif","image/bmp":"bmp","image/webp":"webp"}.get(item["mime"])
                        if not extension: _fail("COVER_FORMAT","Picture MIME has no verified extension")
                        name=scripting.evaluate(args.get("pattern","%_FILENAME%"),row["fields"],row["technical"],captured=context)
                        from ..scripting.converters import safe_relative_path
                        target=no_links(output/safe_relative_path(name+"-"+str(index)+"."+extension))
                        if target.exists() or str(target).casefold() in destinations: _fail("OUTPUT_EXISTS","Cover output already exists or collides")
                        destinations.add(str(target).casefold())
                        payload["files"].append({"path":str(target),"data_base64":item["data_base64"],"sha256":item["sha256"]})
                else:
                    replacement=script_tags.prepare_pictures(path,pictures.transform(existing,options,imported))
                    if replacement!=existing:
                        payload["writes"].append({"track_id":row["track_id"],"path":str(path),"changes":{},"pictures":replacement,
                                                 "audio_identity":_audio_identity(path,pictures_changed=True),"untouched":script_tags.untouched(path,{},pictures_changed=True)})
        elif kind in {"rename", "filename_to_filename"}:
            if len({row["track_id"] for row in records}) != len(records): _fail("DUPLICATE_TARGET", "Rename requires unique track IDs")
            moves=[]; destinations=set()
            for row in records:
                path=Path(row["path"])
                root=self.engine._under(args["destination_root"],self.engine.music_roots) if args.get("destination_root") else max((root for root in self.engine.music_roots if path.is_relative_to(root)),key=lambda root:len(str(root)))
                fields=dict(row["fields"])
                if kind=="filename_to_filename": fields.update(scripting.filename_to_tags(str(path),args["match_pattern"]))
                destination=no_links(root/scripting.tags_to_filename(args["pattern"],fields,str(path),row["technical"],captured=context))
                self.engine._under(destination,self.engine.music_roots)
                if destination==path: continue
                if destination.exists(): _fail("DESTINATION_EXISTS", "Rename destination already exists",path=str(destination))
                if str(destination).casefold() in destinations: _fail("PATH_COLLISION", "Rename destinations collide")
                destinations.add(str(destination).casefold())
                moves.append({"track_id":row["track_id"],"old_path":str(path),"new_path":str(destination),"sha256":row["source_fingerprint"]})
            if {move["old_path"].casefold() for move in moves}&destinations: _fail("PATH_COLLISION", "Rename swaps/cycles require separate transactions")
            payload["moves"]=moves
            payload["application_revision"]=self.engine.application.query("playlists.list",{})["revision"]
        elif kind == "template":
            raw = args["text"].encode("utf-8")
            if len(raw) > MAX_DOCUMENT:
                _fail("LIMIT", "Template exceeds four MB")
            output = self.engine._under(args["output_directory"], self.engine.export_roots)
            if not output.is_dir():
                _fail("OUTPUT_SCOPE", "Choose an existing explicitly allowed output directory")
            result = scripting.plan_export(scripting.import_mte(raw), tracks, default_name=args.get("default_name", "export.txt"),
                                           partition=args.get("partition"), bom=args.get("bom", False),
                                           ansi_encoding=args.get("ansi_encoding"), captured=context)
            files = []
            for item in result.files:
                path = no_links(output / item.relative_path)
                if path.exists():
                    _fail("OUTPUT_EXISTS", "Template output must be new", path=str(path))
                files.append({"path": str(path), "data_base64": base64.b64encode(item.data).decode(), "sha256": hashlib.sha256(item.data).hexdigest()})
            payload["files"] = files
        elif kind == "playlists":
            values = dict(args["recipe"])
            for key in ("all_of", "any_of"):
                values[key] = tuple(scripting.Predicate(**item) for item in values.get(key, []))
            result = scripting.plan_playlists(scripting.PlaylistRecipe(**values), tracks,captured=context)
            existing = {item["name"]: item for item in self.engine.application.query("playlists.list", {})["playlists"]}
            payload["playlists"] = []
            for item in result.playlists:
                before = existing.get(item.name)
                payload["playlists"].append({"name": item.name, "track_ids": [entry.track_id for entry in item.occurrences],
                                               "recipe_playlist_id": item.playlist_id, "target": before})
            if len({item["name"].casefold() for item in payload["playlists"]}) != len(payload["playlists"]):
                _fail("PLAYLIST_NAME_COLLISION", "Recipe groups must resolve to distinct names")
        else:
            _fail("KIND_UNSUPPORTED", "Unknown script plan kind")
        frozen = _json(payload)
        if len(frozen)>64*1024*1024: _fail("PLAN_LIMIT","Script plan exceeds64 MiB; use a smaller selection")
        digest = hashlib.sha256(frozen).hexdigest()
        with self.engine.connect() as connection:
            connection.execute("INSERT INTO plans VALUES(?,?,?)", (identity, digest, _json(payload).decode()))
        return {"plan_id": identity, "digest": digest, "plan": payload, "source_writes": kind in SOURCE_KINDS}

    def execute(self, args, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            _fail("INVALID_ARGUMENT", "Execution requires a bounded idempotency key")
        with self.engine.lock, self.engine.application._write_lock:
            record = self.engine.query("plans.get", {"id": args["plan_id"]})
            payload = record["plan"]
            if (record["digest"] != args["digest"] or hashlib.sha256(_json(payload)).hexdigest() != args["digest"]
                    or payload.get("schema") != "xm5.script-plan"):
                _fail("PLAN_STALE", "Exact script-plan digest required")
            source = payload["kind"] in SOURCE_KINDS
            if source and args.get("approve_source_writes") is not True:
                _fail("SOURCE_WRITE_APPROVAL_REQUIRED", "Execution must explicitly approve this exact source-write plan")
            directory = self.engine.home / "script-actions" / args["plan_id"]
            directory.mkdir(parents=True, exist_ok=True)
            journal = directory / "execution.json"
            if journal.exists():
                previous = json.loads(journal.read_bytes())
                if previous["key"] != key:
                    _fail("PLAN_ALREADY_EXECUTED", "Plan is already bound to another execution key")
                if "result" in previous:
                    return {**previous["result"], "replayed": True}
                _fail("EXECUTION_OUTCOME_UNKNOWN", "Interrupted script execution requires inspection; it is never replayed automatically", journal=str(journal))
            current = self._capture(payload["selection"])
            if current != payload["capture"]:
                _fail("PLAN_STALE", "Exact source bytes, metadata, location or backend revision changed")
            if "playlists" in payload:
                for item in payload["playlists"]:
                    if item["target"]:
                        actual = self.engine.application.query("playlists.get", {"id": item["target"]["id"]})["playlist"]
                        if actual != item["target"]:
                            _fail("PLAN_STALE", "Playlist target changed after planning")
                    elif any(p["name"].casefold() == item["name"].casefold() for p in self.engine.application.query("playlists.list", {})["playlists"]):
                        _fail("PLAN_STALE", "Playlist name was created after planning")
            for item in payload.get("inputs", []):
                source_input=self.engine._under(item["path"],self.engine.import_roots+self.engine.music_roots)
                if _version(source_input)!=item["version"] or _hash(source_input)!=item["sha256"]: _fail("PLAN_STALE", "External source input changed")
            for move in payload.get("moves", []):
                if no_links(move["new_path"]).exists(): _fail("DESTINATION_EXISTS", "Rename destination appeared after planning")
            if payload["kind"] in {"rename", "filename_to_filename"} and self.engine.application.query("playlists.list",{})["revision"]!=payload["application_revision"]:
                _fail("PLAN_STALE", "Application state changed after rename planning")
            for item in payload.get("files", []):
                path = self.engine._under(item["path"], self.engine.export_roots)
                if path.exists():
                    _fail("OUTPUT_EXISTS", "Template destination appeared after planning")
            state = {"key": key, "digest": args["digest"], "status": "preparing", "outcomes": []}
            _save(journal, state)
            try:
                if payload["kind"] in {"rename", "filename_to_filename"}:
                    result = self._rename_execute(payload,directory,state,journal,key)
                elif source:
                    result = self._source_execute(payload, directory, state, journal, key)
                elif payload["kind"] in {"template", "cover_export"}:
                    outputs = []
                    for item in payload["files"]:
                        path = self.engine._under(item["path"], self.engine.export_roots)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        blob = base64.b64decode(item["data_base64"], validate=True)
                        if hashlib.sha256(blob).hexdigest() != item["sha256"]:
                            _fail("PLAN_STALE", "Frozen template bytes changed")
                        with path.open("xb") as stream:
                            stream.write(blob)
                            stream.flush()
                            os.fsync(stream.fileno())
                        outputs.append(str(path))
                        state["outcomes"].append({"path": str(path), "status": "created"})
                        _save(journal, state)
                    result = {"status": "complete", "files": outputs, "source_writes": False}
                else:
                    receipts = []
                    for position, item in enumerate(payload["playlists"]):
                        target = item["target"]
                        command = "playlists.replace" if target else "playlists.create"
                        values = {"track_ids": item["track_ids"], **({"id": target["id"]} if target else {"name": item["name"]})}
                        receipt = self.engine.application.execute(command, values, expected_revision=target["revision"] if target else None,
                                                                  idempotency_key=f"script:{args['plan_id']}:{position}")
                        receipts.append(receipt)
                        state["outcomes"].append({"playlist_id": receipt["result"]["id"], "status": "applied"})
                        _save(journal, state)
                    result = {"status": "complete", "receipts": receipts, "source_writes": False,
                              "identity_policy": "backend creates IDs; replacement preserves existing occurrences by track queues"}
            except Exception as exc:
                result = {"status": "partial" if state["outcomes"] else "failed", "error": str(exc), "outcomes": state["outcomes"], "journal": str(journal)}
            state.update(status=result["status"], result=result)
            _save(journal, state)
            return result

    def _rename_execute(self, payload, directory, state, journal, key):
        captures = {row["track_id"]: row for row in payload["capture"]}
        created, removed = [], []
        try:
            for position, move in enumerate(payload["moves"]):
                source = self.engine._under(move["old_path"],self.engine.music_roots)
                destination = self.engine._under(move["new_path"],self.engine.music_roots)
                capture = captures[move["track_id"]]
                if _version(source)!=capture["version"] or _hash(source)!=move["sha256"] or source.stat().st_nlink!=1:
                    _fail("SOURCE_CHANGED","Rename source changed before exact backup")
                backup=directory/(str(position)+".original"+source.suffix)
                _copy(source,backup)
                if _hash(backup)!=move["sha256"]: _fail("SOURCE_CHANGED","Rename backup differs")
                destination.parent.mkdir(parents=True,exist_ok=True)
                no_links(destination)
                _copy(backup,destination)
                os.utime(destination,ns=(source.stat().st_atime_ns,capture["version"]["mtime_ns"]))
                created.append(move)
                if _hash(destination)!=move["sha256"] or _version(source)!=capture["version"] or _hash(source)!=move["sha256"]:
                    _fail("SOURCE_CHANGED","Rename source/destination changed during staging")
                source.unlink()
                removed.append(move)
                state["outcomes"].append({**move,"backup":str(backup),"status":"relocated"})
                _save(journal,state)
            receipt = self.engine.application.execute("library.relocate_files",{"moves":payload["moves"]},
                        expected_revision=payload["application_revision"],idempotency_key="script-relocate:"+directory.name) if payload["moves"] else None
            return {"status":"complete","source_writes":True,"moves":payload["moves"],"receipt":receipt,"journal":str(journal)}
        except Exception as exc:
            recovery=[]
            # If application state already observes a new path, do not create a
            # contradictory rollback. Preserve the exact journal for inspection.
            backend_committed=any(self.engine.application.query("tracks.get",{"id":move["track_id"]})["track"]["path"] and os.path.normcase(self.engine.application.query("tracks.get",{"id":move["track_id"]})["track"]["path"])==os.path.normcase(move["new_path"]) for move in removed)
            if backend_committed:
                return {"status":"recovery_required","source_writes":True,"error":str(exc),"journal":str(journal)}
            for move in reversed(created):
                source,destination=no_links(move["old_path"]),no_links(move["new_path"])
                if not destination.exists() or _hash(destination)!=move["sha256"]:
                    recovery.append({"path":str(destination),"status":"conflict"});continue
                if move in removed:
                    if source.exists(): recovery.append({"path":str(source),"status":"conflict"});continue
                    _copy(destination,source)
                    os.utime(source,ns=(destination.stat().st_atime_ns,captures[move["track_id"]]["version"]["mtime_ns"]))
                    if _hash(source)!=move["sha256"]: recovery.append({"path":str(source),"status":"conflict"});continue
                destination.unlink()
                recovery.append({"path":str(source),"status":"restored"})
            return {"status":"recovery_required" if any(row["status"]!="restored" for row in recovery) else "rolled_back",
                    "source_writes":bool(created),"error":str(exc),"recovery":recovery,"journal":str(journal)}

    def _source_execute(self, payload, directory, state, journal, key):
        prepared, applied, exported, playlist_receipts = [], [], [], []
        captures = {row["track_id"]: row for row in payload["capture"]}
        try:
            # All files stage and verify before any original is replaced.
            for position, item in enumerate(payload["writes"]):
                path = self.engine._under(item["path"], self.engine.music_roots)
                stage = directory / (str(position) + ".stage" + path.suffix)
                backup = directory / (str(position) + ".original" + path.suffix)
                _copy(path, backup)
                if _hash(backup) != captures[item["track_id"]]["source_fingerprint"]:
                    _fail("SOURCE_CHANGED", "Exact backup differs from the planned original source")
                _copy(backup, stage)
                script_tags.write(str(stage), item["changes"], item.get("pictures"),item.get("native_delete",[]))
                if _audio_identity(stage,pictures_changed="pictures" in item) != item["audio_identity"] or script_tags.untouched(stage, item["changes"], pictures_changed="pictures" in item,native_delete=item.get("native_delete",[])) != item["untouched"]:
                    _fail("PRESERVATION_FAILED", "Staged mutation changed non-target tags, artwork or original audio bytes")
                actual = script_tags.read_values(stage)
                if any(actual.get(field, []) != value for field, value in item["changes"].items()):
                    _fail("WRITE_VERIFICATION_FAILED", "Staged tag fields do not match the exact plan")
                if "pictures" in item and script_tags.pictures(stage)!=item["pictures"]:
                    _fail("WRITE_VERIFICATION_FAILED","Staged picture set differs from exact plan")
                prepared.append((item, path, stage, backup, _hash(stage)))
            for item, path, stage, backup, after in prepared:
                capture = captures[item["track_id"]]
                if path.stat().st_nlink != 1 or _version(path) != capture["version"] or _hash(path) != capture["source_fingerprint"]:
                    _fail("SOURCE_CHANGED", "Source changed before atomic replacement")
                # Keep stage and immutable original backup for recovery. The
                # adjacent temporary permits atomic same-volume replacement.
                adjacent = no_links(path.with_name(".xm5-script-" + uuid.uuid4().hex + path.suffix))
                _copy(stage, adjacent)
                try:
                    if _hash(path) != capture["source_fingerprint"]:
                        _fail("SOURCE_CHANGED", "Source changed during final preparation")
                    os.replace(adjacent, path)
                finally:
                    adjacent.unlink(missing_ok=True)
                applied.append((item, path, backup, after))
                state["outcomes"].append({"track_id": item["track_id"], "path": str(path), "before_sha256": capture["source_fingerprint"],
                                           "after_sha256": after, "backup": str(backup), "status": "applied"})
                _save(journal, state)
            refresh = self.engine.application.execute("library.refresh", {"paths": [str(item[1]) for item in applied]},
                                                       idempotency_key="script-refresh:" + directory.name) if applied else None
            for item in payload.get("files",[]):
                target=self.engine._under(item["path"],self.engine.export_roots); target.parent.mkdir(parents=True,exist_ok=True)
                blob=base64.b64decode(item["data_base64"],validate=True)
                if hashlib.sha256(blob).hexdigest()!=item["sha256"]: _fail("PLAN_STALE","Frozen action export bytes changed")
                with target.open("xb") as stream:
                    stream.write(blob);stream.flush();os.fsync(stream.fileno())
                exported.append(item)
                state["outcomes"].append({"path":str(target),"status":"created"});_save(journal,state)
            for position,item in enumerate(payload.get("playlists",[])):
                target=item["target"];command="playlists.replace" if target else "playlists.create"
                values={"track_ids":item["track_ids"],**({"id":target["id"]} if target else {"name":item["name"]})}
                receipt=self.engine.application.execute(command,values,expected_revision=target["revision"] if target else None,
                    idempotency_key="script-group:"+directory.name+":"+str(position))
                playlist_receipts.append(receipt);state["outcomes"].append({"playlist_id":receipt["result"]["id"],"status":"applied"});_save(journal,state)
            return {"status": "complete", "source_writes": True, "outcomes": state["outcomes"], "refresh": refresh,
                    "playlist_receipts":playlist_receipts,"files":[item["path"] for item in exported],"journal": str(journal)}
        except Exception as exc:
            recovery = []
            for position,receipt in enumerate(reversed(playlist_receipts)):
                try:
                    self.engine.application.execute("undo",{"receipt_id":receipt["receipt_id"]},idempotency_key="script-group-rollback:"+directory.name+":"+str(position))
                    recovery.append({"playlist_id":receipt["result"]["id"],"status":"restored"})
                except Exception as rollback_error: recovery.append({"playlist_id":receipt["result"]["id"],"status":"conflict","error":str(rollback_error)})
            for item in reversed(exported):
                target=self.engine._under(item["path"],self.engine.export_roots)
                if target.exists() and _hash(target)==item["sha256"]:
                    target.unlink();recovery.append({"path":str(target),"status":"restored"})
                else: recovery.append({"path":str(target),"status":"conflict"})
            for item, path, backup, after in reversed(applied):
                if _hash(path) != after:
                    recovery.append({"path": str(path), "status": "conflict", "backup": str(backup)})
                    continue
                adjacent = no_links(path.with_name(".xm5-rollback-" + uuid.uuid4().hex + path.suffix))
                try:
                    _copy(backup, adjacent)
                    if _hash(path) != after or path.stat().st_nlink != 1:
                        recovery.append({"path": str(path), "status": "conflict", "backup": str(backup)})
                        continue
                    os.replace(adjacent, path)
                    recovery.append({"path": str(path), "status": "restored"})
                except OSError as rollback_error:
                    recovery.append({"path": str(path), "status": "failed", "backup": str(backup), "error": str(rollback_error)})
                finally:
                    adjacent.unlink(missing_ok=True)
            if applied and all(item["status"] == "restored" for item in recovery):
                try:
                    self.engine.application.execute("library.refresh", {"paths": [str(item[1]) for item in applied]}, idempotency_key="script-rollback-refresh:" + directory.name)
                except Exception as refresh_error:
                    recovery.append({"status": "refresh_failed", "error": str(refresh_error)})
            return {"status": "recovery_required" if any(item["status"] != "restored" for item in recovery) else "rolled_back" if applied else "failed",
                    "error": str(exc), "source_writes": bool(applied), "outcomes": state["outcomes"], "recovery": recovery, "journal": str(journal)}
