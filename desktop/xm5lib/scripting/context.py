"""Captured host/UI values for reproducible date, locale and random expressions."""
from __future__ import annotations

import codecs
from datetime import datetime, timezone, timedelta
import locale
import os
import random
import re
import time
import uuid

from .formatter import ScriptError


def capture_context(provided=None):
    """Capture once at plan time. Reading context never writes tags or files."""
    supplied = dict(provided or {})
    now = datetime.now().astimezone()
    values = locale.localeconv()
    if os.name == "nt":
        import ctypes
        codepage = "cp" + str(ctypes.windll.kernel32.GetACP())
    else:
        codepage = locale.getpreferredencoding(False)
    result = {"timestamp": int(time.time()), "utc_offset_seconds": int(now.utcoffset().total_seconds()),
              "timezone_mode": "fixed" if "utc_offset_seconds" in supplied else "local_at_plan", "timestamp_offsets": {},
              "ansi_encoding": codepage, "thousands_sep": values["thousands_sep"], "grouping": values["grouping"],
              "months_long": [datetime(2001, month, 1).strftime("%B") for month in range(1, 13)],
              "months_short": [datetime(2001, month, 1).strftime("%b") for month in range(1, 13)],
              "weekdays_long": [datetime(2001, 1, day).strftime("%A") for day in range(1, 8)],
              "weekdays_short": [datetime(2001, 1, day).strftime("%a") for day in range(1, 8)],
              "random_seed": str(uuid.uuid4()), "uuid_namespace": str(uuid.uuid4()), "columns": None,
              "columns_by_track": None, "columns_by_occurrence": None}
    if set(supplied) - set(result):
        raise ScriptError("CONTEXT", "Unknown captured-context field")
    result.update(supplied)
    def validate_columns(columns):
        if not isinstance(columns,list) or len(columns)>256:
            raise ScriptError("CONTEXT", "Columns must be a bounded list")
        for column in columns:
            if (not isinstance(column,dict) or set(column)-{"id","name","value"}
                    or not isinstance(column.get("name"),str) or len(column["name"])>1024
                    or not isinstance(column.get("value"),str) or len(column["value"])>65536
                    or "id" in column and (not isinstance(column["id"],str) or len(column["id"])>255)):
                raise ScriptError("CONTEXT", "Capture actual bounded column name/value objects")
    if result["columns"] is not None: validate_columns(result["columns"])
    for key in ("columns_by_track","columns_by_occurrence"):
        mapping=result[key]
        if mapping is None: continue
        if not isinstance(mapping,dict) or len(mapping)>10000 or any(not isinstance(identity,str) or not identity or len(identity)>1024 for identity in mapping):
            raise ScriptError("CONTEXT", "Column maps require bounded exact identity keys")
        for columns in mapping.values(): validate_columns(columns)
    return result


