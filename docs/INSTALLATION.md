# Install XM5 Studio

XM5 Studio 0.2.2 is a Windows x64 application. The setup EXE and portable ZIP include Python, Qt, mpv and FFmpeg. They work offline without a developer Python installation.

Run `XM5Studio-0.2.2-Windows-x64-Setup.exe`, choose English or Simplified Chinese, then choose the installation folder and shortcuts. Setup installs for the current Windows account and defaults to `%LocalAppData%\Programs\XM5 Studio`. Use a writable folder on a local fixed drive. The Start menu and desktop shortcuts open `XM5Studio.exe` directly.

When the application opens for the first time, select optional music and PEQ folders or choose **Skip for now**. Folders can also be added later in **Folders** and **PEQ**. Selecting folders registers their locations; source music remains in place. The installer's language seeds a fresh profile. Existing profiles keep their language and settings.

## Profiles

Installed applications keep `config.json`, `data/`, `cache/` and `runtime/` in `%LocalAppData%\XM5Studio`. Moving the program directory does not move the profile. Portable applications keep these beside the application. The two distributions use independent profiles by default.

An explicit `distribution.json` selects `installed` or `portable` mode. Development checkouts without that file retain portable behavior. The GUI, backend, local command client, session discovery and log paths use the same resolver. Process-local `XM5LIB_HOME` selects an explicit profile for scripts or tests, relative to the program directory if necessary. `XM5LIB_BACKEND_HOME` keeps its existing backend-only override.

Before the first installed launch creates a profile, you can choose **Import profile**, **Continue fresh**, or **Cancel**. To import, close the portable application and select its folder containing `config.json` and `data/backend`. The application copies validated settings, playlists, favorites, notes and EQ presets into a new installed profile. Relative music and PEQ folder locations continue to point to the original portable folder; music stays in place. Runtime sessions, discovery tokens, active locks and caches are excluded. The portable folder remains unchanged. A failed import shows its reason and leaves the destination uncreated; it never silently substitutes an empty profile. Existing installed profiles open without this prompt. Keep the portable folder intact, and choose a separate empty program destination for setup; setup refuses to convert a portable folder in place.

## Update and uninstall

Close the application and its scripts before updating or uninstalling. Finish or cancel an active export using the application. Setup refuses while the GUI or backend owns the activity guard and never force-closes it.

Rerun setup for an upgrade or same-version repair. The stable installer identity retains the location and shortcut choices. Before copying, setup checks the old owned-file list, removes unchanged obsolete program files and preserves unrelated files. If an obsolete owned file was modified or its path contains a link, setup refuses and identifies it. Restore that file from the previous package or choose a new empty installation folder. A failed update can require rerunning setup; complete rollback is not promised.

Uninstall removes the files and shortcuts recorded by setup. The profile, original music, PEQ folders and exported card contents remain. Reinstalling opens the retained profile. If installation fails, the automatically generated setup log is in `%TEMP%` under a name beginning `Setup Log`; use `/LOG="C:\path\setup.log"` to choose a log explicitly.

## Build locally

First assemble and review one portable payload with [the build guide](BUILD_STABLE.md). It must have version `0.2.2`, an exact `BUILD-MANIFEST.json`, the reviewed bundled runtime, `XM5Studio.exe` and the project icon at `xm5lib/ui/assets/studio/app.ico`. Build the native launcher with a MinGW-w64 compiler and resource compiler:

```powershell
python tools/build_launcher.py --wsl --output .local/launcher/XM5Studio.exe
```

Omit `--wsl` for native MinGW tools. `--cc`, `--windres` and `--icon` select explicit inputs. The source is in `src/desktop_launcher/`; the PE has a GUI subsystem, an `asInvoker` manifest, version information and the project icon. The launcher calls bundled Python directly with Windows argument quoting, waits for it and returns its exit code. `XM5Library.cmd --check` remains the console diagnostic entry. The launcher receipt records compiler, source, icon and output hashes. MinGW's PE timestamp is disabled for repeatable launcher builds.

The tested compiler is [Inno Setup 6.7.4](https://github.com/jrsoftware/issrc/releases/tag/is-6_7_4). Download `innosetup-6.7.4.exe` from that immutable release and verify SHA-256 `a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c` plus a valid Windows Authenticode signature from Pyrsys B.V., following [the official verification instructions](https://jrsoftware.org/isdl-verify.php). Its upstream `/PORTABLE=1 /CURRENTUSER /NOICONS /TASKS=""` options put the compiler in a chosen local directory without associations or an uninstall registration. Pass an explicit `/DIR` below `.local/`; do not launch its IDE during packaging. Retain the compiler's `License.txt`. Its license permits use and redistribution subject to the included attribution conditions; commercial users are asked to support its maintainers.

Inno Setup 6.7.4's installer does not bundle Simplified Chinese messages. Obtain the [translation linked by Inno Setup](https://jrsoftware.org/files/istrans/) from [Zhenghan Yang's translation repository](https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation), retain its attribution, and pass it as a local build input. The tested UTF-8 file has SHA-256 `bf0751fa176569c6faa2f6e17ed2734617bef325d5cc06eae030fdd0258ee778`. The build receipt pins the exact translation used.

```powershell
python tools/build_installer.py `
  --payload C:\build\XM5Studio `
  --stage-dir .local/installer-stage `
  --output .local/releases/XM5Studio-0.2.2-Windows-x64-Setup.exe `
  --iscc C:\build\inno-6.7.4\ISCC.exe `
  --chinese-messages C:\build\ChineseSimplified.isl
```

Both output and staging locations must be new. The builder verifies every input hash and rejects extra files, profiles, generated caches and links. It copies into separate staging, changes the distribution descriptor to installed mode, generates the exact owned-file inventory and compiles `tools/installer/XM5Studio.iss`. It does not download tools, install drivers, communicate with a player or write to a card. `--local-allow-unreviewed` exists for synthetic packaging tests; those outputs are not public distributions.

The setup build receipt records payload, compiler, translation, script and output hashes. The setup EXE is currently unsigned; signing and clean-machine acceptance remain release decisions. Keep the corresponding-source archive and component notices with the binary downloads. This installer does not change the paired firmware's hardware-validation status.
