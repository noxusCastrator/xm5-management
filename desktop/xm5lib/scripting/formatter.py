"""Parsed, bounded Mp3tag-style format strings. No filesystem or process access."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache, reduce
import ntpath
import re
from typing import Mapping, MutableMapping


class ScriptError(ValueError):
    def __init__(self, code: str, message: str, position: int | None = None):
        self.code, self.position = code, position
        super().__init__(f"{code}: {message}" + (f" at offset {position}" if position is not None else ""))


@dataclass(frozen=True)
class Node:
    kind: str
    value: str = ""
    children: tuple = ()
    position: int = 0


@dataclass(frozen=True)
class Value:
    text: str = ""
    truth: bool = False
    found: bool = False


MAX_TEXT = 1_000_000
MAX_DEPTH = 64
UNSUPPORTED = {}

# Maximum 64 arguments only for integer arithmetic; other variadics use <64.
ARITIES = {
    "ansi": (1, 1), "fmtdate": (1, 2), "fmtnum": (1, 1), "rand": (0, 0), "uuid": (0, 0),
    "columnnames": (2, 2), "columnvalues": (2, 2),
    **{n: (1, 1) for n in ("lower", "upper", "len", "reverse", "char", "ord", "folderdepth", "getenv", "not", "odd", "isdigit", "get")},
    **{n: (2, 2) for n in ("left", "right", "cutleft", "cutright", "num", "repeat", "sort", "strchr", "strrchr", "strstr", "strrstr", "distance", "validate", "strcmp", "stricmp", "eql", "neql", "grtr", "geql", "less", "leql", "if2", "meta_sep", "put", "puts")},
    **{n: (1, 2) for n in ("caps", "caps2", "caps3", "trim", "trimleft", "trimright", "meta")},
    "mid": (2, 3), "dedup": (2, 3), "fmtduration": (1, 4), "regexp": (3, 4),
    "replace": (3, 63), "if": (3, 3), "ifgreater": (4, 4), "iflonger": (4, 4),
    "and": (2, 63), "or": (2, 63), "list": (3, 4),
    **{n: (2, 64) for n in ("add", "sub", "mul", "div", "mod")},
}


class Parser:
    def __init__(self, text: str):
        if not isinstance(text, str):
            raise ScriptError("TYPE", "Format string must be text")
        if len(text) > MAX_TEXT:
            raise ScriptError("LIMIT", "Format string exceeds one million characters")
        self.text, self.i = text, 0

    def sequence(self, stops: str = "", depth: int = 0) -> tuple[Node, ...]:
        if depth > MAX_DEPTH:
            raise ScriptError("LIMIT", "Format nesting exceeds 64", self.i)
        nodes = []
        while self.i < len(self.text) and self.text[self.i] not in stops:
            start, ch = self.i, self.text[self.i]
            if ch == "'":
                self.i += 1
                if self.i < len(self.text) and self.text[self.i] == "'":
                    self.i += 1
                    nodes.append(Node("literal", "'", position=start))
                    continue
                out = []
                while self.i < len(self.text):
                    if self.text[self.i] == "'":
                        if self.i + 1 < len(self.text) and self.text[self.i + 1] == "'":
                            out.append("'"); self.i += 2; continue
                        self.i += 1
                        break
                    out.append(self.text[self.i]); self.i += 1
                else:
                    raise ScriptError("SYNTAX", "Unclosed quote", start)
                nodes.append(Node("literal", "".join(out), position=start))
            elif ch == "%":
                end = self.text.find("%", self.i + 1)
                if end < 0:
                    raise ScriptError("SYNTAX", "Unclosed field placeholder", start)
                name = self.text[self.i + 1:end]
                if not name or any(c in name for c in "\r\n$[]"):
                    raise ScriptError("SYNTAX", "Invalid field name", start)
                nodes.append(Node("field", name.upper(), position=start)); self.i = end + 1
            elif ch == "[":
                self.i += 1
                children = self.sequence("]", depth + 1)
                if self.i == len(self.text):
                    raise ScriptError("SYNTAX", "Unclosed optional section", start)
                self.i += 1
                nodes.append(Node("optional", children=children, position=start))
            elif ch == "$":
                match = re.match(r"\$([A-Za-z_][A-Za-z_0-9]*)\(", self.text[self.i:])
                if not match:
                    raise ScriptError("SYNTAX", "Expected $function(...); quote a literal dollar sign", start)
                self.i += len(match[0]); args = []
                if self.i < len(self.text) and self.text[self.i] != ")":
                    while True:
                        args.append(self.sequence(",)", depth + 1))
                        if self.i >= len(self.text) or self.text[self.i] != ",": break
                        self.i += 1
                if self.i >= len(self.text) or self.text[self.i] != ")":
                    raise ScriptError("SYNTAX", "Unclosed function", start)
                self.i += 1
                nodes.append(Node("call", match[1].lower(), tuple(args), start))
            elif ch == "]":
                raise ScriptError("SYNTAX", "Unexpected closing bracket", start)
            else:
                # Parentheses and commas only delimit when inside a function.
                self.i += 1
                while self.i < len(self.text) and self.text[self.i] not in stops + "'%[$]": self.i += 1
                nodes.append(Node("literal", self.text[start:self.i], position=start))
        return tuple(nodes)


@lru_cache(maxsize=512)
def _cached_parse(text: str) -> tuple[Node, ...]:
    return Parser(text).sequence()


def parse(text: str) -> tuple[Node, ...]:
    if not isinstance(text, str): raise ScriptError("TYPE", "Format string must be text")
    return _cached_parse(text)


def validate(nodes: tuple[Node, ...], export: bool = False) -> None:
    for node in nodes:
        if node.kind == "optional": validate(node.children, export)
        if node.kind != "call": continue
        if node.value in UNSUPPORTED:
            raise ScriptError("UNSUPPORTED_FUNCTION", f"${node.value}: {UNSUPPORTED[node.value]}", node.position)
        arity = ARITIES.get(node.value)
        if export and node.value in {"loop", "loopend", "filename"}:
            arity = {"loop": (1, 2), "loopend": (0, 0), "filename": (1, 2)}[node.value]
        if arity is None:
            raise ScriptError("UNKNOWN_FUNCTION", f"${node.value} is not supported in this context", node.position)
        if not arity[0] <= len(node.children) <= arity[1] or (node.value == "replace" and len(node.children) % 2 != 1):
            raise ScriptError("ARITY", f"Invalid argument count for ${node.value}", node.position)
        for arg in node.children: validate(arg, export)


def integer(text: str) -> int:
    """Mp3tag's numeric prefix convention (e.g. 03/12 -> 3)."""
    m = re.match(r"\s*([+-]?\d+)", text, flags=re.ASCII)
    try:
        value = int(m[1]) if m else 0
    except ValueError as exc:
        raise ScriptError("LIMIT", "Integer literal is too large") from exc
    if value.bit_length() > 63:
        raise ScriptError("NUMBER_RANGE", "Only signed 64-bit magnitude arithmetic is supported")
    return value


