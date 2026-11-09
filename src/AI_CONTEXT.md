# XM5 Management source context

Read the project README and AGENTS.md before changes. This repository contains desktop manager version 0.2.1 and paired V2.43-LIB01 storage firmware version 0.2.0. For desktop edits, use [DEVELOPMENT.md](../docs/DEVELOPMENT.md) to prepare dependencies, run the live source and run the public tests. Firmware build inputs are needed only for firmware work. The standalone stock-media PEQ project is https://github.com/noxusCastrator/xm5-peq .

## Source map

- `../desktop/`: portable companion, backend Engine, Qt views and local command interface.
- `native_storage/`: verified inline artwork reads, original UI hooks, image ownership, relative playlist/favorites exchange and USB ownership gates.
- `audio_storage/`: original sample-peak correction before PEQ, stock EQ ordering and safe stream gates.
- `organizer/`: bounded native FLAC preparation and index rebuilding; ordinary audio and original metadata are preserved.
- `startup/library/`: reconstruction fragments for the preserved rescue startup and embedded organizer helpers.
- `audio/`, `ui/` and the earlier startup sources: pinned PEQ08 prerequisite retained for reproducible builds.
- `../tools/build_stable.py`: complete paired firmware recipe. `../tools/build.py` reconstructs the historical PEQ08 prerequisite.

## Contracts

Use [STORAGE_FORMAT.md](../docs/STORAGE_FORMAT.md) for the actual XM5I layout, per-song cover slots, relative M3U8 rules, optional lyrics/peak data and recoverable file deltas. Source PC files remain unchanged by export; explicit tag/rename actions have their own reviewed plans. Struct is a rebuildable hint for inline song data but also holds persistent playlists and settings.

The managed card uses `xm5/music`, `xm5/EQ` and `xm5/Struct`. Converted player presets are in `xm5/Struct/peq`. The paired firmware bypasses original large-cover conversion inside the managed namespace, admits prepared small JPEGs, and releases hidden image owners. Preserve card-only and missing playlist references, duplicates/order, explicit conflict choices and the USB ownership boundary.

PEQ and stock EQ have separate switches. PEQ supports the documented local PCM representations through192 kHz; stock EQ remains at or below48 kHz. DSD/DoP and unsafe representations bypass PCM DSP. Sample-peak correction is independent of PEQ and does not guarantee freedom from EQ clipping.

## Build and evidence

Follow [BUILD_STABLE.md](../docs/BUILD_STABLE.md) with explicit official inputs and Linux-aware metadata preservation. Keep byte guards, ABI checks, four-overlay constraints, rootfs size limits and exact release hashes. Source manifests cover published files; .local holds ignored inputs and release/evidence copies.

Read [VALIDATION_0.2.0.md](../docs/VALIDATION_0.2.0.md) before making acceptance claims. Published source reproduced the exact release BIN; software and full-system VM checks passed. The new storage firmware has not received physical-device acceptance. PEQ08 owner feedback does not verify it. Documentation maintenance does not authorize hardware operations or mail.

Shanling permitted publication of the project on GitHub. That permission is not product validation or a blanket third-party license; see [THIRD_PARTY.md](../THIRD_PARTY.md).
