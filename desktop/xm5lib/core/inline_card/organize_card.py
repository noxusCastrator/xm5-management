"""Prepare explicitly selected card copies with XM5 thumbnail and track data."""

import argparse
from io import BytesIO
import json
import math
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from threading import Thread

from PIL import Image, ImageOps

from . import xm5_inline as xm5


from . import formats


ADAPTERS = {".wav", ".aif", ".aiff", ".mp3", ".m4a"}
MAX_ART_BYTES = 16 * 1024 * 1024
MAX_ART_PIXELS = 40_000_000
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
COVER_NAMES = ("cover", "folder", "front", "album")
LOG_BYTES = 32 * 1024
FILTER = ("aformat=sample_fmts=dbl,astats=measure_perchannel=none:"
          "measure_overall=Peak_level+RMS_level+Number_of_NaNs+Number_of_Infs")
STATS = re.compile(
    rb"\] (Peak level dB|RMS level dB|Number of NaNs|Number of Infs): ([^\r\n]+)"
)


def _collect(stream, destination, limit, tail):
    while data := stream.read(8192):
        if tail:
            destination.extend(data)
            del destination[:-limit]
        elif len(destination) <= limit:
            destination.extend(data[:limit + 1 - len(destination)])


def _ffmpeg(executable, source, options, timeout, *, output_limit=0, loglevel="error", cancel=None):
    if not executable:
        raise xm5.InlineError("FFmpeg is unavailable. Specify --ffmpeg or use a cover with --no-levels.")
    command = [str(executable), "-nostdin", "-hide_banner", "-nostats", "-v", loglevel, "-xerror",
               "-protocol_whitelist", "file,pipe", "-max_alloc", str(64 * 1024 * 1024),
               "-i", str(source), *options]
    log = bytearray()
    output = bytearray()
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          stdout=subprocess.PIPE if output_limit else subprocess.DEVNULL) as process:
        readers = [Thread(target=_collect, args=(process.stderr, log, LOG_BYTES, True))]
        if output_limit:
            readers.append(Thread(target=_collect, args=(process.stdout, output, output_limit, False)))
        for reader in readers:
            reader.start()
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                xm5.check_cancel(cancel)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    process.wait(min(remaining, 0.1))
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise xm5.InlineError(f"FFmpeg exceeded its {timeout:g}-second time limit") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for reader in readers:
                reader.join()
        if len(output) > output_limit:
            raise xm5.InlineError("Embedded cover exceeds the 16 MiB limit")
        return process.returncode, bytes(output), bytes(log)


def _decoder_error(operation, log):
    lines = log.decode("utf-8", "replace").splitlines()
    detail = lines[-1][:512] if lines else "No diagnostic"
    return xm5.InlineError(f"{operation} failed: {detail}")


def _embedded_cover(ffmpeg, track, timeout, cancel=None):
    code, data, log = _ffmpeg(
        ffmpeg, track,
        ["-map", "0:v:disp:attached_pic:0", "-frames:v", "1", "-c:v", "copy", "-f", "image2pipe", "pipe:1"],
        timeout, output_limit=MAX_ART_BYTES, cancel=cancel,
    )
    if code and b"matches no streams" in log:
        return b""
    if code:
        raise _decoder_error("Embedded cover extraction", log)
    return data


def sample_levels(ffmpeg, track, timeout, cancel=None):
    """Reduce decoded PCM to original peak and peak minus RMS in milli-decibels."""
    code, _, log = _ffmpeg(
        ffmpeg, track,
        ["-map", "0:a:0", "-vn", "-sn", "-dn", "-af", FILTER, "-c:a", "pcm_f64le", "-f", "null", "-"],
        timeout, loglevel="info", cancel=cancel,
    )
    if code:
        raise _decoder_error("Sample analysis", log)
    fields = dict(STATS.findall(log))
    try:
        peak, rms, nans, infs = (
            float(fields[key]) for key in (b"Peak level dB", b"RMS level dB",
                                           b"Number of NaNs", b"Number of Infs")
        )
    except (KeyError, ValueError) as exc:
        raise xm5.InlineError("FFmpeg did not report complete sample statistics") from exc
    if nans != 0 or infs != 0:
        return None, None, "Decoded samples include NaN or infinity"
    if peak == rms == -math.inf:
        return None, None, "Silent track"
    if not (math.isfinite(peak) and math.isfinite(rms) and peak >= rms):
        raise xm5.InlineError("FFmpeg reported invalid peak or RMS")
    crest = round((peak - rms) * 1000)
    if peak > 0:
        return None, crest, "Decoded sample peak exceeds 0 dBFS"
    return round(peak * 1000), crest, ""


