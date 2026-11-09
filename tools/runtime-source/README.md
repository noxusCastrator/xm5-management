# Minimal Windows local-audio runtime

This build replaces the previous general-purpose GPL media executables. It builds mpv at commit `902818753849d9ad8f0096fad6ca5aeaa37113b9` with GPL code disabled and FFmpeg 8.0.3 without GPL, nonfree, version3, networking or external codec libraries. The package is for the XM5 companion's local-audio workflow. It is not a full-featured mpv distribution.

The runtime source package retains the exact upstream archives listed in `SOURCES.json`, the compatibility patch, complete build commands, compiler versions, configuration and license texts. All non-system libraries in the two executables come from this source set. Standard MinGW/GCC compiler runtime code uses its original runtime licenses and exceptions. No Vulkan loader or GPU backend is shipped; Vulkan-Headers are declarations required by libplacebo's disabled-backend stubs.

Use a fresh Ubuntu 24.04/WSL build environment. Install the build tools:

```sh
sudo apt-get install gcc-mingw-w64-x86-64-posix g++-mingw-w64-x86-64-posix \
    meson ninja-build nasm cmake pkg-config python3 python3-jinja2 make gpg
```

If using the accompanying source ZIP, copy its `sources` directory into `/var/tmp/xm5-runtime-build-20261108/sources` before running the scripts. The downloader verifies those cached archives against the frozen manifest; existing archives are not fetched again. The signature verifier also uses its cached public key and signature. Build output is confined to that fixed directory. Preserve any existing build there before starting a separate fresh reconstruction.

```sh
python3 download_sources.py
python3 verify_source_release.py
python3 build_runtime.py --phase all
```

The result is `prefix/bin/mpv.exe` and `prefix/bin/ffmpeg.exe` under the build directory. Build commands use at most eight jobs. The source archives are unchanged. The small mpv patch accepts only the disabling values `load-scripts=no`, `osc=no` and `ytdl=no` when the corresponding scripting engines were compiled out; attempts to enable them remain unsupported. It also identifies this modified build in the version string. The recipe records the C++ standard-runtime link dependency for static libplacebo in its pkg-config metadata.

On Windows, run `validate_windows.py` with the two executables, the companion source and an empty output directory. Its bundled synthetic fixtures avoid a dependency on another encoder. It uses the actual companion Session class and always selects null output. It checks local audio formats, Unicode paths, peak analysis, picture preparation, per-channel PEQ at 44.1/192 kHz, JSON IPC control, output enumeration and process cleanup. It does not claim real analog output or physical XM5 acceptance.

`prepare_runtime.py` combines only the unchanged Python/Qt files from the earlier hash-locked portable runtime with these source-built media executables. It creates a new runtime manifest, updated dependency lock, corresponding-source ZIP and exact-binary source review. The application itself remains source files, and its Qt libraries remain replaceable. The resulting source ZIP must accompany the public portable binary download on the release page. Keep the original Qt, Python, Microsoft and other dependency notices as well; the media source package does not relicense them.

Build dates and compiler/tool versions can affect binary hashes. The recorded release hashes identify the tested artifacts; they do not promise bit-for-bit reproduction with a different compiler. The release source includes all material needed to rebuild the media components and replace either executable.
