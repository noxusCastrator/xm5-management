"""Read saved Peace (.peace) profiles into manager presets, or reject them with every reason found.

Peace files are read-only inputs; live ``config.txt`` / ``peace.txt`` are never written.

Filter-type mapping inherited from the previously tested converter
(peq-lab/tools/export_peace_to_xm5.py): no Filter entry = peaking, 14 = LSC, 15 = HSC. Any other
Filter ID is rejected: its meaning is not inferred from the number. Omitted Gain entries are taken
as 0 dB (Peace omits default values; an all-default "Equalizer Default" profile has no Gains
section at all). Speaker-specific sections ([Gains1], [Filters2], ...) carrying non-zero gains are
rejected because the speaker-index mapping has not been verified; dormant default sections
(Frequencies<n>/Qualities<n>, or all-zero gains) are reported and ignored.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .peq import Band, Preset, validate

FILTER_IDS = {None: "PK", "14": "LSC", "15": "HSC"}
METADATA_KEYS = {"id", "configuration icon", "description", "device", "device guid", "preamp"}
# General keys that are effects; accepted only at their neutral value.
NEUTRAL_EFFECTS = {"channels delay": "0", "stereo shift": "0", "spatial shift": "0"}
_NUM = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


@dataclass
class PeaceResult:
    path: str
    name: str
    sha256: str
    preset: Preset | None = None
    reasons: list[str] = field(default_factory=list)   # why it was rejected
    notes: list[str] = field(default_factory=list)     # what was ignored and why

    @property
    def ok(self) -> bool:
        return self.preset is not None


def _sections(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            if current in sections:
                raise ValueError(f"duplicate section [{current}]")
            sections[current] = {}
        elif current is not None and "=" in line:
            k, v = (x.strip() for x in line.split("=", 1))
            if k in sections[current]:
                raise ValueError(f"duplicate key {current}.{k}")
            sections[current][k] = v
        else:
            raise ValueError(f"unrecognized line {n}")
    return sections


def _indexed(section: dict[str, str], prefix: str, reasons: list[str], where: str) -> dict[int, str]:
    out = {}
    for k, v in section.items():
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", k)
        if not m:
            reasons.append(f"[{where}] unexpected key {k}")
            continue
        out[int(m.group(1))] = v
    return out


def _num(v: str, label: str, reasons: list[str]) -> float | None:
    if not _NUM.match(v):
        reasons.append(f"{label}={v!r} is not a plain decimal number")
        return None
    return float(v)


def read_profile(path: Path) -> PeaceResult:
    path = Path(path)
    data = path.read_bytes()
    res = PeaceResult(str(path), path.stem, hashlib.sha256(data).hexdigest())
    try:
        # Keys and numbers are ASCII; device names may be in the ANSI code page. Latin-1 keeps bytes.
        ini = _sections(data.decode("latin-1"))
    except ValueError as exc:
        res.reasons.append(f"not a readable Peace profile: {exc}")
        return res
    reasons, notes = res.reasons, res.notes
    general = {k.lower(): v for k, v in ini.get("General", {}).items()}
    for k, v in general.items():
        if k in METADATA_KEYS:
            continue
        if k in NEUTRAL_EFFECTS:
            if v.strip() == NEUTRAL_EFFECTS[k]:
                notes.append(f"General '{k}' = {v} is neutral; ignored")
            else:
                reasons.append(f"active Peace effect '{k}' = {v} cannot be represented")
            continue
        reasons.append(f"unsupported General setting '{k}' = {v}")
    speakers = ini.get("Speakers", {})
    target0 = speakers.get("SpeakerTargets0")
    if target0 is not None and target0.lower() != "all":
        reasons.append(f"common EQ targets speaker '{target0}', not all channels")
    for name, sec in ini.items():
        if name in ("Frequencies", "Gains", "Qualities", "Filters", "General", "Speakers"):
            continue
        m = re.fullmatch(r"(Frequencies|Qualities|Gains|Filters)(\d+)", name)
        if not m:
            reasons.append(f"unsupported section [{name}]")
        elif m.group(1) in ("Frequencies", "Qualities"):
            continue  # dormant per-speaker shape defaults
        elif m.group(1) == "Gains":
            active = {k: v for k, v in sec.items() if _NUM.match(v) and float(v) != 0}
            if active:
                reasons.append(f"speaker-specific EQ in [{name}] ({len(active)} non-zero gains); "
                               "speaker mapping is not verified, so it is not converted")
            elif sec:
                notes.append(f"[{name}] has only 0 dB gains; ignored")
        else:
            if sec:
                reasons.append(f"speaker-specific filter types in [{name}] are not converted")

    freqs = _indexed(ini.get("Frequencies", {}), "Frequency", reasons, "Frequencies")
    gains = _indexed(ini.get("Gains", {}), "Gain", reasons, "Gains")
    quals = _indexed(ini.get("Qualities", {}), "Quality", reasons, "Qualities")
    filters = _indexed(ini.get("Filters", {}), "Filter", reasons, "Filters")
    preamp = 0.0
    if "preamp" in general:
        v = _num(general["preamp"], "PreAmp", reasons)
        preamp = v if v is not None else 0.0
    else:
        notes.append("no PreAmp entry; 0 dB assumed")

    bands = []
    for i in sorted(set(freqs) | set(gains) | set(quals) | set(filters)):
        fid = filters.get(i)
        kind = FILTER_IDS.get(fid)
        if kind is None:
            reasons.append(f"band {i}: Peace filter ID {fid} is not a supported/verified type")
            continue
        g = _num(gains[i], f"Gain{i}", reasons) if i in gains else 0.0
        if g is None:
            continue
        if g == 0:
            notes.append(f"band {i}: 0 dB {kind} is an identity filter; omitted")
            continue
        if i not in freqs or i not in quals:
            reasons.append(f"band {i}: {g:g} dB band without {'frequency' if i not in freqs else 'Q'}")
            continue
        f, q = _num(freqs[i], f"Frequency{i}", reasons), _num(quals[i], f"Quality{i}", reasons)
        if f is None or q is None:
            continue
        bands.append(Band(kind, f, g, q))
    if not bands and preamp == 0 and not reasons:
        reasons.append("no active EQ (no non-zero band and 0 dB preamp)")
    preset = Preset(name=res.name, preamp=preamp, bands=bands, source=f"peace:{path}", source_sha256=res.sha256)
    for e in validate(preset):
        reasons.append(e)
    if not reasons:
        res.preset = preset
    return res


def read_folder(folder: Path) -> list[PeaceResult]:
    return [read_profile(p) for p in sorted(Path(folder).glob("*.peace"), key=lambda p: p.name.casefold())]
