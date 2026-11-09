# Building 0.1.0 a

Use Linux / WSL with Python 3.12 or newer, bash, GNU tar, squashfs-tools 4.6.1 with LZO support, and the project's GCC 12 mipsel-linux-gnu toolchain. The toolchain root must contain `usr/bin/mipsel-linux-gnu-gcc-12`, binutils, and its host runtime libraries. The existing O32, MIPS32r2, FPXX, nan2008, and fixed-address linker settings are retained.

Supply official ONIX XM5 V2.43 `update.bin` locally. Its required SHA256 is:

```text
8b3f05155840c7b821f3bcbcd4690eaf64a00e867c7c3cef68f7be1959368be4
```

Firmware and toolchains must not be committed to Git. Run from the repository root:

```sh
sudo python3 tools/build.py --stock /path/to/official/update.bin --toolchain /path/to/toolchain/root
```

The script extracts the input into its own `build/work/`, compiles audio and UI payloads, inserts the project startup hook, and repackages four explicitly permitted overlays. It retains baseline hashes, patch-byte guards, ELF checks, original UI subtree checks, Linux metadata checks, and the rootfs size budget. Package staging uses Linux `/var/tmp`. It does not connect to the player, write a card, flash firmware, or access the development workspace.

Output: `build/0.1.0-a/update.bin`. All four overlays and the final image must match the hashes in `release.json`; otherwise the build stops. Before another build, preserve or explicitly move existing `build/work/` and `build/0.1.0-a/` outputs. The script does not automatically delete old results.

The public version is `0.1.0 a`, with planned tag `v0.1.0-a`. The image retains firmware identifier `V2.43-PEQ08`; naming the release did not modify the installed artifact. `dist/` and `build/` are ignored by Git. Tool versions can affect reproducibility. Matching hashes establish reconstruction, not hardware stability or recoverability.
