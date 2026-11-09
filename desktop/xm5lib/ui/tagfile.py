"""The tag editor's file access, like Mp3tag: read the tags exactly as they are in the file, and write
changes back into the file.

Only the inspector uses this. After writing, the window asks the backend to read those files again
(``library.refresh``), so the library, playlists and favourites keep the same tracks.

Fields are plain text, as Mp3tag shows them: several values of one field are joined with "; ", the
track/disc number includes its total ("10/51"). Writing replaces only the fields that changed.
Formats: FLAC, Ogg Vorbis/Opus/FLAC (Vorbis comments), MP3/WAV/AIFF/DSF/DFF (ID3, the file's own
ID3 version kept; new tags are ID3v2.3), MP4/M4A (iTunes atoms), APE/WavPack/Musepack (APEv2) and WMA
(fields only, no cover). Anything else is shown read-only.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field

import mutagen

FIELDS = ("title", "artist", "album", "date", "tracknumber", "genre", "comment", "albumartist", "composer",
          "discnumber")
SEP = "; "
MAX_COVER_BYTES = 16 * 1024 * 1024
PICTURE_KINDS = {0: "other", 1: "icon", 2: "icon", 3: "front", 4: "back", 5: "leaflet", 6: "media", 7: "artist",
                 8: "artist", 9: "artist", 10: "band", 11: "composer", 12: "artist", 13: "location", 14: "studio",
                 15: "studio", 16: "studio", 17: "other", 18: "other", 19: "other", 20: "other"}


class TagError(Exception):
    pass


@dataclass
class Picture:
    data: bytes
    mime: str = "image/jpeg"
    kind: int = 3                       # ID3/FLAC picture type; 3 = front cover
    width: int = 0
    height: int = 0

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class FileTags:
    path: str
    fields: dict = field(default_factory=dict)
    pictures: list = field(default_factory=list)
    format: str = ""
    writable: bool = False
    covers: bool = False                 # pictures can be written too
    error: str | None = None

    @property
    def cover(self) -> Picture | None:
        front = [p for p in self.pictures if p.kind == 3]
        return (front or self.pictures or [None])[0]


# ---------------------------------------------------------------- helpers
def _join(values) -> str:
    if values is None:
        return ""
    if isinstance(values, (list, tuple)):
        return SEP.join(str(v).strip() for v in values if str(v).strip())
    return str(values).strip()


def _with_total(number: str, total: str) -> str:
    if number and total and "/" not in number:
        return f"{number}/{total}"
    return number


def _split_total(value: str) -> tuple[str, str]:
    a, _, b = value.partition("/")
    return a.strip(), b.strip()


def sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _dimensions(data: bytes) -> tuple[int, int]:
    try:
        from PySide6.QtGui import QImageReader
        from PySide6.QtCore import QBuffer, QByteArray
        buf = QBuffer()
        buf.setData(QByteArray(data))
        buf.open(QBuffer.ReadOnly)
        size = QImageReader(buf).size()
        return (size.width(), size.height()) if size.isValid() else (0, 0)
    except Exception:
        return 0, 0


def picture(data: bytes, kind: int = 3) -> Picture:
    w, h = _dimensions(data)
    return Picture(data, sniff_mime(data), kind, w, h)


def _kind(audio) -> str:
    from mutagen.apev2 import APEv2
    from mutagen.asf import ASFTags
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Tags
    tags = getattr(audio, "tags", None)
    name = type(audio).__name__
    if name in ("FLAC", "OggVorbis", "OggOpus", "OggFLAC", "OggSpeex", "OggTheora"):
        return "vorbis"
    if isinstance(tags, ID3) or name in ("MP3", "WAVE", "AIFF", "DSF", "DSDIFF", "TrueAudio"):
        return "id3"
    if isinstance(tags, MP4Tags) or name == "MP4":
        return "mp4"
    if isinstance(tags, APEv2) or name in ("MonkeysAudio", "WavPack", "Musepack", "OptimFROG", "TAK"):
        return "ape"
    if isinstance(tags, ASFTags) or name == "ASF":
        return "asf"
    return ""


def _open(path):
    try:
        audio = mutagen.File(path)
    except Exception as exc:
        raise TagError(f"{type(exc).__name__}: {exc}") from exc
    if audio is None:
        raise TagError("unrecognized audio format")
    return audio


# ---------------------------------------------------------------- reading
VORBIS = {"title": "title", "artist": "artist", "album": "album", "date": "date", "genre": "genre",
          "albumartist": "albumartist", "composer": "composer"}
ID3_FRAMES = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "date": "TDRC", "tracknumber": "TRCK",
              "genre": "TCON", "albumartist": "TPE2", "composer": "TCOM", "discnumber": "TPOS"}
MP4_KEYS = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "date": "\xa9day", "genre": "\xa9gen",
            "comment": "\xa9cmt", "albumartist": "aART", "composer": "\xa9wrt"}
APE_KEYS = {"title": "Title", "artist": "Artist", "album": "Album", "date": "Year", "tracknumber": "Track",
            "genre": "Genre", "comment": "Comment", "albumartist": "Album Artist", "composer": "Composer",
            "discnumber": "Disc"}
ASF_KEYS = {"title": "Title", "artist": "Author", "album": "WM/AlbumTitle", "date": "WM/Year",
            "tracknumber": "WM/TrackNumber", "genre": "WM/Genre", "comment": "Description",
            "albumartist": "WM/AlbumArtist", "composer": "WM/Composer", "discnumber": "WM/PartOfSet"}


def read(path) -> FileTags:
    path = str(path)
    out = FileTags(path)
    try:
        audio = _open(path)
    except TagError as exc:
        out.error = str(exc)
        return out
    kind = _kind(audio)
    out.format = type(audio).__name__
    tags = audio.tags
    f = out.fields
    try:
        if kind == "vorbis":
            get = (lambda k: tags.get(k)) if tags is not None else (lambda k: None)
            for name, key in VORBIS.items():
                f[name] = _join(get(key))
            if not f["albumartist"]:
                f["albumartist"] = _join(get("album artist"))
            f["comment"] = _join(get("comment")) or _join(get("description"))
            f["tracknumber"] = _with_total(_join(get("tracknumber")), _join(get("tracktotal")) or _join(get("totaltracks")))
            f["discnumber"] = _with_total(_join(get("discnumber")), _join(get("disctotal")) or _join(get("totaldiscs")))
            pics = list(getattr(audio, "pictures", []) or [])
            if tags is not None and not pics:
                from mutagen.flac import Picture as FP
                for b64 in tags.get("metadata_block_picture") or []:
                    try:
                        pics.append(FP(base64.b64decode(b64)))
                    except Exception:
                        pass
            out.pictures = [Picture(p.data, p.mime or sniff_mime(p.data), p.type, p.width, p.height) for p in pics]
            out.writable = out.covers = True
        elif kind == "id3":
            for name, frame in ID3_FRAMES.items():
                fr = tags.get(frame) if tags is not None else None
                if fr is None:
                    f[name] = ""
                elif frame == "TCON":
                    f[name] = _join(fr.genres)
                else:
                    f[name] = _join([str(t) for t in fr.text])
            comms = tags.getall("COMM") if tags is not None else []
            main = [c for c in comms if not c.desc] or [c for c in comms if not c.desc.startswith("iTun")]
            f["comment"] = _join(main[0].text) if main else ""
            apics = tags.getall("APIC") if tags is not None else []
            out.pictures = [Picture(a.data, a.mime or sniff_mime(a.data), int(a.type), *_dimensions(a.data)) for a in apics]
            out.writable = out.covers = True
        elif kind == "mp4":
            t = tags or {}
            for name, key in MP4_KEYS.items():
                f[name] = _join(t.get(key))
            if not f["genre"] and t.get("gnre"):
                from mutagen.id3 import TCON
                f["genre"] = _join(TCON(text=[f"({t['gnre'][0] - 1})"]).genres)
            for name, key in (("tracknumber", "trkn"), ("discnumber", "disk")):
                v = (t.get(key) or [None])[0]
                f[name] = (f"{v[0]}/{v[1]}" if v[1] else f"{v[0]}") if v and v[0] else ""
            from mutagen.mp4 import MP4Cover
            out.pictures = [Picture(bytes(c), "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg", 3,
                                    *_dimensions(bytes(c))) for c in t.get("covr") or []]
            out.writable = out.covers = True
        elif kind == "ape":
            t = tags
            for name, key in APE_KEYS.items():
                v = t.get(key) if t is not None else None
                f[name] = _join(list(v)) if v is not None and v.kind == 0 else ""
            for key in ("Cover Art (Front)", "Cover Art (Back)"):
                v = t.get(key) if t is not None else None
                if v is not None and v.kind == 1:
                    data = bytes(v.value)
                    data = data.split(b"\x00", 1)[1] if b"\x00" in data[:256] else data
                    out.pictures.append(Picture(data, sniff_mime(data), 3 if "Front" in key else 4, *_dimensions(data)))
            out.writable = out.covers = True
        elif kind == "asf":
            t = tags or {}
            for name, key in ASF_KEYS.items():
                f[name] = _join([str(v) for v in t.get(key, [])])
            out.writable = True
        else:
            for name in FIELDS:
                f.setdefault(name, "")
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        out.writable = out.covers = False
    for name in FIELDS:
        f.setdefault(name, "")
    return out


# ---------------------------------------------------------------- writing
KEEP = object()


def write(path, changes: dict, cover=KEEP) -> None:
    """``changes``: field -> text ("" removes the field). ``cover``: KEEP, None (remove the front covers)
    or a :class:`Picture` (replaces the front covers). Raises TagError; the file is checked afterwards."""
    path = str(path)
    bad = set(changes) - set(FIELDS)
    if bad:
        raise TagError(f"unknown fields: {sorted(bad)}")
    audio = _open(path)
    kind = _kind(audio)
    if not kind:
        raise TagError(f"{type(audio).__name__}: tags cannot be written for this format")
    if cover is not KEEP and kind == "asf":
        raise TagError("WMA: covers cannot be written")
    if isinstance(cover, Picture) and cover.size > MAX_COVER_BYTES:
        raise TagError("cover picture is larger than 16 MB")
    info = audio.info
    before = (getattr(info, "length", 0) or 0, getattr(info, "sample_rate", None), getattr(info, "channels", None))
    try:
        {"vorbis": _write_vorbis, "id3": _write_id3, "mp4": _write_mp4, "ape": _write_ape, "asf": _write_asf}[kind](
            audio, path, changes, cover)
    except TagError:
        raise
    except Exception as exc:
        raise TagError(f"{type(exc).__name__}: {exc}") from exc
    check = _open(path).info
    after = (getattr(check, "length", 0) or 0, getattr(check, "sample_rate", None), getattr(check, "channels", None))
    if abs(after[0] - before[0]) > 0.05 or after[1:] != before[1:]:
        raise TagError(f"the audio stream looks different after writing ({before} -> {after})")


def _set_vorbis(tags, key, value, *aliases):
    for k in (key,) + aliases:
        if k in tags:
            del tags[k]
    if value:
        tags[key] = [value]


def _write_vorbis(audio, path, changes, cover):
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    for name, value in changes.items():
        if name in ("tracknumber", "discnumber"):
            num, total = _split_total(value)
            tkey = "tracktotal" if name == "tracknumber" else "disctotal"
            alias = "totaltracks" if name == "tracknumber" else "totaldiscs"
            keep_alias = alias in t and tkey not in t
            _set_vorbis(t, name, num)
            _set_vorbis(t, alias if keep_alias else tkey, total, tkey if keep_alias else alias)
        elif name == "comment":
            _set_vorbis(t, "comment", value, "description")
        elif name == "albumartist":
            _set_vorbis(t, "albumartist", value, "album artist")
        else:
            _set_vorbis(t, VORBIS[name], value)
    if cover is not KEEP:
        from mutagen.flac import Picture as FP
        new = None
        if isinstance(cover, Picture):
            new = FP()
            new.type, new.mime, new.desc = 3, cover.mime, ""
            new.width, new.height, new.depth, new.data = cover.width, cover.height, 24, cover.data
        if type(audio).__name__ == "FLAC":
            keep = [p for p in audio.pictures if p.type != 3]
            if not [p for p in audio.pictures if p.type == 3] and audio.pictures and new is None:
                keep = audio.pictures[1:]          # the shown picture was not marked "front": remove it
            audio.clear_pictures()
            for p in keep + ([new] if new is not None else []):
                audio.add_picture(p)
        else:
            entries = []
            for b64 in t.get("metadata_block_picture") or []:
                try:
                    p = FP(base64.b64decode(b64))
                except Exception:
                    continue
                if p.type != 3:
                    entries.append(b64)
            if new is not None:
                entries.append(base64.b64encode(new.write()).decode("ascii"))
            _set_vorbis(t, "metadata_block_picture", None, "coverart", "coverartmime")
            if entries:
                t["metadata_block_picture"] = entries
    audio.save()


def _write_id3(audio, path, changes, cover):
    from mutagen.id3 import APIC, COMM, Frames
    if audio.tags is None:
        audio.add_tags()
        version = 3
    else:
        version = 3 if audio.tags.version[1] < 4 else 4
    t = audio.tags
    for name, value in changes.items():
        if name == "comment":
            for c in [c for c in t.getall("COMM") if not c.desc]:
                t.pop(c.HashKey, None)
            if value:
                t.add(COMM(encoding=3, lang="eng", desc="", text=[value]))
            continue
        frame = ID3_FRAMES[name]
        t.delall(frame)
        if name == "date":
            for old in ("TYER", "TDAT", "TIME", "TRDA"):
                t.delall(old)
        if value:
            t.add(Frames[frame](encoding=3, text=[value]))
    if cover is not KEEP:
        apics = t.getall("APIC")
        fronts = [a for a in apics if a.type == 3] or apics[:1]
        for a in fronts:
            t.pop(a.HashKey, None)
        if isinstance(cover, Picture):
            t.add(APIC(encoding=3, mime=cover.mime, type=3, desc="", data=cover.data))
    try:
        audio.save(v2_version=version)
    except TypeError:                        # formats whose save() has no v2_version
        audio.save()


def _write_mp4(audio, path, changes, cover):
    from mutagen.mp4 import MP4Cover
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    for name, value in changes.items():
        if name in ("tracknumber", "discnumber"):
            key = "trkn" if name == "tracknumber" else "disk"
            num, total = _split_total(value)
            t.pop(key, None)
            if num:
                try:
                    t[key] = [(int(num), int(total or 0))]
                except ValueError as exc:
                    raise TagError(f"{name} must be a number (like 3 or 3/12)") from exc
            continue
        key = MP4_KEYS[name]
        t.pop(key, None)
        if name == "genre":
            t.pop("gnre", None)
        if value:
            t[key] = [value]
    if cover is not KEEP:
        t.pop("covr", None)
        if isinstance(cover, Picture):
            fmt = MP4Cover.FORMAT_PNG if cover.mime == "image/png" else MP4Cover.FORMAT_JPEG
            t["covr"] = [MP4Cover(cover.data, imageformat=fmt)]
    audio.save()


def _write_ape(audio, path, changes, cover):
    from mutagen.apev2 import BINARY, APEValue
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    for name, value in changes.items():
        key = APE_KEYS[name]
        if key in t:
            del t[key]
        if value:
            t[key] = value
    if cover is not KEEP:
        if "Cover Art (Front)" in t:
            del t["Cover Art (Front)"]
        if isinstance(cover, Picture):
            ext = {"image/png": "png", "image/gif": "gif", "image/bmp": "bmp"}.get(cover.mime, "jpg")
            t["Cover Art (Front)"] = APEValue(f"cover.{ext}".encode() + b"\x00" + cover.data, BINARY)
    audio.save()


def _write_asf(audio, path, changes, cover):
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    for name, value in changes.items():
        key = ASF_KEYS[name]
        if key in t:
            del t[key]
        if value:
            t[key] = [value]
    audio.save()


__all__ = ["FIELDS", "FileTags", "Picture", "TagError", "KEEP", "read", "write", "picture", "sniff_mime"]
