"""Bind the real stock owner destructor to the new insertion adapter fixture."""
from pathlib import Path
import hashlib,json
import argparse
parser = argparse.ArgumentParser(description='Extract guarded stock bytes into an owned native-test build')
parser.add_argument('--peq08-demo', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
HERE = args.output.resolve()
(HERE/'build').mkdir(parents=True, exist_ok=True)
source = args.peq08_demo.resolve()
data=source.read_bytes()
assert hashlib.sha256(data).hexdigest()=='e3596659301a04e61882506c0ff2c48013866ca800ceff5e67c531486feb7df3'
lo,hi=0x6501cc,0x6502b8
body=data[lo-0x400000:hi-0x400000]
(HERE/'build/owner.bin').write_bytes(body)
(HERE/'build/bitmap.bin').write_bytes(data[0x63ab44-0x400000:0x63ac44-0x400000])
got={-10492:'0x63d9c4',-15260:'stock_free',-14728:'0x63ab44',-32692:'owner_quiet',
     -3004:'owner_noop',-4240:'owner_noop',-3996:'owner_stdout',-32720:'0x760000',
     -3288:'memset',-11264:'0x64e320'}
lines=['.set noreorder','.option pic0','.section .native_owner,"ax",@progbits','.incbin "build/owner.bin"',
       '.section .owner_size,"ax",@progbits','j stock_bitmap_size','nop',
       '.section .owner_bitmap,"ax",@progbits','.incbin "build/bitmap.bin"',
       '.section .owner_graphic,"ax",@progbits','j test_graphic_destroy','nop',
       '.text','.globl stock_bitmap_destroy','stock_bitmap_destroy:','lui $25,0x63','ori $25,$25,0xab44','jr $25','nop',
       '.text','.globl native_owner_drop','native_owner_drop:','lui $25,0x65','ori $25,$25,0x1cc','jr $25','nop',
       '.globl owner_quiet','owner_quiet:','jr $31','li $2,1','.globl owner_noop','owner_noop:','jr $31','move $2,$0',
       '.data','owner_stdout:','.word 0','.section .owner_got,"aw",@progbits']
offset=0
for key,symbol in sorted(got.items()):
    dest=0x8054d0+key-0x7fd000
    lines += [f'.space {dest-offset}',f'.word {symbol}'];offset=dest+4
(HERE/'build/owner.S').write_text('\n'.join(lines)+'\n')
(HERE/'build/OWNER-ORIGIN.json').write_text(json.dumps(dict(source=str(source),start=hex(lo),end=hex(hi),bytes=len(body),sha256=hashlib.sha256(body).hexdigest()),indent=2)+'\n')