def context_function(name, args, captured, counters, technical=None):
    if not isinstance(captured, dict):
        raise ScriptError("CONTEXT_REQUIRED", "$" + name + " requires captured host/UI context")
    def required(key):
        if key not in captured or captured[key] is None:
            raise ScriptError("CONTEXT_REQUIRED", "$" + name + " requires " + key)
        return captured[key]
    if name == "ansi":
        try: return args[0].encode(required("ansi_encoding"), "replace").decode(required("ansi_encoding"))
        except (LookupError, UnicodeError): raise ScriptError("CODEPAGE", "Unknown/inapplicable captured ANSI codepage")
    if name == "rand":
        if "random" not in counters: counters["random"] = random.Random(str(required("random_seed")))
        return str(counters["random"].randrange(0, 1 << 31))
    if name == "uuid":
        index = counters.get("uuid", 0); counters["uuid"] = index + 1
        try: return str(uuid.uuid5(uuid.UUID(str(required("uuid_namespace"))), str(index)))
        except ValueError: raise ScriptError("CONTEXT", "Captured UUID namespace is invalid")
    if name in ("columnnames", "columnvalues"):
        columns = captured.get("columns")
        if name=="columnvalues":
            technical=technical or {}
            def identity(key):
                value=technical.get(key,())
                return value[0] if isinstance(value,(list,tuple)) and value else str(value) if value else None
            occurrences,tracks=captured.get("columns_by_occurrence"),captured.get("columns_by_track")
            occurrence,track=identity("_OCCURRENCE_ID"),identity("_TRACK_ID")
            if occurrences is not None and occurrence in occurrences: columns=occurrences[occurrence]
            elif tracks is not None and track in tracks: columns=tracks[track]
            elif occurrences is not None or tracks is not None:
                raise ScriptError("CONTEXT_REQUIRED", "$columnValues needs captured values for this exact track/occurrence")
        if columns is None: raise ScriptError("CONTEXT_REQUIRED", "$"+name+" requires columns")
        if not isinstance(columns, list) or len(columns) > 256 or any(not isinstance(c, dict) for c in columns):
            raise ScriptError("CONTEXT", "Capture actual visible columns as bounded name/value objects")
        return "".join(args[0] + str(column["name" if name == "columnnames" else "value"]) + args[1] for column in columns)
    if name == "fmtnum":
        from .formatter import integer
        number = integer(args[0]); sign = "-" if number < 0 else ""; number = str(abs(number))
        separator, grouping = str(required("thousands_sep")), list(required("grouping"))
        parts, index, previous = [], 0, None
        while number and grouping:
            width = grouping[min(index, len(grouping) - 1)]
            if type(width) is not int or width < 0: raise ScriptError("CONTEXT", "Invalid locale digit grouping")
            if width == 127: break
            if width == 0: width = previous
            if not width: break
            parts.append(number[-width:]); number = number[:-width]; previous = width
            index += 1
        if number: parts.append(number)
        return sign + separator.join(reversed(parts))
    if name == "fmtdate":
        try:
            timestamp = int(args[1]) if len(args) > 1 else int(required("timestamp"))
            if captured.get("timezone_mode") == "local_at_plan":
                offsets = required("timestamp_offsets")
                if str(timestamp) not in offsets:
                    offsets[str(timestamp)] = int(datetime.fromtimestamp(timestamp).astimezone().utcoffset().total_seconds())
                offset = int(offsets[str(timestamp)])
            else:
                offset = int(required("utc_offset_seconds"))
            if not -86400 < offset < 86400: raise ValueError("timezone offset")
            value = datetime.fromtimestamp(timestamp, timezone(timedelta(seconds=offset)))
        except (ValueError, OverflowError, OSError): raise ScriptError("DATE_RANGE", "Invalid captured raw Unix timestamp/timezone")
        month, day = value.month - 1, value.weekday()
        names = {}
        for key, index, length in (("months_long", month, 12), ("months_short", month, 12), ("weekdays_long", day, 7), ("weekdays_short", day, 7)):
            rows = required(key)
            if not isinstance(rows, list) or len(rows) != length: raise ScriptError("CONTEXT", "Invalid captured date names")
            names[key] = rows[index]
        tokens = {"yyyy": f"{value.year:04}", "yy": f"{value.year % 100:02}", "MMMM": names["months_long"], "MMM": names["months_short"],
                  "MM": f"{value.month:02}", "M": str(value.month), "dddd": names["weekdays_long"], "ddd": names["weekdays_short"],
                  "EEEE": names["weekdays_long"], "EE": names["weekdays_short"], "dd": f"{value.day:02}", "d": str(value.day),
                  "HH": f"{value.hour:02}", "H": str(value.hour), "hh": f"{(value.hour - 1) % 12 + 1:02}", "h": str((value.hour - 1) % 12 + 1),
                  "mm": f"{value.minute:02}", "m": str(value.minute), "ss": f"{value.second:02}", "s": str(value.second)}
        return re.sub("|".join(sorted(tokens, key=len, reverse=True)), lambda match: str(tokens[match[0]]), args[0])
    raise ScriptError("UNKNOWN_FUNCTION", name)
