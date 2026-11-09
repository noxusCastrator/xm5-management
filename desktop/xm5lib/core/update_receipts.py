"""Recognize only exact native playlist bytes explicitly imported by the user."""
import re
from pathlib import PurePosixPath

def inspect_update(root, acknowledged_playlists=()):
    from . import card_library as card
    result=card.inspect_destination(root)
    if result.get('clean') or not acknowledged_playlists:return result
    if not result.get('recognized') or result.get('version')!=2 or not result.get('verification',{}).get('ok'):return result
    accepted={}
    for record in acknowledged_playlists:
        relative=card.safe_relative(record['path']);p=PurePosixPath(relative)
        digest=record['sha256']
        if p.parent.as_posix()!='Struct/playlists' or p.suffix.lower() not in ('.m3u','.m3u8') or not re.fullmatch('[0-9a-f]{64}',digest):
            raise ValueError('Only exact imported native playlist bytes can be acknowledged')
        if relative in accepted and accepted[relative]!=digest:raise ValueError('Conflicting playlist receipts')
        _,actual=card.file_digest(card.checked_path(root,relative))
        if actual!=digest:return result
        accepted[relative]=digest
    unknown=[name for name in result['unknown_files'] if name not in accepted]
    drift=[record for record in result['drifted_files'] if not isinstance(record,dict) or record.get('path') not in accepted]
    result=dict(result,unknown_files=unknown,drifted_files=drift,acknowledged_playlists=accepted)
    result['clean']=not unknown and not drift and not result['unknown_directories']
    return result
