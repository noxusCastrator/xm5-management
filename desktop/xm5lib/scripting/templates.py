"""Lossless .mte import and deterministic, non-writing text export plans."""
from __future__ import annotations
from dataclasses import dataclass
from itertools import groupby
import hashlib
import ntpath
from .formatter import Context, Node, ScriptError, integer, parse, validate
from .actions import CapturedTrack, SourceDocument, Diagnostic, decode_document
from .converters import safe_relative_path


@dataclass(frozen=True)
class Loop:
    key: tuple[Node, ...]
    limit: tuple[Node, ...] | None
    body: tuple


@dataclass(frozen=True)
class ExportTemplate:
    source: SourceDocument
    nodes: tuple
    filename: Node | None = None
    diagnostics: tuple[Diagnostic, ...] = ()


def import_mte(raw: bytes, name: str = "imported.mte", encoding: str | None = None) -> ExportTemplate:
    try: source, text = decode_document(raw, name, encoding)
    except ScriptError as exc:
        source = SourceDocument(bytes(raw), name, encoding or "undecoded", hashlib.sha256(raw).hexdigest())
        return ExportTemplate(source, (), None, (Diagnostic(exc.code, str(exc)),))
    try: return _parse_template(source, text)
    except ScriptError as exc:
        # Keep the original template and its provenance even when execution
        # cannot be admitted. The UI can show and save unsupported syntax.
        return ExportTemplate(source, (), None, (Diagnostic(exc.code, str(exc)),))


def _parse_template(source: SourceDocument, text: str) -> ExportTemplate:
    nodes = parse(text); validate(nodes, export=True)
    filename = None
    remaining = []
    seen_content = False
    for node in nodes:
        if node.kind == "call" and node.value == "filename":
            if filename is not None or seen_content: raise ScriptError("EXPORT_FILENAME", "$filename must occur once at the beginning")
            filename = node
        else:
            remaining.append(node)
            if node.kind != "literal" or node.value.strip(): seen_content = True
    def build(index=0, nested=False):
        result = []
        while index < len(remaining):
            node = remaining[index]; index += 1
            if node.kind == "call" and node.value == "loopend":
                if not nested: raise ScriptError("EXPORT_LOOP", "Unexpected $loopend", node.position)
                return tuple(result), index
            if node.kind == "call" and node.value == "loop":
                body, index = build(index, True)
                result.append(Loop(node.children[0], node.children[1] if len(node.children) > 1 else None, body))
            else:
                def reject_embedded(n):
                    if n.kind == "call" and n.value in {"loop", "loopend", "filename"}:
                        raise ScriptError("EXPORT_CONTEXT", "Export structural directives must be standalone, not inside a function/optional section", n.position)
                    for child in n.children:
                        if isinstance(child, Node): reject_embedded(child)
                        else:
                            for item in child: reject_embedded(item)
                reject_embedded(node); result.append(node)
        if nested: raise ScriptError("EXPORT_LOOP", "Missing $loopend")
        return tuple(result), index
    body, _ = build()
    # filename arguments may contain normal functions, never nested structure.
    if filename:
        for arg in filename.children: validate(arg)
    return ExportTemplate(source, body, filename)


@dataclass(frozen=True)
class ExportFile:
    relative_path: str
    data: bytes
    encoding: str
    track_ids: tuple[str, ...]
    partition: str = ""


@dataclass(frozen=True)
class ExportPlan:
    files: tuple[ExportFile, ...]
    source_sha256: str
    inputs: tuple[tuple[str, str], ...]

    def assert_current(self, tracks):
        if tuple((t.track_id, t.fingerprint()) for t in tracks) != self.inputs:
            raise ScriptError("STALE_PLAN", "Export selection or captured values changed")