def prepare_images(data):
    """Make three opaque square baseline JPEGs from the selected original."""
    result = {}
    try:
        with Image.open(BytesIO(data)) as original:
            if original.width * original.height > MAX_ART_PIXELS:
                raise xm5.InlineError("Cover exceeds the 40 million pixel limit")
            with ImageOps.exif_transpose(original).convert("RGBA") as image:
                for side, capacity in xm5.SLOT_SIZES.items():
                    resized = ImageOps.contain(image, (side, side), Image.Resampling.LANCZOS)
                    canvas = Image.new("RGB", (side, side))
                    canvas.paste(resized, ((side - resized.width) // 2, (side - resized.height) // 2),
                                 resized.getchannel("A"))
                    for quality in (90, 80, 65, 50, 35, 20, 8):
                        output = BytesIO()
                        canvas.save(output, "JPEG", quality=quality, optimize=True, progressive=False)
                        if output.tell() <= capacity - 4:
                            result[side] = output.getvalue()
                            break
                    else:
                        raise xm5.InlineError(f"Cannot fit the {side} pixel cover in its JPEG slot")
    except (OSError, Image.DecompressionBombError) as exc:
        raise xm5.InlineError(f"Cannot read cover: {exc}") from exc
    return result


def _regular(path):
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and not xm5._reparse(info)


def _sibling_cover(track):
    candidates = [p for p in track.parent.iterdir()
                  if p.suffix.lower() in IMAGE_SUFFIXES and _regular(p)]
    for name in (track.stem.casefold(), *COVER_NAMES):
        matches = [p for p in candidates if p.stem.casefold() == name]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
    return candidates[0] if len(candidates) == 1 else None


def _read_cover(ffmpeg, track, cover, timeout, cancel=None, source_context=None):
    if cover is not None:
        return xm5._bounded_read(cover, MAX_ART_BYTES), "explicit"
    art = _embedded_cover(ffmpeg, track, timeout, cancel)
    if art:
        return art, "embedded"
    sibling = _sibling_cover(Path(source_context) if source_context else track)
    if sibling is not None:
        return xm5._bounded_read(sibling, MAX_ART_BYTES), "adjacent"
    return b"", ""


def _read_lyrics(track, lyrics):
    if lyrics is None:
        for candidate in track.parent.iterdir():
            if candidate.name == f"{track.stem}.lrc" and _regular(candidate):
                lyrics = candidate
                break
    return xm5._bounded_read(lyrics, xm5.MAX_LYRICS) if lyrics is not None else b""


def _wrapped(track):
    payload = formats.read_payload(track)
    if payload is not None:
        xm5.validate_body(payload)
    return payload is not None


def _wrapped_header(track):
    span = formats.locate(track)
    if span is None:
        return False
    if not xm5.LYRICS_OFFSET <= span.size <= xm5.LYRICS_OFFSET + xm5.MAX_LYRICS:
        raise xm5.InlineError("XM5 block length is outside its permitted range")
    with track.open("rb") as file:
        xm5._check_slot_headers(file, span.offset)
    return True


def rebuild_index(card, *, cancel=None):
    return xm5.rebuild_index(card, {suffix: _wrapped_header for suffix in ADAPTERS}, cancel=cancel)


def organize_one(card, relative, ffmpeg=None, *, cover=None, lyrics=None, levels=True, timeout=3600,
                 publish_index=True, cancel=None, source_context=None, refresh=False):
    """Read original data, commit one selected card copy and update its track hint."""
    xm5.check_cancel(cancel)
    if xm5._relative_parts(relative)[:2] != ["xm5", "music"]:
        raise xm5.InlineError("Select a card-relative track below xm5/music")
    xm5.check_legacy_card(card)
    track = xm5._card_path(card, relative)
    original = track.lstat()
    if not stat.S_ISREG(original.st_mode):
        raise xm5.InlineError("Selected track must be a regular file")
    suffix = track.suffix.lower()
    if suffix != ".flac" and suffix not in ADAPTERS:
        raise formats.Unsupported(f"No inline adapter for {suffix or 'this extension'}")
    if suffix == ".flac":
        if xm5.inspect_track(track)["present"] and not refresh:
            return {"status": xm5.organize_track(card, relative, publish_index=publish_index, cancel=cancel)}
    elif _wrapped(track) and not refresh:
        if publish_index:
            xm5.refresh_index_record(card, relative, True)
        return {"status": "present"}

    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    art, art_source = _read_cover(ffmpeg, track, cover, timeout, cancel, source_context)
    images = prepare_images(art) if art else {}
    lrc = _read_lyrics(Path(source_context) if source_context else track, lyrics)
    peak, crest, diagnostic = sample_levels(ffmpeg, track, timeout, cancel) if levels else (None, None, "")
    body = None
    if images or lrc or peak is not None or crest is not None or refresh:
        body = xm5.encode_body(images, peak, crest, lrc)
    latest = xm5._card_path(card, relative).lstat()
    if xm5._snapshot(original) != xm5._snapshot(latest) or original.st_ctime_ns != latest.st_ctime_ns:
        raise xm5.InlineError("Track changed during preparation")
    xm5.check_cancel(cancel)

    if suffix == ".flac":
        status = xm5.organize_track(card, relative, body, publish_index=publish_index, cancel=cancel, replace_body=refresh)
    else:
        if publish_index:
            xm5._read_index(xm5._index_path(card))
        if body is not None:
            formats.write(card, relative, body)
        if publish_index:
            xm5.refresh_index_record(card, relative, body is not None)
        status = "inserted" if body is not None else "absent"
    result = {"status": status, "artwork": art_source, "image_sizes": list(images),
              "lyrics_bytes": len(lrc), "peak_mdbfs": peak, "crest_mdb": crest}
    if diagnostic:
        result["diagnostic"] = diagnostic
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    organize = commands.add_parser("organize")
    organize.add_argument("card", type=Path)
    organize.add_argument("track", nargs="+", help="Card-relative tracks using forward slashes")
    organize.add_argument("--cover", type=Path)
    organize.add_argument("--lyrics", type=Path)
    organize.add_argument("--ffmpeg", type=Path)
    organize.add_argument("--no-levels", action="store_true")
    organize.add_argument("--timeout", type=float, default=3600)
    rebuild = commands.add_parser("rebuild-index")
    rebuild.add_argument("card", type=Path)
    args = parser.parse_args()
    if args.command == "rebuild-index":
        try:
            print(json.dumps(rebuild_index(args.card), ensure_ascii=True))
        except (ValueError, OSError) as exc:
            parser.exit(2, f"{exc}\n")
        return 0
    if (args.cover or args.lyrics) and len(args.track) != 1:
        parser.error("Explicit --cover or --lyrics needs exactly one selected track")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    failed = False
    results = []
    for relative in args.track:
        try:
            result = organize_one(args.card, relative, args.ffmpeg, cover=args.cover, lyrics=args.lyrics,
                                  levels=not args.no_levels, timeout=args.timeout)
        except formats.Unsupported as exc:
            result = {"status": "unsupported", "diagnostic": str(exc)}
        except (ValueError, OSError) as exc:
            result = {"status": "error", "diagnostic": str(exc)}
            failed = True
        results.append({"path": relative, **result})
    print(json.dumps(results, ensure_ascii=True))
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
