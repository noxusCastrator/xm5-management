"""Native multivalue tag access for staged, exact-source script transactions.

WAV/MP3 use ID3; native FLAC uses Vorbis comments. Unknown binary frames and
non-target pictures stay untouched. The caller owns backups and publication.
"""
from __future__ import annotations

import base64
import hashlib
import re

import mutagen
from mutagen.id3 import APIC, COMM, Frames, TXXX, USLT

ID3_FIELDS = {"TITLE": "TIT2", "ARTIST": "TPE1", "ALBUM": "TALB", "DATE": "TDRC", "YEAR": "TDRC",
              "TRACK": "TRCK", "TRACKNUMBER": "TRCK", "DISCNUMBER": "TPOS", "GENRE": "TCON",
              "ALBUMARTIST": "TPE2", "COMPOSER": "TCOM", "BPM": "TBPM", "COPYRIGHT": "TCOP",
              "ENCODEDBY": "TENC", "LYRICIST": "TEXT", "LANGUAGE": "TLAN", "PUBLISHER": "TPUB",
              "ISRC": "TSRC", "TITLESORT": "TSOT", "ARTISTSORT": "TSOP", "ALBUMSORT": "TSOA",
              "ALBUMARTISTSORT": "TSO2", "COMPILATION": "TCMP", "SUBTITLE": "TIT3", "CONTENTGROUP": "TIT1"}
CANONICAL = {"TRACK": "TRACKNUMBER", "YEAR": "DATE"}


def canonical(field):
    field = str(field).upper()
    field = CANONICAL.get(field, field)
    if not field or field in {"COVER", "PICTURE"} or field.startswith("_") or re.search(r"[\x00-\x1f=%]", field) or len(field) > 255:
        raise ValueError("Invalid writable tag field")
    return field


def _audio(path):
    audio = mutagen.File(path)
    if audio is None or type(audio).__name__ not in {"FLAC", "WAVE", "MP3"}:
        raise ValueError("Staged script tags admit native FLAC, WAVE and MP3")
    return audio


def read_values(path):
    audio = _audio(path)
    if type(audio).__name__ == "FLAC":
        result={}
        for key,values in (audio.tags or {}).items():
            field=canonical(key)
            if field in result: raise ValueError("Ambiguous aliased native Vorbis field: "+field)
            result[field]=list(map(str,values))
        return result
    result, tags = {}, audio.tags
    if tags is None:
        return result
    for field, frame in ID3_FIELDS.items():
        if field in CANONICAL:
            continue
        values = tags.getall(frame)
        if values:
            result[field] = [str(text) for value in values for text in value.text]
    for value in tags.getall("TXXX"):
        name = canonical(value.desc)
        if name in result:
            raise ValueError("Ambiguous standard/custom tag field: " + name)
        result[name] = list(map(str, value.text))
    comments = [value for value in tags.getall("COMM") if not value.desc]
    if comments:
        result["COMMENT"] = [str(text) for value in comments for text in value.text]
    lyrics = [value for value in tags.getall("USLT") if not value.desc]
    if lyrics:
        result["UNSYNCEDLYRICS"] = [value.text for value in lyrics]
    return result


def pictures(path):
    audio = _audio(path)
    values = audio.pictures if type(audio).__name__ == "FLAC" else (audio.tags.getall("APIC") if audio.tags else [])
    return [{"mime": value.mime, "type": int(value.type), "description": value.desc,
             "native": {key:getattr(value,key) for key in ("width","height","depth","colors")} if type(audio).__name__=="FLAC" else {},
             "data_base64": base64.b64encode(value.data).decode("ascii"), "sha256": hashlib.sha256(value.data).hexdigest()}
            for value in values]


def prepare_pictures(path, rows):
    """Preserve retained FLAC dimensions; declare dimensions for new pictures."""
    import copy
    import io
    from PIL import Image
    flac=type(_audio(path)).__name__=="FLAC"
    result=copy.deepcopy(rows)
    for row in result:
        if not flac: row["native"]={}
        elif not row.get("native"):
            with Image.open(io.BytesIO(base64.b64decode(row["data_base64"],validate=True))) as image:
                row["native"]={"width":image.width,"height":image.height,"depth":32 if image.mode=="RGBA" else 24,"colors":0}
    return result


def _value(value):
    if isinstance(value, bytes): return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    if isinstance(value, (list, tuple)): return [_value(item) for item in value]
    if isinstance(value, dict): return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (str, int, bool, float)) or value is None: return value
    return str(value)


