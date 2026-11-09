"""Filename converters return values/plans, never rename files or write tags."""
from __future__ import annotations
import ntpath
import re
from .formatter import ScriptError, evaluate


def match_values(text: str, pattern: str, *, allow_path_separators=False) -> dict[str, str]:
    """Matching syntax is literal text + %field%; brackets are literal here.

    Each field matches within one path component; a caller may use %dummy% to
    discard data. Repeated real fields must agree. Adjacent fields are rejected
    because there is no unique split. Full-string match is required.
    """
    if not isinstance(text,str) or not isinstance(pattern,str): raise ScriptError("TYPE","Matching inputs must be text")
    if len(text)>65536 or len(pattern)>8192: raise ScriptError("MATCH_LIMIT","Matching text/pattern exceeds bounded size")
    parts = re.split(r"(%[^%]+%)", pattern)
    captures, tokens, previous = [], [], False
    for part in parts:
        if not part: continue
        if part.startswith("%") and part.endswith("%"):
            name = part[1:-1].upper()
            if previous: raise ScriptError("AMBIGUOUS_PATTERN", "Adjacent placeholders have no delimiter")
            if not name or name.startswith("_"): raise ScriptError("READONLY_FIELD", f"Cannot import into {name}")
            captures.append(name); tokens.append(("capture",name)); previous = True
        else:
            if "%" in part: raise ScriptError("SYNTAX", "Unclosed matching placeholder")
            tokens.append(("literal",part)); previous = False
    if not captures: raise ScriptError("EMPTY_PATTERN", "A matching pattern needs a placeholder")
    if len(captures)>64: raise ScriptError("MATCH_LIMIT","Matching pattern exceeds64 fields")
    # A budgeted matcher replaces the unbounded lazy-capture regex. Repeated
    # delimiters cannot make an external expression consume unbounded CPU.
    budget=[100_000]
    failed=set()
    def same(literal,offset):
        budget[0]-=len(literal)+1
        if budget[0]<0: raise ScriptError("MATCH_LIMIT","Matching work budget exhausted")
        actual=text[offset:offset+len(literal)]
        return actual.replace("/","\\")==literal.replace("/","\\")
    def visit(index,offset):
        budget[0]-=1
        if budget[0]<0: raise ScriptError("MATCH_LIMIT","Matching work budget exhausted")
        if (index,offset) in failed: return None
        if index==len(tokens): return [] if offset==len(text) else None
        kind,value=tokens[index]
        if kind=="literal":
            if same(value,offset):
                result=visit(index+1,offset+len(value))
                if result is not None: return result
        else:
            end=len(text)
            if not allow_path_separators:
                for separator in ("/","\\"):
                    found=text.find(separator,offset)
                    if found>=0: end=min(end,found)
            candidates=range(offset,end+1) if index+1<len(tokens) else (len(text),)
            for split in candidates:
                budget[0]-=1
                if budget[0]<0: raise ScriptError("MATCH_LIMIT","Matching work budget exhausted")
                if split>end: continue
                if index+1<len(tokens) and not same(tokens[index+1][1],split): continue
                result=visit(index+1,split)
                if result is not None: return [text[offset:split]]+result
        failed.add((index,offset))
        return None
    matched=visit(0,0)
    if matched is None: raise ScriptError("NO_MATCH", "Input does not match the entire pattern")
    result = {}
    for name, value in zip(captures, matched):
        if name == "DUMMY": continue
        if name in result and result[name] != value: raise ScriptError("CONFLICT", f"Repeated {name} captures disagree")
        result[name] = value
    return result


def filename_to_tags(path: str, pattern: str) -> dict[str, str]:
    """Import filename stem or the explicitly requested tail of folder structure.

    Extension is excluded, as in Mp3tag Filename - Tag. An absolute pattern
    matches the whole path; a relative one with folders matches that many tail
    components, without scanning or opening any filesystem directory.
    """
    stem = ntpath.splitext(path.replace("/", "\\"))[0]
    normalized = pattern.replace("/", "\\")
    if not ntpath.isabs(normalized):
        count = normalized.count("\\") + 1
        stem = "\\".join(stem.split("\\")[-count:])
    return match_values(stem, normalized)


def safe_relative_path(path: str) -> str:
    """Portable destination relative to a root that the caller must authorize."""
    normalized = path.replace("/", "\\")
    if not normalized or ntpath.isabs(normalized) or ntpath.splitdrive(normalized)[0]:
        raise ScriptError("PATH_SCOPE", "Destination must be a nonempty relative path")
    components = normalized.split("\\")
    for component in components:
        if not component or component in {".", ".."} or component.rstrip(" .") != component:
            raise ScriptError("PATH_SCOPE", "Empty, dot, parent and trailing-dot/space components are forbidden")
        if re.search(r'[\x00-\x1f<>:"|?*]', component): raise ScriptError("PATH_INVALID", "Destination contains a Windows-invalid character")
        if component.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1,10)], *[f"LPT{i}" for i in range(1,10)]}:
            raise ScriptError("PATH_INVALID", "Destination contains a reserved device name")
    return "/".join(components)


def tags_to_filename(pattern: str, fields, original_path: str, technical=None, environment=None, captured=None) -> str:
    """Return a validated relative filename, appending the original extension.

    The format describes the stem. Absolute relocation is an explicit service
    decision and is deliberately excluded from this pure converter.
    """
    stem = evaluate(pattern, fields, technical, environment=environment, captured=captured)
    return safe_relative_path(stem + ntpath.splitext(original_path)[1])
