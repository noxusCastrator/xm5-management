"""Read-only original-source sample peaks using FFmpeg's C-side astats reducer.

Only Peak_level is enabled. No perceptual weighting, true-peak interpolation,
PEQ, audio rewriting or full decoded PCM buffering is involved.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from .track_info import calculate_gain, canonical_json
from .bundled_runtime import BundledRuntimeError, require_tool

FILTER = "aformat=sample_fmts=dbl,astats=measure_perchannel=none:measure_overall=Peak_level+Number_of_NaNs+Number_of_Infs"
PEAK_PATTERN = re.compile(r"Peak level dB:\s*(-inf|[-+]?\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
NONFINITE_PATTERN = re.compile(r"Number of (?:NaNs|Infs):\s*([-+]?\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
MAX_DIAGNOSTIC_BYTES = 8192


class PeakCancelled(RuntimeError):
    pass


class SourceChangedError(RuntimeError):
    pass


@dataclass(frozen=True)
class PeakResult:
    status: str
    peak: float | None
    peak_dbfs: float | None
    source_sha256: str
    source_size: int
    source_version: dict
    recipe: dict
    recipe_sha256: str
    diagnostic: str = ""
    cached: bool = False

    def to_dict(self):
        return asdict(self)


def _cancelled(cancel):
    return bool(cancel and (cancel() if callable(cancel) else cancel.is_set()))


def _check_cancel(cancel):
    if _cancelled(cancel):
        raise PeakCancelled("Sample-peak scan cancelled")


def source_version(path):
    stat = Path(path).stat()
    return {"path": str(Path(path).resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "device": stat.st_dev, "inode": stat.st_ino}


def source_digest(path, cancel=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            _check_cancel(cancel)
            digest.update(block)
    _check_cancel(cancel)
    return digest.hexdigest()


class PeakScanner:
    """Explicitly constructed local analyzer with an optional owned cache.

    FFmpeg emits Peak_level with six decimals of dB. The result's linear peak is
    reconstructed at that reporting resolution; source identity is exact SHA256.
    """

    def __init__(self, *, ffmpeg_path=None, cache_dir=None, timeout_seconds=3600):
        self.runtime_error = ""
        if ffmpeg_path is not None:  # Explicit injection is used by isolated tests/maintenance only.
            self.ffmpeg_path = str(ffmpeg_path)
        else:
            try:
                self.ffmpeg_path = str(require_tool("ffmpeg"))
            except BundledRuntimeError as exc:
                self.ffmpeg_path = ""
                self.runtime_error = str(exc)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Peak timeout must be positive and finite")
        self.timeout_seconds = timeout_seconds
        self._recipe = None

    def recipe(self):
        if self._recipe is None:
            if not self.ffmpeg_path:
                version = "unavailable: " + (self.runtime_error or "No bundled FFmpeg executable")
            else:
                try:
                    probe = subprocess.run([self.ffmpeg_path, "-version"], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10, check=False)
                    version = probe.stdout[:8192].decode("utf-8", "replace").splitlines()[0]
                    help_result = subprocess.run([self.ffmpeg_path, "-hide_banner", "-h", "filter=astats"],
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                 timeout=10, check=False)
                    help_text = help_result.stdout[:32768].decode("utf-8", "replace")
                    if help_result.returncode or not all(x in help_text for x in ("measure_perchannel", "measure_overall", "Peak_level", "Number_of_NaNs", "Number_of_Infs", "none")):
                        version = "unsupported astats options: " + version
                except (OSError, subprocess.TimeoutExpired, IndexError) as exc:
                    version = "unavailable: " + type(exc).__name__
            self._recipe = {"id": "xm5-source-sample-peak-astats-v1", "ffmpeg_version": version,
                            "stream": "first-audio-stream", "filter": FILTER,
                            "reported_db_resolution": 0.000001, "linear_peak": "10**(Peak_level/20)",
                            "nonfinite_samples": "reject using astats NaN/Inf counters",
                            "silence": "Peak_level=-inf => peak=0,gain=0", "decode_errors": "fatal-xerror",
                            "lufs": False, "true_peak": False, "peq": False}
        return dict(self._recipe)

    def scan(self, path, *, expected_sha256=None, expected_size=None, cancel=None):
        path = Path(path)
        _check_cancel(cancel)
        before = source_version(path)
        digest = source_digest(path, cancel)
        if source_version(path) != before or expected_sha256 is not None and digest != expected_sha256 or expected_size is not None and before["size"] != expected_size:
            raise SourceChangedError("Original source changed before sample-peak scan")
        recipe = self.recipe()
        recipe_hash = hashlib.sha256(canonical_json(recipe)).hexdigest()
        cache_key = hashlib.sha256(canonical_json({"source_version": before, "source_sha256": digest, "recipe": recipe})).hexdigest()
        target = self.cache_dir / (cache_key + ".json") if self.cache_dir is not None else None
        cached = None
        if target is not None and target.is_file() and target.stat().st_size < 32768:
            try:
                record = json.loads(target.read_text(encoding="utf-8"))
                peak = record["peak"]
                if (record["status"] == "ok" and record["source_version"] == before and record["source_sha256"] == digest
                        and record["recipe"] == recipe and record["recipe_sha256"] == recipe_hash
                        and isinstance(peak, (int, float)) and not isinstance(peak, bool) and math.isfinite(peak) and peak >= 0
                        and record["source_size"] == before["size"]):
                    if (peak == 0 and record["peak_dbfs"] is None or peak > 0 and isinstance(record["peak_dbfs"], (int, float))
                            and math.isfinite(record["peak_dbfs"]) and abs(20 * math.log10(peak) - record["peak_dbfs"]) < 1e-8):
                        cached = PeakResult(**{**record, "cached": True})
            except (ValueError, KeyError, TypeError, OSError):
                pass  # Corrupt cache is disposable; analyze the unchanged source again.
        if cached is not None:
            result = cached
        elif path.suffix.lower() in {".dsf", ".dff", ".iso"}:
            result = PeakResult("unsupported", None, None, digest, before["size"], before, recipe, recipe_hash,
                                "DSD/DoP sources do not receive PCM peak correction")
        elif not self.ffmpeg_path or recipe["ffmpeg_version"].startswith(("unavailable", "unsupported")):
            result = PeakResult("unavailable", None, None, digest, before["size"], before, recipe, recipe_hash,
                                recipe["ffmpeg_version"])
        else:
            status, peak, peak_dbfs, diagnostic = self._analyze(path, cancel)
            result = PeakResult(status, peak, peak_dbfs, digest, before["size"], before, recipe, recipe_hash, diagnostic)
        # The analyzer never grants write ownership to the source. Both exact
        # bytes and stat identity must still match after decode/cache access.
        if source_version(path) != before or source_digest(path, cancel) != digest or source_version(path) != before:
            raise SourceChangedError("Original source changed during sample-peak scan")
        if target is not None and result.status == "ok" and not result.cached:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
            try:
                with temporary.open("xb") as output:
                    output.write(canonical_json(result.to_dict()) + b"\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return result

    def _analyze(self, path, cancel):
        command = [self.ffmpeg_path, "-nostdin", "-hide_banner", "-nostats", "-v", "info", "-xerror", "-i", str(path),
                   "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", FILTER, "-c:a", "pcm_f64le", "-f", "null", "-"]
        diagnostics = deque(maxlen=MAX_DIAGNOSTIC_BYTES)
        peaks = deque(maxlen=2)
        nonfinite = deque(maxlen=4)
        pending = bytearray()

        def collect(stream):
            # Fixed-size reads and a capped incomplete line prevent hostile
            # decoder output from growing memory with track duration.
            while block := stream.read(4096):
                diagnostics.extend(block)
                pending.extend(block)
                while b"\n" in pending:
                    line, _, remainder = pending.partition(b"\n")
                    pending[:] = remainder
                    match = PEAK_PATTERN.search(line.decode("utf-8", "replace"))
                    if match:
                        peaks.append(match.group(1))
                    invalid = NONFINITE_PATTERN.search(line.decode("utf-8", "replace"))
                    if invalid:
                        nonfinite.append(float(invalid.group(1)))
                if len(pending) > 8192:
                    del pending[:-8192]

        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError as exc:
            return "unavailable", None, None, str(exc)[:2048]
        thread = threading.Thread(target=collect, args=(process.stderr,), daemon=True)
        thread.start()
        started = time.monotonic()
        timed_out = False
        try:
            while process.poll() is None:
                _check_cancel(cancel)
                if time.monotonic() - started > self.timeout_seconds:
                    timed_out = True
                    process.kill()
                    break
                time.sleep(0.02)
            process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            thread.join(timeout=10)
            process.stderr.close()
        _check_cancel(cancel)
        diagnostic = bytes(diagnostics).decode("utf-8", "replace")[-4096:]
        if timed_out:
            return "failed", None, None, "Sample-peak decoder timed out\n" + diagnostic
        if process.returncode or len(peaks) != 1:
            return "failed", None, None, "Sample-peak decoder failed or returned no unique overall peak\n" + diagnostic
        if len(nonfinite) != 2 or any(value != 0 for value in nonfinite):
            return "failed", None, None, "Nonfinite source samples or missing NaN/Inf validation\n" + diagnostic
        if peaks[0].lower() == "-inf":
            return "ok", 0.0, None, ""
        level = float(peaks[0])
        try:
            peak = 10 ** (level / 20)
        except OverflowError:
            return "failed", None, None, "Nonfinite sample peak"
        if not math.isfinite(level) or not math.isfinite(peak) or peak <= 0:
            return "failed", None, None, "Invalid sample peak"
        return "ok", peak, level, ""


def scan_peak(path, **options):
    """Convenience library entry; explicit paths only, no global cache/profile."""
    scanner_options = {key: options.pop(key) for key in ("ffmpeg_path", "cache_dir", "timeout_seconds") if key in options}
    return PeakScanner(**scanner_options).scan(path, **options)
