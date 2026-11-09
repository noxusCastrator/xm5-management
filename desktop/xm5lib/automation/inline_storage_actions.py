"""Explicit inline-card jobs through the existing storage owner."""

from ..application import DomainError
from ..core.bundled_runtime import require_tool
from ..core.inline_card import export_card, organize_card, xm5_inline
from ..core.inline_card import library


NAMES = frozenset({"storage.inline.export", "storage.inline.organize", "storage.inline.rebuild"})


def _strings(values, label, maximum):
    if (not isinstance(values, list) or not 1 <= len(values) <= maximum
            or any(not isinstance(value, str) or not value for value in values)):
        raise DomainError("INVALID_ARGUMENT", f"{label} needs 1–{maximum} explicit paths", {})
    if len(values) != len(set(values)):
        raise DomainError("INVALID_ARGUMENT", f"{label} contains repeated paths", {})
    return values


def _options(args):
    levels = args.get("analyze_peak", True)
    if type(levels) is not bool:
        raise DomainError("INVALID_ARGUMENT", "analyze_peak expects true or false", {})
    return levels


def submit(engine, name, args, progress=None, cancel=None):
    if name not in NAMES:
        raise DomainError("UNKNOWN_COMMAND", "Unknown inline storage operation", {})
    allowed = {
        "storage.inline.export": {"parent", "music_roots", "eq_roots", "analyze_peak"},
        "storage.inline.organize": {"root", "tracks", "analyze_peak"},
        "storage.inline.rebuild": {"root"},
    }[name]
    if set(args) - allowed:
        raise DomainError("INVALID_ARGUMENT", "Unexpected inline storage argument", {})
    root_key = "parent" if name == "storage.inline.export" else "root"
    if not isinstance(args.get(root_key), str) or not args[root_key]:
        raise DomainError("INVALID_ARGUMENT", f"{root_key} needs an explicit card folder", {})
    results = []
    try:
        xm5_inline.check_cancel(cancel)
        if name == "storage.inline.export":
            parent = engine._under(args["parent"], engine.export_roots)
            music = [engine._under(path, engine.music_roots) for path in
                     _strings(args.get("music_roots"), "music_roots", 128)]
            eq = args.get("eq_roots", [])
            if not isinstance(eq, list):
                raise DomainError("INVALID_ARGUMENT", "eq_roots must be a list", {})
            eq = [engine._under(path, engine.import_roots + engine.music_roots) for path in
                  _strings(eq, "eq_roots", 128)] if eq else []
            from .inline_library_actions import InlineLibraryActions
            actions = InlineLibraryActions(engine)
            plan = actions.dispatch("cards.plan", {"format": "XM5I", "destination": str(parent / "xm5"),
                "roots": [str(path) for path in music], "peq_roots": [str(path) for path in eq],
                "peak_correction_enabled": _options(args)}, progress, cancel)
            return actions.dispatch("cards.execute", {"plan_id": plan["plan_id"], "digest": plan["digest"]},
                                    progress, cancel)

        root = engine._under(args["root"], engine.export_roots)
        xm5_inline.check_legacy_card(root)
        if name == "storage.inline.rebuild":
            result = organize_card.rebuild_index(root, cancel=cancel)
            if (root / library.MANIFEST).is_file():
                library.refresh_owned(root)
            if result["skipped"]:
                return {"status": "partial", **result,
                        "error": {"code": "INLINE_UNREADABLE", "message": "Some track headers need inspection"}}
            return {"status": "complete", **result}

        selected = _strings(args.get("tracks"), "tracks", 10000)
        levels = _options(args)
        for relative in selected:
            if xm5_inline._relative_parts(relative)[:2] != ["xm5", "music"]:
                raise DomainError("INVALID_ARGUMENT", "Choose tracks below xm5/music", {})
            xm5_inline._card_path(root, relative)
        ffmpeg = require_tool("ffmpeg")
        for relative in selected:
            xm5_inline.check_cancel(cancel)
            try:
                result = organize_card.organize_one(root, relative, ffmpeg, levels=levels, cancel=cancel)
            except organize_card.formats.Unsupported as exc:
                result = {"status": "unsupported", "diagnostic": str(exc)}
            results.append({"path": relative, **result})
            if progress:
                progress({"phase": "inline_organize", "current": len(results), "total": len(selected),
                          "path": relative})
        if (root / library.MANIFEST).is_file():
            library.refresh_owned(root, [row["path"] for row in results if row["status"] != "unsupported"])
        return {"status": "complete", "tracks": results}
    except InterruptedError:
        return {"status": "cancelled", "tracks": results}
    except (ValueError, OSError) as exc:
        error = {"code": "INLINE_FAILED", "message": str(exc)}
        return {"status": "partial" if results else "failed", "tracks": results, "error": error}