def plan_export(template: ExportTemplate, tracks, default_name="export.txt", *, partition=None,
                environment=None, bom=False, ansi_encoding=None, captured=None) -> ExportPlan:
    """Render an explicit captured sequence; paths/data are proposed writes only.

    Loops group equal sort keys, use stable case-insensitive lexical sorting,
    and preserve captured order for equal keys. Nested loops refine each group.
    A leaf loop emits each occurrence; limit caps occurrences per equal key.
    Outer text around nested loops is emitted once per key group. Variables in
    loops are separate from variables outside, shared by loop siblings.
    """
    if any(d.blocking for d in template.diagnostics):
        raise ScriptError("EXPORT_BLOCKED", "; ".join(d.message for d in template.diagnostics if d.blocking))
    tracks = tuple(tracks)
    if len(tracks) > 100_000: raise ScriptError("LIMIT", "Export exceeds 100,000 captured tracks")
    inputs = tuple((t.track_id, t.fingerprint()) for t in tracks)
    if partition is not None: validate(parse(partition))
    partitions = {}
    for track in tracks:
        key = Context(track.fields, track.technical, environment=environment, captured=captured).run(parse(partition)).text if partition is not None else ""
        partitions.setdefault(key, []).append(track)
    if not partitions and partition is None: partitions[""] = []
    files = []
    for partition_key, subset in partitions.items():
        subset = tuple(subset)
        base = subset[0] if subset else CapturedTrack("", "", {}, "", "")
        outside_variables, loop_variables = {}, {}
        context_counters = {}
        output_count = [0]
        def context(track, inside): return Context(track.fields, track.technical, loop_variables if inside else outside_variables, environment, captured, context_counters)
        def render(nodes, scope, current, inside=False):
            pieces = []
            for node in nodes:
                if isinstance(node, Loop):
                    pairs = [(context(t, True).run(node.key).text, i, t) for i,t in enumerate(scope)]
                    pairs.sort(key=lambda x: x[0].casefold())
                    for _, grouped in groupby(pairs, key=lambda x: x[0].casefold()):
                        group = tuple(p[2] for p in grouped)
                        limit = integer(context(group[0], True).run(node.limit).text) if node.limit is not None else None
                        if limit is not None and limit < 1: raise ScriptError("EXPORT_LIMIT", "Loop limit must be positive")
                        nested_loop = any(isinstance(n, Loop) for n in node.body)
                        if nested_loop:
                            chosen = group[:limit] if limit is not None else group
                            pieces.append(render(node.body, chosen, chosen[0], True))
                        else:
                            for track in group[:limit] if limit is not None else group:
                                pieces.append(render(node.body, group, track, True))
                else:
                    text = context(current, inside).node(node).text
                    output_count[0] += len(text)
                    if output_count[0] > 16_000_000: raise ScriptError("LIMIT", "Export result exceeds 16 MB of characters")
                    pieces.append(text)
            return "".join(pieces)
        destination, encoding = default_name, "utf-8"
        if template.filename:
            args = template.filename.children
            name = context(base, False).run(args[0]).text
            if name:
                if "." not in ntpath.basename(name) and "/" not in name and "\\" not in name:
                    destination = ntpath.splitext(default_name)[0] + "." + name.lstrip(".")
                else: destination = name
            if len(args) == 2: encoding = context(base, False).run(args[1]).text.lower()
        if encoding not in {"utf-8", "utf-16", "ansi"}: raise ScriptError("EXPORT_ENCODING", "Use utf-8, utf-16 or explicit ANSI codepage")
        destination = safe_relative_path(destination)
        result = render(template.nodes, subset, base)
        codec = "utf-16-le" if encoding == "utf-16" else ansi_encoding if encoding == "ansi" else "utf-8"
        if not codec: raise ScriptError("EXPORT_ENCODING", "ANSI export requires an explicit codepage; system locale is not read")
        try: data = result.encode(codec)
        except (UnicodeError, LookupError) as exc: raise ScriptError("EXPORT_ENCODING", f"Text cannot be represented with {codec}") from exc
        if bom and encoding in {"utf-8", "utf-16"}: data = (b"\xef\xbb\xbf" if encoding == "utf-8" else b"\xff\xfe") + data
        files.append(ExportFile(destination, data, codec, tuple(t.track_id for t in subset), partition_key))
    paths = [f.relative_path.casefold() for f in files]
    if len(paths) != len(set(paths)): raise ScriptError("PATH_COLLISION", "Export partitions resolve to the same destination")
    return ExportPlan(tuple(files), template.source.sha256, inputs)
