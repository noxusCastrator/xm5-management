"""Compile the manager's PEQ subset to an mpv lavfi graph; no audio side effects."""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass

from . import peq

FILTER_LABEL = "xm5peq"
PCM_SOURCE_KINDS = ("pcm", "decoded-dsd")


class DSPError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Section:
    channel: str
    b0: float
    b1: float
    b2: float
    a1: float
    a2: float

    def response(self, frequency: float, sample_rate: int) -> complex:
        z = cmath.exp(-2j * math.pi * frequency / sample_rate)
        return (self.b0 + self.b1 * z + self.b2 * z * z) / (1 + self.a1 * z + self.a2 * z * z)


@dataclass(frozen=True)
class CompiledPreset:
    name: str
    sample_rate: int
    preamp_db: float
    sections: tuple[Section, ...]
    graph: str
    source_kind: str

    def mpv_filter(self) -> dict:
        """Native af property value; use JSON/native nodes, never command strings."""
        return {"name": "lavfi", "label": FILTER_LABEL, "enabled": True, "params": {"graph": self.graph}}

    def mpv_option(self) -> str:
        """One argv value for offline mpv runs. Graph contains generated constants only."""
        return f"@{FILTER_LABEL}:lavfi=[{self.graph}]"

    def response(self, frequencies: list[float]) -> dict[str, list[float]]:
        out = {}
        for channel in ("L", "R"):
            curve = []
            for frequency in frequencies:
                if not math.isfinite(frequency) or not 0 <= frequency < self.sample_rate / 2:
                    raise DSPError("invalid_frequency", "Response frequency must be below Nyquist")
                db = self.preamp_db
                for section in self.sections:
                    if section.channel in ("ALL", channel):
                        db += 20 * math.log10(max(abs(section.response(frequency, self.sample_rate)), 1e-12))
                curve.append(db)
            out[channel] = curve
        return out


def compile_preset(preset: peq.Preset, sample_rate: int, *, channels: str = "stereo",
                   source_kind: str = "pcm") -> CompiledPreset:
    """Use the existing validated RBJ coefficients at the actual decoded PCM rate.

    Stereo only: L/R map to FL/FR, ALL affects both. DSD/DoP passthrough must be
    rejected; explicitly decoded DSD PCM can be processed at its decoded rate.
    No resampling, limiter, automatic gain adjustment or APO approximation occurs.
    """
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 768000:
        raise DSPError("invalid_rate", "Decoded PCM sample rate must be an integer from 8000 to 768000 Hz")
    if channels != "stereo":
        raise DSPError("unsupported_channels", "Desktop PEQ requires a confirmed stereo channel layout")
    if source_kind not in PCM_SOURCE_KINDS:
        raise DSPError("unsupported_source", "PEQ requires PCM; DSD/DoP passthrough is not supported")
    errors = peq.validate(preset)
    if errors:
        raise DSPError("invalid_preset", "; ".join(errors))
    sections = []
    filters = [f"volume=volume={_number(10 ** (preset.preamp / 20))}:precision=double:replaygain=ignore"]
    for band in preset.bands:
        try:
            coefficients = peq.coefficients(band, sample_rate)
        except ValueError as exc:
            raise DSPError("nyquist", str(exc)) from exc
        if coefficients is None:
            continue
        section = Section(band.channel, *coefficients)
        sections.append(section)
        b0, b1, b2, a1, a2 = coefficients
        channel = {"ALL": "all", "L": "FL", "R": "FR"}[band.channel]
        filters.append("biquad=" + ":".join(("a0=1", f"a1={_number(a1)}", f"a2={_number(a2)}",
                       f"b0={_number(b0)}", f"b1={_number(b1)}", f"b2={_number(b2)}",
                       f"channels={channel}", "normalize=0", "transform=tdii", "precision=f64")))
    return CompiledPreset(preset.name, sample_rate, float(preset.preamp), tuple(sections), ",".join(filters), source_kind)


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise DSPError("nonfinite_coefficient", "Filter coefficient is not finite")
    return format(value, ".17g")
