"""Frozen case-sensitive card-library v2 paths and read-only legacy detection."""
from pathlib import Path
from .restore_card import detect_layout, playlist_files, safe_relative

CONTAINER = "xm5"
MUSIC = "music"
PEQ_ORIGINALS = "EQ"
STRUCT = "Struct"
MANIFEST = "Struct/manifest.json"
CHECKSUM = "Struct/manifest.sha256"
INCOMPLETE = "Struct/INCOMPLETE"
ART = "Struct/covers/native"
ART_INDEX = "Struct/indexes/art-index.bin"
ART_READY = "Struct/indexes/art.ready"
ART_TSV = "Struct/indexes/art-index.tsv"
PLAYLISTS = "Struct/playlists"
PEQ_DERIVED = "Struct/peq"
LYRICS = "Struct/lyrics"
RESTORE_TOOL = "Struct/recovery/RESTORE_LIBRARY.py"
INBOX = "Struct/inbox"

def destination_for(parent):
    return Path(parent).absolute() / CONTAINER

def card_path(container_relative):
    return CONTAINER + "/" + safe_relative(container_relative)
