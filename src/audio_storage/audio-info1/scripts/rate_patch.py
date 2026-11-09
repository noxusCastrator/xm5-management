"""Lead-reviewed two gate trampolines, tied to exact compiled payload bytes."""
from pathlib import Path
import struct
LINK=0xe10000
CODE={
 'xm5_gate_a':[0x3c0200b0,0x8c429e30,0x10400008,0,0x8c42002c,0x10400005,0,
               0x8c42003c,0x3c030002,0x3463ee01,0x0043102a,0x0810b391,0],
 'xm5_gate_b':[0x8e649e30,0x00001025,0x10800008,0,0x8c82002c,0x10400005,0,
               0x8c42003c,0x3c030002,0x3463ee01,0x0043102a,0x0810beb4,0]}
OFFSETS={'xm5_gate_a':0x2ce2c,'xm5_gate_b':0x2fac0}
def symbols(elf):
 b=Path(elf).read_bytes()
 assert b[:7]==b'\x7fELF\x01\x01\x01'
 shoff=struct.unpack_from('<I',b,32)[0];size,num=struct.unpack_from('<HH',b,46)
 sections=[struct.unpack_from('<10I',b,shoff+size*i) for i in range(num)]
 found={}
 for s in sections:
  if s[1]!=2:continue
  strings=sections[s[6]];table=b[strings[4]:strings[4]+strings[5]]
  for pos in range(s[4],s[4]+s[5],s[9]):
   n,value,length,info,other,index=struct.unpack_from('<IIIBBH',b,pos)
   name=table[n:table.find(b'\0',n)].decode('ascii')
   if name in CODE or name=='xm5_info_start':found[name]=value
 assert set(found)==set(CODE)|{'xm5_info_start'}
 return found
def gate_edits(payload_path):
 p=Path(payload_path);image=p.read_bytes()
 targets=symbols(p.parent.parent/'build/payload.elf')
 edits={}
 for name,va in targets.items():
  if name=='xm5_info_start':
   edits[0x2a614]=struct.pack('<II',0x08000000|(va>>2),0);continue
  expected=struct.pack('<'+'I'*len(CODE[name]),*CODE[name])
  assert va>=LINK and va+len(expected)<=LINK+len(image),name
  assert image[va-LINK:va-LINK+len(expected)]==expected,name
  assert va%4==0
  edits[OFFSETS[name]]=struct.pack('<II',0x08000000|(va>>2),0)
 return edits
