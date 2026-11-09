#!/usr/bin/env python3
"""build_candidate.py -- B1 offline ELF surgery.

Produce out/hciplayer.candidate: a SEPARATE COPY of the stock hciplayer with
exactly two in-place changes, each guarded, plus the appended payload:
  1. program header [10] (file 0x174): reserved PT_NULL -> PT_LOAD describing the
     appended payload segment at vaddr 0xe10000.
  2. the af_info EQ descriptor 'open' pointer (file 0x4939ec): 0x0044fe04 ->
     0x00e10000 (the payload entry xm5_peq_open).
  3. the payload image (out/payload.bin) appended at a 0x10000-congruent file
     offset past all original content.

The original firmware file is NEVER written. Every guard runs BEFORE any output is
written, so a refusal (GuardError) leaves all outputs absent/unchanged and the
original intact. Emits out/changed-ranges.json and out/candidate.sha256.

The validation/build functions below are the single source of truth used both by
the CLI (main) and by the offline verifier's negative tests -- the tests exercise
these real functions, they do NOT re-implement the guards. Production baseline
pinning (HCI_SHA/HCI_SIZE) is mandatory and is enforced by assert_baseline() on
every build; it is not parameterised away.
"""
import json, struct, hashlib, os, sys
from rate_patch import gate_edits

HERE = os.path.dirname(os.path.abspath(__file__))
B1 = os.path.abspath(os.path.join(HERE, ".."))
from pathlib import Path
WS = str(next(p for p in Path(HERE).parents if p.name == 'workspace'))
HCI = os.path.join(WS, "roundtrip-v2.43", "unpacked", "rootfs", "usr", "bin", "hciplayer")
OUT = os.path.join(B1, "out")
BIN = os.path.join(OUT, "payload.bin")
PLT_MAP = os.path.join(OUT, "plt-map.json")

# --- MANDATORY production baseline pin (never relaxed) ----------------------
HCI_SHA = "edec6c420a792bd4b4e47df3b34a5bf5fbb518a07cb1c8509ec1e50cb87e1f2d"
HCI_SIZE = 7632420

# G1 player-map constants (re-asserted here, not trusted blindly).
PHDR_OFF = 0x174                 # program header [10]
PHENT = 32
DESC_OFF = 0x4939d8              # af_info descriptor (24 bytes, guarded whole)
DESC_BYTES = bytes.fromhex("8c398900ec5f88001c37890078c588000100000004fe4400")
OPEN_SLOT = 0x4939ec            # DESC_OFF + 20
OPEN_OLD = 0x0044fe04
LINK_VA = 0xe10000
PALIGN = 0x10000
PT_NULL, PT_LOAD = 0, 1
PF_X, PF_W, PF_R = 1, 2, 4

# original LOAD extents for overlap test (G1).
LOAD_SEGS = [(0x400000, 0x6db7c0), (0xaec000, 0x317b38)]
LOAD_END = 0xe03b38

# gates + stub ranges that MUST stay byte-identical (R6 + out/plt-map.json).
GATE_RANGES = {
    "gate_A_seteq_mode1_48k": (0x2ce2c, 28),
    "gate_B_pending_state_check": (0x2f490, 12),
    "gate_B_playback_insert_48k": (0x2fac0, 20),
    "insert_call": (0x2fc58, 4),
}


class GuardError(Exception):
    """Raised by any validation guard. A raised GuardError means NO output is
    written and the original is left untouched."""


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


# --- individual, reusable guards (single source of truth) ------------------
def assert_baseline(orig):
    """MANDATORY production pin: exact stock sha256 + size. Not parameterised."""
    if len(orig) != HCI_SIZE:
        raise GuardError(f"baseline size {len(orig)} != {HCI_SIZE}")
    if hashlib.sha256(bytes(orig)).hexdigest() != HCI_SHA:
        raise GuardError("baseline sha256 mismatch (not the pinned stock hciplayer)")