def normalize_fields(fields: Mapping) -> dict[str, tuple[str, ...]]:
    if not isinstance(fields, Mapping): raise ScriptError("TYPE", "Fields must be a mapping")
    result = {}
    for key, value in fields.items():
        key = str(key).upper()
        if key in result:
            raise ScriptError("DUPLICATE_FIELD", f"Case-insensitive duplicate field {key}")
        if value is None: values = ()
        elif isinstance(value, (list, tuple)): values = tuple(str(v) for v in value if v is not None)
        else: values = (str(value),)
        result[key] = values
    return result


def regex_replace(text: str, pattern: str, replacement: str, ignore_case: bool = False) -> str:
    """A bounded common Boost Perl profile, deliberately not full Boost.Regex."""
    if len(text) > 8192 or len(pattern) > 512:
        raise ScriptError("REGEX_LIMIT", "Regex input exceeds 8192 characters or pattern exceeds 512 characters")
    # Reject unsupported engine-specific syntax instead of misinterpreting it.
    if re.search(r"\\[0-9gkKpPRXCGQElLuUN]|\(\?(?!:)|\(\*|\[\[:|\+\+|\*\+|\?\+|\}\+", pattern):
        raise ScriptError("REGEX_UNSUPPORTED", "Lookarounds, named groups, recursion, Unicode/Boost classes and possessive quantifiers are outside the compatible profile")
    # Python re has no execution timeout. Forbid a quantified group containing
    # quantification/alternation; cap text. This deliberately rejects some safe patterns.
    if re.search(r"\([^)]*(?:[*+?{]|\|)[^)]*\)(?:[*+?]|\{)", pattern):
        raise ScriptError("REGEX_UNSUPPORTED", "Repeated compound groups are excluded from the bounded regex profile")
    if re.search(r"\)(?:[*+?]|\{)", pattern):
        raise ScriptError("REGEX_UNSUPPORTED", "Quantified groups are outside the bounded regex profile")
    # Admit only small repetition profiles. Unbounded repetitions must consume
    # disjoint ASCII sets, e.g. ([a-z]+)([0-9]+) or ^\d+\s*. This rejects
    # quadratic overlapping cases such as a+a+$ without needing a timeout.
    # This is conservative admission, not a claim of full regex equivalence.
    repetition_sets, atom, quantifiers, index, choices, group_depth = [], None, 0, 0, 1, 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            if pattern[index:index+2] == r"\x":
                if not re.fullmatch(r"[0-9A-Fa-f]{2}", pattern[index+2:index+4]):
                    raise ScriptError("REGEX_UNSUPPORTED", "Only two-digit hexadecimal escapes are supported")
                atom = pattern[index:index+4]; index += 4; continue
            atom = pattern[index:index+2]; index += 2; continue
        if character == "[":
            end = index + 1
            while end < len(pattern):
                if pattern[end] == "\\": end += 2; continue
                if pattern[end] == "]": break
                end += 1
            atom = pattern[index:end+1]; index = end+1; continue
        if character in "+*?{" and not (character == "?" and index and pattern[index-1] == "("):
            # A ? immediately after a repetition is the lazy marker.
            if character == "?" and index and pattern[index-1] in "+*?}": index += 1; continue
            quantifiers += 1
            if quantifiers > 8: raise ScriptError("REGEX_UNSUPPORTED", "At most eight repetitions are admitted")
            unbounded = character in "+*"
            if character == "?": choices *= 2
            if character == "{":
                end = pattern.find("}", index)
                bounds = re.fullmatch(r"(\d+)(?:,(\d*))?", pattern[index+1:end]) if end >= 0 else None
                if bounds:
                    unbounded = bounds[2] == ""
                    maximum = int(bounds[2] or bounds[1])
                    if maximum > 100: raise ScriptError("REGEX_UNSUPPORTED", "Explicit repeat bounds are limited to 100")
                    if not unbounded: choices *= maximum - int(bounds[1]) + 1
                    index = end
                else: raise ScriptError("REGEX_UNSUPPORTED", "Use explicit valid repeat bounds or escape a literal brace")
            if choices > 256: raise ScriptError("REGEX_UNSUPPORTED", "Bounded repetition choice budget exceeds 256")
            if unbounded:
                try:
                    members = {code for code in range(128) if atom and re.fullmatch(atom, chr(code), flags=re.ASCII | (re.IGNORECASE if ignore_case else 0))}
                except re.error: members = set(range(128))
                if atom is None or not members: members = set(range(128))
                if any(members & previous for previous in repetition_sets):
                    raise ScriptError("REGEX_UNSUPPORTED", "Overlapping unbounded repetitions are outside the bounded profile")
                repetition_sets.append(members)
            atom = None
        elif character == "|":
            # Keep repetition sets across nested alternatives: sequential ambiguous
            # groups may backtrack across each other. Bound literal choices too.
            if group_depth == 0: repetition_sets = []
            choices *= 2
            if choices > 256: raise ScriptError("REGEX_UNSUPPORTED", "Alternation and repetition choice budget exceeds 256")
            atom = None
        elif character in "()^$":
            if character == "(": group_depth += 1
            if character == ")": group_depth -= 1
            if group_depth > 32: raise ScriptError("REGEX_LIMIT", "Regex group nesting exceeds 32")
            atom = None
        else: atom = re.escape(character) if character != "." else "."
        index += 1
    if re.search(r"\\(?![nrt\\$])", replacement) or re.search(r"\$(?!\d|\$|&)", replacement):
        raise ScriptError("REGEX_UNSUPPORTED", "Replacement supports $0..$99, $&, $$, and escaped newline/tab/backslash only")
    try:
        compiled = re.compile(pattern, re.ASCII | (re.IGNORECASE if ignore_case else 0))
        def replace(match):
            def token(m):
                value = m[0]
                if value == "$$": return "$"
                if value == "$&": return match[0]
                if value.startswith("$"):
                    index = int(value[1:])
                    if index > compiled.groups: raise ScriptError("REGEX_GROUP", f"No capture group {index}")
                    return match[index] or ""
                return {r"\n": "\n", r"\r": "\r", r"\t": "\t", r"\\": "\\", r"\$": "$"}[value]
            return re.sub(r"\$\$|\$&|\$\d{1,2}|\\[nrt\\$]", token, replacement)
        output = compiled.sub(replace, text)
    except (re.error, RecursionError, OverflowError) as exc:
        raise ScriptError("REGEX_SYNTAX", str(exc)) from exc
    if len(output) > MAX_TEXT: raise ScriptError("LIMIT", "Regex result exceeds output limit")
    return output


