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
b=source.read_bytes();assert hashlib.sha256(b).hexdigest()=='e3596659301a04e61882506c0ff2c48013866ca800ceff5e67c531486feb7df3'
body=b[0x628074-0x400000:0x62833c-0x400000];(HERE/'build/paint.bin').write_bytes(body)
got={-11968:'0x628030',-15416:'0x6448d4',-14140:'test_image_cast',-14136:'test_helper',-22216:'test_load',-14132:'0x64fcfc',-15412:'test_rect',-2476:'memcpy',-14280:'0x643ec4'}
lines=['.set noreorder','.option pic0','.section .native_paint,"ax",@progbits','.incbin "build/paint.bin"']
for sec,fn in [('paint_cast','test_image_cast'),('paint_vg','test_vg'),('paint_transform','pa_need_transform'),('paint_draw','pa_canvas_image')]:
 lines +=[f'.section .{sec},"ax",@progbits',f'j {fn}','nop']
lines +=['.text','.globl native_image_paint','native_image_paint:','lui $25,0x62','ori $25,$25,0x8074','jr $25','nop','.section .paint_got,"aw",@progbits']
offset=0
for key,symbol in sorted(got.items()):
 dest=0x8054d0+key-0x7fd000
 if dest>offset:lines +=[f'.space {dest-offset}']
 lines +=[f'.word {symbol}'];offset=dest+4
(HERE/'build/paint.S').write_text('\n'.join(lines)+'\n')
(HERE/'build/PAINT-ORIGIN.json').write_text(json.dumps(dict(source=str(source),start='0x628074',end='0x62833c',bytes=len(body),sha256=hashlib.sha256(body).hexdigest()),indent=2)+'\n')
