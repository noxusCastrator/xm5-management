# Internal PEQ editor

The PEQ editor belongs to XM5 Library. `open_peace()` opens that internal page. Peace/APO files are optional import formats and require no installed Peace process.

```text
new_eq("Listening")
edit_eq(preamp=-4,bands=[{type:"PK",frequency:1000,gain:2,q:1}])
edit_eq_band(1,channel="L",enabled=true)
validate_eq()
save_eq()
```

`presets()` lists saved presets. `open_eq` opens one by exact name. `new_eq` creates an editor draft. `edit_eq`, `add_eq_band`, `edit_eq_band` and `remove_eq_band` change actual editor fields. Frequencies use Hz, gains/preamp use dB, and Q is unitless. Supported types/channels and bounds come from the same PEQ validator as the page. `set_eq_preview_rate` changes its curve rate. Invalid edits report issues and do not masquerade as saved presets.

`save_eq(name=...,as_copy=true)` saves a distinct name; collisions fail explicitly. `discard_eq_edits` reloads the stored preset. `delete_eq` removes the saved library preset. `import_eq(file)` reads an explicit APO document. `export_eq(preset,file)` writes validated firmware text and refuses an existing destination.

`use_eq(name)` or `enable_eq(true)` applies a saved preset through the actual desktop playback engine. Choose an output first. `enable_eq(false)` disables it. This affects desktop preview; it does not operate a connected XM5. Player-mode stock EQ settings retain their existing simulation behavior.

`inspect_peace(folder)` reads optional source files. `set_peace_sources` chooses registered source IDs. `import_peace()` without selected profile IDs returns an inspection list with stable IDs and imports nothing. `select_peace_profiles([id,...]); import_peace()` imports only those explicit convertible profiles.
