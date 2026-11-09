# Control XM5 Library

Start the portable application with `XM5Library.cmd` first. Its function console and external script client control the same window and backend. They keep working while the window is minimized and stop when it closes. The client does not start another application instance.

From PowerShell in the extracted application folder:

```powershell
.\xm5.cmd 'status()'
.\xm5.cmd --file 'C:\Tasks\evening.xm5'
's = tracks(artist="Mira"); read_results(s)' | .\xm5.cmd --stdin
```

The same function calls work in the application's console. Scripts allow assignments, named arguments, lists and maps. They do not execute Python or shell commands. The complete script is checked before its first effect. An empty selection means no tracks; `tracks()` explicitly selects the library.

`--stdin` expects UTF-8. `--file` accepts UTF-8 and BOM-marked UTF-16. For a Windows PowerShell 5 Unicode pipeline, set `$OutputEncoding` to UTF-8 for that call and restore it afterward. PowerShell 7 uses UTF-8 pipelines by default. File input is the simpler option for scripts containing non-ASCII text.

## Find commands

Use `help("command")`, `commands("topic")` or `xm5.cmd --catalog`. The exact names, arguments and limits are in [COMMANDS.json](../desktop/xm5lib/console/COMMANDS.json). The grouped manuals cover:

- [Player and window](../desktop/xm5lib/console/manuals/PLAYER-UI.md): output choice, volume, queue, album/page selection, window state and player mode.
- [Library and playlists](../desktop/xm5lib/console/manuals/LIBRARY-PLAYLISTS.md): source folders, searches, playlist order, duplicate occurrences, favorites and notes.
- [Tags and actions](../desktop/xm5lib/console/manuals/TAGS.md): the tag editor, formatter functions, renames, covers, MTA actions and reviewed write plans.
- [PEQ](../desktop/xm5lib/console/manuals/PEQ.md): the internal preset editor, optional Peace/APO imports and desktop preview.
- [Export and sync](../desktop/xm5lib/console/manuals/EXPORT-SYNC.md): preview, file-delta updates, health, repair, device-state return and transaction recovery.
- [Inline card preparation](../desktop/xm5lib/console/manuals/INLINE-CARD.md): selected-card files, Struct rebuilding and peak settings.

## Work with playlists and tags

```text
s = tracks(artist="Mira",sort="title")
p = create_playlist("Evening",s,open=true)
add_to_playlist(p,track("Blue",artist="Mira"))
e = playlist_entries(p)
read_results(e)
```

Playlist entries have occurrence IDs, so two appearances of one song can be moved or removed separately. `rename_playlist`, `set_playlist_cover` and `set_favorite` use the normal application services. Revision checks protect concurrent edits.

Library metadata and file tags are different operations. `set_library_tags` changes library overrides. An explicit tag plan writes selected source files only after `apply_plan`:

```text
s = tracks(album="Glass")
p = preview_tags({TITLE:"New title"},s)
read_results(p)
apply_plan(p)
```

The formatter, text/tag converters, numbering, rename plans and supported MTA actions are implemented inside the application. Mp3tag is not required. This is a supported subset of its scripting behavior: unsupported functions or action steps reject the group. Script source writes currently support WAV, native FLAC and MP3; the interactive tag inspector has its own format support. A stale source, unsupported container or colliding output blocks the plan. Keep the exact plan receipt when applying it.

## Export and bring back

```text
set_peak_options(midpoint_dbfs=0,correction_limit_db=4,peak_correction_enabled=true)
set_export(destination="I:/",music=["D:/Music/Collection"],playlists=["Evening"])
p = preview_export()
read_results(p)
export_card(plan=p)
card_playlists("I:/")
```

The destination is an existing parent directory. The application manages its `xm5` child. Use the actual returned playlist filename for intake:

```text
r = inspect_return(file="I:/xm5/Struct/playlists/Evening-12345678.m3u8",folder="I:/")
import_return(inspection=r,decision="device")
```

Conflicts require `decision="device"`, `decision="pc"`, or `decision="copy",name="Evening from XM5"`. Card return preserves valid paths for songs missing from the PC as red missing rows. It does not pull their audio. Later exports keep those references. The separate `import_playlist` command imports an ordinary PC playlist against indexed local tracks; its missing-entry policy is explicit and does not perform this card-intake workflow.

For batch card state, use `inspect_device_library`, then `plan_device_library` with exact row keys and decisions `device`, `pc`, `keep_both` or `retain`. Apply that frozen plan with `apply_plan`. Ordinary lists preserve repeats; Favorites deduplicates exact card paths and preserves PC favorites outside the returned card scope.

Updates stage changed files and metadata; unchanged audio stays in place. `storage_transactions("I:/")` reports exact transaction IDs. `recover_storage("I:/","transaction-id",action="resume")` continues a guarded interrupted update. Use `action="rollback"` to restore replaced files, or `recover_export(transaction="I:/")` for the rollback shortcut. Recovery needs the retained journal/staging/backup files, not source drives.

## Handle a command failure

The script client prints a request ID. A local `not_accepted` result submitted no work. After a transport failure with an unknown outcome, inspect `xm5.cmd --status ID` before retrying. Reuse the same ID and exact script only within the same GUI session. `--cancel ID` requests cancellation; it does not undo completed writes. Normal application edits use conditional undo receipts; source-file plans and card transactions have their own backup/recovery paths.

`XM5Library.cmd cli` exposes the lower-level JSON backend interface:

```powershell
.\XM5Library.cmd cli status
.\XM5Library.cmd cli capabilities
.\XM5Library.cmd cli query playlists.list
```

Backend commands use a JSON input file, an idempotency key and any required resource revision. Long-running commands return a job ID; query `jobs.get` for the result. This interface has no GUI-coordinate commands. Use the function client for normal desktop/AI control and typed dialog handling. No installed Peace, Mp3tag or foobar2000 is needed.
