# Which source to edit

The paired management firmware builds these active components:

| Component | Source | Build entry |
| --- | --- | --- |
| Artwork, playlists, favorites and USB-storage UI hooks | `src/native_storage/` | `tools/build_storage_components.py` |
| PCM PEQ and inline source gain | `src/audio_storage/audio-info1/` | Its `scripts/build_payload.sh`, invoked by the component builder |
| Audio-side metadata reader | `src/audio_storage/player-info1/` | Included by the active audio build |
| On-player preparation | `src/organizer/` | Component builder |
| Desktop domain and card formats | `desktop/xm5lib/application/`, `desktop/xm5lib/core/` | Desktop test runner |
| Local commands | `desktop/xm5lib/automation/` | Desktop test runner |
| Window and controls | `desktop/xm5lib/ui/` | Qt tests |

`src/audio/`, `src/ui/`, `tools/audio/`, `tools/ui/`, and `tools/package/` reconstruct the hash-pinned PEQ08 prerequisite. They are retained historical inputs. Current storage changes belong in the active trees above. `tools/package_stable/` packages the paired firmware. Similar filenames are intentional: changing a frozen prerequisite breaks the guarded source build.

The UI reader and audio reader have different responsibilities and link constraints. Compare shared-looking container implementations and test both callers before consolidation. Do not delete a copy based only on its filename.

## Stock ABI boundaries

The UI hooks target the pinned V2.43/PEQ08 executable. `hook_thunks.S` declares stock entry addresses. `patch_demo.py` and `extra_hooks.py` verify displaced instructions and input hashes before injection. `prepared_io.c` checks the target O32 `stat64` layout at compile time. Keep these checks when reorganizing source.

Examples in `player_hooks.c` include folder records at `0x805de4`, cover-refresh state at `0x806454`, record path/type fields at offsets 12/52, and widget width/height at offsets 8/12. These are executable ABI observations. Consult the guarded hook sites before changing them. A new official firmware needs a fresh mapping.

The `.clang-format` profile applies to active `src/native_storage` C, header and include files. Formatting equivalence is checked with the MIPS build. Frozen PEQ08 sources remain unchanged.
