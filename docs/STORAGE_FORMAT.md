# Card information format

The application owns one folder on the destination:

```text
xm5/
  music/                         Original source-folder structure and audio copies
  EQ/                            Compatible PEQ text presets
  Struct/
    tracks.tsv                   Rebuildable presence index
    settings.ini                 Optional peak-correction settings
    library.json                 Desktop export/ownership record
    playlists/*.m3u8             Relative playlists, including Favorites.m3u8
    playlist-covers/              Prepared playlist pictures and retained originals
    lyrics/                      Retained imported lyric originals
    peq/                         Converted firmware-compatible presets
    recovery/RESTORE_LIBRARY.py   Standalone copy-out tool
```

Music and EQ files remain ordinary files. Audio frames, original tags and original pictures are retained. Added data is attached only to explicitly selected card files or exported copies. The original computer sources are not rewritten by export. Other files on the card are outside this managed folder.

Each prepared song contains its own `XM5I` information record. The player verifies the actual record before using it. `Struct/tracks.tsv` is a fast hint, not a second copy of the pictures or an authority that can override the song. It has sorted, unique UTF-8 lines `1<TAB>card-relative-path<LF>` for present records or `0<TAB>path<LF>` for unprepared files. Rebuilding the index does not require a permanent song ID. A missing index disables prepared browsing until rebuilt; it must not reactivate the original large-cover conversion path.

Directory artwork uses the first descendant with a valid prepared image at the requested size. The 0.2.2 reader advances this search in slices of at most 64 candidates. An unfinished scan returns pending and shows the ordinary icon; it does not report a completed miss or invoke full-size artwork conversion. Thirty-two small scan jobs retain progress. Completed results are reconsidered after five seconds or 64 reuses, and a selected image is verified before reuse. Closing/changing the card or replacing its index invalidates progress. Both hint values remain eligible for verification. This bounds candidates per call, not elapsed filesystem latency; a single OS read may still block.

Within a common information record, all offsets below are zero-based bytes relative to its `XM5I` marker:

| Offset | Length | Content |
| --- | --- | --- |
| 0 | 4 | ASCII `XM5I` |
| 4 | 98,304 | 330-square JPEG slot |
| 98,308 | 32,768 | 160-square JPEG slot |
| 131,076 | 12,288 | 80-square JPEG slot |
| 143,364 | 4 | Signed little-endian sample peak, milli-dBFS |
| 143,368 | 4 | Signed little-endian peak-to-average difference, milli-dB |
| 143,372 | Up to 131,072 | Optional UTF-8 LRC text |

Each picture slot begins with a four-byte unsigned little-endian JPEG length, followed by that JPEG and zero padding. A zero length means no prepared picture. Images are baseline JPEGs of the exact square dimensions. `INT32_MIN` means the associated numeric field is absent. Missing peak means zero correction, not an assumed audio level.

For FLAC, the second metadata block is an APPLICATION block after the required 34-byte STREAMINFO block. Its block header starts at absolute byte 42 and its `XM5I` marker starts at byte 46. The player can read this canonical location directly. This absolute offset is specific to FLAC. Other supported profiles locate the same common record in their normal metadata containers: RIFF WAVE `XM5I` chunks, AIFF `APPL`, MP3 ID3 PRIV owner `org.xm5.inline`, and nonfragmented M4A/MP4 UUID boxes. Unsupported profiles and offset-sensitive metadata are left unchanged and reported as unprepared. These adapters do not support every possible container variant.

Directory pictures use a deterministic first descendant with valid prepared art. Playback pictures always belong to the playing song. Equal pictures in different songs remain separate records. Original native widgets use several dimensions; small requests select the 80-pixel record, medium requests select 160 and larger requests select 330. Widgets can scale these small images to their actual dimensions. Original full-resolution picture decoding is excluded from this display path.

Playlist covers are separate from song records. `<stem>.80.jpg`, `<stem>.160.jpg` and `<stem>.330.jpg` in `Struct/playlist-covers/` correspond to the M3U8 filename stem. The original selected cover remains under `playlist-covers/original/`.

M3U8 entries are relative to the playlist's own directory. Order and duplicate entries are meaningful. A reference may use `..` to reach `music`, but it must resolve within the card. Missing valid relative references remain available for editing and are skipped during playback. Unsafe or escaping paths reject desktop intake and leave the input file unchanged. Favorites are a special playlist with exact-path deduplication. No claim is made that a renamed or moved file retains its old playlist links. `#PLAYLIST:` stores the editable display name independently of the filename.

Songs added directly to a card can return as references without PC audio copies. The desktop retains their canonical card-relative paths as unresolved resources. Later exports include those M3U8 references, even when the selected destination has no corresponding audio. A reference does not make the application an owner of that audio file. The ownership/hashes in `library.json` apply only to files exported or explicitly prepared by the application.

Optional `settings.ini` keys use integer milli-dB:

```ini
gain_midpoint_mdb=0
gain_limit_mdb=4000
```

Allowed midpoint is -60,000 through 0; allowed limit is 0 through 12,000. Gain is `clamp(midpoint - original_peak, -limit, limit) / 1000` dB. This uses original sample peak. It does not measure LUFS, add a compressor or account for PEQ gain. Lyrics and peak data may be absent independently.

Preparation uses a new temporary file and publishes it only after validation. A failed or interrupted write leaves the original song intact. Index rebuilding and later playback can recover from stale hints. This does not replace a backup or protect a card from external corruption.

Fresh export prepares a new isolated tree. Updates use file-level deltas: new/changed files and generated metadata are staged, unchanged audio remains in place, and only replaced preimages move to `.xm5-before-<transaction>/xm5/`. The working directory is `.xm5-library-<transaction>` at card root. Legacy fresh-export work can also use `.xm5-export-*`. The paired firmware skips these generated root directories so it does not scan retained backup audio.

Backups are retained deliberately and consume card space. `storage_transactions(folder)` in the application console, or the corresponding `storage.transactions` query, lists retained transaction/backup paths. Normal synchronization does not prune them. Keep stages, backups and journals together until any interrupted transaction is resolved; deleting a referenced backup can block rollback. This release adds no automatic backup cleanup.

Publication keeps `Struct/INCOMPLETE` until the generation is complete. `.xm5-activation.json` points to a transaction with a checksummed frozen plan and a compact mutable journal. Recovery rejects mutable fields that try to replace the frozen operation list. Exact preimage/staged hashes support resume or rollback after an interrupted rename, without source drives. An external edit blocks recovery rather than being overwritten. Backups and interrupted transactions are not automatically deleted. This is application-level recovery, not a guarantee against filesystem or flash corruption.

The normal exporter creates XM5I. Historical INFO02/XMI1 cards retain their own handlers and layout; an explicit low-level legacy format plan selects that exporter. Inline preparation refuses a legacy tree instead of mixing formats.
