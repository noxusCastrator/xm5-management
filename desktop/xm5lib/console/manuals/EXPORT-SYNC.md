# Export, repair and state reconciliation

```text
set_export(destination="C:/Exports",music=["source-id"],playlists=["playlist-id"],presets=["Listening"])
p = preview_export()
read_results(p)
export_card(plan=p)
verify_card("C:/Exports/xm5")
```

`destination` in `set_export` is an existing parent directory. The export page builds its canonical `xm5` container below it. Explicit source/playlist/preset lists may be empty; they never mean all. Preview freezes the full backend export plan. Execution requires that exact plan ID/digest and waits for the actual job receipt. `set_peak_options` controls original-sample-peak midpoint, correction limit and enable state. PEQ is outside that analysis.

`card_playlists`, `inspect_return` and `import_return` expose the existing ordinary M3U return flow. Conflicting PC/card edits require an explicit decision. `restore_library` restores to a supplied new destination. `recover_export` accepts an exact interrupted update control directory.

`library_health(folder,sources={id:path})`, `plan_sync`, `plan_repair`, `storage_plan`, `storage_transactions` and `recover_storage` operate only on the explicit exported container/source map. Preview before applying a sync or repair. Recovery names an exact transaction and chooses resume or rollback. Partial, cancelled and recovery-required outcomes remain errors carrying actual receipts.

`inspect_device_library(folder)` reads native XM5 playlist/favorite state. `plan_device_library(folder,associations={native_id:pc_playlist_id},decisions={...})` plans reconciliation with stable explicit associations. Two-sided conflicts require choices such as device, pc, keep_both or retain. A catalog generation boundary requires explicit `allow_new_generation=true`. `apply_plan` applies the frozen native state plan through the same service. These commands work with local generated/exported folders; nothing auto-detects or operates a physical card/device.

`open_lyrics`, `inspect_lyrics`, `import_lyrics`, `read_lyrics`, `preview_lyrics`, `use_lyrics` and `remove_lyrics` use explicit track/source IDs. `find_lyrics` and `fill_missing_lyrics` are explicit online searches; export itself retains local lyrics and does not silently search online.

## Inline cards

The normal export now uses XM5I information inside each song. See [INLINE-CARD.md](INLINE-CARD.md) for that format. The original information-file reader remains available for old exports.

`library_health` verifies the inline library's owned file hashes without writing. `plan_sync` and `plan_repair` accept explicitly registered source folders and stage a reviewed file-level delta. Unchanged prepared tracks stay in place without copying or decoding. New/changed files and generated metadata require free space; overwritten preimages are moved to a backup directory. Preview includes rewrite workspace in its disk estimate. An offline or absent source is preserved on the card and never treated as a deletion request.

```text
library_health("I:/")
p = plan_sync("I:/", sources={"collection":"D:/Music/Collection"})
apply_plan(p)
r = plan_repair("I:/", sources={"collection":"D:/Music/Collection"}, choices={"xm5/music/Collection/Album/Track.flac":"replace_from_source"})
apply_plan(r)
```

Only an owned music/EQ path may use `replace_from_source`. An unowned collision stays protected. Inline sync does not accept arbitrary generated-byte update objects; use the library, playlist and preset commands before preparing the export.

`inspect_device_library` inspects ordinary M3U8 files for inline cards. Its row keys are exact card-relative playlist paths and the special `favorites` key. Associations map an exact path to a PC playlist ID. Decisions are `device`, `pc`, `keep_both` or `retain`. A missing known playlist is retained by default; removing the PC version needs an explicit `device` decision. A tombstone filename or native numeric ID is never enough to infer a deletion.

```text
inspect_device_library("I:/")
p = plan_device_library("I:/", decisions={"favorites":"device", "xm5/Struct/playlists/Evening-12345678.m3u8":"pc"})
apply_plan(p)
recover_export(transaction="I:/")
```

Use actual row keys from the inspection. The frozen plan checks the current card build, every selected input file and the PC application revision before intake. Each applied PC mutation has a normal undo receipt. If a later item fails unexpectedly, the result is partial and retains the completed receipts.

`recover_export` takes the inline card parent when `.xm5-activation.json` exists and rolls back an interrupted delta. `storage_transactions` lists exact transaction IDs; `recover_storage(folder, transaction_id, action="resume")` or `action="rollback"` chooses recovery. The hashed plan, verified staged bytes and retained preimages are sufficient without source drives. Publication keeps `Struct/INCOMPLETE` until it is complete. An externally edited target blocks recovery rather than being overwritten. This journal handles interrupted application operations; it cannot promise atomicity against damaged flash or filesystem corruption.

Device-added songs absent from the PC library remain persistent card-only references when importing playlists or Favorites. They appear as red missing rows, are skipped in PC playback and keep their relative paths in later exports. No audio is pulled from the card, and the application does not claim ownership of those files. Repeated ordinary-list entries remain repeated; Favorites deduplicates paths. Removing a reference or choosing the PC version is an explicit user action.
