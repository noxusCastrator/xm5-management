"""Ordered action groups, lossless .mta import, and non-writing operation plans."""
from __future__ import annotations
from dataclasses import dataclass, field
import copy
import hashlib
import json
import ntpath
import re
from typing import Any, Mapping
from .formatter import ScriptError, evaluate, normalize_fields, regex_replace, parse, validate
from .converters import match_values, safe_relative_path


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    section: str = ""
    blocking: bool = True


@dataclass(frozen=True)
class SourceDocument:
    raw_bytes: bytes
    name: str
    encoding: str
    sha256: str


def decode_document(raw: bytes, name: str, encoding: str | None = None) -> tuple[SourceDocument, str]:
    """BOM-aware; an undecodable legacy file needs an explicit encoding."""
    if len(raw) > 4_000_000: raise ScriptError("LIMIT", "Source document exceeds 4 MB")
    chosen = encoding or ("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    try: text = raw.decode(chosen)
    except (UnicodeError, LookupError) as exc: raise ScriptError("ENCODING", "Cannot decode source; supply its explicit legacy encoding") from exc
    return SourceDocument(bytes(raw), name, chosen, hashlib.sha256(raw).hexdigest()), text


@dataclass(frozen=True)
class Action:
    kind: str
    options: Mapping[str, Any] = field(default_factory=dict)
    section: str = ""
    raw_options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ActionGroup:
    name: str
    actions: tuple[Action, ...]
    source: SourceDocument | None = None
    diagnostics: tuple[Diagnostic, ...] = ()


# IDs have author-published evidence; no guesses for newer/undocumented IDs.
MTA_IDS = {1: "case", 2: "replace", 4: "regex", 5: "format", 6: "remove_duplicates",
           7: "guess", 8: "merge", 9: "remove", 10: "remove_except", 11: "codepage",
           12: "import_cover", 13: "export_cover", 14: "import_text", 15: "export", 16: "split"}
SUPPORTED_ACTIONS = {"case", "replace", "regex", "format", "remove_duplicates", "guess", "merge", "remove", "remove_except", "split", "codepage"}


def _unicode_escape(text: str) -> str:
    # Mp3tag .mta stores non-ASCII as \uXXXX; never apply Python's general
    # unicode_escape codec to regexes or paths (it would corrupt \n, \t, etc.).
    result = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1],16)), text)
    try: return result.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError as exc: raise ScriptError("ENCODING", "Unpaired UTF-16 escape in .mta") from exc


def import_mta(raw: bytes, name: str = "imported.mta", encoding: str | None = None) -> ActionGroup:
    try: source, text = decode_document(raw, name, encoding)
    except ScriptError as exc:
        source = SourceDocument(bytes(raw), name, encoding or "undecoded", hashlib.sha256(raw).hexdigest())
        return ActionGroup(ntpath.splitext(ntpath.basename(name))[0], (), source, (Diagnostic(exc.code, str(exc)),))
    try: return _parse_mta(source, text, name)
    except ScriptError as exc:
        return ActionGroup(ntpath.splitext(ntpath.basename(name))[0], (), source, (Diagnostic(exc.code, str(exc)),))


