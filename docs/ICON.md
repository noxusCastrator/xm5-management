# XM5 Studio application icon

The icon is an original vector drawing for XM5 Studio. A continuous warm gold
signal path forms an S on a charcoal rounded square. The open counters and broad
stroke preserve the silhouette at taskbar sizes. Its gold matches the desktop
palette; it contains no ONIX logo, extracted player artwork, font glyph or stock
icon.

The editable master is
[`app.svg`](../desktop/xm5lib/ui/assets/studio/app.svg). It uses standard SVG paths,
rounded rectangles and linear gradients, with a 1024-unit view box. The outer
margin and rounded corners are transparent. Keep the gold mark and its spacing
together when scaling; do not add small text or crop the tile.

The same directory contains these generated exports:

| File | Content |
| --- | --- |
| `app.png` | 1024 x 1024 RGBA PNG |
| `app-256.png` | 256 x 256 RGBA PNG |
| `app.ico` | 16, 24, 32, 48, 64, 128 and 256 px RGBA frames |

## Rebuild

Use the approved desktop dependency runtime described in
[DEVELOPMENT.md](DEVELOPMENT.md). No additional packages, firmware inputs or
physical device are needed. From this repository root:

```powershell
.\desktop\vendor\python\python.exe -I tools\render_studio_icon.py
.\desktop\vendor\python\python.exe -I tools\render_studio_icon.py --check
```

An interpreter in another checkout can also run the script by absolute path. The
script explicitly adds its own checkout's desktop source path, rather than relying
on the isolated interpreter's configured application path.

Qt SVG renders each output size independently at four times its final resolution.
Pillow applies the final Lanczos downsample, clears its negligible 1-2/255 alpha
fringe and encodes the PNG. The ICO stores each
size as a lossless PNG frame with a 32-bit RGBA directory entry, for modern
Windows. There are no external SVG resources, fonts or raster inputs. The approved
runtime used for these exports provides PySide6 6.12.0 and Pillow 12.0.0; use the
same versions for byte-for-byte reproduction.

`--check` renders in memory, verifies exact exported bytes and performs no writes.
Both modes check each frame's dimensions, transparent outer corners, opaque tile
centre, alpha range and exact ICO pixel data. To inspect the actual small frames
on light and dark surfaces, generate an optional local contact sheet:

```powershell
.\desktop\vendor\python\python.exe -I tools\render_studio_icon.py `
  --preview .local\studio-icon-preview.png
```

Keep contact sheets and other review images under ignored `.local/`. Edit the SVG,
rerender the three exports and review the 16, 24 and 32 px silhouettes together
before accepting a design change. This artwork belongs to the project; third-party
player assets elsewhere in the repository retain their separate provenance.
