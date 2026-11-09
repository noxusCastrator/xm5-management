# Convert an EQ preset for XM5 PEQ

This is a self-contained conversion brief for **XM5 PEQ 0.1.0 a (alpha)**. Give an AI this entire document together with your source EQ settings. You do not need to give it the firmware or the rest of the repository.

## What the user should provide

Prefer an exported Equalizer APO configuration containing the actual active settings. You can also provide an AutoEQ parametric export, a Peace preset with enough information to identify its filter types, or a complete frequency/gain/Q table.

Include the preamp, filter types, frequency units, gain units, Q or other shape parameters, enabled/disabled status, and any per-channel settings. If using screenshots, include all active processing pages; a slider view alone may omit important settings. Include files referenced by the configuration if you expect their contents to be resolved. State whether stock XM5 EQ will also be enabled.

Tell the AI: "Use this document to convert the supplied settings. Preserve the supported parameters. If information is missing or active processing cannot be represented, explain that before producing a file."

## Instructions to the converting AI

Convert the user's existing settings into the text subset described below. This is format conversion, not headphone tuning. Do not select a target curve, download a replacement AutoEQ preset, change loudness, or redesign filters unless the user separately requests it.

A source preset and a similarly named downloaded preset are not interchangeable. Use the actual supplied settings, including their preamp and shelving definitions.

### Output file

- Produce one UTF-8 text file with a `.txt` extension. UTF-16 and raw Peace `.peace` files are unsupported.
- Prefer UTF-8 without a BOM and ordinary LF or CRLF line endings.
- Suggest an ASCII filename such as `My-Headphone.txt`, at most 63 bytes including the extension. Do not use a leading dot, `..`, path separators, or control characters.
- The user will place the file directly in `EQ/` at the TF card's root. Subdirectories are not scanned.
- The final internally saved preset is limited to 16,384 bytes, including the source header inserted by the player. Keep generated content within 15 KiB to leave comfortable room for that header.
- Each line must stay within 512 bytes. Put explanations outside the file; use separate `#` comment lines if comments are needed inside it.
- Do not wrap the file contents in JSON, INI sections, Markdown headings, or a Peace configuration format. A fenced code block in your response may delimit the file, but its fences must not be saved in the `.txt`.

### Supported commands

Use these spellings and parameter order:

```text
Preamp: -6 dB
Filter 1: ON PK Fc 1000 Hz Gain -2 dB Q 1.5
Filter 2: ON LSC Fc 100 Hz Gain 3 dB Q 0.7
```

The values above are syntax examples, not a headphone recommendation. Never substitute them for the user's settings.

| Item | Accepted form and limits |
| --- | --- |
| Preamp | `Preamp: <value> dB`; each value and the effective total must be within -60 to +24 dB |
| Peak filter | `Filter N: ON PK Fc <Hz> Hz Gain <dB> dB Q <Q>`; `PEQ` is also accepted as a peak alias |
| Shelving filter | Same parameter order with `LSC`, `HSC`, `LS`, or `HS`; preserve the source type |
| Frequency | 20 through 20,000 Hz, subject to shelving-center and sample-rate checks below |
| Filter gain | -24 through +24 dB |
| Q | 0.1 through 10; required for PK/PEQ |
| Filter count | At most 30 ON filters in the complete file, including zero-gain filters |
| Disabled filter | `Filter N: OFF ...`; inactive filters do not consume an ON slot |
| Neutral filter | `Filter N: ON None` with no further parameters |
| Channels | `Channel: ALL`, `Channel: L`, `Channel: R`, or `Channel: L R` |
| Comments | Separate lines starting with `#` |

Use plain numbers with a decimal point when needed, no thousands separators, and no `nan` or `inf`. Scientific notation is accepted. Keep numbers within 63 characters and preserve the supplied precision; do not round merely for appearance.

Frequency tokens with exactly three fractional digits can be rejected as ambiguous thousands notation. For example, `Fc 31.250 Hz` is ambiguous. If the intended value is explicitly known to be 31.25 Hz, write `31.25`; an unambiguous scientific form is also available, such as `1.234125e3` for 1234.125 Hz. If the source notation itself is ambiguous, ask the user instead of choosing an interpretation.

Preserve repeated preamp commands and their channel scopes, or combine them only after computing the exact effective gains. They add; a later preamp line does not simply replace an earlier one. Without channel commands, filters and preamp apply to both left and right.

For channel-specific settings, retain the source distinction. Do not average left and right. The supported channel names are L, R, and ALL; numbered channels and multichannel layouts are unsupported. The effective left/right preamp difference must be no more than 12 dB. All channel filters together count toward the 30-filter limit. If these conditions cannot be met, report the limitation rather than changing the settings.

