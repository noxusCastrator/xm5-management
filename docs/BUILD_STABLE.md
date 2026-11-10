# Building the library components

The original [PEQ build](BUILD.md) describes the 0.1.0-a baseline. Preserve its source and metadata when rebuilding that historical image. The new source directories are `src/native_storage` (player UI and storage bindings), `src/audio_storage` (audio/peak adapter), `src/organizer` (on-player preparation) and `desktop` (portable companion source). Original firmware and compiler binaries are not committed.

Use Linux or WSL with Python 3.12+, bash and the existing GCC 12 MIPS32r2 O32 FPXX nan2008 toolchain. GNU MIPS binutils must be available in the supplied binutils directory. Supply the stock V2.43 rootfs read-only and a PEQ08 `demo` reconstructed from the earlier release. The required pins are:

```text
stock update.bin: 8b3f05155840c7b821f3bcbcd4690eaf64a00e867c7c3cef68f7be1959368be4
stock hciplayer:  edec6c420a792bd4b4e47df3b34a5bf5fbb518a07cb1c8509ec1e50cb87e1f2d
PEQ08 demo:       e3596659301a04e61882506c0ff2c48013866ca800ceff5e67c531486feb7df3
```

Build separate copies with explicit inputs:

```sh
python3 tools/build_storage_components.py \
  --stock-rootfs /path/to/stock/rootfs \
  --peq08-demo /path/to/peq08/demo \
  --toolchain /path/to/toolchain/root \
  --binutils /path/to/binutils/usr/bin \
  --output /path/to/new/component-build
```

`--workspace /path/to/existing/workspace` is an optional shortcut for those same read-only baseline locations. It is not required by the published source. A private directory named `workspace` is created inside the new output to preserve the adapters' relative source layout. Existing output is refused. The script builds `demo`, `hciplayer.candidate`, `libxm5inline.so` and `xm5-organize`, records their hashes in `COMPONENTS.json`, and never opens a device or card.

These component files are not an `update.bin`. To reconstruct the paired firmware, run:

```sh
sudo python3 tools/build_stable.py --stock /path/to/official/update.bin \
  --toolchain /path/to/toolchain/root --binutils /path/to/binutils/usr/bin \
  --output /path/to/new/firmware-build
```

The builder reconstructs the pinned historical PEQ08 input first, builds the new components, recreates the exact RX3 startup base from the published insertion fragments, and then embeds the organizer library and shell entry. The rescue hook and existing upgrade lifecycle remain present. Helpers are restored to private RAM files at boot; no new flash paths are added. An optional `--peq08-build` points to an existing completed historical build workspace with the same hash-pinned inputs.

Final packaging preserves Linux ownership, permissions, symlinks and the original kernel. It permits exactly four overlay paths and runs ELF, rootfs-size and independent package checks. Output is `package/out/final/update.bin` inside the requested build directory, with `release-reconstruction.json` recording its hashes. Do not substitute historical PEQ08 hashes for a later package. Source build and package verification do not establish physical-device stability or recoverability.

The package tag and release version come from this checkout's `release.json`. The replacement 0.2.2 candidate uses `V2.43-LIB03`; the 0.2.0 historical image used `V2.43-LIB01`. LIB02 was an earlier local candidate. Use [SOURCE_MAP.md](SOURCE_MAP.md) to distinguish the active storage code from the frozen PEQ08 build inputs. Public host/target native test commands are in [DEVELOPMENT.md](DEVELOPMENT.md).

To edit and run the Windows desktop source, start with [the development guide](DEVELOPMENT.md). Firmware inputs, Linux and a MIPS toolchain are not needed for desktop development. Setup can reuse the approved public dependency runtime without compiling Qt or the media tools.

To rebuild those media dependencies, use [the runtime recipe](../tools/runtime-source/README.md), then combine them with the unchanged Python/Qt dependency runtime using its `prepare_runtime.py`. The exact archive URLs, versions and hashes are recorded in the resulting lock and runtime manifest. Assemble the final source and reviewed runtime:

```powershell
python tools/build_portable.py --source desktop `
  --runtime-dir C:\path\to\reviewed\runtime `
  --firmware C:\path\to\matching\update.bin `
  --launcher C:\path\to\XM5Studio.exe `
  --output C:\path\to\new\XM5Library `
  --zip C:\path\to\new\XM5Studio-0.2.2-Windows-x64-Portable.zip `
  --public --source-review C:\path\to\RUNTIME-SOURCE-REVIEW.json
```

The builder copies only source files and runtime-manifest-listed dependency files. It excludes profiles, databases, caches, credentials and logs. Run `XM5Library.cmd --check` in the extracted output to verify its bundled runtime; no installed Peace, Mp3tag or foobar2000 is used.

The 0.2.2 payload includes the manifest-pinned paired firmware, native launcher, icon and a portable distribution descriptor. Firmware mismatch fails before packaging. Use [the installation guide](INSTALLATION.md) to create the native launcher and wrap the same payload in an offline installer. Installer compilation requires Inno Setup 7.1.1 or newer for the full dependency tree's long paths.

The historical `tools/portable/assemble.py` and its original download lock remain available for reconstructing the older dependency baseline. Its general-purpose GPL media executables are not the 0.2.0 release runtime. Public runtime builds require the exact executable/source-archive review shown above. See [runtime distribution](RUNTIME_DISTRIBUTION.md). Include the corresponding-source ZIP alongside the portable binary download.