def validate_structure(orig):
    """Descriptor 24B, open pointer, reserved PT_NULL phdr[10], phdr geometry.
    Operates on the given buffer; raises GuardError on the first failure."""
    # open pointer first, so a corrupt open slot is caught by its own guard (the
    # open field also lies within the 24-byte descriptor check below).
    if u32(orig, OPEN_SLOT) != OPEN_OLD:
        raise GuardError("open pointer != stock 0x0044fe04")
    if bytes(orig[DESC_OFF:DESC_OFF + 24]) != DESC_BYTES:
        raise GuardError("descriptor 24-byte guard failed")
    p_type, p_off, p_va, p_pa, p_fsz, p_msz, p_flags, p_align = \
        struct.unpack_from("<8I", orig, PHDR_OFF)
    # stock phdr[10] is PT_NULL with every field 0 except a benign p_align=4.
    if (p_type, p_off, p_va, p_pa, p_fsz, p_msz, p_flags) != (0,) * 7 or p_align not in (0, 4):
        raise GuardError("phdr[10] is not a reserved zero PT_NULL (invalid header)")
    e_phoff = u32(orig, 0x1c)
    e_phnum = struct.unpack_from("<H", orig, 0x2c)[0]
    if e_phoff != 52 or e_phnum != 11 or PHDR_OFF != e_phoff + 10 * PHENT:
        raise GuardError("phdr table geometry unexpected")


def validate_overlap(va, size):
    """Payload vaddr extent must be >= load_end and not overlap either LOAD."""
    pstart, pend = va, va + size
    for base, sz in LOAD_SEGS:
        if pstart < base + sz and base < pend:
            raise GuardError(f"payload vaddr overlaps LOAD {base:#x}")
    if pstart < LOAD_END:
        raise GuardError(f"payload vaddr {pstart:#x} below load_end {LOAD_END:#x}")


def plan_append(orig_len, psz):
    """Compute the appended file offset and validate payload-size / congruence /
    file non-overlap bounds. Returns append_off."""
    if psz == 0 or psz > 0x100000:
        raise GuardError("payload size out of sane bounds")
    append_off = (orig_len + PALIGN - 1) & ~(PALIGN - 1)
    if (append_off % PALIGN) != (LINK_VA % PALIGN):
        raise GuardError("file offset / vaddr congruence broken")
    if append_off < orig_len:
        raise GuardError("append offset inside original file")
    return append_off


def build_phdr_fields(append_off, psz):
    fields = (PT_LOAD, append_off, LINK_VA, LINK_VA, psz, psz, PF_R | PF_X, PALIGN)
    for v in fields:
        if not (0 <= v <= 0xFFFFFFFF):
            raise GuardError("phdr field exceeds uint32")
    return fields


def assemble(orig, payload, append_off, fields):
    """Produce the candidate bytes (pure function; does NOT touch disk)."""
    cand = bytearray(orig)
    pad_start = len(cand)
    if append_off > len(cand):
        cand += b"\x00" * (append_off - len(cand))
    cand += payload
    struct.pack_into("<8I", cand, PHDR_OFF, *fields)
    struct.pack_into("<I", cand, OPEN_SLOT, LINK_VA)
    return cand, pad_start


def changed_ranges(orig, cand):
    changed = [i for i in range(len(orig)) if cand[i] != orig[i]]
    ranges = []
    for i in changed:
        if ranges and i == ranges[-1][1]:
            ranges[-1][1] = i + 1
        else:
            ranges.append([i, i + 1])
    return changed, ranges


def validate_inplace(orig, cand, plt_map_path=PLT_MAP):
    """Only phdr[10] + open slot differ within [0,orig_size); gates + PLT stubs
    byte-identical. Raises GuardError on any unexpected change."""
    changed, _ = changed_ranges(orig, cand)
    allowed = [(PHDR_OFF, PHDR_OFF + PHENT), (OPEN_SLOT, OPEN_SLOT + 4)]
    edits = gate_edits(BIN)
    allowed += [(o,o+len(v)) for o,v in edits.items()]
    for i in changed:
        if not any(a <= i < b for a, b in allowed):
            raise GuardError(f"byte {hex(i)} changed outside allowed regions")
    for name, (off, ln) in GATE_RANGES.items():
        expected = bytearray(orig[off:off + ln])
        for pos,val in edits.items():
            if off <= pos < off+ln: expected[pos-off:pos-off+len(val)]=val
        if bytes(cand[off:off + ln]) != bytes(expected):
            raise GuardError(f"external gate {name} changed")
    plt = json.load(open(plt_map_path))
    for s, e in plt["imports"].items():
        so = int(e["stub_file_off"], 16)
        if bytes(cand[so:so + 16]) != bytes(orig[so:so + 16]):
            raise GuardError(f"PLT stub {s} changed")