class Context:
    def __init__(self, fields, technical=None, variables=None, environment=None, captured=None, counters=None):
        self.captured = captured
        self.counters = counters if counters is not None else {}
        self.fields = normalize_fields(fields)
        self.technical = normalize_fields(technical or {})
        self.variables: MutableMapping = variables if variables is not None else {}
        if environment is not None and not isinstance(environment, Mapping): raise ScriptError("TYPE", "Environment must be an explicit mapping")
        self.environment = {str(k).upper(): str(v) for k, v in (environment or {}).items()}

    def run(self, nodes: tuple[Node, ...]) -> Value:
        values = [self.node(n) for n in nodes]
        text = "".join(v.text for v in values)
        if len(text) > MAX_TEXT: raise ScriptError("LIMIT", "Result exceeds one million characters")
        return Value(text, any(v.truth for v in values), any(v.found for v in values))

    def node(self, node: Node) -> Value:
        if node.kind == "literal": return Value(node.value)
        if node.kind == "field":
            scope = self.technical if node.value.startswith("_") else self.fields
            key = {"TRACK":"TRACKNUMBER", "YEAR":"DATE"}.get(node.value,node.value) if node.value not in scope else node.value
            values = scope.get(key, ())
            text = ", ".join(values)
            return Value(text, bool(text), bool(text))
        if node.kind == "optional":
            value = self.run(node.children)
            return value if value.found else Value()
        name, args = node.value, node.children
        if name == "if":
            condition = self.run(args[0]); result = self.run(args[1 if condition.truth else 2])
            return Value(result.text, result.truth, condition.found or result.found)
        if name == "if2":
            first = self.run(args[0])
            return first if first.truth else self.run(args[1])
        if name in {"ifgreater", "iflonger"}:
            first, second = self.run(args[0]), self.run(args[1])
            test = (integer(first.text) if name == "ifgreater" else len(first.text)) > integer(second.text)
            result = self.run(args[2 if test else 3])
            return Value(result.text, result.truth, first.found or second.found or result.found)
        values = [self.run(a) for a in args]; a = [v.text for v in values]
        found = any(v.found for v in values)
        def output(text, truth=None):
            return Value(str(text), any(v.truth for v in values) if truth is None else truth, found)
        def boolean(truth): return Value("1" if truth else "0", bool(truth), found)
        if name in {"and", "or", "not"}:
            return boolean(all(v.truth for v in values) if name == "and" else any(v.truth for v in values) if name == "or" else not values[0].truth)
        if name in {"eql", "neql", "strcmp", "stricmp"}:
            equal = a[0] == a[1] if name == "strcmp" else a[0].casefold() == a[1].casefold()
            return boolean(not equal if name == "neql" else equal)
        if name in {"grtr", "geql", "less", "leql"}:
            x, y = integer(a[0]), integer(a[1])
            return boolean({"grtr": x > y, "geql": x >= y, "less": x < y, "leql": x <= y}[name])
        if name == "odd": return boolean(integer(a[0]) % 2 != 0)
        if name == "isdigit": return boolean(bool(a[0]) and a[0][0] in "0123456789")
        if name in {"add", "sub", "mul", "div", "mod"}:
            numbers = [integer(x) for x in a]
            def op(x, y):
                if name in {"div", "mod"} and not y: raise ScriptError("DIV_ZERO", "Division by zero")
                quotient = (abs(x) // abs(y)) * (-1 if (x < 0) != (y < 0) else 1) if name in {"div", "mod"} else 0
                result = {"add": lambda: x+y, "sub": lambda: x-y, "mul": lambda: x*y, "div": lambda: quotient, "mod": lambda: x-quotient*y}[name]()
                if abs(result).bit_length() > 63: raise ScriptError("NUMBER_RANGE", "Arithmetic exceeds supported range")
                return result
            return output(reduce(op, numbers))
        if name in {"get", "put", "puts"}:
            if name != "get": self.variables[a[0]] = a[1]
            return output("" if name == "puts" else self.variables.get(a[0], ""))
        if name in {"ansi", "fmtdate", "fmtnum", "rand", "uuid", "columnnames", "columnvalues"}:
            from .context import context_function
            return output(context_function(name, a, self.captured, self.counters, self.technical))
        if name == "getenv": return output(self.environment.get(a[0].upper(), ""))
        if name in {"meta", "meta_sep"}:
            key=a[0].upper()
            if key not in self.fields: key={"TRACK":"TRACKNUMBER", "YEAR":"DATE"}.get(key,key)
            items = self.fields.get(key, ())
            if name == "meta" and len(a) == 2:
                index = integer(a[1]); result = items[index] if 0 <= index < len(items) else ""
            else: result = (a[1] if name == "meta_sep" else ", ").join(items)
            return Value(result, bool(result), bool(result))
        if name == "list":
            sep = a[3] if len(a) == 4 else ", "
            text = "".join(a[0]+key+a[1]+sep.join(items)+a[2] for key, items in self.fields.items())
            return Value(text, bool(self.fields), bool(self.fields))
        if name in {"lower", "upper", "reverse", "len"}:
            return output({"lower": lambda: a[0].lower(), "upper": lambda: a[0].upper(), "reverse": lambda: a[0][::-1], "len": lambda: len(a[0])}[name]())
        if name in {"caps", "caps2", "caps3"}:
            triggers = a[1] if len(a) > 1 else (" " if name != "caps3" else "")
            text = a[0] if name == "caps2" else a[0].lower()
            return output("".join(c.upper() if i == 0 or text[i-1] in triggers else c for i, c in enumerate(text)))
        if name in {"trim", "trimleft", "trimright"}:
            chars = a[1] if len(a) > 1 else None
            return output({"trim": str.strip, "trimleft": str.lstrip, "trimright": str.rstrip}[name](a[0], chars))
        if name in {"left", "right", "cutleft", "cutright"}:
            n = max(0, integer(a[1]))
            return output({"left": lambda: a[0][:n], "right": lambda: a[0][-n:] if n else "", "cutleft": lambda: a[0][n:], "cutright": lambda: a[0][:-n] if n else a[0]}[name]())
        if name == "mid":
            start, count = max(1, integer(a[1])) - 1, max(0, integer(a[2])) if len(a) == 3 else 1
            return output(a[0][start:start+count])
        if name == "num":
            width = max(0, integer(a[1]))
            if width > MAX_TEXT: raise ScriptError("LIMIT", "Numeric width too large")
            return output(str(integer(a[0])).zfill(width))
        if name == "char":
            n = integer(a[0])
            if not 0 <= n <= 0x10ffff or 0xd800 <= n <= 0xdfff: raise ScriptError("CODEPOINT", "Invalid Unicode scalar")
            return output(chr(n) if n else "")
        if name == "ord":
            if a[0] and ord(a[0][0]) > 0xffff: raise ScriptError("CODEPOINT", "$ord is limited to U+FFFF")
            return output(ord(a[0][0]) if a[0] else 0)
        if name == "repeat":
            n = max(0, integer(a[1]))
            if len(a[0])*n > MAX_TEXT: raise ScriptError("LIMIT", "Repeated result too large")
            return output(a[0]*n)
        if name == "replace":
            text = a[0]
            for i in range(1, len(a), 2):
                if not a[i]: raise ScriptError("EMPTY_SEARCH", "Empty replacement search is ambiguous")
                text = text.replace(a[i], a[i+1])
                if len(text) > MAX_TEXT: raise ScriptError("LIMIT", "Replacement result too large")
            return output(text)
        if name == "regexp": return output(regex_replace(*a[:3], ignore_case=len(a) == 4 and a[3] == "1"))
        if name in {"strchr", "strrchr", "strstr", "strrstr"}:
            needle = a[1][:1] if name.endswith("chr") else a[1]
            pos = a[0].rfind(needle) if name in {"strrchr", "strrstr"} else a[0].find(needle)
            return output(pos + 1 if needle else 0)
        if name in {"sort", "dedup"}:
            if not a[1]: raise ScriptError("EMPTY_SEPARATOR", "Segment separator cannot be empty")
            parts = a[0].split(a[1])
            if name == "sort": parts.sort()
            else:
                seen, result = set(), []
                for part in parts:
                    key = part.casefold() if len(a) > 2 and a[2] == "1" else part
                    if key not in seen: seen.add(key); result.append(part)
                parts = result
            return output(a[1].join(parts))
        if name == "validate": return output(re.sub(r'[/?*"<>|:]', lambda _: a[1], a[0]))
        if name == "folderdepth":
            path = a[0].replace("/", "\\")
            return output(len([p for p in ntpath.splitdrive(ntpath.dirname(path))[1].split("\\") if p]))
        if name == "distance":
            if len(a[0])*len(a[1]) > 4_000_000: raise ScriptError("LIMIT", "Edit-distance input too large")
            row = list(range(len(a[1])+1))
            for i, x in enumerate(a[0], 1):
                new = [i]
                for j, y in enumerate(a[1], 1): new.append(min(new[-1]+1, row[j]+1, row[j-1]+(x != y)))
                row = new
            return output(row[-1])
        if name == "fmtduration":
            unit = a[1] if len(a) > 1 else "s"
            if unit not in {"m", "s", "ms"}: raise ScriptError("DURATION_UNIT", "Use m, s or ms")
            milliseconds = integer(a[0]) * {"m": 60000, "s": 1000, "ms": 1}[unit]
            if milliseconds < 0: raise ScriptError("DURATION_RANGE", "Duration must be nonnegative")
            seconds, ms = divmod(milliseconds, 1000); minutes, seconds = divmod(seconds, 60)
            show_hours = len(a) > 2 and a[2] == "1"
            if show_hours:
                hours, minutes = divmod(minutes, 60); text = f"{hours}:{minutes:02}:{seconds:02}"
            else: text = f"{minutes:02}:{seconds:02}"
            if len(a) > 3 and a[3] == "1": text += f".{ms:03}"
            return output(text)
        raise ScriptError("UNKNOWN_FUNCTION", name)


def evaluate(text, fields, technical=None, variables=None, environment=None, captured=None, counters=None) -> str:
    """Evaluate captured data only. Variables mutate only the supplied mapping.

    Missing fields become empty text. Nonempty literal text has false Boolean
    status; populated fields and Boolean functions supply true status. Optional
    sections require at least one populated placeholder, including nested calls.
    The entire AST is validated before any variable mutation or branch execution.
    """
    nodes = parse(text); validate(nodes)
    if variables is not None and not isinstance(variables, MutableMapping): raise ScriptError("TYPE", "Variables must be a mutable mapping")
    # An evaluation error must not leave partial variable assignments behind.
    working = dict(variables or {})
    result = Context(fields, technical, working, environment, captured, counters).run(nodes).text
    if variables is not None: variables.update(working)
    return result
