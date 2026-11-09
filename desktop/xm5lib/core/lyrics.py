"""Lyrics discovery, explicit associations, portable storage and export planning.

Only managed profile files are written. Audio and original sidecars are read-only.
All public results are JSON serializable; stored/exported paths are relative.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import re
import sqlite3
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .lyrics_provider import LRCLIBProvider, _cancelled

MAX_SOURCE_BYTES = 1024 * 1024
NORMALIZATION_RECIPE = "xm5.lyrics.normalized"
NORMALIZATION_VERSION = 2
_TIME = re.compile(r"\[(\d{1,6}):(\d{2})(?:[.:](\d{1,3}))?\]")
_META = re.compile(r"\[([a-zA-Z][\w-]*):([^\]]*)\]")
_LANG = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", re.I)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _track_id(track):
    value = track.get("id", track.get("track_id")) if isinstance(track, dict) else track
    if value is None or not str(value).strip() or len(str(value)) > 512:
        raise ValueError("stable track id required")
    return str(value)


def decode_source(raw, encoding=None):
    """Decode a working view, retaining bytes separately and reporting guesses."""
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("lyrics source exceeds 1 MiB")
    diagnostics = []
    if encoding:
        text = raw.decode(encoding, errors="strict")
        return text.lstrip("\ufeff"), encoding, diagnostics
    for bom, name in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                      (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
                      (codecs.BOM_UTF16_BE, "utf-16")):
        if raw.startswith(bom):
            return raw.decode(name), name, diagnostics
    try:
        return raw.decode("utf-8"), "utf-8", diagnostics
    except UnicodeDecodeError:
        for name in ("gb18030", "cp1252"):
            try:
                text = raw.decode(name)
                diagnostics.append({"code": "encoding_guessed", "message": f"working view decoded as {name}"})
                return text, name, diagnostics
            except UnicodeDecodeError:
                pass
    diagnostics.append({"code": "decode_replacement", "message": "working view contains replacement characters"})
    return raw.decode("utf-8", errors="replace"), "unknown", diagnostics


def _stamp(milliseconds):
    milliseconds = max(0, milliseconds)
    minutes, rest = divmod(milliseconds, 60000)
    seconds, fraction = divmod(rest, 1000)
    return f"[{minutes:02d}:{seconds:02d}.{fraction:03d}]"


def parse_lyrics(raw, format="lrc", *, encoding=None):
    """Parse synthetic or user text; malformed rows are diagnosed, never repaired in place."""
    text, detected, diagnostics = decode_source(raw, encoding)
    if "\x00" in text:
        raise ValueError("binary/NUL lyrics source")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues, meta, plain = [], {}, []
    offset = 0
    expanded_bytes = 0

    def warn(code, line, message):
        diagnostics.append({"code": code, "line": line, "message": message})

    if format == "srt":
        for number, block in enumerate(re.split(r"\n\s*\n", text.strip()), 1):
            lines = block.splitlines()
            if lines and lines[0].strip().isdigit():
                lines = lines[1:]
            match = re.fullmatch(r"(\d{2,3}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*"
                                 r"(\d{2,3}):(\d{2}):(\d{2})[,\.](\d{3})(?:\s+.*)?", lines[0]) if lines else None
            if not match:
                warn("malformed_srt", number, "missing or invalid SRT timing")
                plain.extend(lines)
                continue
            values = [int(x) for x in match.groups()]
            start = ((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3]
            end = ((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7]
            if any(values[i] >= 60 for i in (1, 2, 5, 6)) or end < start:
                warn("invalid_srt_range", number, "invalid SRT seconds, minutes or range")
                continue
            cue_text = "\n".join(lines[1:])
            cues.append({"time_ms": start, "end_ms": end, "text": cue_text})
            plain.extend(lines[1:])
    elif format == "lrc":
        for number, line in enumerate(text.splitlines(), 1):
            position, times = 0, []
            while True:
                match = _TIME.match(line, position)
                if not match:
                    break
                minutes, seconds = int(match[1]), int(match[2])
                if seconds >= 60:
                    warn("invalid_timestamp", number, "LRC seconds must be below 60")
                else:
                    fraction = int((match[3] or "0").ljust(3, "0"))
                    times.append((minutes * 60 + seconds) * 1000 + fraction)
                position = match.end()
            if position:
                content = line[position:]
                expanded_bytes += len(times) * (len(content.encode("utf-8")) + 20)
                if expanded_bytes > MAX_SOURCE_BYTES:
                    raise ValueError("expanded lyrics working copy exceeds 1 MiB")
                if content.startswith("["):
                    warn("malformed_timestamp", number, "unparsed tag after timestamp")
                for moment in times:
                    cues.append({"time_ms": moment, "text": content})
                plain.append(content)
                continue
            match = _META.fullmatch(line.strip())
            if match:
                key, value = match[1].casefold(), match[2]
                meta[key] = value
                if key == "offset":
                    try:
                        candidate = int(value)
                        if abs(candidate) > 86400000:
                            raise ValueError()
                        offset = candidate
                    except ValueError:
                        warn("invalid_offset", number, "offset must be integer milliseconds within one day")
                elif key not in ("ar", "al", "ti", "au", "by", "re", "ve", "length", "lang"):
                    warn("unknown_tag", number, "unrecognized LRC metadata retained in source")
                continue
            if line.strip():
                plain.append(line)
                warn("malformed_timestamp" if line.lstrip().startswith("[") else "untimed_line", number,
                     "line retained in plain view without a usable timestamp")
        for cue in cues:
            cue["time_ms"] += offset
            if cue["time_ms"] < 0:
                warn("negative_time_clamped", 0, "working timestamp clamped to zero after offset")
                cue["time_ms"] = 0
    elif format == "txt":
        plain = text.splitlines()
    else:
        raise ValueError("supported lyrics formats: lrc, srt, txt")
    if any(cues[i]["time_ms"] > cues[i + 1]["time_ms"] for i in range(len(cues) - 1)):
        warn("out_of_order", 0, "working timestamps sorted; original order preserved in source")
    cues.sort(key=lambda cue: cue["time_ms"])
    normalized = "\n".join(_stamp(c["time_ms"]) + c["text"].replace("\n", " / ") for c in cues)
    if not cues:
        normalized = "\n".join(plain)
    if len(normalized.encode("utf-8")) >= MAX_SOURCE_BYTES:
        raise ValueError("lyrics working copy exceeds 1 MiB")
    return {"format": format, "kind": "synced" if cues else "plain", "encoding": detected,
            "text": text, "plain_text": "\n".join(plain), "synced_text": normalized if cues else "",
            "normalized_text": normalized + ("\n" if normalized else ""), "cues": cues,
            "metadata": meta, "offset_ms": offset, "diagnostics": diagnostics}


class LyricsPathError(ValueError):
    code = "linked_path"


def _no_links(path, *, allow_missing=False):
    """Reject preexisting links/reparse points, checking ancestors root first.

    Do not resolve before checking: that would discard the evidence. These are
    preexisting-path checks, not a guarantee against concurrent path replacement.
    """
    path = Path(path)
    if ".." in path.parts or (path.drive and not path.is_absolute()):
        raise LyricsPathError("ambiguous parent/drive-relative lyrics path rejected")
    if not path.is_absolute():
        path = Path.cwd() / path
    for component in [*reversed(path.parents), path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            if allow_missing:
                break
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise LyricsPathError(f"symlink or reparse lyrics path rejected: {component}")
    return path


def _bounded_file(path):
    path = _no_links(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("lyrics source is not a regular file")
    if info.st_size > MAX_SOURCE_BYTES:
        raise ValueError("lyrics source exceeds 1 MiB")
    with path.open("rb") as handle:
        raw = handle.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("lyrics source exceeds 1 MiB")
    return raw


def _embedded(path):
    """Yield read-only tag text snapshots; raw audio/tag encoding is never changed."""
    import mutagen

    path = _no_links(path)
    audio = mutagen.File(path)
    tags = getattr(audio, "tags", None)
    if tags is None:
        return
    if hasattr(tags, "getall"):
        for frame in tags.getall("USLT"):
            yield str(frame.text).encode("utf-8"), "txt", str(frame.lang or "und"), {
                "tag": "USLT", "description": str(frame.desc), "tag_encoding": int(frame.encoding),
                "storage_form": "decoded-tag-text", "audio_unmodified": True}
        for frame in tags.getall("SYLT"):
            # ID3 format 2 is milliseconds; format 1 is MPEG frames and cannot
            # be converted without a verified frame-rate/timebase.
            if frame.format != 2:
                continue
            text = "\n".join(_stamp(int(moment)) + str(value) for value, moment in frame.text)
            yield text.encode("utf-8"), "lrc", str(frame.lang or "und"), {
                "tag": "SYLT", "description": str(frame.desc), "tag_encoding": int(frame.encoding),
                "storage_form": "decoded-tag-text", "audio_unmodified": True}
    for key in tags.keys():
        if str(key).casefold() not in ("lyrics", "syncedlyrics", "unsyncedlyrics", "©lyr"):
            continue
        values = tags[key]
        if not isinstance(values, (list, tuple)):
            values = [values]
        for value in values:
            yield str(value).encode("utf-8"), "lrc" if str(key).casefold() == "syncedlyrics" else "txt", "und", {
                "tag": str(key), "storage_form": "decoded-tag-text", "audio_unmodified": True}


class LyricsStore:
    """Content-addressed originals and many-to-many stable track associations.

    Use one store per profile; SQLite protects simultaneous worker transactions.
    Each method opens a short-lived connection, so the store may move with profile.
    """

    def __init__(self, profile_root):
        self.root = _no_links(profile_root, allow_missing=True).resolve() / "lyrics"
        _no_links(self.root, allow_missing=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "lyrics.sqlite3"
        self.lock = threading.RLock()
        self._provider = None
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL,
                    original_rel TEXT NOT NULL, normalized_rel TEXT NOT NULL,
                    format TEXT NOT NULL, kind TEXT NOT NULL, language TEXT NOT NULL,
                    encoding TEXT NOT NULL, provenance TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS associations (
                    track_id TEXT NOT NULL, source_id TEXT NOT NULL, explicit INTEGER NOT NULL,
                    priority INTEGER NOT NULL, candidate_id TEXT,
                    PRIMARY KEY(track_id, source_id));
                CREATE TABLE IF NOT EXISTS selections (track_id TEXT PRIMARY KEY, source_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candidates (
                    track_id TEXT NOT NULL, candidate_id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(track_id, candidate_id));
            """)

    @contextmanager
    def _db(self):
        _no_links(self.db_path, allow_missing=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _path(self, relative):
        result = _no_links(self.root / relative, allow_missing=True).resolve()
        if not result.is_relative_to(self.root) or result == self.root:
            raise ValueError("managed lyrics path escaped profile")
        return result

    def _write(self, relative, raw):
        path = self._path(relative)
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if path.stat().st_size != len(raw) or path.read_bytes() != raw:
                    raise ValueError("managed lyrics content hash collision or corruption")
                return
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(raw)
            temporary.replace(path)

    def _add(self, raw, format, *, origin, language="und", provenance=None, encoding=None):
        parsed = parse_lyrics(raw, format, encoding=encoding)
        if not parsed["normalized_text"].strip():
            raise ValueError("empty lyrics source")
        if language == "und" and _LANG.fullmatch(parsed["metadata"].get("lang", "")):
            language = parsed["metadata"]["lang"]
        render_format = "lrc" if parsed["cues"] else "txt"
        render_encoding = "utf-8-sig" if parsed["cues"] else "utf-8"
        provenance = {"origin": origin, **(provenance or {}), "normalization": {
            "recipe": NORMALIZATION_RECIPE, "version": NORMALIZATION_VERSION,
            "format": render_format, "encoding": render_encoding}}
        digest = _hash(raw)
        identity = {"sha256": digest, "format": format, "language": language,
                    "provenance": {k: v for k, v in provenance.items() if k != "retrieved_at"}}
        source_id = _hash(_json(identity).encode("utf-8"))
        original = f"originals/{digest}.{format}"
        normalized_raw = parsed["normalized_text"].encode(render_encoding)
        if len(normalized_raw) > MAX_SOURCE_BYTES:
            raise ValueError("prepared lyrics working copy exceeds 1 MiB")
        normalized = f"rendered/{_hash(normalized_raw)}.{render_format}"
        with self.lock:
            self._write(original, raw)
            self._write(normalized, normalized_raw)
            with self._db() as db:
                db.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    source_id, digest, original, normalized, format, parsed["kind"], language,
                    parsed["encoding"], _json(provenance), _now()))
        return self._source(source_id)

    def _source(self, source_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if not row:
            raise ValueError("unknown lyrics source")
        value = dict(row)
        value["provenance"] = json.loads(value["provenance"])
        return value

    def _associate(self, track_id, source_id, *, explicit=False, priority=30, candidate_id=None):
        with self._db() as db:
            db.execute("""INSERT INTO associations VALUES (?,?,?,?,?)
                       ON CONFLICT(track_id,source_id) DO UPDATE SET
                       explicit=MAX(explicit,excluded.explicit),priority=MIN(priority,excluded.priority),
                       candidate_id=COALESCE(excluded.candidate_id,candidate_id)""",
                       (track_id, source_id, int(explicit), priority, candidate_id))

    def list(self, track_id=None):
        with self._db() as db:
            sql = """SELECT s.*,a.track_id,a.explicit,a.priority,a.candidate_id FROM sources s
                     JOIN associations a ON s.source_id=a.source_id"""
            rows = db.execute(sql + (" WHERE a.track_id=?" if track_id is not None else "") +
                              " ORDER BY a.explicit DESC,a.priority,s.kind DESC,s.source_id",
                              (_track_id(track_id),) if track_id is not None else ()).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["explicit"] = bool(value["explicit"])
            value["provenance"] = json.loads(value["provenance"])
            values.append(value)
        return values

    def _preferred(self, track_id):
        records = self.list(track_id)
        with self._db() as db:
            row = db.execute("SELECT source_id FROM selections WHERE track_id=?", (track_id,)).fetchone()
        if row:
            selected = next((r for r in records if r["source_id"] == row[0] and r["explicit"]), None)
            if selected:
                return selected
        return records[0] if records else None

    def list_candidates(self, track_id):
        with self._db() as db:
            rows = db.execute("SELECT data FROM candidates WHERE track_id=? ORDER BY candidate_id",
                              (_track_id(track_id),)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def state_fingerprint(self):
        """Cheap export revision including explicit preferred-version changes.

        Read one SQLite snapshot. Unassociated candidate caches and lyric text
        reads cannot affect the fingerprint of the exported association state.
        """
        with self._db() as db:
            db.execute("BEGIN")
            associations = [tuple(row) for row in db.execute(
                "SELECT track_id,source_id,explicit,priority,candidate_id FROM associations "
                "ORDER BY track_id,source_id")]
            selections = [tuple(row) for row in db.execute(
                "SELECT track_id,source_id FROM selections ORDER BY track_id")]
            sources = [tuple(row) for row in db.execute(
                "SELECT source_id,content_sha256,original_rel,normalized_rel,format,kind,language,encoding,provenance "
                "FROM sources WHERE source_id IN (SELECT source_id FROM associations) ORDER BY source_id")]
        return _hash(_json({"associations": associations, "selections": selections,
                            "sources": sources}).encode("utf-8"))

    def get(self, track_id, source_id=None, *, candidate_id=None):
        track_id = _track_id(track_id)
        records = self.list(track_id)
        # A candidate can be previewed for its own track before association.
        candidate_sources = []
        for candidate in self.list_candidates(track_id):
            if candidate_id is None or candidate["candidate_id"] == candidate_id:
                candidate_sources.extend(candidate["source_ids"])
                if candidate_id is not None and source_id is None:
                    source_id = candidate["preferred_source_id"]
        selected = next((r for r in records if r["source_id"] == source_id), None) if source_id else self._preferred(track_id)
        if selected is None and source_id in candidate_sources:
            selected = self._source(source_id)
        if selected is None:
            return {"track_id": track_id, "found": False, "source": None, "versions": records}
        raw = _bounded_file(self._path(selected["original_rel"]))
        if _hash(raw) != selected["content_sha256"]:
            raise ValueError("stored lyrics original checksum mismatch")
        parsed = parse_lyrics(raw, selected["format"], encoding=selected["encoding"] if selected["encoding"] != "unknown" else None)
        return {"track_id": track_id, "found": True, "source": selected,
                "versions": records, **parsed}

    def attach(self, track_id, candidate_id, *, source_id=None, explicit=True):
        track_id = _track_id(track_id)
        with self._db() as db:
            row = db.execute("SELECT data FROM candidates WHERE track_id=? AND candidate_id=?",
                             (track_id, candidate_id)).fetchone()
        if row:
            candidate = json.loads(row[0])
            sources = candidate["source_ids"]
            chosen = source_id or candidate["preferred_source_id"]
            if not chosen or chosen not in sources:
                raise ValueError("candidate has no selectable lyrics")
        else:
            chosen = source_id or candidate_id
            self._source(chosen)
            sources = [chosen]
        for value in sources:
            self._associate(track_id, value, explicit=explicit, priority=0 if explicit else 30,
                            candidate_id=candidate_id)
        if explicit:
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO selections VALUES (?,?)", (track_id, chosen))
        return self.get(track_id)

    def import_file(self, track_id, path, *, language="und", encoding=None):
        """Explicit user file selection. No mutation of the selected source."""
        path = Path(path)
        format = path.suffix.lower().lstrip(".")
        if format not in ("lrc", "srt", "txt"):
            raise ValueError("choose an .lrc, .srt or .txt file")
        source = self._add(_bounded_file(path), format, origin="user", language=language,
                           provenance={"filename": path.name}, encoding=encoding)
        return self.attach(track_id, source["source_id"])

    def remove_association(self, track_id, source_id=None):
        track_id = _track_id(track_id)
        with self._db() as db:
            sql = "DELETE FROM associations WHERE track_id=?"
            args = (track_id,)
            if source_id is not None:
                sql += " AND source_id=?"
                args += (source_id,)
            count = db.execute(sql, args).rowcount
            db.execute("DELETE FROM selections WHERE track_id=?" + (" AND source_id=?" if source_id else ""), args)
        return {"track_id": track_id, "removed": count}

    def inspect_track(self, track, *, embedded_reader=None, cancel=None):
        """Local-only discovery. Exact file stem, optional language suffix, no title lookup."""
        track_id = _track_id(track)
        audio_path = Path(track["path"])
        diagnostics, discovered = [], []
        if _cancelled(cancel):
            return {"track_id": track_id, "status": "cancelled", "selected": self._preferred(track_id),
                    "sources": self.list(track_id), "diagnostics": []}
        try:
            audio_path = _no_links(audio_path)
            if not stat.S_ISREG(audio_path.stat().st_mode):
                raise ValueError("track path is not a regular file")
        except FileNotFoundError:
            return {"track_id": track_id, "selected": self._preferred(track_id), "sources": self.list(track_id),
                    "diagnostics": [{"code": "audio_missing", "message": "track path is unavailable"}]}
        except (OSError, ValueError) as exc:
            return {"track_id": track_id, "selected": self._preferred(track_id), "sources": self.list(track_id),
                    "diagnostics": [{"code": "audio_unreadable", "reason": getattr(exc, "code", type(exc).__name__),
                                     "message": str(exc)}]}
        try:
            _no_links(audio_path.parent)
            siblings = sorted(audio_path.parent.iterdir(), key=lambda p: p.name.casefold())
        except (OSError, ValueError) as exc:
            siblings = []
            diagnostics.append({"code": "sidecar_directory_unreadable", "reason": getattr(exc, "code", type(exc).__name__),
                                "message": str(exc)})
        for path in siblings:
            if _cancelled(cancel):
                return {"track_id": track_id, "status": "cancelled", "selected": self._preferred(track_id),
                        "sources": self.list(track_id), "diagnostics": diagnostics}
            suffix = path.suffix.lower().lstrip(".")
            if suffix not in ("lrc", "srt", "txt"):
                continue
            language = "und"
            if path.stem.casefold() != audio_path.stem.casefold():
                prefix = audio_path.stem + "."
                if not path.stem.casefold().startswith(prefix.casefold()):
                    continue
                language = path.stem[len(prefix):]
                if not _LANG.fullmatch(language):
                    continue
            try:
                source = self._add(_bounded_file(path), suffix, origin="sidecar", language=language,
                                   provenance={"filename": path.name})
                discovered.append(source["source_id"])
                self._associate(track_id, source["source_id"], priority=10)
            except (OSError, ValueError, UnicodeError) as exc:
                diagnostics.append({"code": "sidecar_unreadable", "filename": path.name,
                                    "reason": getattr(exc, "code", type(exc).__name__), "message": str(exc)})
        try:
            _no_links(audio_path)
            for raw, format, language, provenance in (embedded_reader or _embedded)(audio_path):
                if _cancelled(cancel):
                    return {"track_id": track_id, "status": "cancelled", "selected": self._preferred(track_id),
                            "sources": self.list(track_id), "diagnostics": diagnostics}
                source = self._add(raw, format, origin="embedded", language=language, provenance=provenance)
                discovered.append(source["source_id"])
                self._associate(track_id, source["source_id"], priority=20)
        except Exception as exc:
            # Broken tags must not make an intact sidecar unusable.
            diagnostics.append({"code": "embedded_unreadable", "reason": getattr(exc, "code", type(exc).__name__),
                                "message": str(exc)[:512] or type(exc).__name__})
        # Remove obsolete discovery associations; explicit selections survive source removal.
        with self._db() as db:
            rows = db.execute("SELECT source_id FROM associations WHERE track_id=? AND explicit=0 AND priority IN (10,20)",
                              (track_id,)).fetchall()
            for row in rows:
                if row[0] not in discovered:
                    db.execute("DELETE FROM associations WHERE track_id=? AND source_id=?", (track_id, row[0]))
        return {"track_id": track_id, "selected": self._preferred(track_id),
                "sources": self.list(track_id), "diagnostics": diagnostics}

    def search(self, track, *, provider=None, cancel=None):
        """Explicit network action; candidates are stored but never implicitly attached."""
        track_id = _track_id(track)
        if provider is None:
            if self._provider is None:
                self._provider = LRCLIBProvider(cache_path=self.root / "provider-cache.json")
            provider = self._provider
        result = provider.search(track, cancel=cancel)
        if _cancelled(cancel) or result["status"] == "cancelled":
            return {"track_id": track_id, "status": "cancelled", "candidates": []}
        candidates = []
        for value in result.get("candidates", []):
            if _cancelled(cancel):
                return {"track_id": track_id, "status": "cancelled", "candidates": []}
            candidate = {k: v for k, v in value.items() if k not in ("plain_text", "synced_text")}
            sources = []
            preferred = None
            diagnostics = []
            for field, format in (("plain_text", "txt"), ("synced_text", "lrc")):
                if not value.get(field):
                    continue
                try:
                    source = self._add(value[field].encode("utf-8"), format, origin="provider", provenance={
                        "provider": value["provider"], "provider_id": value["id"],
                        "retrieved_at": _now(), "url": f"https://lrclib.net/api/get/{value['id']}",
                        "storage_form": "provider-json-field-utf8", "metadata": {
                            key: value.get(key) for key in ("title", "artist", "album", "duration")}})
                except (ValueError, UnicodeError) as exc:
                    diagnostics.append({"code": "invalid_lyrics_variant", "format": format, "message": str(exc)})
                    continue
                sources.append(source["source_id"])
                preferred = source["source_id"]
            candidate.update(source_ids=sources, preferred_source_id=preferred, diagnostics=diagnostics)
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO candidates VALUES (?,?,?)",
                           (track_id, candidate["candidate_id"], _json(candidate)))
            candidates.append(candidate)
        safe_auto = result.get("auto_candidate_id")
        if not any(c["candidate_id"] == safe_auto and c["source_ids"] for c in candidates):
            safe_auto = None
        return {**{k: v for k, v in result.items() if k != "candidates"}, "auto_candidate_id": safe_auto,
                "track_id": track_id, "candidates": candidates}

    def fetch_missing(self, track, *, provider=None, enabled=False, cancel=None):
        """Opt-in automation; strongest local source and explicit choices win."""
        inspected = self.inspect_track(track, cancel=cancel)
        if inspected.get("status") == "cancelled":
            return {"track_id": _track_id(track), "status": "cancelled", "attached": False}
        if inspected["selected"]:
            return {"track_id": _track_id(track), "status": "local_available", "attached": False}
        if not enabled:
            return {"track_id": _track_id(track), "status": "disabled", "attached": False}
        result = self.search(track, provider=provider, cancel=cancel)
        candidate_id = result.get("auto_candidate_id")
        if candidate_id and not _cancelled(cancel):
            self.attach(_track_id(track), candidate_id, explicit=False)
            result["attached"] = True
        else:
            result["attached"] = False
        return result

    def plan_export_entries(self, track_ids=None):
        """Internal copy requests. Absolute `source` MUST be removed from manifests."""
        selected_ids = {_track_id(x) for x in track_ids} if track_ids is not None else None
        records = [r for r in self.list() if selected_ids is None or r["track_id"] in selected_ids]
        entries, mappings = [], []
        for record in records:
            original = "Struct/lyrics/" + record["original_rel"]
            rendered = "Struct/lyrics/" + record["normalized_rel"]
            for relative, kind in ((record["original_rel"], "lyrics-original"),
                                   (record["normalized_rel"], "lyrics-rendered")):
                source = self._path(relative)
                raw = _bounded_file(source)
                if kind == "lyrics-original" and _hash(raw) != record["content_sha256"]:
                    raise ValueError("stored lyrics original checksum mismatch")
                if kind == "lyrics-rendered" and source.suffix == ".lrc" and not raw.startswith(codecs.BOM_UTF8):
                    raise ValueError("legacy prepared LRC lacks UTF-8 BOM; re-import and select the current rendering")
                entries.append({"source": str(source), "relative": "Struct/lyrics/" + relative,
                                "track_id": record["track_id"], "original_path": original,
                                "kind": kind, "provenance": record["provenance"],
                                "sha256": _hash(raw), "size": len(raw)})
            preferred = self._preferred(record["track_id"])
            mappings.append({"track_id": record["track_id"], "source_id": record["source_id"],
                             "sha256": record["content_sha256"], "original_path": original,
                             "normalized_path": rendered, "kind": record["kind"],
                             "language": record["language"], "encoding": record["encoding"],
                             "explicit": record["explicit"], "selected": bool(preferred and
                             preferred["source_id"] == record["source_id"]), "provenance": record["provenance"]})
        if not mappings:
            return []
        payload = (_json({"schema": "xm5.lyrics-associations", "version": 1, "associations": mappings}) + "\n").encode("utf-8")
        relative = f"export-maps/{_hash(payload)}.json"
        self._write(relative, payload)
        entries.append({"source": str(self._path(relative)), "relative": "Struct/lyrics/associations.json",
                        "track_id": "", "original_path": "Struct/lyrics/associations.json",
                        "kind": "lyrics-map", "provenance": {"origin": "managed"},
                        "sha256": _hash(payload), "size": len(payload)})
        return entries
