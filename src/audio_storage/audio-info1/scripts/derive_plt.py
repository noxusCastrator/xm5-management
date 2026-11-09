#!/usr/bin/env python3
"""derive_plt.py -- derive, from the stock hciplayer alone, the PLT stub address
for each libc symbol the payload imports, and verify the full 16-byte stub
instruction pattern and GOTPLT slot. Output: out/plt-map.json.

No toolchain needed: parses the ELF (.rel.plt via dynamic DT_JMPREL) directly and
decodes each MIPS32 stub by hand. The stock binary is opened read-only.

PLT model (lead, re-derived here):
  PLT0  @ 0x00ada5a0 (0x20 bytes) lazy resolver; sets gp itself, reads t8.
  stub  @ 0x00ada5c0 + N*16 for the N-th .rel.plt JUMP_SLOT entry:
        lui   t7, %hi(slot)       3c0f00b0
        lw    t9, %lo(slot)(t7)   8df9....
        jr    t9                  03200008
        addiu t8, t7, %lo(slot)   25f8....   (delay slot: t8 = &slot, for PLT0)
  slot  @ GOTPLT = 0x00af9644 + N*4  (== the .rel.plt r_offset).
Calling the stub (not the raw slot) preserves lazy binding (PLT0 needs t8) and
sets t9 correctly; the stub is self-contained (sets its own t7/t8, PLT0 sets gp).
"""
import json, struct, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
from pathlib import Path
WS = str(next(p for p in Path(HERE).parents if p.name == 'workspace'))
HCI = os.path.join(WS, "roundtrip-v2.43", "unpacked", "rootfs", "usr", "bin", "hciplayer")
HCI_SHA = "edec6c420a792bd4b4e47df3b34a5bf5fbb518a07cb1c8509ec1e50cb87e1f2d"

# LOAD[4]: file off 0 -> vaddr 0x400000 (R-X). Valid for every address we touch.
TEXT_VA = 0x400000
PLT0 = 0x00ada5a0
PLT_FIRST = 0x00ada5c0
GOTPLT0 = 0x00af9644      # r_offset of .rel.plt[0]
PLT_STRIDE = 16

# Symbols the gc-sections payload imports (from A1 import-manifest + adapter src).
NEEDED = ["memset","memcpy","sin","cos","strtod","__pow_finite",
          "calloc","free","strcmp","strncmp","open64","read","close","strlen","strchr"]

def va2off(va):
    return va - TEXT_VA

def main():
    import hashlib
    data = open(HCI, "rb").read()
    sha = hashlib.sha256(data).hexdigest()
    assert sha == HCI_SHA, f"hciplayer hash mismatch: {sha}"

    # --- minimal ELF32 LE parse to locate .dynamic, then DT_JMPREL/.dynsym/.dynstr
    e_phoff = struct.unpack_from("<I", data, 0x1c)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x2a)[0]
    e_phnum = struct.unpack_from("<H", data, 0x2c)[0]
    dyn_off = dyn_va = None
    for i in range(e_phnum):
        p = e_phoff + i*e_phentsize
        p_type, p_off, p_va = struct.unpack_from("<III", data, p)
        if p_type == 2:  # PT_DYNAMIC
            dyn_off, dyn_va = p_off, p_va
    assert dyn_off is not None
    DT = {}
    off = dyn_off
    while True:
        tag, val = struct.unpack_from("<iI", data, off)
        off += 8
        if tag == 0: break
        DT[tag] = val
    jmprel = va2off(DT[0x17])          # DT_JMPREL (.rel.plt)
    pltrelsz = DT[2]                   # DT_PLTRELSZ bytes
    symtab = va2off(DT[6])             # DT_SYMTAB (.dynsym)
    strtab = va2off(DT[5])             # DT_STRTAB (.dynstr)
    syment = DT[11]                    # DT_SYMENT (16)

    def symname(idx):
        st_name = struct.unpack_from("<I", data, symtab + idx*syment)[0]
        end = data.index(b"\0", strtab + st_name)
        return data[strtab+st_name:end].decode()

    # --- walk .rel.plt: r_offset, r_info -> (name, index)
    plt_by_name = {}
    n = pltrelsz // 8
    for i in range(n):
        r_off, r_info = struct.unpack_from("<II", data, jmprel + i*8)
        symidx = r_info >> 8
        rtype = r_info & 0xff
        assert rtype == 127, f"entry {i} not JUMP_SLOT"   # R_MIPS_JUMP_SLOT=127
        name = symname(symidx)
        base = name.split("@")[0]
        stub = PLT_FIRST + i*PLT_STRIDE
        assert r_off == GOTPLT0 + i*4, f"gotplt slot mismatch at {i}"
        plt_by_name.setdefault(base, {"index": i, "stub": stub, "gotplt": r_off,
                                      "full": name})

    def decode_stub(stub, gotplt):
        o = va2off(stub)
        w = struct.unpack_from("<IIII", data, o)
        # expected pattern
        hi = (gotplt >> 16) & 0xffff
        lo = gotplt & 0xffff
        if lo & 0x8000: hi = (hi + 1) & 0xffff      # %hi carry for %lo sign
        exp0 = 0x3c0f0000 | hi                        # lui  t7, hi
        exp1 = 0x8df90000 | lo                        # lw   t9, lo(t7)
        exp2 = 0x03200008                             # jr   t9
        exp3 = 0x25f80000 | lo                        # addiu t8, t7, lo
        ok = (w[0]==exp0 and w[1]==exp1 and w[2]==exp2 and w[3]==exp3)
        return ok, [f"{x:08x}" for x in w], [f"{x:08x}" for x in (exp0,exp1,exp2,exp3)]

    out = {"schema":"xm5-b1-plt-map/1","target":{"path":"roundtrip-v2.43/.../hciplayer",
           "sha256":HCI_SHA}, "plt0":f"0x{PLT0:08x}", "plt_first":f"0x{PLT_FIRST:08x}",
           "gotplt_first":f"0x{GOTPLT0:08x}", "stride":PLT_STRIDE, "imports":{}}
    allok = True
    for s in NEEDED:
        if s not in plt_by_name:
            out["imports"][s] = {"error":"NOT A STOCK PLT IMPORT"}; allok=False; continue
        e = plt_by_name[s]
        ok, got_words, exp_words = decode_stub(e["stub"], e["gotplt"])
        allok = allok and ok
        out["imports"][s] = {"full_symbol":e["full"],"rel_plt_index":e["index"],
            "plt_stub":f"0x{e['stub']:08x}","gotplt_slot":f"0x{e['gotplt']:08x}",
            "stub_file_off":f"0x{va2off(e['stub']):x}","stub_bytes_verified":ok,
            "stub_words":got_words,"expected_words":exp_words}
    out["all_stubs_verified"] = allok
    outdir = os.path.join(HERE, "..", "out"); os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "plt-map.json"), "w", newline="\n") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))
    print("ALL_STUBS_VERIFIED:", allok)
    return 0 if allok else 1

if __name__ == "__main__":
    sys.exit(main())
