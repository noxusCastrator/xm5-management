# Release files and checks

The source repository and development workspace are separate. Only the reviewed source snapshot enters the repository. Firmware inputs, compiler installations, device dumps, working databases and dependency archives remain local.

After integrating the final source, validate the actual compiled player and application path. Record host tests, target ABI tests, whole-system VM results, package checks and physical-device observations separately. A previous successful PEQ08 installation does not verify the later storage code on hardware. Hardware testing scope belongs in the release notes.

Version `0.2.2` uses firmware identifier `V2.43-LIB03`. The original `0.2.0` used `V2.43-LIB01`. The exact four firmware overlays, original V2.43 input hash and final `update.bin` hash belong in `release.json`. A version name is not evidence of correctness.

Release assets are:

- `update.bin`: the actual final firmware image.
- `XM5Studio-0.2.2-Windows-x64-Portable.zip`: a clean, relocated portable application with reviewed runtime dependencies.
- `XM5Studio-0.2.2-Windows-x64-Setup.exe`: the matching Windows x64 installer.
- `XM5Studio-0.2.2-source.zip` and `XM5Studio-0.2.2-source.tar.gz`: the project source at the tagged commit.
- `XM5Studio-runtime-source-0.2.2.zip`: the corresponding source for bundled media executables.
- `SHA256SUMS.txt`: hashes of the actual uploaded files.

Keep 0.2.2 marked as a pre-release. Software and VM acceptance do not establish physical-device acceptance. Upload all assets to a draft, compare their server-reported SHA-256 digests with the local checksums, then publish. Never overwrite an existing published tag or release to retry a failed upload.

Before uploading, verify hashes after ZIP creation and extraction. The portable app must launch using its relative bundled paths from a fresh directory, with no development environment or user database included. Keep the firmware's warnings, supported presets and known limitations visible. Preserve Shanling's publication correspondence locally; its permission to put this project on GitHub is not a claim of product validation.

Publish this project's reviewed source and release files from the `xm5-management` repository. Standalone PEQ belongs to the separate public `xm5-peq` repository; reverse-engineering evidence belongs to private `xm5-decomposition`. Local inputs and profiles stay in ignored `.local/` directories.

Desktop-only maintenance releases can retain the previously published paired firmware and dependency binaries. Record that baseline separately in `release.json`, rerun the public desktop tests and relocated-package checks, and retain the exact corresponding-source archive alongside the new desktop ZIP. Do not present inherited firmware or hardware evidence as a new test.

A packaging script must not commit, push, change repository visibility, send mail or operate the player by itself.