### Preserve shelving semantics

`LS` and `LSC`, and `HS` and `HSC`, have different frequency semantics when Q is specified. Do not rename one into the other just because both are shelves. Preserve the exported type, frequency, gain, and Q.

Shelves without Q use the supported default slope S = 0.9. That default may only be used when it is also the source's intended behavior. If a UI shows a different slope or bandwidth but does not expose Q, request the actual export or a verified type/parameter definition. Do not invent Q = 0.707 or Q = 1.

For LS/HS with Q, the parser applies its Equalizer APO corner-to-center shift. If the resulting center leaves 20–20,000 Hz, the preset is rejected even when the input frequency is in range. Preserve the source values and report that constraint; do not clamp or relabel the filter to make it import.

At playback, a nonzero-gain filter at or above half the actual sample rate is rejected. A file's valid syntax alone does not prove it will compile at every sample rate. Ask about the intended lowest playback sample rate if this boundary matters.

### Unsupported active processing

The following cannot be emitted as supported commands:

- GraphicEQ, convolution, impulse responses, VST plugins, delays, channel copying/mixing, loudness correction, and arbitrary IIR/BiQuad definitions.
- LP, HP, BP, notch, all-pass, and Modal filters.
- `BW Oct`, explicit shelf slope forms such as `LS 6dB`, and arbitrary parameter order.
- `Include`, device/stage selection, conditionals, Eval, inline backtick expressions, and other dynamic configuration commands.

Do not silently drop active unsupported processing, approximate a notch with a peak, replace convolution with a few filters, or treat a graphical EQ as a parametric preset. Explain why a complete parameter-preserving conversion is unavailable and ask whether the user wants a separately labeled approximation. Until then, do not present a partial conversion as a ready-to-use equivalent.

A static supported chain may be extracted from referenced files only when all necessary source contents and the active configuration context are known. Report which context was extracted. Never guess the result of device selection, conditionals, or expressions.

Peace preset files may use numeric filter identifiers and separate speaker settings. Do not infer their meaning from the number alone. Confirm the actual filter labels/export and inspect all active gain/filter sections. A screenshot lacking preamp, Q, filter type, or other active effects is incomplete input; ask for the missing fields.

### Gain and clipping

Keep the source preamp, including a negative value. Do not reset it to zero, normalize the loudness, or add an arbitrary safety attenuation during conversion. If a complete plain APO file has no preamp, its default is 0 dB; state that assumption. An incomplete Peace screenshot is not evidence that its preamp is zero.

XM5 PEQ does not automatically budget headroom for the entire chain or guarantee clipping-free playback. At sample rates up to 48 kHz, enabled stock EQ runs after PEQ and can add more gain. Above 48 through 192 kHz, stock EQ is skipped. DSD/DoP bypasses PCM DSP, and USB DAC mode is outside this local-playback path.

If positive gains or stock EQ suggest a clipping risk, explain it separately. An optional lower-preamp suggestion must remain separate from the parameter-preserving file until the user approves that change.

### Check before answering

1. Every active source processing item has either been represented or explicitly identified as unsupported.
2. Frequency units, gain units, filter types, Q, channel scopes, and effective preamp are known.
3. No active filter was omitted, reordered, clamped, retuned, or replaced without explicit approval. Clearly inactive filters may be omitted if reported.
4. The output uses canonical command spelling, fixed parameter order, and safe numeric notation.
5. Frequency/gain/Q/preamp limits, channel gain difference, active filter count, and file limits are satisfied.
6. Missing parameters and ambiguous source notation were resolved with the user, not guessed.
7. You distinguish a documentation-based format check from execution on the XM5 or comparison against Equalizer APO.

## Required response

If conversion is complete, provide:

1. A suggested filename.
2. One code block containing only the exact file contents.
3. A short conversion report with source type, active filter count, preamp, channel handling, and any explicitly inactive items omitted.
4. Any remaining clipping or playback-rate considerations. State that syntax review does not prove acoustic equivalence or device stability.
5. The instruction to save the file as UTF-8 under `EQ/`, rescan, select it, and enable PEQ. Remind the user to select the file again after editing it.

If conversion is blocked, list the specific missing settings or unsupported active items and the next information needed. Do not output a supposedly equivalent installable preset until the blocker is resolved. If the user approves an approximation, label it prominently and identify every change.

This document converts preset data only. It does not authorize firmware flashing, certify AI output, or establish that the alpha firmware is safe on another device.
