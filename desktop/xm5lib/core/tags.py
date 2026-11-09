"""Read-only tag/stream inspection with Mutagen, and tag writing for export *copies* only."""
from __future__ import annotations

from pathlib import Path

import mutagen

AUDIO_EXTENSIONS = {".flac", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".ape", ".wv",
                    ".aif", ".aiff", ".dsf", ".dff", ".alac", ".mka", ".wma"}

_EASY_KEYS = {"title": "title", "artist": "artist", "album": "album", "albumartist": "albumartist",
              "tracknumber": "tracknumber", "discnumber": "discnumber", "date": "date", "genre": "genre"}


def _first(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _number(value) -> int | None:
    s = _first(value)
    if not s:
        return None
    s = s.split("/")[0].strip()
    try:
        return int(s)
    except ValueError:
        return None


def codec_name(audio) -> str:
    kind = type(audio).__name__
    info = audio.info
    if kind == "MP4":
        c = str(getattr(info, "codec", "") or "")
        return "ALAC" if c.startswith("alac") else "AAC" if c.startswith("mp4a") else (c.upper() or "MP4")
    return {"FLAC": "FLAC", "MP3": "MP3", "EasyMP3": "MP3", "WAVE": "WAV", "OggVorbis": "Vorbis",
            "OggOpus": "Opus", "AIFF": "AIFF", "DSF": "DSD", "DSDIFF": "DSD", "WavPack": "WavPack",
            "MonkeysAudio": "APE", "ASF": "WMA", "EasyMP4": "AAC"}.get(kind, kind)


def read(path: Path) -> dict:
    """Stream info and the common tags. Raises on unreadable files (the scanner records the error)."""
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError("unrecognized audio format")
    info = audio.info
    out = {
        "codec": codec_name(audio),
        "sample_rate": getattr(info, "sample_rate", None),
        "bits": getattr(info, "bits_per_sample", None) or None,
        "channels": getattr(info, "channels", None),
        "bitrate": getattr(info, "bitrate", None),
        "duration": float(getattr(info, "length", 0) or 0),
        "has_embedded_art": int(bool(embedded_picture(audio))),
    }
    easy = None
    try:
        easy = mutagen.File(path, easy=True)
    except Exception:
        easy = None
    tags = getattr(easy, "tags", None) if easy is not None else None
    from mutagen.id3 import ID3
    if isinstance(tags, ID3):          # WAV/AIFF/DSF/DFF carry plain ID3 frames (no easy interface)
        tags = None
    if tags is not None and hasattr(tags, "keys"):
        get = lambda k: tags.get(k) if k in tags else None  # noqa: E731
    else:
        get = _raw_getter(audio)
    for field, key in _EASY_KEYS.items():
        out[field] = get(key)
    out["comment"] = _comment(audio)
    for k in ("title", "artist", "album", "albumartist", "date", "genre"):
        out[k] = _first(out[k])
    out["tracknumber"] = _number(out["tracknumber"])
    out["discnumber"] = _number(out["discnumber"])
    return out


def _raw_getter(audio):
    """Fallback for formats without an easy interface (e.g. WAV with ID3, DSF)."""
    tags = getattr(audio, "tags", None)
    frames = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
              "tracknumber": "TRCK", "discnumber": "TPOS", "date": "TDRC", "genre": "TCON"}

    def get(key):
        if tags is None:
            return None
        frame = frames.get(key)
        if frame and frame in tags:
            v = tags[frame]
            return [str(x) for x in getattr(v, "text", [v])]
        if key in tags:
            return tags[key]
        return None
    return get


def _comment(audio) -> str | None:
    tags = getattr(audio, "tags", None)
    if tags is None:
        return None
    try:
        if hasattr(tags, "getall"):
            frames = tags.getall("COMM")
            return _first(frames[0].text) if frames else None
        for key in ("comment", "COMMENT", "\xa9cmt"):
            if key in tags:
                return _first(tags[key])
    except Exception:
        return None
    return None


def embedded_picture(audio) -> bytes | None:
    """Front cover (or first picture) bytes from an already-open Mutagen object."""
    try:
        pics = getattr(audio, "pictures", None)
        if pics:
            front = [p for p in pics if p.type == 3]
            return (front or pics)[0].data
        tags = getattr(audio, "tags", None)
        if tags is None:
            return None
        if hasattr(tags, "getall"):
            apic = tags.getall("APIC")
            if apic:
                front = [p for p in apic if p.type == 3]
                return (front or apic)[0].data
        if "covr" in tags and tags["covr"]:
            return bytes(tags["covr"][0])
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- export copies only

WRITABLE_CODECS = {".flac", ".mp3", ".m4a"}


def write_playback_cover(copy_path: Path, jpeg: bytes, side: int = 330) -> None:
    """Replace pictures in an export copy with one standard front-cover tag.

    Uses the format's existing FLAC picture / ID3 APIC / MP4 covr fields.
    The caller verifies unchanged audio payload after both tags and art are saved.
    """
    ext = copy_path.suffix.lower()
    if ext == ".flac":
        from mutagen.flac import FLAC, Picture
        audio = FLAC(copy_path)
        picture = Picture()
        picture.type, picture.mime, picture.desc = 3, "image/jpeg", "Front cover"
        picture.width = picture.height = side
        picture.depth, picture.data = 24, jpeg
        audio.clear_pictures()
        audio.add_picture(picture)
        audio.save()
    elif ext == ".mp3":
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError
        try:
            audio = ID3(copy_path)
        except ID3NoHeaderError:
            audio = ID3()
        audio.delall("APIC")
        audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Front cover", data=jpeg))
        audio.save(copy_path)
    elif ext == ".m4a":
        from mutagen.mp4 import MP4, MP4Cover
        audio = MP4(copy_path)
        audio["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
    else:
        raise ValueError(f"cover writing not supported for {ext}")


def write_tags(copy_path: Path, values: dict[str, str]) -> None:
    """Apply managed overrides to an exported copy (never to a source file).

    ``values``: field -> text; empty text removes that tag. Only FLAC, MP3 and M4A are written;
    callers must check ``WRITABLE_CODECS`` and report other formats instead of calling this.
    """
    ext = copy_path.suffix.lower()
    if ext not in WRITABLE_CODECS:
        raise ValueError(f"tag writing not supported for {ext}")
    easy = mutagen.File(copy_path, easy=True)
    if easy is None:
        raise ValueError("unrecognized audio format")
    if easy.tags is None:
        easy.add_tags()
    for field, value in values.items():
        if field == "comment":  # not an easy key everywhere; written below
            continue
        if not value:
            if field in easy.tags:
                del easy.tags[field]
        else:
            easy.tags[field] = [value]
    easy.save()
    if "comment" in values:
        _write_comment(copy_path, ext, values["comment"])


def _write_comment(path: Path, ext: str, value: str) -> None:
    if ext == ".flac":
        from mutagen.flac import FLAC
        a = FLAC(path)
        if value:
            a["comment"] = [value]
        elif "comment" in a:
            del a["comment"]
        a.save()
    elif ext == ".mp3":
        from mutagen.id3 import COMM, ID3, ID3NoHeaderError
        try:
            t = ID3(path)
        except ID3NoHeaderError:
            t = ID3()
        t.delall("COMM")
        if value:
            t.add(COMM(encoding=3, lang="eng", desc="", text=[value]))
        t.save(path)
    elif ext == ".m4a":
        from mutagen.mp4 import MP4
        a = MP4(path)
        if value:
            a["\xa9cmt"] = [value]
        elif "\xa9cmt" in a:
            del a["\xa9cmt"]
        a.save()
