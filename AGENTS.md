# XM5 Management

This public project contains the local music manager and its paired XM5 storage firmware. Read README.md for the user workflow, docs/STORAGE_FORMAT.md for the on-card contract, docs/BUILD_STABLE.md for source builds, and docs/SCRIPTING.md for the local command interface.

The standalone stock-media PEQ alternative is the sibling xm5-peq project. Here, preserve existing music sources during export, relative playlist references, card-only/missing entries, explicit conflict choices and recoverable per-file delta updates. Original artwork/audio remain intact; the paired player uses prepared small artwork.

Keep local release files, official inputs, profiles and build products under ignored .local/. Source manifests cover tracked publishable files only. Repository/documentation changes do not alter the published 0.2.0 BIN or portable-runtime hashes.

The 0.2.0 storage firmware passed software and full-system VM acceptance. It has not received new physical-device acceptance. No device/card operation or outgoing email is authorized by repository maintenance. Stock software/assets and third-party components retain their own rights.
