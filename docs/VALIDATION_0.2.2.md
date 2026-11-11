# XM5 Studio 0.2.2 validation

This pre-release includes Windows x64 portable/installed distribution, the XM5 Studio icon, optional copy-only profile import, non-clearing update-card preparation, data-preservation fixes and bounded directory artwork lookup. It has no Windows sign-in startup feature. See [the review decisions](REVIEW_FIXES_0.2.2.md).

## Software evidence

The public regression suite passes **131 tests** on the current Windows x64 host, including a native application/backend/export test, firmware-copy widgets, installed paths, profile import and packaging. It includes 14 storage/conflict regressions and a separate 88-scenario before/after interruption matrix within one test. The latter count is scenarios, not 88 additional unittest cases. The crossed playlist edits and corrupt-backup rollback cases failed before their fixes and pass afterward. Playback tests use NULL output. Storage tests use owned synthetic directories and never enumerate real media.

A packaged-app probe used the actual bundled firmware and real Qt window with an injected temporary-directory volume provider. Cancel preserved an existing `update.bin`. Confirm replaced it with 56,371,200 verified bytes while preserving the unrelated sentinel file. The copied SHA-256 was `be16c472ba1bdcf46dc6a5b93e218ea74c8baf967aa6cc5e43505efd7ee8fdd9`. Native screenshots show the replacement question and completion result. Absent destinations copy without a replacement dialog; the public core/widget suite tests that branch.

The rebuilt production installer completed an actual install, native first launch, runtime check, GUI/command-profile check, same-version reinstall, active-application refusal and uninstall in an owned local directory. Reinstall preserved the profile and unrelated file and removed an unchanged obsolete owned module. Uninstall retained user files and removed its temporary current-user registration. No restart or process termination was requested. This is host integration testing, not a clean Windows VM or every DPI/OS combination.

Full-payload testing found two installer issues before acceptance: Inno Setup 6 could not process the long Qt dependency paths, and an initial obsolete-file scan repeatedly parsed the entire inventory. The final builder requires verified Inno Setup 7.1.1 or newer, retains all reviewed dependency files, and uses an indexed current-file list. The final production lifecycle passed after these corrections.

The original vector icon has transparent corners and ICO frames at 16, 24, 32, 48, 64, 128 and 256 pixels. Rendering was reproduced byte-for-byte; Qt and Windows loaded the frames. The native x64 launcher was built reproducibly from the included source with matching outputs across builds. The setup EXE is unsigned.

## Firmware evidence

Standalone and bundled firmware are **V2.43-LIB03**, SHA-256 `be16c472ba1bdcf46dc6a5b93e218ea74c8baf967aa6cc5e43505efd7ee8fdd9`. The UI executable changes the directory lookup and removes duplicate queries. Its fixed 32-job array occupies 66,560 bytes of BSS; two unsigned words hold the shared allowance. The kernel, audio executable, startup/rescue script and EQ resource remain byte-identical to LIB01. The independent package verifier passed, including permitted paths, hashes, ELF checks and Linux metadata preservation.

The package identifier differs from the original updater's `V2.43` comparison string. The About page still displays `V2.43`. Same-package firmware reinstallation has not received a separate VM acceptance run; a package name alone does not establish that behavior.

The real C directory-reader regression covers four 129-descendant cases, including valid art last behind stale-zero hints. Every case completes in three slices; no lookup or complete prepare scope opens more than 64 candidate songs. The latter shares one allowance across discovery and size lookups. Repeated requests do not expire a no-art result before its 120-second retry time. It also verifies completed-result reuse, in-place media changes and replacement-index invalidation. This host-GCC test does not measure MIPS filesystem latency.

Four separate target-compiled QEMU-user tests pass: original stock paint/lifecycle, bitmap insertion/destructor behavior, player-hook boundaries, and the import-call bridge. The guarded paint/destructor bytes are extracted from the pinned PEQ08 demo. The public runners require explicit stock/toolchain inputs.

A new full-system run booted stock V2.43 from the original SPL, selected the candidate through the original system-update menu, rebooted into original recovery, installed it, and cold-booted the resulting NAND. Every stage used ordinary disposable NAND/card files, with no external kernel, init script or rootfs overlay. Stopped-image readback matched both package payloads. SPL, recovery kernel/rootfs, userid and the cleared OTA partition stayed unchanged. An unrelated virtual-card sentinel survived the update.

The installed player decoded two synthetic 30-second FLAC tracks. Both prepared 330-pixel cover crops matched the expected JPEG pixels exactly. A 130-entry no-art directory scrolled and returned to its parent. The LIB03 follow-up enabled the stock Folder Album Cover option. Its 80-pixel directory cover and both 330-pixel playback covers matched expected pixels exactly. The virtual card had 117,440,512 bytes free after the package was copied (about 112 MiB). Nonzero virtual PCM was captured. This is not audible DAC acceptance. The earlier metadata-only stress files intentionally cannot decode; they were used for browsing, not audio fidelity. All virtual sessions were stopped after testing.

Formatting active native C with clang-format 18.1.8 produced identical hashes for all four rebuilt components before/after formatting. The functional lookup changes were already present for that comparison. Frozen PEQ08 sources were not reformatted.

## Dependency and hardware limits

The media executables and corresponding-source archive are unchanged. The source archive has a new outer filename, `XM5Studio-runtime-source-0.2.2.zip`, and the same bytes as the 0.2.0 source archive. See [runtime distribution](RUNTIME_DISTRIBUTION.md). The project-source archive contains application, firmware-adapter, launcher, packaging and public-test source; it excludes local inputs, binaries, profiles, credentials and research dumps.

No physical card, raw storage or USB communication was used. Physical firmware acceptance, audible DAC output, clean-machine coverage and a complete self-service USB recovery path remain unvalidated. Copying update files does not flash the player. Keep separate backups. Source and component rights remain as stated in [THIRD_PARTY.md](../THIRD_PARTY.md).

## Upgrade-card edge cases

Three separate stock-menu runs used disposable FAT/NAND files. A nearly full card with 8,654,848 bytes free and a 128 MiB card with stale recovery paths but only 75,762,176 bytes free stayed in the upgrade UI without changing non-userdata NAND. A separate 256 MiB card with the same conflicting paths and ample free space proceeded: the stock updater removed an unrelated file inside `recovery-update`, replaced/removed the old info file, and set the OTA flag. This isolates the original updater's cleanup behavior from its space check. The new desktop preflight refuses these paths without touching them.

The original `check_updatefw_exist` compares rounded free GiB from `getDiskMemoryInfo` with 0.10. The desktop reserve therefore includes a conservative 100 MiB floor as well as actual cluster-rounded archive payload allocation and an 8 MiB margin. Tests cover old-file space released on replacement, exact allocation boundaries, conflicts at every phase and free space consumed during copying. No root recovery files are automatically cleaned.
