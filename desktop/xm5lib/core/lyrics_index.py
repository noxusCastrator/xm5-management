"""PC compiler for immutable, mount-bound native LRC lookup. No source edits."""
import hashlib
import json
import struct
import zlib
from pathlib import Path,PurePosixPath
MAGIC=b'XM5LRC1\0'
READY=b'XLRCRDY1'
MAX_INDEX=16*1024*1024
MAX_LRC=1024*1024

def _relative(value):
    if not isinstance(value,str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('Invalid lyric index path')
    p=PurePosixPath(value)
    if p.is_absolute() or any(x in ('','..','.') for x in value.split('/')):
        raise ValueError('Unsafe lyric index path')
    return value

def compile_index(tracks,entries,build_id):
    maps=[e for e in entries if e.get('kind')=='lyrics-map']
    if len(maps)>1:raise ValueError('Ambiguous lyric association maps')
    associations=[]
    if maps:
        blob=Path(maps[0]['source']).read_bytes()
        if len(blob)>MAX_INDEX or hashlib.sha256(blob).hexdigest()!=maps[0]['sha256']:
            raise ValueError('Lyric association map changed')
        document=json.loads(blob)
        if document.get('schema')!='xm5.lyrics-associations' or document.get('version')!=1:
            raise ValueError('Unsupported lyric association map')
        associations=document['associations']
    selected={}
    for record in associations:
        if not record.get('selected'):continue
        tid=record['track_id']
        if tid in selected:raise ValueError('Ambiguous selected lyric source')
        selected[tid]=record
    files={}
    for e in entries:
        if e.get('kind')=='lyrics-rendered':
            rel=_relative(e['relative'])
            if rel in files and files[rel]['sha256']!=e['sha256']:raise ValueError('Conflicting lyric identity')
            files[rel]=e
    rows=[]
    for track in tracks:
        record=selected.get(track['id'])
        if not record:continue
        rel=_relative(record['normalized_path'])
        if not rel.endswith('.lrc'):continue # Plain text stays available to the desktop.
        e=files.get(rel)
        if e is None or not 0<e['size']<=MAX_LRC:raise ValueError('Selected LRC missing or too large')
        subject=_relative('xm5/'+track['path']);asset=_relative('xm5/'+rel)
        if not asset.startswith('xm5/Struct/lyrics/') or len(subject.encode())>=1024 or len(asset.encode())>=1024:
            raise ValueError('Native lyric path outside bounds')
        rows.append((hashlib.sha256(subject.encode()).digest(),subject,asset,bytes.fromhex(e['sha256']),e['size']))
    rows.sort();keys=[x[0] for x in rows]
    if len(keys)!=len(set(keys)) or len(rows)>100000:raise ValueError('Duplicate or excessive lyric subjects')
    pool=bytearray();intern={};pool_start=64+112*len(rows)
    def string(value):
        b=value.encode('utf-8')
        if b not in intern:intern[b]=(pool_start+len(pool),len(b));pool.extend(b)
        return intern[b]
    records=bytearray()
    for key,subject,asset,digest,size in rows:
        so,sl=string(subject);ao,al=string(asset)
        records.extend(struct.pack('<32s4I32s2I24s',key,so,sl,ao,al,digest,size,0,b'\0'*24))
    total=pool_start+len(pool)
    if total>MAX_INDEX:raise ValueError('Native lyric index too large')
    build=bytes.fromhex(build_id)
    if len(build)!=16:raise ValueError('Invalid build identity')
    header=struct.pack('<8s8I16s8s',MAGIC,1,64,112,len(rows),64,pool_start,len(pool),total,build,b'\0'*8)
    data=header+records+pool
    ready=struct.pack('<8s3I32s2I',READY,1,64,len(data),hashlib.sha256(data).digest(),1,0)
    ready+=struct.pack('<I',zlib.crc32(ready))
    assert len(header)==64 and len(ready)==64
    return {'index':data,'ready':ready,'records':len(rows),'index_path':'Struct/indexes/lyrics.bin','ready_path':'Struct/indexes/lyrics.ready'}
