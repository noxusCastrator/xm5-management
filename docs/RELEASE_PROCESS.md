# Release files and checks

The source repository and development workspace are separate. Only the reviewed source snapshot enters the repository. Firmware inputs, compiler installations, device dumps, working databases and dependency archives remain local.

After integrating the final source, validate the actual compiled player and application path. Record host tests, target ABI tests, whole-system VM results, package checks and physical-device observations separately. A previous successful PEQ08 installation does not verify the later storage code on hardware. Hardware testing scope belongs in the release notes.

The final package uses identifier `V2.43-LIB01` for project version `0.2.0`. Its exact four firmware overlays, original V2.43 input hash and final `update.bin` hash belong in `release.json`. A version name is not evidence of correctness.

Release assets are:

- `update.bin`: the actual final firmware image.
- `XM5-0.2.0-firmware.zip`: that same image with the release notes and checksums.
- `XM5Library-Windows-x64-0.2.0.zip`: a clean, relocated portable application, only when its runtime distribution review is complete.
- `SHA256SUMS.txt`: hashes of the actual uploaded files.
- The corresponding-source archives required by the exact bundled runtime, when applicable.

Before uploading, verify hashes after ZIP creation and extraction. The portable app must launch using its relative bundled paths from a fresh directory, with no development environment or user database included. Keep the firmware's warnings, supported presets and known limitations visible. Preserve Shanling's publication correspondence locally; its permission to put this project on GitHub is not a claim of product validation.

Publish this project's reviewed source and release files from the `xm5-management` repository. Standalone PEQ belongs to the separate public `xm5-peq` repository; reverse-engineering evidence belongs to private `xm5-decomposition`. Local inputs and profiles stay in ignored `.local/` directories.

Desktop-only maintenance releases can retain the previously published paired firmware and dependency binaries. Record that baseline separately in `release.json`, rerun the public desktop tests and relocated-package checks, and retain the exact corresponding-source archive alongside the new desktop ZIP. Do not present inherited firmware or hardware evidence as a new test.

A packaging script must not commit, push, change repository visibility, send mail or operate the player by itself.
