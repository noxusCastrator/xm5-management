# Create an update card

XM5 Studio can prepare a removable drive containing the bundled paired firmware. Open **Settings → Player firmware → Create update card**. The dialog shows the application version, paired firmware version and firmware SHA-256 from the installed package.

The 0.2.2 application uses paired firmware **V2.43-LIB01**, the same firmware distributed with version 0.2.0. This feature prepares the card; installation follows the official XM5 update procedure. The paired storage firmware has software and VM acceptance, without new physical-device acceptance. See the [validation record](VALIDATION_0.2.0.md).

## Prepare the card

1. Back up everything on the target drive to a separate location. Creating an update card removes **all contents of that drive**, including music, playlists, hidden files and existing library data. Only `update.bin` will remain. The confirmation also provides a blue link to the TF Card Export page.
2. Wait for scanning, importing and TF card jobs to finish, and stop desktop playback. Connect the card through a reader or supported USB storage connection.
3. Open **Create update card** and click **Refresh** if needed. Only removable or USB storage roots accepted by the firmware service are offered. No drive is selected automatically. Check the root or drive letter, label and capacity before selecting one.
4. Click **Create update card**. The application inspects the selected drive and prepares a read-only plan. Selecting a drive or preparing this plan does not start clearing it.
5. Read the warning and drive details. Choose **Confirm** to clear the entire selected drive and copy `update.bin`, or **Cancel** to leave it unchanged. Cancel is the default choice.
6. Keep the drive connected while the operation runs. The dialog and application cannot close until the worker finishes. On success, check the displayed SHA-256 for the copied file, safely eject the drive, and follow the official XM5 update instructions.

If the bundled firmware is missing or fails verification, the dialog shows the reason and disables creation. It does not download a replacement. If execution fails, the target may already be partially cleared. Inspect it and restore your separate backup before using it. The application does not claim that cleared files can be rolled back.

For ordinary music preparation, use [TF Card Export](DESKTOP.md). That workflow manages the `xm5` folder and preserves unrelated card data; creating an update card clears the whole selected drive.

## Development checks

The dialog calls `FirmwareCardService` for firmware verification, target discovery, planning and execution. It performs no copying or deletion itself. These operations run on a worker thread, and the service remains responsible for volume identity, path and firmware checks immediately before writing.

Run the widget acceptance tests with the bundled runtime:

```powershell
.\desktop\vendor\python\python.exe -B tests/gui_firmware_card.py
```

The tests use real Qt widgets, an injected fake service and temporary directories. They do not enumerate real drives, communicate with a player or flash firmware. They cover cancellation, explicit confirmation, job guards, worker responsiveness, close protection, visible failure states and both interface languages. These checks do not establish hardware safety or update success on a physical XM5.