def _parse_mta(source: SourceDocument, text: str, name: str) -> ActionGroup:
    sections, current, diagnostics = [], None, []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith((";", "!")): continue
        section_match = re.fullmatch(r"\s*\[([^\]]+)\]\s*", line)
        if section_match:
            current = [section_match[1], []]; sections.append(current); continue
        if current is None or "=" not in line:
            diagnostics.append(Diagnostic("MTA_SYNTAX", f"Unrecognized content at line {line_number}")); continue
        key, value = line.split("=", 1)
        current[1].append((key.strip(), value))
    actions = []
    numbers = set()
    for section, pairs in sections:
        raw_pairs = tuple(pairs)
        if not re.fullmatch(r"#\d+", section):
            diagnostics.append(Diagnostic("MTA_SECTION", "Unknown section retained", section))
            actions.append(Action("unsupported", {}, section, raw_pairs)); continue
        number = int(section[1:])
        if number in numbers: diagnostics.append(Diagnostic("MTA_DUPLICATE_SECTION", "Duplicate action number", section))
        numbers.add(number)
        if len({k for k,v in pairs}) != len(pairs): diagnostics.append(Diagnostic("MTA_DUPLICATE_KEY", "Duplicate option keys", section))
        options = dict(pairs)
        try: identifier = int(options.get("T", ""))
        except ValueError: identifier = -1
        kind = MTA_IDS.get(identifier, "unsupported")
        allowed = {"T", "F"}
        interpreted = {"field": _unicode_escape(options.get("F", ""))}
        if kind == "format":
            allowed |= {"1"}; interpreted["value"] = _unicode_escape(options.get("1", ""))
        elif kind == "guess":
            allowed |= {"1"}; interpreted = {"source": _unicode_escape(options.get("F", "")), "pattern": _unicode_escape(options.get("1", ""))}
        elif kind in {"replace", "regex"}:
            allowed |= {"1", "2", "3"}
            interpreted |= {"search": _unicode_escape(options.get("1", "")), "replacement": _unicode_escape(options.get("2", ""))}
            flag = options.get("3", "0")
            if kind == "replace" and flag in {"0", "0|0", "0|1", "1|0", "1|1"}:
                values = flag.split("|"); interpreted |= {"whole_word": values[0] == "1", "case_sensitive": len(values) > 1 and values[1] == "1"}
            elif kind == "regex" and flag in {"0", "1"}: interpreted["case_sensitive"] = flag == "1"
            else: diagnostics.append(Diagnostic("MTA_OPTION", "Unproven replacement flag", section))
        elif kind in {"remove", "remove_except"}:
            interpreted = {"fields": [p.strip() for p in _unicode_escape(options.get("F", "")).split(";") if p.strip()]}
        elif kind in {"merge", "split"}:
            allowed |= {"1"}; interpreted["separator"] = _unicode_escape(options.get("1", ""))
            if "1" not in options: diagnostics.append(Diagnostic("MTA_REQUIRED", "Missing separator option 1", section))
        elif kind == "remove_duplicates": pass
        elif kind == "case":
            diagnostics.append(Diagnostic("MTA_SCHEMA_UNVERIFIED", "Numeric case modes need a verified source fixture", section))
        else: diagnostics.append(Diagnostic("MTA_UNSUPPORTED", f"Action T={identifier} retained but cannot execute", section))
        unknown = set(options) - allowed
        if unknown: diagnostics.append(Diagnostic("MTA_OPTION", f"Unknown/unverified keys retained: {sorted(unknown)}", section))
        if "T" not in options or "F" not in options:
            diagnostics.append(Diagnostic("MTA_REQUIRED", "Missing T or F option", section))
        if kind in {"format", "guess", "replace", "regex"} and "1" not in options:
            diagnostics.append(Diagnostic("MTA_REQUIRED", "Missing option 1", section))
        if kind in {"replace", "regex"} and "2" not in options:
            diagnostics.append(Diagnostic("MTA_REQUIRED", "Missing replacement option 2", section))
        actions.append(Action(kind, interpreted, section, raw_pairs))
    if not actions: diagnostics.append(Diagnostic("MTA_EMPTY", "No action steps found"))
    # Physical file order is the execution order. Gaps are fine; out-of-order
    # section numbers are retained and diagnosed instead of silently sorted.
    numeric = [int(a.section[1:]) for a in actions if re.fullmatch(r"#\d+", a.section)]
    if numeric != sorted(numeric): diagnostics.append(Diagnostic("MTA_ORDER", "Action sections are not in ascending order; resolve order explicitly"))
    return ActionGroup(ntpath.splitext(ntpath.basename(name))[0], tuple(actions), source, tuple(diagnostics))


@dataclass(frozen=True)
class CapturedTrack:
    track_id: str
    path: str
    fields: Mapping
    revision: str
    source_fingerprint: str
    technical: Mapping = field(default_factory=dict)
    folder_id: str = ""
    occurrence_id: str = ""

    def fingerprint(self) -> str:
        value = {"id": self.track_id, "path": self.path, "fields": normalize_fields(self.fields),
                 "technical": normalize_fields(self.technical), "revision": self.revision,
                 "source_fingerprint": self.source_fingerprint, "folder_id": self.folder_id,
                 "occurrence_id": self.occurrence_id}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Operation:
    kind: str
    track_id: str
    path: str
    expected_revision: str
    source_fingerprint: str
    captured_fingerprint: str
    values: Mapping


