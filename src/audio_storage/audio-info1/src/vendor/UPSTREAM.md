# Pinned core — now P1-portable-core final (supersedes the raw Q2 copy)

`peq.c` / `peq.h` / `peq_platform.h` are the **accepted P1-portable-core** files,
copied unmodified from `tasks/P1-portable-core/src/`. They are MIT-licensed
(derived from q2-ringnav by Diamond Bond); `LICENSE` is retained here.

## Integrated copy (what this adapter links)
Copied verbatim from `tasks/P1-portable-core/src/` on 2026-10-02:
- `peq.c`          sha256 `49827bfa2c8d23642283be36b8e27f7560e86cc2ea8af06f40674dd65a4ef094`
- `peq.h`          sha256 `80e23a0b2399020dc22cb6e8dd4096eb9795a7de1f353dc85030a9b81a5248fa`
- `peq_platform.h` sha256 `efd5e1b5a30869796098a3fface983898d006775cc9c2e031d5b63e651524034`
- `LICENSE`        sha256 `4e19996ba6b11d5e4b8c43541c2ff86e1dcb082adc49e020be32a7fb7cab4190`

P1 verified these: 4128 numeric/parser/contract/DSP tests and 300000 ASan/UBSan
fuzz mutations, all passing (lead re-ran independently). API changes vs the raw Q2
core: `peq_compile`/`peq_reset`/`peq_update` now take a `peq_error *`; `peq_dsp`
has a `failed` flag; `peq_apply_text` parses+applies text in one call and clears to
bypass on any rejection. The disk helpers (`peq_load`/`peq_save`/`peq_import`) are
**gone from `peq.c`** — they now live in P1's optional `peq_store.c`.

## Deliberately NOT vendored
- `peq_store.c` / `peq_store.h` — binary Q2 (`Q2PEQ02`) storage. This adapter uses
  the **text `active.txt`** protocol (parsed by `peq_parse`/`peq_apply_text`), so
  `peq_store` is **not copied and not linked**. This removes the old file's stdio
  disk-helper imports (`access`/`fsync`/`snprintf`/`rename`/`unlink`/`fopen`/…).

## Upstream origin (unchanged)
- repo: https://github.com/DiamondBond/q2-ringnav
- commit: `d127c6ea07af20bde73dd842df255ce58d52e5be`
- raw `patch/peq.c` sha256 `0027ae8706392964ac1a7767f9e7bc0706152dbde453ff4b456b1eabb4c8da13`
- raw `patch/peq.h` sha256 `ec552001dc58542de994540faa93f9ddc38de3c0ea417dce90665e890ebd6d2e`

## Target build note
`peq_platform.h` selects the libc source: on the host build it includes the system
`<math.h>/<stdlib.h>/<string.h>`; on the target it includes `../xm5_platform.h`
(via `-DPEQ_PLATFORM_HEADER='"xm5_platform.h"'`), which declares only the stock
hciplayer imports the core needs (`memset`, `memcpy`, `sin`, `cos`, `__pow_finite`,
`strtod`) and maps `pow→__pow_finite`, `PEQ_ISFINITE→__builtin_isfinite`.
`sqrt`/`fabs` compile to FPU ops. `xm5_platform.h` is original A1 work, not upstream.
