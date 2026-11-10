Use Windows x64 and an installed Python 3.12 or newer. From the repository root, run:

```powershell
python tools/setup_desktop.py
.\desktop\XM5Library.cmd
```

Setup downloads the exact approved [0.2.0 portable application](https://github.com/noxusCastrator/xm5-management/releases/tag/v0.2.0), checks its SHA-256 against the pinned release, and verifies the runtime manifest and every dependency file. It installs only `desktop/vendor/`, omitting generated Python bytecode and removing those cache entries from the installed manifest. The dependency base remains 0.2.0 when editing later project versions. The ZIP cache and temporary staging stay under ignored `.local/dev-setup/`. Source files, artwork and application profiles are left intact.

Edit the Python files under `desktop/xm5lib/`, close the application, and run `.\desktop\XM5Library.cmd` again. The bundled isolated Python runtime imports those live repository files through its existing `python314._pth`; no Python, Qt or media compilation is needed for Python edits. Windows x64 is the only tested desktop platform. The 0.2.2 paired firmware adds bounded directory-cover lookup; its hash is separate from 0.2.0.

To exercise update-card preparation from source, copy the matching release's standalone `update.bin` to `desktop/firmware/update.bin`. It is an ignored binary input; `desktop/firmware/manifest.json` pins its hash, size and paired version. The dialog reports missing or changed firmware instead of allowing preparation. Public tests use synthetic temporary directories and never require a real card. Build a native launcher and installer using [INSTALLATION.md](INSTALLATION.md).

| Source | Purpose |
| --- | --- |
| `desktop/bootstrap.py` | Isolated runtime entry and dependency check |
| `desktop/xm5lib/ui/` | Qt windows, pages and application service |
| `desktop/xm5lib/core/` | Library data, artwork, export and synchronization |
| `desktop/xm5lib/automation/` | Local backend actions and automation API |
| `desktop/xm5lib/ui/console_*.py`, `desktop/xm5lib/control/` | In-app console and control of the open session |
| `tests/`, `tools/run_desktop_tests.py` | Public regression tests and their runner |

For an offline setup, copy the same release ZIP to your computer and run:

```powershell
python tools/setup_desktop.py --archive C:\downloads\XM5Library-Windows-x64-0.2.0.zip
```

Setup accepts an existing `desktop/vendor/` only when its dependency files match the approved runtime. It ignores generated Python bytecode caches, and refuses other changes, missing files, extra files and links without replacing or deleting them. Review such a directory manually or use a fresh source checkout. To check the installed files without downloading or writing:

```powershell
python tools/setup_desktop.py --check-only
.\desktop\XM5Library.cmd --check
```

Run the public regression suite with the bundled interpreter:

```powershell
.\desktop\vendor\python\python.exe -B tools/run_desktop_tests.py
```

For the optional GUI smoke test, run setup first, then add `--gui`. It opens a native Qt window on Windows and uses NULL audio output:

```powershell
.\desktop\vendor\python\python.exe -B tools/run_desktop_tests.py --gui
```

The setup helper's ZIP and failure-handling tests can also run with the installed Python:

```powershell
python -m unittest discover -s tests -p test_setup_desktop.py
```

The binary dependencies come from the public release, which also supplies their corresponding-source archive. See [runtime distribution](RUNTIME_DISTRIBUTION.md) for its review and licenses. There is no repository-wide LICENSE; see the [component provenance and license boundaries](../THIRD_PARTY.md). For a separate local portable package after editing, follow [the build guide](BUILD_STABLE.md), including its additional requirements for public distribution.

## Storage and native regressions

The default public suite includes source/card preservation, stale plans, independent playlist edits, corrupted rollback backups and an interruption matrix. The matrix cuts before/after observed copy, rename, journal-save and unlink boundaries, disconnects the source, resumes or rolls back, then checks replay. These simulate process interruption; they do not simulate torn sectors or physical power-loss durability.

On Linux/WSL, run the real C directory reader with synthetic cards and host GCC:

```sh
python3 tools/run_native_directory_tests.py
```

For the target-compiled lifecycle, bitmap, player-hook and import-bridge tests, supply the pinned compiler/QEMU and reconstructed PEQ08 demo from [BUILD_STABLE.md](BUILD_STABLE.md):

```sh
python3 tools/run_native_boundary_tests.py --toolchain /path/to/toolchain/root \
  --qemu /path/to/qemu-mipsel-static --peq08-demo /path/to/peq08/demo
```

The runner extracts original paint/destructor bytes only from the hash-checked demo into a temporary build. No stock binary is committed. The [source map](SOURCE_MAP.md) distinguishes active firmware code from frozen prerequisites. Historical 146-case desktop and 3027-assertion audio records remain historical evidence, not the count of this public suite.