@dataclass(frozen=True)
class ActionPlan:
    group_name: str
    operations: tuple[Operation, ...]
    inputs: tuple[tuple[str, str], ...]
    source_sha256: str | None

    def assert_current(self, tracks) -> None:
        current = tuple((t.track_id, t.fingerprint()) for t in tracks)
        if current != self.inputs: raise ScriptError("STALE_PLAN", "Captured tracks, order or versions changed")


def _field_name(name: str) -> str:
    name = str(name).upper()
    if not name or name.startswith("_") or name in {"COVER", "PICTURE"}:
        raise ScriptError("READONLY_FIELD", f"Tag-only action cannot write {name}")
    if any(c in name for c in "\x00\r\n=%"): raise ScriptError("FIELD_NAME", "Invalid target field")
    return name


def _targets(options, fields) -> list[str]:
    names = str(options.get("field", "")).upper()
    if names == "_ALL":
        raise ScriptError("ACTION_UNSUPPORTED", "_ALL includes filenames in Mp3tag; use explicit _TAG and a separate planned filename converter")
    if names == "_TAG": return [_field_name(name) for name in fields]
    return [_field_name(name.strip()) for name in names.split(";") if name.strip()]


def plan_actions(group: ActionGroup, tracks, environment=None, captured=None) -> ActionPlan:
    """Simulate each ordered action in memory, then emit net tag/path changes.

    Caller must apply via shared file-tag service with backups, tag-format
    validation, exact source recheck, revisions, index/path refresh and rollback.
    Every unsupported step blocks the whole group before any plan is returned.
    """
    tracks = tuple(copy.deepcopy(tuple(tracks)))
    if group.diagnostics and any(d.blocking for d in group.diagnostics):
        raise ScriptError("ACTION_BLOCKED", "; ".join(d.message for d in group.diagnostics if d.blocking))
    if len({t.track_id for t in tracks}) != len(tracks): raise ScriptError("DUPLICATE_TARGET", "A tag-write plan requires unique track IDs")
    schemas = {"format": {"field", "value"}, "guess": {"source", "pattern"}, "replace": {"field", "search", "replacement", "whole_word", "case_sensitive"},
               "regex": {"field", "search", "replacement", "case_sensitive"}, "case": {"field", "mode", "triggers"}, "remove": {"fields"},
               "remove_except": {"fields"}, "remove_duplicates": {"field", "only_same_content"}, "merge": {"field", "separator"}, "split": {"field", "separator"}, "codepage": {"field", "source_encoding", "target_encoding"}}
    for action in group.actions:
        if action.kind not in SUPPORTED_ACTIONS: raise ScriptError("ACTION_UNSUPPORTED", f"{action.kind} cannot execute")
        if set(action.options) - schemas[action.kind]: raise ScriptError("ACTION_OPTION", f"Unknown options in {action.kind}")
        if action.kind == "format": validate(parse(str(action.options.get("value", ""))))
        if action.kind == "guess": validate(parse(str(action.options.get("source", ""))))
    renamed = False
    path_fields = {"_PATH", "_FILEPATH", "_FILENAME", "_FILENAME_EXT", "_FOLDERPATH", "_DIRECTORY", "_EXTENSION"}
    def path_reference(nodes):
        for node in nodes:
            if node.kind == "field" and node.value in path_fields: return True
            if node.kind == "optional" and path_reference(node.children): return True
            if node.kind == "call" and any(path_reference(arg) for arg in node.children): return True
        return False
    for action in group.actions:
        expression = action.options.get("value" if action.kind == "format" else "source") if action.kind in {"format", "guess"} else None
        if renamed and expression is not None and path_reference(parse(str(expression))):
            raise ScriptError("ACTION_UNSUPPORTED", "A technical-path expression after rename requires a new caller capture")
        if action.kind == "format" and str(action.options.get("field", "")).upper() == "_FILENAME": renamed = True
    operations = []
    context_counters = {}
    for track in tracks:
        if not track.track_id or not track.revision or not track.source_fingerprint: raise ScriptError("PRECONDITION", "Each track needs a stable ID, revision and exact source fingerprint")
        original = normalize_fields(track.fields); fields = dict(original); destination = None
        for action in group.actions:
            options, kind = action.options, action.kind
            if kind == "guess":
                values = match_values(evaluate(str(options.get("source", "")), fields, track.technical, environment=environment, captured=captured, counters=context_counters), str(options.get("pattern", "")), allow_path_separators=True)
                for key, value in values.items(): fields[_field_name(key)] = (value,) if value else ()
                continue
            if kind == "format" and str(options.get("field", "")).upper() == "_FILENAME":
                destination = safe_relative_path(evaluate(str(options.get("value", "")), fields, track.technical, environment=environment, captured=captured, counters=context_counters) + ntpath.splitext(track.path)[1])
                continue
            if kind in {"remove", "remove_except"}:
                names = {_field_name(n) for n in options.get("fields", ())}
                for key in list(fields):
                    if (key in names) == (kind == "remove"): fields.pop(key)
                continue
            targets = _targets(options, fields)
            if not targets and str(options.get("field", "")).upper() != "_TAG": raise ScriptError("FIELD_NAME", "Action needs a target field")
            for key in targets:
                values = fields.get(key, ())
                if kind == "format": values = (evaluate(str(options.get("value", "")), fields, track.technical, environment=environment, captured=captured, counters=context_counters),)
                elif kind == "replace":
                    search, replacement = str(options.get("search", "")), str(options.get("replacement", ""))
                    if not search: raise ScriptError("EMPTY_SEARCH", "Replace search cannot be empty")
                    pattern = re.escape(search)
                    # Mp3tag's documented separators are whitespace and
                    # .,;:!?; Python \b would also split hyphens/apostrophes.
                    if options.get("whole_word", False): pattern = r"(?<![^\s.,;:!?])" + pattern + r"(?![^\s.,;:!?])"
                    flags = 0 if options.get("case_sensitive", False) else re.IGNORECASE
                    values = tuple(re.sub(pattern, lambda m: replacement, v, flags=flags) for v in values)
                elif kind == "regex": values = tuple(regex_replace(v, str(options.get("search", "")), str(options.get("replacement", "")), not options.get("case_sensitive", False)) for v in values)
                elif kind == "case":
                    mode = options.get("mode", "")
                    function = {"upper": "upper", "lower": "lower", "mixed": "caps", "mixed_preserve": "caps2", "sentence": "caps3"}.get(mode)
                    if function is None: raise ScriptError("CASE_MODE", "Choose upper, lower, mixed, mixed_preserve or sentence")
                    values = tuple(evaluate(f"${function}(%VALUE%)", {"VALUE": v}) if "triggers" not in options else evaluate(f"${function}(%VALUE%,%TRIGGERS%)", {"VALUE": v, "TRIGGERS": options["triggers"]}) for v in values)
                elif kind == "codepage":
                    try:
                        values = tuple(value.encode(str(options["source_encoding"])).decode(str(options["target_encoding"])) for value in values)
                    except (LookupError, UnicodeError) as exc:
                        raise ScriptError("CODEPAGE", "Exact codepage repair cannot encode/decode the captured value") from exc
                elif kind == "remove_duplicates": values = tuple(dict.fromkeys(values)) if options.get("only_same_content",False) else values[:1]
                elif kind in {"merge", "split"}:
                    separator = str(options.get("separator", ""))
                    if not separator: raise ScriptError("EMPTY_SEPARATOR", "Split/merge requires a separator")
                    values = (separator.join(values),) if kind == "merge" else tuple(piece for value in values for piece in value.split(separator))
                fields[key] = tuple(v for v in values if v != "")
                if not fields[key]: fields.pop(key)
        changes = {key: list(fields[key]) for key in fields if fields[key] != original.get(key)}
        removed = sorted(set(original) - set(fields))
        preconditions = (track.track_id, track.path, track.revision, track.source_fingerprint, track.fingerprint())
        if changes or removed: operations.append(Operation("set_tags", *preconditions, {"set": changes, "remove": removed}))
        if destination is not None: operations.append(Operation("rename_file", *preconditions, {"relative_destination": destination}))
    destinations = [str(o.values["relative_destination"]).casefold() for o in operations if o.kind == "rename_file"]
    if len(set(destinations)) != len(destinations): raise ScriptError("PATH_COLLISION", "Several planned renames have the same destination")
    return ActionPlan(group.name, tuple(operations), tuple((t.track_id, t.fingerprint()) for t in tracks), group.source.sha256 if group.source else None)
