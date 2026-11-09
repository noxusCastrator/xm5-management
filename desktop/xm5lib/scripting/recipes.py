"""Readable playlist recipes over a caller-frozen track/folder selection.

Beets smartplaylist is a design reference, not a compatibility claim. This
module deliberately uses structured predicates, not shell/query evaluation.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import uuid
from .actions import CapturedTrack
from .formatter import ScriptError, evaluate, integer, normalize_fields, parse, validate


@dataclass(frozen=True)
class Predicate:
    field: str
    operator: str
    value: str


@dataclass(frozen=True)
class PlaylistRecipe:
    recipe_id: str
    name: str
    folder_ids: tuple[str, ...]
    group_by: str = ""
    all_of: tuple[Predicate, ...] = ()
    any_of: tuple[Predicate, ...] = ()
    # (field name, numeric, descending); stable ties preserve captured order.
    sort: tuple[tuple[str, bool, bool], ...] = ()
    duplicates: str = "preserve"


@dataclass(frozen=True)
class PlaylistOccurrence:
    occurrence_id: str
    track_id: str
    source_occurrence_id: str


@dataclass(frozen=True)
class PlannedPlaylist:
    playlist_id: str
    name: str
    group: str
    occurrences: tuple[PlaylistOccurrence, ...]


@dataclass(frozen=True)
class PlaylistPlan:
    recipe_id: str
    playlists: tuple[PlannedPlaylist, ...]
    inputs: tuple[tuple[str, str], ...]
    recipe_sha256: str

    def assert_current(self, tracks):
        if tuple((t.track_id, t.fingerprint()) for t in tracks) != self.inputs:
            raise ScriptError("STALE_PLAN", "Playlist capture changed")


NAMESPACE = uuid.UUID("7e45446b-52c3-48de-81aa-6cc8b522497a")


def plan_playlists(recipe: PlaylistRecipe, tracks, environment=None, captured=None) -> PlaylistPlan:
    """Return replace/create requests; applying requires normal playlist revisions.

    recipe_id is a durable app ID, independent of display name. folder_ids must
    explicitly list captured boundaries; no disk traversal or library querying.
    Every occurrence needs a unique caller-provided stable occurrence ID, even
    when the same track is intentionally repeated. IDs survive input reordering.
    """
    tracks = tuple(tracks)
    counters = {}
    if not recipe.recipe_id or not recipe.folder_ids: raise ScriptError("PLAYLIST_SCOPE", "Recipe requires a stable ID and explicit folder boundaries")
    if recipe.duplicates not in {"preserve", "first_track"}: raise ScriptError("DUPLICATES", "Use preserve or first_track")
    validate(parse(recipe.group_by)); validate(parse(recipe.name))
    predicates = (*recipe.all_of, *recipe.any_of)
    for predicate in predicates:
        if predicate.operator not in {"contains", "equals", "equals_case", "missing", "present", "greater", "less", "at_least", "at_most"}:
            raise ScriptError("QUERY_OPERATOR", f"Unsupported operator {predicate.operator}")
    captured_occurrences = [t.occurrence_id for t in tracks]
    if any(not t.track_id or not t.folder_id for t in tracks):
        raise ScriptError("TRACK_ID", "Captured tracks require stable track and folder IDs")
    if any(not identity for identity in captured_occurrences) or len(set(captured_occurrences)) != len(captured_occurrences):
        raise ScriptError("OCCURRENCE_ID", "Each captured occurrence needs a distinct stable occurrence ID")
    def test(track, predicate):
        fields = normalize_fields(track.technical if predicate.field.startswith("_") else track.fields)
        items = fields.get(predicate.field.upper(), ())
        value, operator = predicate.value, predicate.operator
        if operator == "missing": return not any(items)
        if operator == "present": return any(items)
        def one(item):
            if operator == "contains": return value.casefold() in item.casefold()
            if operator == "equals": return value.casefold() == item.casefold()
            if operator == "equals_case": return value == item
            x, y = integer(item), integer(value)
            return {"greater": x > y, "less": x < y, "at_least": x >= y, "at_most": x <= y}[operator]
        return any(one(item) for item in items)
    groups = {}
    for track in tracks:
        if track.folder_id not in recipe.folder_ids: continue
        if not all(test(track, p) for p in recipe.all_of): continue
        if recipe.any_of and not any(test(track, p) for p in recipe.any_of): continue
        key = evaluate(recipe.group_by, track.fields, track.technical, environment=environment, captured=captured, counters=counters)
        groups.setdefault(key, []).append(track)
    planned = []
    for key in sorted(groups, key=lambda k: (k.casefold(), k)):
        group = groups[key]
        for field, numeric, descending in reversed(recipe.sort):
            def sort_key(track):
                values = normalize_fields(track.technical if field.startswith("_") else track.fields).get(field.upper(), ())
                text = values[0] if values else ""
                return integer(text) if numeric else text.casefold()
            group.sort(key=sort_key, reverse=descending)
        if recipe.duplicates == "first_track":
            seen, filtered = set(), []
            for track in group:
                if track.track_id not in seen: seen.add(track.track_id); filtered.append(track)
            group = filtered
        playlist_id = str(uuid.uuid5(NAMESPACE, json.dumps([recipe.recipe_id,key], ensure_ascii=False)))
        occurrences = tuple(PlaylistOccurrence(str(uuid.uuid5(uuid.UUID(playlist_id), track.occurrence_id)), track.track_id, track.occurrence_id) for track in group)
        name = evaluate(recipe.name, group[0].fields, group[0].technical, environment=environment, captured=captured, counters=counters)
        if not name.strip(): raise ScriptError("PLAYLIST_NAME", "Playlist name evaluates to empty")
        planned.append(PlannedPlaylist(playlist_id, name, key, occurrences))
    canonical = {"recipe_id": recipe.recipe_id, "name": recipe.name, "folder_ids": recipe.folder_ids, "group_by": recipe.group_by,
                 "all_of": [vars(p) for p in recipe.all_of], "any_of": [vars(p) for p in recipe.any_of], "sort": recipe.sort, "duplicates": recipe.duplicates}
    recipe_hash = hashlib.sha256(json.dumps(canonical,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return PlaylistPlan(recipe.recipe_id, tuple(planned), tuple((t.track_id,t.fingerprint()) for t in tracks), recipe_hash)
