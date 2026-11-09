# Library and playlists

`music_folders()` and `eq_folders()` return registered source IDs and enabled state. Add an explicit existing folder with `add_music_folder` or `add_eq_folder`. `relink_folder(source_id,path,kind="music")` preserves its identity. `enable_folder` and `remove_folder` change source registration; they do not delete source files. `scan()` uses the configured enabled music roots. `refresh_tracks(s)` rereads only explicit indexed tracks.

```text
s = tracks(artist="Mira",sort="title")
read_results(s,start=1,count=50)
p = create_playlist("Evening",s,open=true)
add_to_playlist(p,track("Blue",artist="Mira"))
e = playlist_entries(p)
```

Track results retain stable track IDs. A playlist-entry result retains stable playlist and occurrence IDs, so repeated appearances of one track stay distinct. Missing IDs fail explicitly. `remove_from_playlist` and `move_playlist_items` accept immediate 1-based positions or a song plus its occurrence number. Replacements use `replace_playlist` and preserve existing occurrence identities where possible. Revisions guard writes against concurrent edits.

For a separately named folder playlist, capture the selected tracks and page their exact folder IDs before planning. Use one explicit folder ID and one distinct literal playlist name in each recipe; a shared literal name for several groups is a collision.

```text
s = tracks()
c = capture_tracks(s)
read_results(c,start=1,count=50)
p = plan_playlists({recipe_id:"folder-solo",name:"Artist C - 独奏",folder_ids:["exact folder_id from c"],duplicates:"preserve"},s)
read_results(p,start=1,count=50)
apply_plan(p)
```

`rename_playlist`, `delete_playlist`, `set_playlist_cover`, `clear_playlist_cover` and `edit_playlist_details` use the same state services as the page. `set_playlist_finder` shows and searches its actual song finder. `import_playlist` reads an explicit ordinary M3U/M3U8; unresolved entries require `missing="skip"` or fail. Ordinary GUI playlists cannot preserve unindexed streams. `export_playlist` writes exact selected occurrences; existing output is refused unless `overwrite="replace"` is explicit.

`set_favorite(s,true)`, `set_note("note",s)` and `set_library_tags({album:"name"},s)` edit backend library metadata. Library overrides leave source file tags unchanged. `set_tags` and the plan commands in [Tags](TAGS.md) write source files. `history()` lists GUI undo receipts; `undo()` uses the existing conditional undo path. Source script backup journals are separate from that GUI history.
