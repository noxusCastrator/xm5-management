# XM5 PEQ

Adds a parametric equalizer (PEQ) to the ONIX XM5.

Users can save EQ presets to a TF card, then select and apply them on the player. Compatible Equalizer APO and AutoEQ text presets are supported. The player reads their filter frequencies, gains, Q values and preamp settings for local music playback.

This project is based on the official V2.43 firmware.

The companion XM5 Library application manages local music, playlists, favorites and PEQ presets on a computer. It prepares an `xm5` folder for the player's TF card or USB storage. Your original computer files stay unchanged. See the [desktop guide](docs/DESKTOP.md) and [card format](docs/STORAGE_FORMAT.md).

[Download version 0.2.0](https://github.com/noxusCastrator/XM5_PEQ/releases/tag/v0.2.0): paired firmware and the Windows portable companion. See the [validation record](docs/VALIDATION_0.2.0.md) for test scope and the [build guide](docs/BUILD_STABLE.md) for source builds.

## Features

- Keeps the factory EQ and adds a separate PEQ page. Swipe left from the factory EQ page to open it.
- Independent switches for PEQ and the factory EQ. Can be used separately or together.
- Supports up to 30 active filters.
- PEQ supports local PCM playback up to 192 kHz. The factory EQ remains limited to 48 kHz. PEQ does not apply to DSD playback or USB DAC mode.
- The interface follows the factory XM5 style and supports Simplified Chinese and English.

## Usage

For firmware installation, refer to the [XM5 TF-card update instructions provided by ONIX](https://onixhiend.co.uk/overture-firmware-update/).

1. Create an `EQ` folder at the TF card root. Place your presets inside it as UTF-8 `.txt` files.
2. Swipe left from the factory EQ page to open the PEQ page.
3. Tap `PEQ OFF` to open the preset list, then tap `Rescan` to scan for files.
4. Select a preset and enable PEQ.

For a managed library card, select presets in the companion and export them with the library. The player reads the converted presets from `xm5/Struct/peq`; the original preset files stay in `xm5/EQ`.

See the [configuration documentation](docs/CONFIG_FORMAT.md) for preset formats and limits. To convert other formats, give an AI your EQ settings together with the [AI conversion guide](docs/AI_EQ_CONVERSION.md) provided by this project.

## Warning

This is unofficial modified firmware. The earlier PEQ08 build was confirmed usable on one XM5 owned by the author. Check this release's validation record for the scope of later storage changes. This does not guarantee successful installation or operation on other devices. Flashing may cause data loss or boot failure, and repairs may be necessary. Physical recovery after a failed flash has not been validated for version 0.2.0.

Before flashing, back up your data and keep the official recovery package and instructions.

Excessive EQ gain can cause clipping or distortion, especially when both EQ layers are enabled. Keep the preset's preamp setting and lower it further if necessary.

Thanks to Shanling for their technical support.
