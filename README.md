# XM5 Studio

A local music manager and desktop companion for the ONIX XM5, with paired storage firmware.

Prepare a card from music folders you already use, organize it on your computer, and bring back playlist and favorite edits from the player. The Windows companion prepares the data; its paired firmware, based on official XM5 V2.43, reads it on the XM5.

For PEQ with the factory music and artwork handling, use [xm5-peq](https://github.com/noxusCastrator/xm5-peq). Copy presets into the card's root `EQ` folder; no companion is needed. The management firmware already includes PEQ. See [which project to choose](docs/PROJECT_CHOICE.md).

## Features

- Keep existing folder structures inside `xm5/music/<source folder>`.
- Create ordered playlists with covers. Bring back playlists and Favorites edited on the player, with explicit conflict choices.
- Add 80-, 160- and 330-pixel images, optional lyrics and original sample-peak information to supported song copies. Keep the original artwork.
- Transfer new or changed files during updates. Unchanged music stays in place, replaced files have backups, and unmanaged card data is preserved.
- Manage PEQ and use basic desktop playback to check tracks. The Windows portable package includes its tools; installed Peace, Mp3tag and foobar2000 are not required.
- Control the running application with function scripts through its console or local client.
- Use the Windows x64 installer or portable ZIP, with the same bundled tools and an original application icon.
- Copy the bundled, verified `update.bin` to an update card. Existing music and other files stay in place. Replacing an existing `update.bin` requires confirmation.

## Usage

Version 0.2.2 is a local candidate awaiting publication. Its release folder contains the installer, portable ZIP, standalone `update.bin`, project source archives and corresponding runtime source ZIP. The paired firmware includes a bounded directory-cover lookup to reduce repeated scans of large folders.

1. Run the installer, or extract the complete portable ZIP and open `XM5Studio.exe`.
2. To prepare the paired player firmware, open **Settings → Player firmware → Create update card**, or copy the standalone `update.bin` yourself. Follow the official XM5 update procedure.
3. Add music folders in **Folders** and preset folders in **PEQ**. Create playlists and choose their covers.
4. In **TF Card**, select sources and a destination, preview, then export. A card reader and the player's USB storage connection use the same layout.
5. Safely disconnect storage before playback. Use **Bring back / Restore** to inspect edits made on the XM5.

Export manages one `xm5` folder containing `music`, `EQ` and `Struct`. It refuses an unrelated existing `xm5` folder; the rest of the card need not be cleared. Export leaves PC originals unchanged. Explicit source-tag and rename actions are separate and can change selected originals.

Read the [desktop guide](docs/DESKTOP.md), [card format](docs/STORAGE_FORMAT.md), [scripting guide](docs/SCRIPTING.md), [build instructions](docs/BUILD_STABLE.md) and [PEQ format](docs/CONFIG_FORMAT.md).

The [installation guide](docs/INSTALLATION.md) explains installed and portable profiles, explicit portable-profile import, upgrades and uninstall. The [update-card guide](docs/FIRMWARE_CARD.md) explains firmware preparation.

## Develop the desktop app

On Windows x64 with Python 3.12 or newer, run `python tools/setup_desktop.py` from this checkout, then run `.\desktop\XM5Library.cmd`. Setup installs verified dependencies; the app runs the editable Python source in `desktop/xm5lib/`. See [development and tests](docs/DEVELOPMENT.md) for the source map, regression suite and packaging steps.

## Warning

This is unofficial modified firmware. The paired storage firmware has software and VM validation, without new physical-device acceptance. Earlier one-device feedback applies to PEQ08. Read the [validation record](docs/VALIDATION_0.2.2.md).

Creating an update card copies only the root `update.bin`. If that file already exists, choose whether to replace it. Other files stay in place. This does not format the drive or flash the player.

Flashing can cause data loss or boot failure. Back up your music and card, and keep the official recovery package and instructions. Recovery is not guaranteed. Update backups and journals do not replace a separate music backup. Keep the paired firmware and companion together: older stock firmware can scan retained backup music.

EQ boosts can clip. Keep the preset's preamp setting and lower it further if needed.

Thanks to Shanling for their technical support and the official recovery package.
