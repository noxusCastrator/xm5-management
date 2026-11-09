# XM5 Library

XM5 Library manages local music, playlists, favorites and PEQ presets for an ONIX XM5. It prepares the player's card from music folders you already use. It also provides basic computer playback for checking tracks and EQ settings.

Extract the complete Windows x64 portable package into a writable folder and run `XM5Library.cmd`. The application includes its runtime and media tools; installed copies of Peace, Mp3tag and foobar2000 are not required. Application settings and caches stay with this portable application. Select an audio output before computer playback. Changing the computer's audio output does not change the player's local playback configuration.

Add your existing music folders in the Folders page and EQ folders in the PEQ page. Keep managing the original files with your existing tools if you prefer. The application reads these sources and prepares separate card copies. Export does not remove the original computer files or replace their directory layout.

Create playlists and set their covers on the computer. Their order is explicit, and repeated entries are allowed. Favorites use a dedicated playlist. Export first previews the work, then creates the managed `xm5` folder on a selected destination. USB storage and an offline TF card use the same format. An existing recognized inline export can be updated. The application refuses an unrelated existing `xm5` folder and preserves unmanaged files, including files inside an existing managed folder.

The exported copies retain their audio, original tags and original pictures. Added information contains small 80-, 160- and 330-pixel JPEGs, optional lyrics and original sample-peak data. The player reads these prepared pictures instead of repeatedly decoding the full original artwork during browsing. Its scan can prepare eligible files added directly to the card. Unprepared or invalid pictures remain placeholders until preparation succeeds.

Peak correction uses the selected midpoint and maximum correction, with defaults of 0 dBFS and 4 dB. It is independent of PEQ and does not predict clipping caused by EQ boosts. Lyrics are optional; no lyric search is required for export.

Use the card return page after editing playlists or favorites on the player. When both sides changed, choose the player version, retain the PC version, or save the player version as another playlist. Missing known playlists need an explicit deletion decision. Renaming an actual audio file can break a relative playlist entry. Changing its title tag does not rename it.

Songs added directly to the card can be brought back as playlist references without copying their audio to the computer. They appear as red missing rows and are skipped during computer playback. Later exports keep their relative paths, even on another card where that audio is absent. Ordinary playlists keep repeated entries; Favorites deduplicates them. Removing a row remains an explicit edit.

Updates stage new or changed files and generated metadata. Unchanged music stays in place, without another copy or analysis. Only replaced files move to a `.xm5-before-*` backup; an update does not duplicate the complete library. Changed owned targets, unowned collisions and unreviewed player playlist edits block replacement. Backups are retained and consume space.

Cancellation or interruption during publication leaves `Struct/INCOMPLETE` and a transaction journal. The recovery controls can resume or roll back without the original source drives. An external edit after interruption blocks recovery instead of being overwritten. Keep the paired firmware: older stock scanners can find duplicate music in retained backup directories. These journals do not replace a separate backup of your music.

The tag editor and tag scripts are separate from export. Applying an explicit source tag or rename plan changes those selected source files. Library-only notes and tag overrides do not write file tags. Review the plan and its supported formats before applying it.

The normal export uses inline XM5I information. Older INFO02/XMI1 exports keep their existing inspection and return handlers; they are not silently rewritten as inline cards. An explicit low-level `format="XMI1"` plan selects the historical exporter. New card preparation refuses an old INFO02 tree. Ordinary unprepared music remains usable as music, with placeholders in the prepared-cover path.

Automation is available while the application is running. Use the built-in console or `xm5.cmd` for function scripts. `XM5Library.cmd cli` is the separate JSON backend client. See [scripting](SCRIPTING.md) for examples and grouped manuals. Both clients discover the local authenticated session; access tokens should not be copied into scripts or shared documents.

See [card format](STORAGE_FORMAT.md) for the on-card structure and [build instructions](BUILD_STABLE.md) for source builds.
