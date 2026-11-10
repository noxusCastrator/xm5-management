# Provenance and license boundaries

The PEQ core used by the audio and UI components derives from [DiamondBond/q2-ringnav](https://github.com/DiamondBond/q2-ringnav), revision `d127c6ea07af20bde73dd842df255ce58d52e5be`, under MIT. LICENSE and UPSTREAM.md files are retained beside the relevant sources. That license applies to the components it covers.

The project contains custom player adaptation, text compatibility handling, and native UI bindings. Minimal AWTK interface declarations and related adaptation still require a file-level provenance and license review. Equalizer APO application source is not included; full application compatibility is not claimed.

Builders obtain stock V2.43 executables, artwork, fonts, and system files from their own official input. The source tree includes only the added startup hook and insertion metadata, not the complete stock S90play script. Modified firmware contains stock material. Local builds and release files are kept under ignored `.local/`.

There is no repository-wide LICENSE yet. Shanling agreed that this project could be published on GitHub on 2026-10-13. That permission is not product validation or a license for unrelated third-party components. Stock software and artwork retain their owners' rights. Desktop runtime components retain their own licenses and corresponding-source requirements; see [runtime distribution](docs/RUNTIME_DISTRIBUTION.md).

The companion desktop source includes the exact UI asset files listed in `tools/portable/ASSET-BASELINE.json`. These include vendor-owned images and compiled layouts plus project-created case art. Their original asset notice is retained; Shanling's project publication permission does not grant rights to unrelated third-party fonts or components. Firmware fonts are not included in the desktop asset set.
