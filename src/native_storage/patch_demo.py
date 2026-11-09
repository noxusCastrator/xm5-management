"""Extend PEQ08's existing load segment; preserve its code/data/zero BSS.
Produces an OFFLINE EXPERIMENT, not a ready-to-install firmware package.
"""
from pathlib import Path
import hashlib,json,struct

HERE=Path(__file__).resolve().parent
TASKS=next(p for p in HERE.parents if p.name=='workspace')/'peq-lab/tasks'
BASELINE=TASKS/'U9-four-row-peq-ui/out/demo'
PIN='e3596659301a04e61882506c0ff2c48013866ca800ceff5e67c531486feb7df3'
BASE=0x900000
def sha(b):return hashlib.sha256(b).hexdigest()
def va_offset(b,va,n=4):
    off=struct.unpack_from('<I',b,28)[0];stride,count=struct.unpack_from('<HH',b,42)
    for i in range(count):
        typ,start,addr,_,size,*_=struct.unpack_from('<8I',b,off+i*stride)
        if typ==1 and addr<=va and va+n<=addr+size:return start+va-addr
    raise ValueError(f'No file-backed mapping for {va:#x}+{n}')
def apply(b,p):
    assert sha(b)==p['baseline_sha256']
    out=bytearray(b)
    for h in p['hunks']:
        at=h['offset'];old=bytes.fromhex(h['before']);new=bytes.fromhex(h['after'])
        assert out[at:at+len(old)]==old
        out[at:at+len(old)]=new
    assert sha(out)==p['modified_sha256'];return bytes(out)
