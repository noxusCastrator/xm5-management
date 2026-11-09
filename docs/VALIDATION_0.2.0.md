# Version 0.2.0 validation

This record covers the paired `V2.43-LIB01` firmware and Windows x64 XM5 Library application. Software checks and full-system virtual-device checks passed. The new storage firmware has not been installed on a physical XM5 during this validation. The owner's successful PEQ08 installation applies to that earlier build.

## Desktop

The final desktop acceptance ledger covers 146 distinct cases. These exercise real Qt controllers, the authenticated localhost API and the application Engine, with explicit overlap between focused and carried tests. The final changes were rechecked through the visual export page and normal HTTP entry.

Checks include folder export, playlists with repeated or missing entries, favorites, native rename/return, conflict choices, persistent card-only references, cancellation, insufficient space and resume/rollback after interrupted renames. Independent review reproduced and closed a mutable-journal integrity defect. Unchanged music retained its inode and bytes during a delta update with audio-copy and analysis calls trapped. Changed targets and external recovery edits blocked replacement.

The final Windows ZIP was extracted to another path. Its bundled launcher check, runtime manifest, complete ZIP inventory and privacy scan passed. The application and source distribution contain no user profile, music database, local connection token or private workstation path. Computer audio tests used a null output; they do not verify physical WASAPI output.

## Player

Target-compiled tests include the actual stock libcstl iterator ABI, seven stock-libc stat/error cases, 16 native-reader cases, 18 playlist/favorite cases, image ownership/failure paths and the original image-paint implementation. The audio payload passed 3,027 MIPS assertions, including the retained PEQ behavior and inline gain settings. These are simulated target tests, not measurements of physical 192 kHz throughput or analog distortion.

The full-system VM used the original SPL, partition layout, recovery and kernel. The final firmware package passed the normal stock TF-update path, recovery installation and SPL cold boot without diagnostic application overlays. Readback checks compare the installed kernel/rootfs with the selected package and check that SPL and recovery partitions were preserved. This does not prove recovery from every physical flash failure.

The production application also ran a bounded artwork workload with the original system and a synthetic card. Eleven image crops matched the expected prepared JPEGs with a maximum channel error of three: directory and track thumbnails, custom playlist art, album rows, all-music rows, favorites and both computer-prepared and native-prepared playback covers. Inline timed lyrics and native preparation were exercised. Original song metadata and audio frames were preserved.

The same run recorded 1,196 complete guest memory samples. Peak application RSS was 30,320 KiB, lifetime high-water RSS was 30,548 KiB, and minimum guest MemAvailable was 67,144 KiB. Across six page hide/reopen snapshots, RSS changed from 30,244 to 30,304 KiB and then stayed at 30,304 KiB for the following 84 samples. The process did not restart and no OOM was observed. This short workload is not a long-duration leak test. It did not separately capture a 160-pixel widget; the 160 JPEG path was covered by decoder/reader tests.

## Packaging

The official V2.43 input is pinned to SHA-256 `8b3f05155840c7b821f3bcbcd4690eaf64a00e867c7c3cef68f7be1959368be4`. The package changes exactly four files, listed with final hashes in [release.json](../release.json). Independent verification checked original content, file modes, owners, links, ELF compatibility and the original rootfs size limit. The kernel is unchanged. The original rescue startup hook remains present.

Published source scripts independently reproduced all four overlay files. The rootfs retains 128 KiB LZO blocks and uses the stronger LZO1X-999 encoder; the stock kernel mounted it during the real virtual-device upgrade. The corresponding-source archive accompanies the exact source-built desktop media executables. Other runtime dependencies retain their notices and pinned manifests.

Back up music and card contents before use. Native preparation currently writes FLAC; other supported song profiles are prepared on the computer. Physical USB ownership timing, long-duration hardware behavior and physical recovery after a failed installation remain unvalidated.
