# Player and window

`set_volume(30)`, `set_play_mode("shuffle")`, `pause()`, `stop()`, `seek("1:20")`, `next_track()` and `previous_track()` use this window's player. Playback requires an explicit selected output. `outputs()`, `refresh_outputs()` and `set_output("exact ID")` expose that choice. `test_output()` uses the selected output, a bounded quiet generated tone and restores the queue afterward.

```text
s = tracks(album="Glass")
play(s,start=1)
q = queue()
read_results(q)
add_to_queue(track("Blue"),next=true)
```

Queue results include stable entry IDs for repeated songs. Pass an exact entry ID list to `remove_from_queue` or `move_queue_items`; 1-based positions are also accepted for immediate edits. `clear_queue()` stops and empties it. Transport operations finish after actual request callbacks, including any chained output connection, rather than after event posting.

`show_page`, `open_album`, `open_artist`, `open_genre`, `open_resolution`, `open_folder` and `open_playlist` navigate library views. `search` and `clear_search` use the real search field. `select(s)` displays a matching track selection. `a=albums(); select(a,target="albums")` selects actual album tiles by returned IDs. `sort_view` follows the All Songs header sorting; playlist order uses playlist editing commands. Column visibility/order/width use `set_columns`, `set_column_width` and `reset_columns`. `view_state()` reports the actual view and selected track IDs.

For the firmware-style player UI:

```text
set_player_mode(true)
device_state()
device_controls(start=1,count=100)
device_activate("exact returned control ID")
device_controls()
```

Each inspected control lists its label, semantic route and supported actions. `gesture="long"` invokes its long-press controller; `gesture="value",value=...` sets an advertised slider. `gesture="page",value=2` changes a cover/lyrics/info slide to an advertised 1-based page. Virtual rows can be paged directly without scrolling. Handles expire after page, library, playlist, setting or row replacement changes. Inspect again after each action. Power/screen/key locks and volume caps remain the controller's rules.

`device_key("power")`, `device_key("home")`, `device_key("back")`, `device_panel(true)`, `device_knob(-2)` and `device_type("text",replace=true,submit=true)` cover physical keys, pull-down controls, knob and focused text fields. `set_device_scale(1)` uses a supported body scale. Device hardware sound-path options retain their existing simulation limits; `device_state()` states this explicitly.

`set_window_state("minimized")` keeps the session alive. `screenshot()` returns this window's PNG bytes. Desktop dialogs use `show_dialog`, `answer_dialog` and `close_dialog`; player-mode dialogs remain visible in `device_controls()`. `quit_app(jobs="cancel",drafts="discard")` closes the window and invalidates control; a racing transport failure must be treated as session termination and checked, not retried with a new mutation ID.