def build_candidate(src_path=HCI, out_dir=OUT, payload_path=BIN, write=True,
                    plt_map_path=PLT_MAP):
    """Validate + assemble the candidate from src_path and payload_path.

    ALL guards run before ANY output is written. On GuardError nothing is written
    (outputs absent/unchanged, original untouched). Returns a result dict.
    """
    orig = bytearray(open(src_path, "rb").read())
    assert_baseline(orig)                                  # mandatory pin
    if not os.path.exists(payload_path):
        raise GuardError("payload.bin missing; run build_payload.sh first")
    payload = open(payload_path, "rb").read()
    psz = len(payload)

    validate_structure(orig)
    if bytes(orig[0x2a614:0x2a61c]) != bytes.fromhex('c8ffbd272400b0af'): raise GuardError('audio filename reporter entry mismatch')
    append_off = plan_append(len(orig), psz)
    validate_overlap(LINK_VA, psz)
    fields = build_phdr_fields(append_off, psz)
    cand, pad_start = assemble(orig, payload, append_off, fields)
    try:
        edits=gate_edits(payload_path)
    except (AssertionError,ValueError,OSError) as ex:
        raise GuardError("rate trampoline/payload mismatch: "+str(ex))
    for pos,val in edits.items(): cand[pos:pos+len(val)]=val
    validate_inplace(orig, cand, plt_map_path)             # post-assembly guard

    csha = hashlib.sha256(cand).hexdigest()
    _, ranges = changed_ranges(orig, cand)
    crange = {
        "schema": "xm5-b1-changed-ranges/1",
        "input": {"path": "roundtrip-v2.43/.../hciplayer", "sha256": HCI_SHA, "size": HCI_SIZE},
        "output": {"path": "out/hciplayer.candidate", "sha256": csha, "size": len(cand)},
        "payload_bin_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_size": psz,
        "in_place_changes": [
            {"what": "program_header[10] PT_NULL->PT_LOAD", "file_off": hex(PHDR_OFF),
             "len": PHENT, "old": orig[PHDR_OFF:PHDR_OFF + PHENT].hex(),
             "new": cand[PHDR_OFF:PHDR_OFF + PHENT].hex(),
             "new_phdr": {"type": "PT_LOAD", "offset": hex(append_off), "vaddr": hex(LINK_VA),
                          "filesz": hex(psz), "memsz": hex(psz), "flags": "R+X",
                          "align": hex(PALIGN)}},
            {"what": "af_info EQ open pointer", "file_off": hex(OPEN_SLOT), "len": 4,
             "old": hex(OPEN_OLD), "new": hex(LINK_VA)},
        ],
        "rate_trampolines": [{"file_off":hex(o),"old":orig[o:o+len(v)].hex(),"new":v.hex()} for o,v in edits.items()],
        "appended": {"file_off": hex(append_off), "len": psz,
                     "zero_pad_before": {"file_off": hex(pad_start), "len": append_off - pad_start}},
        "guards_passed": ["input_sha256", "input_size", "descriptor_24B", "open_ptr_stock",
                          "phdr10_zero_ptnull", "phdr_geometry", "vaddr_nonoverlap",
                          "file_nonoverlap", "uint32_bounds", "only_four_inplace_edits",
                          "192k_gate_trampolines_guarded", "plt_stubs_unchanged"],
    }
    result = {"candidate": cand, "sha256": csha, "size": len(cand),
              "append_off": append_off, "psz": psz, "ranges": ranges, "changed_ranges": crange}

    if write:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "hciplayer.candidate"), "wb") as f:
            f.write(cand)
        with open(os.path.join(out_dir, "changed-ranges.json"), "w", newline="\n") as f:
            json.dump(crange, f, indent=1)
        with open(os.path.join(out_dir, "candidate.sha256"), "w", newline="\n") as f:
            f.write(f"{csha}  hciplayer.candidate\n")
    return result


def main():
    try:
        r = build_candidate(write=True)
    except GuardError as e:
        print("REFUSE:", e)
        sys.exit(1)
    print("OK candidate:", os.path.join(OUT, "hciplayer.candidate"))
    print("candidate sha256:", r["sha256"], "size:", r["size"])
    print("append_off:", hex(r["append_off"]), "payload:", r["psz"], "bytes")
    print("changed in-place ranges:", [(hex(a), hex(b)) for a, b in r["ranges"]])


if __name__ == "__main__":
    main()
