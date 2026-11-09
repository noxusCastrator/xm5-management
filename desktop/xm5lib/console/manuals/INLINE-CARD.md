# Prepare music for the inline firmware

Use these commands in the existing application console. Register the selected music, EQ and export folders through the application's folder controls first.

```text
export_inline_card("I:/", music=["D:/Music/Collection"], eq=["D:/EQ/Headphones"])
organize_card("I:/", ["xm5/music/Collection/Album/Track.flac"])
rebuild_card_index("I:/")
```

`export_inline_card` copies each selected root into a new `xm5` tree below the destination parent, prepares the supported copied tracks and builds the index. An existing `xm5` entry blocks this operation. Original source folders and unrelated destination files remain available.

`organize_card` prepares only the listed card-relative files. Its default `analyze_peak=true` calculates original sample peak and peak-to-average data. Use `analyze_peak=false` to keep those fields empty when generating new information. A valid existing block is preserved. Local matching LRC files supply optional lyrics.

`rebuild_card_index` rereads track headers and regenerates `xm5/Struct/tracks.tsv`. Playlist and favorite files stay in place. The commands report the backend job result after the existing window refresh finishes. Stop uses the same job cancellation route as other storage commands.

The normal TF page and `preview_export` / `export_card` now use this inline format. The original INFO02 command path is retained only for legacy inspection and is not selected by the normal export page. Inline operations refuse a tree with an INFO02 manifest.

## Full visual-equivalent workflow

```text
set_peak_options(midpoint_dbfs=0, correction_limit_db=4, peak_correction_enabled=true)
set_export(destination="I:/", music=["D:/Music/Collection"], playlists=["Evening"])
plan = preview_export()
export_card(plan=plan)
card_playlists("I:/")
inspection = inspect_return(file="I:/xm5/Struct/playlists/Evening-12345678.m3u8", folder="I:/")
import_return(inspection=inspection)
```

Use the actual playlist path returned by `card_playlists`, rather than copying the illustrative suffix. A conflict needs `decision="device"`, or `decision="copy", name="Evening from XM5"`. Keeping the PC version requires no import. Favorites use the fixed `Favorites.m3u8`; importing it changes the card-scoped PC favorites atomically and preserves favorites outside that export.

Peak midpoint is -60..0 dBFS and the correction limit is 0..12 dB. The card stores integer milli-decibel settings in `Struct/settings.ini`; default values are 0 and 4000. This is original-sample-peak correction, with no loudness target or PEQ-dependent compensation.

Existing inline cards can be updated through the same preview/export workflow. Bring back or explicitly retain the PC version of changed native playlists first. Only new/changed files and generated metadata are staged; unchanged audio remains in place. A changed owned target or an unowned collision blocks replacement. Unselected sources and unrelated card files remain present. Only replaced file preimages move to `.xm5-before-...`; unchanged music is not duplicated. Those backups are never automatically deleted.

An interrupted publication keeps `Struct/INCOMPLETE`, the activation pointer and its transaction journal. `storage_transactions("I:/")` lists exact transactions. `recover_storage("I:/", "transaction-id", action="resume")` continues from verified staged files; `action="rollback"` restores retained preimages. `recover_export(transaction="I:/")` is the rollback shortcut. Neither recovery action needs source drives. Changed files discovered after interruption block both actions for manual review.

Valid card playlist paths which the PC has never seen remain as card-only references. Import displays them as red missing rows and does not copy or decode their audio. PC playback skips them. Later exports keep their relative M3U8 paths, even to a new empty card. Ordinary playlists retain order and repeated references; Favorites deduplicates the exact path. `decision="device"` and `decision="copy"` both preserve these rows. The PC-retain choice remains explicit.

`open_export(tab="organize")` opens the explicit card-maintenance tab. `organize_card` and `rebuild_card_index` are its script equivalents. A valid existing inline block is retained, not regenerated. The generated `Struct/recovery/RESTORE_LIBRARY.py NEW_OUTPUT_FOLDER` copies ordinary music, original EQ and playlists out without this application. It needs only Python's standard library. XM5 metadata uses standard container extension blocks, so other players can ignore it; the recovery tool does not remove those blocks.
