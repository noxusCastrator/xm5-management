# XM5 Studio 0.2.2 local candidate

This build remains local until the owner requests publication. It adds Windows x64 portable/installed distribution, the XM5 Studio name and original icon, optional copy-only portable-profile import, and update-card preparation. It has no Windows sign-in startup feature.

## Software evidence

The public regression suite passes 97 tests on the current Windows x64 host, including a native application/backend/export test, update-card widgets, installed-path selection, portable-profile import and installer packaging. Playback tests use NULL output. Core update-card and import tests use owned synthetic directories and never enumerate or modify real media.

A separate packaged-app probe used the actual bundled firmware and real Qt window with an injected temporary-directory volume provider. Cancel preserved the original file. Confirm removed the synthetic contents, copied 56,371,200 bytes and verified SHA-256 `124fa80d5ea48d267254f838e175202393e872951d4a44323127ec3d0eae2b1f`. Only `update.bin` remained. Native screenshots were checked for the warning, blue export-page link, confirmation/cancel buttons and completion result.

The real production installer was installed into an owned local test directory, using its normal current-user registration and no shortcuts. Its native launcher completed the actual first-launch profile/folder choices. Its Qt window and both command clients used the separate synthetic installed profile. Same-version reinstall preserved the profile and an unrelated user-added file, and removed an unchanged obsolete owned program file. An active application guard refused installation without terminating a process. Uninstall removed the temporary registration and program files while retaining the profile and unrelated file. No restart was requested. This is host integration testing, not a clean Windows VM or every DPI/OS combination.

Full-payload testing found two installer issues before acceptance: Inno Setup 6 could not process the long Qt dependency paths, and an initial obsolete-file scan repeatedly parsed the entire inventory. The final builder requires verified Inno Setup 7.1.1 or newer, retains all reviewed dependency files, and uses an indexed current-file list. The final production lifecycle passed after these corrections.

The original vector icon has transparent corners and ICO frames at 16, 24, 32, 48, 64, 128 and 256 pixels. Rendering was reproduced byte-for-byte; Qt and Windows loaded the frames. The native x64 launcher was built reproducibly from the included source with matching outputs across builds. The setup EXE is unsigned.

## Firmware and dependency scope

The standalone and bundled `update.bin` remain the exact 0.2.0 / V2.43-LIB01 image. No kernel, player, rescue or audio code was rebuilt for 0.2.2. Refer to [0.2.0 validation](VALIDATION_0.2.0.md) for software/VM firmware evidence and its physical-device limits. Preparing an update card copies files; it does not install firmware, format storage, alter partitions or run a recovery procedure.

The media executables and corresponding-source archive are unchanged. The source archive has a new outer filename, `XM5Studio-runtime-source-0.2.2.zip`, and the same bytes as the 0.2.0 source archive. See [runtime distribution](RUNTIME_DISTRIBUTION.md). The project-source archive contains application, firmware-adapter, launcher, packaging and public-test source; it excludes local inputs, binaries, profiles, credentials and research dumps.

No real card, raw storage or USB communication was used for this candidate. Physical firmware acceptance, real-card clearing, audible DAC output, clean-machine coverage and universal crash recovery are not established by these tests. Update-card preparation is destructive after explicit confirmation; backup remains the user's responsibility. Source and component rights remain as stated in [THIRD_PARTY.md](../THIRD_PARTY.md).
