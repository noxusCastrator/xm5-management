#!/bin/bash
# build_payload.sh -- B1: compile the A1 adapter + P1 core as an address-fixed,
# NON-PIC MIPS payload and link it at the reserved vaddr 0xe10000, with every live
# libc import bound (--defsym) to its verified ORIGINAL PLT stub (out/plt-map.json).
#
# Lead linker decision honoured:
#   -mno-abicalls -fno-pic -fno-pie -G0  => no GOT, no gp dependence, direct jal.
#   O32 LE hard-float FPXX nan2008, no fast-math, no stack protector.
#   Imports resolve to PLT stubs (preserve lazy-bind/t8/t9), NOT raw GOT slots.
# No firmware is linked or mutated here; output is a standalone ELF + flat .bin.
set -eu
B1=$(cd "$(dirname "$0")/.." && pwd)
WS="${XM5_WORKSPACE:?Set XM5_WORKSPACE}"
TR="$WS/peq-lab/tasks/T1-toolchain/toolchain/root"
export LD_LIBRARY_PATH="$TR/usr/lib/x86_64-linux-gnu"
BIN="$TR/usr/bin"
GCC="$BIN/mipsel-linux-gnu-gcc-12"
LD="$BIN/mipsel-linux-gnu-ld"
RE="$BIN/mipsel-linux-gnu-readelf"
NM="$BIN/mipsel-linux-gnu-nm"
OC="$BIN/mipsel-linux-gnu-objcopy"
OD="$BIN/mipsel-linux-gnu-objdump"
A1="$B1"
IO="$B1/../player-info1"
B="$B1/build"; O="$B1/out"; EV="$B1/evidence"; mkdir -p "$B" "$O" "$EV"

LINK_VA=0xe10000

TGT="-march=mips32r2 -mabi=32 -EL -mhard-float -mfpxx -mno-odd-spreg -mnan=2008"
NOPIC="-mno-abicalls -fno-pic -fno-pie -G0 -fno-stack-protector"
CF="$TGT $NOPIC -DXM5_FREESTANDING -O2 -std=c11 -ffreestanding -ffp-contract=off -fno-math-errno"
CF="$CF -DPEQ_PLATFORM_HEADER=\"xm5_platform.h\""
CF="$CF -fno-builtin-pow -fno-builtin-sin -fno-builtin-cos"
CF="$CF -ffunction-sections -fdata-sections -Wall -Wextra"

echo "== compile (non-PIC, address-fixed target)"
"$GCC" $CF -I"$A1/src" -c "$A1/src/xm5_peq_player.c" -o "$B/adapter.o"
"$GCC" $CF -I"$A1/src" -c "$A1/src/vendor/peq.c"      -o "$B/peq.o"

"$GCC" $CF -DXM5_FREESTANDING -fno-builtin -I"$IO" -c "$IO/prepared_io.c" -o "$B/prepared_io.o"
"$GCC" $CF -fno-builtin -c "$B1/../formats/other_container.c" -o "$B/inline_containers.o"
"$GCC" $CF -c "$A1/src/stock_calls.S" -o "$B/stock_calls.o"
"$GCC" $CF -c "$A1/src/rate_gates.S" -o "$B/rate_gates.o"
echo "== build --defsym import bindings from out/plt-map.json"
DEFSYMS=$(python3 - "$O/plt-map.json" <<'PY'
import json,sys
m=json.load(open(sys.argv[1]))
for name,e in m["imports"].items():
    assert e.get("stub_bytes_verified"), name
    print(f"--defsym {name}={e['plt_stub']}")
PY
)
echo "$DEFSYMS" > "$EV/defsym_bindings.txt"

echo "== link at $LINK_VA (gc-sections rooted at xm5_peq_open)"
"$LD" -EL -G0 --gc-sections -e xm5_peq_open -u xm5_peq_open -u xm5_gate_a -u xm5_gate_b -u xm5_info_start \
  --no-warn-mismatch \
  -T "$B1/scripts/payload.lds" \
  $DEFSYMS \
  "$B/adapter.o" "$B/peq.o" "$B/prepared_io.o" "$B/inline_containers.o" "$B/stock_calls.o" "$B/rate_gates.o" -o "$B/payload.elf" -Map "$O/payload.map"

echo "== assert fully resolved (no UND, no relocs remaining)"
UND=$("$NM" -u "$B/payload.elf" || true)
if [ -n "$UND" ]; then echo "UNRESOLVED SYMBOLS:"; echo "$UND"; exit 2; fi

echo "== extract flat loadable image (.text+.rodata+.data), record .bss"
"$OC" -O binary --only-section=.text --only-section=.rodata \
  --only-section=.data "$B/payload.elf" "$O/payload.bin"

"$RE" -h "$B/payload.elf" | grep -E "Flags|Entry"
# Linker discards .MIPS.abiflags in flat payload; inspect both input objects.
"$RE" -A "$B/adapter.o" "$B/peq.o" | grep -E "ISA:|FP ABI|GPR size"
"$RE" -S -W "$B/payload.elf" | grep -E "\.text|\.rodata|\.data|\.bss" || true
echo "== entry symbol"; "$NM" "$B/payload.elf" | grep -E " xm5_peq_open$" || true
echo "== disasm: every import call site (jal into a PLT stub)"
"$OD" -d "$B/payload.elf" > "$EV/payload_disasm.txt"
grep -oE "jal[[:space:]]+ad[0-9a-f]{4} <[^>]+>" "$EV/payload_disasm.txt" | sort | uniq -c || true
echo "BUILD_OK"
