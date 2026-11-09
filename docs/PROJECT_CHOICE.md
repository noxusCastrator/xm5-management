# Choose an XM5 project

Both projects add PEQ to the ONIX XM5. Choose how you want to manage your music.

| Project | Use it for |
| --- | --- |
| [xm5-peq](https://github.com/noxusCastrator/xm5-peq) | PEQ with the factory music-library and artwork paths. Copy compatible preset text files into the card's root `EQ` folder. No companion application is needed. |
| [xm5-management](https://github.com/noxusCastrator/xm5-management) | A computer-managed library with prepared artwork, playlists and Favorites that can be brought back from the player. Use the portable companion with its paired firmware, which already includes PEQ. |

Management does not require a separate xm5-peq installation. Each project supplies a complete firmware image for its workflow.

## What management changes

Selected source folders keep their internal structure under `xm5/music`; original EQ inputs go into `xm5/EQ`, and generated data goes into `xm5/Struct`. Supported song copies carry small 80-, 160- and 330-pixel images, optional lyrics and original sample-peak information. Original audio, tags and artwork are retained. Unsupported container profiles stay unchanged and are reported as unprepared.

Ordinary relative playlists keep their order and repeated entries. Favorites is a special playlist. Device-added songs can remain as missing references on the computer without pulling their audio. Conflicts require a choice between the computer version, the player version or a separate playlist.

Updates stage changed files and metadata, keep unchanged music in place, and retain replaced-file backups. Export leaves PC originals unchanged. Explicit source-tag and rename actions can change selected originals.

The Windows portable package needs no installed Peace, Mp3tag or foobar2000. Local scripts control an already-running application.

## Downloads and limits

Download [management 0.2.0](https://github.com/noxusCastrator/xm5-management/releases/tag/v0.2.0) or choose a [standalone PEQ release](https://github.com/noxusCastrator/xm5-peq/releases).

The new management firmware has software and VM validation, not physical-device validation. Earlier owner feedback applies to PEQ08. Keep backups and official recovery materials. Use the paired firmware with managed cards; older stock scanners can discover retained backup music.

See [desktop usage](DESKTOP.md), [storage format](STORAGE_FORMAT.md), [scripting](SCRIPTING.md), [build instructions](BUILD_STABLE.md) and [validation](VALIDATION_0.2.0.md).