def native_removals(path, fields, *, except_fields=False):
    """Freeze exact native frame keys for explicit remove/remove-except."""
    audio=_audio(path)
    selected={str(field).upper() for field in fields}
    aliases={CANONICAL.get(field,field) for field in selected}
    if "PICTURE" in aliases: aliases.add("COVER")
    result=[]
    for key,value in (audio.tags or {}).items():
        if type(audio).__name__=="FLAC":
            name=canonical(key)
        elif key.startswith("APIC:"): name="COVER"
        elif key.startswith("COMM:"): name="COMMENT"
        elif key.startswith("USLT:"): name="UNSYNCEDLYRICS"
        elif key.startswith("TXXX:"): name=canonical(value.desc)
        else:
            name=next((field for field,frame in ID3_FIELDS.items() if field not in CANONICAL and frame==key.split(":",1)[0]),key.split(":",1)[0])
        if (name in aliases)!=except_fields: result.append(key)
    return result


def untouched(path, changed, *, pictures_changed=False, native_delete=()):
    audio = _audio(path); changed = {canonical(field) for field in changed}
    if type(audio).__name__ == "FLAC":
        # Format Value tracknumber explicitly owns only the selected field;
        # totals and alias fields stay independent native multivalue fields.
        values = {key: _value(value) for key, value in (audio.tags or {}).items() if canonical(key) not in changed and key not in native_delete}
    else:
        values = {}
        for key, value in (audio.tags or {}).items():
            if key in native_delete: continue
            if pictures_changed and key.startswith("APIC:"): continue
            if any(key.split(":",1)[0] == ID3_FIELDS.get(field) for field in changed): continue
            if key.startswith("TXXX:") and canonical(value.desc) in changed: continue
            if "COMMENT" in changed and key.startswith("COMM:") and not value.desc: continue
            if "UNSYNCEDLYRICS" in changed and key.startswith("USLT:") and not value.desc: continue
            values[key] = _value({k: v for k,v in vars(value).items() if k != "encoding"})
    return {"tags": values, "pictures": None if pictures_changed else pictures(path)}


def write(path, changes, replacement_pictures=None, native_delete=()):
    audio = _audio(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    flac = type(audio).__name__ == "FLAC"
    version = 3 if not flac and tags.version[1] < 4 else 4
    for key in native_delete:
        if key in tags: del tags[key]
    for field, values in changes.items():
        field = canonical(field)
        if not isinstance(values, list) or len(values) > 1024 or any(not isinstance(v,str) or len(v)>65536 or "\0" in v for v in values):
            raise ValueError("Tag values must be bounded independent text values")
        values = [value for value in values if value]
        if flac:
            if field in tags: del tags[field]
            if values: tags[field] = values
        elif field == "COMMENT":
            for frame in tags.getall("COMM"):
                if not frame.desc: tags.pop(frame.HashKey, None)
            if values: tags.add(COMM(encoding=3, lang="eng", desc="", text=values))
        elif field == "UNSYNCEDLYRICS":
            if len(values) > 1: raise ValueError("ID3 plain unsynced lyrics have one text value")
            for frame in tags.getall("USLT"):
                if not frame.desc: tags.pop(frame.HashKey, None)
            if values: tags.add(USLT(encoding=3, lang="eng", desc="", text=values[0]))
        elif field in ID3_FIELDS:
            frame_id = ID3_FIELDS[field]; tags.delall(frame_id)
            if values: tags.add(Frames[frame_id](encoding=3, text=values))
        else:
            for frame in tags.getall("TXXX"):
                if canonical(frame.desc) == field: tags.pop(frame.HashKey, None)
            if values: tags.add(TXXX(encoding=3, desc=field, text=values))
    if replacement_pictures is not None:
        if not isinstance(replacement_pictures,list) or len(replacement_pictures)>64: raise ValueError("Invalid picture selection")
        if flac: audio.clear_pictures()
        else: tags.delall("APIC")
        descriptions = set()
        for row in replacement_pictures:
            data = base64.b64decode(row["data_base64"], validate=True)
            if len(data)>16*1024*1024 or hashlib.sha256(data).hexdigest()!=row["sha256"]: raise ValueError("Invalid planned picture bytes")
            if type(row["type"]) is not int or not 0<=row["type"]<=20: raise ValueError("Invalid picture type")
            if row["description"] in descriptions and not flac: raise ValueError("ID3 picture descriptions must be unique")
            descriptions.add(row["description"])
            if flac:
                from mutagen.flac import Picture
                import io
                from PIL import Image
                value=Picture(); value.data=data;value.mime=row["mime"];value.type=row["type"];value.desc=row["description"]
                properties=row.get("native",{})
                if set(properties)!={"width","height","depth","colors"} or any(type(v) is not int or not 0<=v<(1<<32) for v in properties.values()):
                    raise ValueError("FLAC picture properties must be explicitly planned")
                value.width,value.height,value.depth,value.colors=(properties[key] for key in ("width","height","depth","colors"))
                audio.add_picture(value)
            else:
                tags.add(APIC(encoding=3,mime=row["mime"],type=row["type"],desc=row["description"],data=data))
    if flac: audio.save()
    else: audio.save(v2_version=version, v23_sep=None)
