# Desktop 0.2.1 validation

This maintenance release makes the Windows desktop source easier to run, modify and package. It does not change the paired 0.2.0 firmware or its hardware acceptance status.

## Changes

- Add a dependency setup helper for an ordinary source checkout. It downloads the exact public 0.2.0 archive, verifies its archive and dependency hashes, and installs only `desktop/vendor/`.
- Add a development guide, source map and public regression suite. The bundled interpreter runs the editable repository source, not an extracted application snapshot.
- Correct the application version from the old development label to 0.2.1.
- Exclude generated Python bytecode from dependency manifests and packaging. Running the app no longer invalidates a later local build.
- Exclude settings backups, locks and interrupted settings-write files from portable packages and Git. Profiles, cache and runtime discovery files are not release inputs.

## Checks

On Windows x64, the actual public dependency download, installation, isolated bundled runtime check and subsequent read-only dependency checks passed. The normalized dependency inventory contains 3,318 files. No global Python packages or installed Peace, Mp3tag or foobar2000 are required by the running app.

The public suite has 29 non-GUI tests and one optional native Windows integration test. All 30 passed. It covers source-byte preservation, scanned tracks, playlist duplicate/order persistence, stale revision rejection, Favorites and undo, relative M3U8 paths, card-only references, invalid paths, configuration preservation/refusal, setup failures and package privacy/integrity.

The native integration test starts the actual Qt window and backend processes using a temporary synthetic library. It scans two songs, creates an ordered playlist and Favorites, imports PEQ, previews and performs an export through the GUI, and verifies the resulting managed folder. It checks that original source bytes remain unchanged and owned backend processes exit. Native English and Chinese text were visually checked in the retained screenshots.

Playback checks use the actual bundled mpv with **NULL audio output**. Position, volume and PEQ configuration passed. These checks do not establish audible output quality, sound-card compatibility or XM5 USB DAC behavior.

The new portable ZIP was extracted into a separate path, checked against its complete build manifest and started with its own isolated runtime. Its application files match the repository source. The existing media executables and corresponding-source ZIP are unchanged; their inventory was verified again.

Run these checks using [DEVELOPMENT.md](DEVELOPMENT.md). The historical 146 desktop cases in the [0.2.0 record](VALIDATION_0.2.0.md) are separate evidence; this release does not claim that all of those historical tests have been republished.

## Scope

Windows x64 is the tested desktop platform. The integration test uses small synthetic files and a normal local output directory. It does not cover every codec, large personal library, concurrent external file edit or interrupted real-card operation.

No physical device, card or USB communication was used for this maintenance release. The storage firmware retains software/VM acceptance and has no new physical-device acceptance. Existing 0.2.0 assets remain unchanged. Component rights are described in [THIRD_PARTY.md](../THIRD_PARTY.md); this maintenance work does not add a repository-wide license.
