# Tags and actions

Use explicit stable track results. Empty selection never expands to all tracks.

```text
s = tracks(album="Glass")
p = preview_tags({TITLE:"New title",CUSTOM:["first","second"]},s)
read_results(p,start=1,count=50)
apply_plan(p)
```

Plans retain exact IDs, digests, source snapshots and output bytes. `apply_plan` accepts the complete receipt or an exact persisted plan ID. Stale sources/revisions, unsupported containers, collisions and unsupported formatter functions fail explicitly. Script source writes currently use supported WAV, native FLAC and MP3 paths; the inspector writer retains its existing broader container support. Neither path promises complete Mp3tag behavior.

`format_text`, `format_tags`, `tags_from_names`, `preview_names`, `tag_to_tag` and `text_to_tags` use the backend formatter/converters. `number_tracks` and `rename_files` default to a plan; inspect it and call `apply_plan`. Filename changes also update indexed locations through the application relocation seam. `capture_tracks` returns the frozen identities/folder IDs used by `plan_playlists` recipes.

Column functions freeze the actual table's visible header order and displayed values for each selected track. Repeated playlist occurrences retain their own position values. Header-only functions capture no row maps, and ordinary literal tag writes/numbering/renames transfer no unused column context. Missing or ambiguous row context fails explicitly. Source/action/template/recipe plans retain needed captured context; another row's values are never substituted. Oversized exact value captures require splitting the explicit selection before submission. Rename previews list every move with exact old/new paths before execution.

`import_actions(file,name="Clean")` imports an explicit MTA file. `action_groups()` reports support diagnostics. `run_actions("Clean",s)` defaults to a plan. An unsupported action blocks the complete group. Text decoding and regex support follow the real action compiler, including explicit incompatibility errors.

The actual tag inspector is available through `edit_tags`, `clear_tag_field`, `keep_tag_field`, `save_tag_edits` and `discard_tag_edits`. These preserve unsaved draft behavior and report per-file partial writes. `set_track_cover` and `remove_track_cover` use its cover editor; `save=false` leaves a draft. `preview_cover` and `export_track_cover` read the file's embedded cover. `plan_covers` and `plan_cover_export` expose richer native cover actions through reviewed plans.

`preview_template` plans template output. `export_metadata` reads an explicit template file and publishes exact output through the guarded backend. `read_results(plan)` pages every write/output/issue row without truncating the stored plan.
