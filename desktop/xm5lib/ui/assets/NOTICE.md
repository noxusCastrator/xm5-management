# Assets

- `xm5/*.png`: icons and default cover extracted from the ONIX XM5 V2.43 firmware rootfs
  (`/release/assets/default/raw/images/`, via `workspace/mvp/assets`). Vendor-owned; used here for a
  personal companion tool. Do not redistribute without permission.
- `sandblast.png`: 155 px tile derived from ScopeWatch `scopewatch-sandblast-light.webp` (author's own work).
- `xm5stock/`: player mode's resources, copied unchanged from the same V2.43 rootfs
  (`/release/assets/default/raw/`): `ui/` (the compiled AWTK layouts of the local-music pages, their
  dialogs, the settings menus and the on-screen keyboard), `styles/` (themes, incl. `keyboard.bin`),
  `strings/zh_CN.bin` and `en_US.bin`, and `images/` (the stock image set).
  Vendor-owned, read at run time, never modified; used here for a personal companion tool. Do not
  redistribute without permission. The firmware font (Microsoft YaHei + Arial) is not copied: the
  same two Windows fonts are used.
- `knob_xl/wheel_*.png`: the case knob's own knurl frames (this project's art, `knob/`) re-rendered at
  714 x 301 for player mode's larger scales.
