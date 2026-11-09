# XM5 Management

A local music manager and paired firmware for a better ONIX XM5 experience.

Prepare a card from music folders you already use, organize it on your computer, and bring back playlist and favorite edits from the player. The Windows companion prepares the data; its paired firmware, based on official XM5 V2.43, reads it on the XM5.

For PEQ with the factory music and artwork handling, use [xm5-peq](https://github.com/noxusCastrator/xm5-peq). Copy presets into the card's root `EQ` folder; no companion is needed. The management firmware already includes PEQ. See [which project to choose](docs/PROJECT_CHOICE.md).

## Features

- Keep existing folder structures inside `xm5/music/<source folder>`.
- Create ordered playlists with covers. Bring back playlists and Favorites edited on the player, with explicit conflict choices.
- Add 80-, 160- and 330-pixel images, optional lyrics and original sample-peak information to supported song copies. Keep the original artwork.
- Transfer new or changed files during updates. Unchanged music stays in place, replaced files have backups, and unmanaged card data is preserved.
- Manage PEQ and use basic desktop playback to check tracks. The Windows portable package includes its tools; installed Peace, Mp3tag and foobar2000 are not required.
- Control the running application with function scripts through its console or local client.

## Usage

Download the paired firmware and portable application from [version 0.2.0](https://github.com/noxusCastrator/xm5-management/releases/tag/v0.2.0).

1. Install `update.bin` using the official XM5 update procedure.
2. Extract the complete application into a writable folder and run `XM5Library.cmd`.
3. Add music folders in **Folders** and preset folders in **PEQ**. Create playlists and choose their covers.
4. In **TF Card**, select sources and a destination, preview, then export. A card reader and the player's USB storage connection use the same layout.
5. Safely disconnect storage before playback. Use **Bring back / Restore** to inspect edits made on the XM5.

Export manages one `xm5` folder containing `music`, `EQ` and `Struct`. It refuses an unrelated existing `xm5` folder; the rest of the card need not be cleared. Export leaves PC originals unchanged. Explicit source-tag and rename actions are separate and can change selected originals.

Read the [desktop guide](docs/DESKTOP.md), [card format](docs/STORAGE_FORMAT.md), [scripting guide](docs/SCRIPTING.md), [build instructions](docs/BUILD_STABLE.md) and [PEQ format](docs/CONFIG_FORMAT.md).

## Warning

This is unofficial modified firmware. Version 0.2.0 has software and VM validation; its new storage firmware has not been physically tested. Earlier one-device feedback applies to PEQ08. Read the [validation record](docs/VALIDATION_0.2.0.md).

Flashing can cause data loss or boot failure. Back up your music and card, and keep the official recovery package and instructions. Recovery is not guaranteed. Update backups and journals do not replace a separate music backup. Keep the paired firmware and companion together: older stock firmware can scan retained backup music.

EQ boosts can clip. Keep the preset's preamp setting and lower it further if needed.

Thanks to Shanling for their technical support and the official recovery package.