def main():
    b=BASELINE.read_bytes();assert sha(b)==PIN
    symbols={}
    for line in (HERE/'build/art-payload.sym').read_text().splitlines():
        parts=line.split()
        if len(parts)==3:symbols[parts[2]]=int(parts[0],16)
    payload=(HERE/'build/art-payload.bin').read_bytes();assert len(payload)>0
    old=struct.unpack_from('<8I',b,0x34+10*32)
    typ,off,va,pa,filesz,memsz,flags,align=old
    assert (typ,va,flags,align)==(1,0x820000,7,0x1000)
    assert off+filesz==len(b) and va+memsz<BASE
    newoff=off+BASE-va
    end=BASE+len(payload);memend=symbols['__art_mem_end']
    assert BASE+len(payload)<=symbols['__art_file_end']<=memend<0xA00000
    hunks=[]
    def change(at,before,after,label):
        assert b[at:at+len(before)]==before,label
        hunks.append(dict(offset=at,before=before.hex(),after=after.hex(),label=label))
    newph=struct.pack('<8I',typ,off,va,pa,end-va,memend-va,flags,align)
    change(0x34+10*32,struct.pack('<8I',*old),newph,'extend PEQ08 load segment')
    entries=[(0x4d7ae4,0x3c1c0033,0x279cd9ec,'pa_set_play'),
             (0x53a81c,0x3c1c002d,0x279cacb4,'pa_folder_row'),
             (0x53d548,0x3c1c002c,0x279c7f88,'pa_folder_play'),
             (0x545f10,0x3c1c002c,0x279cf5c0,'pa_jump_play'),
             (0x54ad18,0x3c1c002c,0x279ca7b8,'pa_power_play'),
             (0x64f6a0,0x3c1c001b,0x279c5e30,'pa_image_set'),
             (0x64f5dc,0x3c1c001b,0x279c5ef4,'pa_image_destroy'),
             (0x650e38,0x3c1c001b,0x279c4698,'pa_manager_destroy'),
             (0x628074,0x3c1c001e,0x279cd45c,'pa_image_paint'),
             (0x4ed448,0x3c1c0032,0x279c8088,'pa_playlist_save'),
             (0x5cc094,0x3c1c0024,0x279c943c,'pa_set_eq'),
             (0x69e09c,0x3c1c0016,0x279c7434,'pa_loader_load'),
             (0x69e084,0x3c1c0016,0x279c744c,'pa_loader_exist')]
    entries.extend(json.loads((HERE/'EXTRA-HOOKS.json').read_text(encoding='utf-8')))
    for va,one,two,name in entries:
        at=va_offset(b,va,12);assert struct.unpack_from('<I',b,at+8)[0]==0x0399e021
        j=0x08000000|((symbols[name]>>2)&0x3ffffff)
        change(at,struct.pack('<2I',one,two),struct.pack('<2I',j,0),f'{va:#x} -> {name}')
    # nf_init is freestanding, not a PIC gp prologue; its thunk replays exactly
    # the displaced stack/register-save words. Only UI file routing changes.
    at=va_offset(b,0x824768,8)
    change(at,struct.pack('<2I',0x27bdffe0,0x8fac0030),struct.pack('<2I',0x08000000|((symbols['pa_nf_init']>>2)&0x3ffffff),0),'nf_init -> managed PEQ folder with legacy fallback')
    for va,word,name,delay in [(0x4d85a8,0x0320f809,'pa_worker_play',None),
                               (0x4de180,0x0411fcc7,'pa_worker_folder',0x02002025),
                               (0x4d83e8,0x0320f809,'pa_worker_same',None),
                               (0x6508c4,0x0411fdff,'pa_decode_entry',None),
                               (0x6508e0,0x0411feb9,'pa_add',None),
                               (0x628158,0x0320f809,'pa_need_transform',None),
                               (0x628324,0x04116ee7,'pa_canvas_image',None),
                               (0x5cce68,0x0320f809,'pa_lyric_path',None),
                               (0x4ed9c0,0x0320f809,'pa_playlist_list',None),
                               (0x4ed734,0x0320f809,'pa_playlist_mkdir',None),
                               (0x4c95b8,0x0320f809,'pa_usb_system',None),
                               (0x55262c,0x0320f809,'pa_usb_system',None),
                               (0x4edb18,0x0320f809,'pa_parse_playlist_entry',None)]:
        at=va_offset(b,va,8);j=0x0c000000|((symbols[name]>>2)&0x3ffffff)
        change(at,struct.pack('<I',word),struct.pack('<I',j),f'{va:#x} -> {name}')
        if delay is not None:change(at+4,b'\0'*4,struct.pack('<I',delay),'pass immutable worker path in a0')
    # Original custom-list image constructor: a0 is its new anonymous widget;
    # s2 is the immutable row index, not a class-record id.
    at=va_offset(b,0x548bcc,8)
    change(at,struct.pack('<2I',0x0320f809,0x24a58ca8),
           struct.pack('<2I',0x0c000000|((symbols['pa_custom_playlist_cover']>>2)&0x3ffffff),0x02402825),
           'custom playlist image -> prepared cover, row index in a1')
    hunks.sort(key=lambda x:x['offset'])
    for a,c in zip(hunks,hunks[1:]):assert a['offset']+len(bytes.fromhex(a['before']))<=c['offset']
    extra=b'\0'*(newoff-len(b))+payload
    hunks.append(dict(offset=len(b),before='',after=extra.hex(),label='zero-backed original PEQ BSS/gap, new code'))
    out=bytearray(b)
    for h in hunks:out[h['offset']:h['offset']+len(bytes.fromhex(h['before']))]=bytes.fromhex(h['after'])
    result=dict(kind='integrated inline storage firmware application',baseline=str(BASELINE),
                baseline_sha256=PIN,modified_sha256=sha(out),payload_sha256=sha(payload),
                native_base=hex(BASE),native_mem_end=hex(memend),hunks=hunks)
    assert apply(b,result)==out
    folder=HERE/'out';folder.mkdir(exist_ok=True)
    (folder/'demo').write_bytes(out)
    (folder/'DIFF_FILE.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    summary={k:v for k,v in result.items() if k!='hunks'}
    summary.update(bytes=len(out),patched_sites=[{k:v for k,v in h.items() if k not in ('before','after')} for h in hunks],
                   preserved_peq_file_bytes=filesz,preserved_peq_zero_bss_bytes=memsz-filesz)
    (folder/'CANDIDATE.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,indent=2))
if __name__=='__main__':main()
