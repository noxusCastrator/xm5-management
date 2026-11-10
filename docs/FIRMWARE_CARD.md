# Create an update card

XM5 Studio can prepare a removable drive containing the bundled paired firmware. Open **Settings → Player firmware → Create update card**. The dialog shows the application version, paired firmware version and firmware SHA-256 from the installed package.

It copies only `update.bin` to the root and preserves other files. The [official ONIX update procedure](https://onixhiend.co.uk/overture-firmware-update/) requires this root file; it does not require an empty or formatted card. The paired firmware has no new physical-device acceptance. See the [release validation record](VALIDATION_0.2.2.md).

## Prepare the card

1. Keep a separate backup of your music. Creating an update card preserves music, playlists, hidden files and existing library data.
2. Wait for scanning, importing and TF card jobs to finish, and stop desktop playback. Connect the card through a reader or supported USB storage connection.
3. Open **Create update card** and click **Refresh** if needed. Only removable or USB storage roots accepted by the firmware service are offered. No drive is selected automatically. Check the root or drive letter, label and capacity before selecting one.
4. Click **Create update card**. The application checks the selected drive and bundled firmware. If there is no root `update.bin`, copying starts.
5. If `update.bin` already exists, choose **Confirm** to replace that file or **Cancel** to keep it. Cancel is the default choice. A directory or link with that name is refused.
6. Keep the drive connected while the operation runs. The dialog and application cannot close until the worker finishes. On success, check the displayed SHA-256 for the copied file, safely eject the drive, and follow the official XM5 update instructions.

If bundled firmware is missing or fails verification, the dialog disables creation. It does not download a replacement. A unique temporary copy is flushed and checked against the complete SHA-256 before publication as `update.bin`. Copy or verification failure preserves the old file.

The app reserves space for both copying and the player's later extraction. It reads the actual uncompressed archive member sizes and the volume's allocation unit. The post-copy reserve is the larger of the allocated extracted files/directory or 100 MiB, plus an 8 MiB margin. V2.43's original menu has a separate free-space check against a rounded 0.10 GiB value; the 100 MiB floor conservatively covers it even when the archive is small.

Let `B` be the new `update.bin` allocation, `E` the existing file's allocation (zero if absent), and `R` the post-copy reserve. The drive needs at least `max(B, B - E + R)` bytes free before copying. This keeps the old file intact during copying, then accounts for its space being released on replacement. Just before publication, the app checks again that current free space plus `E` can cover `R`. Other programs can still consume free space after completion, so avoid changing the card before upgrading.

An existing root `firmware_v20.info` or `recovery-update`, whether a file, directory or link, stops preparation. These paths belong to the player's extraction process. The app identifies the conflict and leaves it unchanged; move it elsewhere yourself before retrying. Conflicts and space are checked at planning, execution and immediately before publication.

Changes to `update.bin` after inspection require a new inspection and replacement choice. Keep other programs from editing it during copying: identity/hash checks cannot coordinate arbitrary external writers. Unrelated files may change without invalidating the operation.

A disconnect or process interruption can leave this operation's `.xm5-update-<id>.tmp` file. It is incomplete firmware; inspect it before removing it. The app never deletes other temporary files. Power-loss durability and filesystem corruption are outside this guarantee. No drive formatting, partition change or player flashing occurs here. For music preparation, use [TF Card Export](DESKTOP.md).

## Development checks

The dialog calls `FirmwareCardService` for firmware verification, target discovery, planning and execution. It performs no file operations itself. These operations run on a worker thread, and the service checks volume identity, paths and firmware immediately before writing. It never traverses unrelated directories.

Run the widget acceptance tests with the bundled runtime:

```powershell
.\desktop\vendor\python\python.exe -B tests/gui_firmware_card.py
```

The tests use real Qt widgets, an injected fake service and temporary directories. They do not enumerate real drives, communicate with a player or flash firmware. They cover cancellation, explicit confirmation, job guards, worker responsiveness, close protection, visible failure states and both interface languages. These checks do not establish hardware safety or update success on a physical XM5.
