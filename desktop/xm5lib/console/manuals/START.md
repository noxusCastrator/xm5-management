# XM5 window control

Open XM5 Library normally. Its local control session follows that window, stays available while minimized, and ends when the window closes. The CLI only connects to an existing session.

```powershell
.\xm5.cmd 'status()'
.\xm5.cmd --file 'C:\Tasks\evening.xm5'
's = tracks(artist="Mira"); read_results(s)' | .\xm5.cmd --stdin
```

Scripts support function calls, named arguments, maps/lists and assignments. They never execute Python or shell text. The whole script, including literal `run_script()` files, is checked before its first effect. Empty selection means zero tracks. `tracks()` explicitly requests the library.

`--stdin` reads strict UTF-8 bytes; `--file` accepts strict UTF-8 and BOM-marked UTF-16. For a Unicode pipeline in Windows PowerShell 5, set its output encoding only for that invocation, then restore it:

```powershell
$savedPipeEncoding = $OutputEncoding
try {
    $OutputEncoding = [Text.UTF8Encoding]::new($false)
    'status()' | .\xm5.cmd --stdin
} finally {
    $OutputEncoding = $savedPipeEncoding
}
```

PowerShell 7 uses UTF-8 pipelines by default. Invalid input bytes or unpaired surrogates are rejected without replacement and before submission. CLI JSON escapes Unicode losslessly so older PowerShell native-output code pages cannot corrupt the response.

The CLI prints a request ID before submission. `admission_status="not_accepted"` means a local input/encoding/connection rejection submitted no new request and had no effect; correct that error without a status lookup or GUI restart. Explicit server rejection also has no effect from that submission. For a transport failure after submission, inspect `xm5.cmd --status ID`; admission/outcome may be unknown. Reuse the same ID and exact script only in the same GUI session. A different script under that ID fails. An ID accepted in an older session is never executed again. `xm5.cmd --cancel ID` requests cancellation; it does not undo completed writes. Unknown server outcomes block further mutations in that session until the normal GUI is restarted and actual state inspected.

If an existing desktop controller opens a modal dialog while the script is waiting, use `xm5.cmd --dialog`. Answer it with `--answer-dialog '{"dialog_id":"returned ID","choice":"returned button ID","values":{}}'`. Field values use exact returned field IDs. `choice="close"` dismisses it; file dialogs also accept `values.file`, `values.directory` and `choice="accept_file"`. These typed actions run on the same Qt owner while its modal loop is active, retain their own idempotency receipts and do not run another script behind the waiting operation.

Use `help("command")`, `commands("topic")`, or `xm5.cmd --catalog`. `COMMANDS.json` and `SUPPORT.json` describe the implemented surface. Fixture coverage is recorded separately; presence in the catalog is not proof that every optional argument was tested.

- [Player and window](PLAYER-UI.md): playback, queue, views, selection, player mode and dialogs.
- [Library and playlists](LIBRARY-PLAYLISTS.md): sources, explicit identities, duplicate occurrences, notes and favorites.
- [Tags and actions](TAGS.md): native values, draft editor, converters, plans and cover actions.
- [PEQ](PEQ.md): internal preset editor, band parameters, validation and desktop preview.
- [Export and sync](EXPORT-SYNC.md): exact export plans, output verification, repair and native state reconciliation.
