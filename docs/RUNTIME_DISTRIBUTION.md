# Desktop runtime distribution

The XM5 Studio 0.2.2 candidate reuses the exact 0.2.0 media binaries. Its `XM5Studio-runtime-source-0.2.2.zip` is byte-identical to `XM5Library-runtime-source-0.2.0.zip` (SHA-256 `7f792e9c34b68e719f95f94d9f56f0f6ebaa3c083dc83d8439477697fa8b3357`); the internal 0.2.0 directory name remains. Both names refer to the same corresponding sources. The separate project-source ZIP includes the native launcher and installer scripts. See [the installation guide](INSTALLATION.md) for the Inno Setup 7.1.1 tool input and launcher build recipe.

The companion uses separate mpv and FFmpeg processes, plus an embedded CPython/PySide6 runtime. Each component retains its original license. Shanling's project publication permission does not relicense unrelated components, and the repository has no blanket license for all project and vendor material.

The 0.2.0 media runtime is built from the exact sources in `tools/runtime-source/SOURCES.json`. It uses mpv commit `902818753849d9ad8f0096fad6ca5aeaa37113b9` with GPL code disabled and FFmpeg 8.0.3 without GPL, nonfree, version3 or network features. It replaces the earlier general-purpose GPL CI binaries rather than redistributing their unresolved dependency inventory.

The same release must contain `XM5Library-runtime-source-0.2.0.zip`. It retains upstream archives, original license texts, exact changes, build commands, compiler versions and configuration for every non-system media library. This includes mandatory libass/libplacebo dependencies even though the application does not use their subtitle/GPU features. Both static media processes can be rebuilt and replaced independently. The integrity manifest can be regenerated for a replacement; it does not prohibit modification or reverse engineering.

The small mpv compatibility patch accepts only the disabling values of scripting options passed by this application. Lua, JavaScript and GPU backends are not added. Actual Windows checks use the existing application controller to verify JSON IPC, local audio formats, peak analysis, image preparation and PEQ at 44.1/192 kHz. All playback tests use null output; WASAPI availability is enumerated separately.

The [runtime source recipe](../tools/runtime-source/README.md) describes reconstruction. Source archives must remain available alongside the corresponding portable binary release. The [FFmpeg licensing page](https://ffmpeg.org/legal.html) explains its upstream licensing and distribution context.

The portable packager accepts an `xm5.runtime-source-review.v1` manifest only when it identifies the exact tested mpv and FFmpeg executable hashes and hashes the accompanying source archive. The known-incomplete legacy media package is not approved by renaming or relabeling it. This technical inventory does not certify unrelated upstream licenses or physical player behavior.

CPython's license, the Qt wheel license files, Microsoft runtime terms and other dependency notices remain required. Qt libraries remain dynamically replaceable in the portable directory. Review the exact versions in the lock file, not a generic package name, before publishing a bundled binary.
