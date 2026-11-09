Use Windows x64 and an installed Python 3.12 or newer. From the repository root, run:

```powershell
python tools/setup_desktop.py
.\desktop\XM5Library.cmd
```

Setup downloads the exact approved [0.2.0 portable application](https://github.com/noxusCastrator/xm5-management/releases/tag/v0.2.0), checks its SHA-256 against the pinned release, and verifies the runtime manifest and every dependency file. It installs only `desktop/vendor/`, omitting generated Python bytecode and removing those cache entries from the installed manifest. The dependency base remains 0.2.0 when editing later project versions. The ZIP cache and temporary staging stay under ignored `.local/dev-setup/`. Source files, artwork and application profiles are left intact.

Desktop source version 0.2.1 keeps paired firmware version 0.2.0. Edit the Python files under `desktop/xm5lib/`, close the application, and run `.\desktop\XM5Library.cmd` again. The bundled isolated Python runtime imports those live repository files through its existing `python314._pth`; no Python, Qt or media compilation is needed. Windows x64 is the only tested desktop platform.

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
