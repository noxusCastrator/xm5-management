# Review fixes for the local 0.2.2 candidate

The review identified real preservation and lookup problems. The following changes are included in the replacement local candidate. Publication remains on hold.

| Finding | Action |
| --- | --- |
| Update-card creation cleared the drive | Removed traversal and clearing. Copy only root `update.bin`; ask before replacing an existing file. Verify a unique temporary copy before publication. |
| Safety tests were hard to reproduce publicly | Added real-module storage regressions, a process-interruption matrix and self-contained native test runners. Historical test counts remain separate. |
| Independent PC/device playlist edits could lose a PC field | Treat crossed name/order edits as a conflict. Require an explicit device/copy/PC choice. |
| Corrupt rollback backup could remove the active new file before failing | Preflight required backups/staging for the whole rollback, then check again before each active move. Keep the active version on a blocked rollback. |
| Directory-cover search could open an entire large subtree repeatedly | Resume in 64-candidate slices, distinguish pending from no-art, cache progress and expire completed results. Remove duplicate row lookups. |
| Native C was difficult to inspect | Format the active native tree. Verify equivalent target build artifacts. Add a source/ABI map. |
| Keyword search did per-track SQL calls | Filter IDs with Unicode casefold semantics and batch the selected page's locations. |
| Automation imported tag operations from UI | Move file-tag operations to `core.tagfile`; retain the UI import as a compatibility shim. |
| HTTP body replay wrote a private Starlette attribute | Use the public ASGI receive protocol, retaining authentication, Host/Origin guards and the streamed 1 MiB limit. |
| Docstrings linked to a missing integration document | Point them to the development guide. |

## Measurements and test limits

A synthetic 20,000-song keyword search took about 0.198 seconds before and 0.061 seconds after on this Windows host; SQL statements fell from 40,003 to 5. These are local warm-cache observations. They do not describe every library or disk.

The original real C reader opened 4,096 media files for one 4,096-descendant no-art directory; three repeats opened 12,288. The public regression checks 129-descendant cases, with valid art last, stale-zero hints and empty prepared slots. Each call opens at most 64 candidate media files and all cases finish in three slices. A completed miss does not rescan immediately. Media/index replacement checks cover retry and generation invalidation. The count bound is not a latency deadline. The fixed 32-job state adds 66,688 bytes of BSS on the MIPS target.

The interruption matrix cuts before/after 22 observed function calls and tests resume/rollback, for 88 scenarios. It checks source preservation, disconnected-source recovery and replay. These are process-boundary tests, not torn-sector or real power-loss tests. Public storage regressions also exercise modified source/card files, retained user files, frozen-plan tampering and crossed playlist edits.

Formatting with clang-format 18.1.8 produced identical `demo`, `hciplayer.candidate`, `libxm5inline.so` and organizer hashes before and after, using the same pinned GCC12 MIPS build. Functional artwork changes precede this formatting comparison. The replacement package changes the UI executable; the kernel, audio executable, startup script and EQ resource remain unchanged from LIB01.

## Decisions retained after review

Retained backups are not garbage by default. They may be required by recovery, so synchronization still keeps them. Their paths are exposed by `storage.transactions`; retention and space use are documented. No automatic deletion or reduced integrity hashing was introduced.

Frozen PEQ08 trees and active storage trees are documented separately in [SOURCE_MAP.md](SOURCE_MAP.md). Similar source files are not blindly deduplicated. Large GUI-module extraction and cross-platform work are outside this corrective release.

The high-rate audio path remains float32 internally. This provides approximately 24 significant bits; it does not guarantee 32-bit integer precision through active processing. Integer conversion truncates without dither. Disabled processing can return the input unchanged. The review's second inline-gain call is a fallback after a failed first lookup, not an unreachable branch. No new audio algorithm, locking scheme or claim of measured audible performance is introduced here. Control/play serialization still depends on the existing player call model; it has no new physical concurrency proof.

Shanling's written GitHub publication permission already exists. A repository-wide license and file-level third-party/AWTK provenance still require separate decisions; this release does not invent those rights. See [THIRD_PARTY.md](../THIRD_PARTY.md).

Physical storage-firmware installation and a complete self-service USB recovery path remain unvalidated. The [release validation record](VALIDATION_0.2.2.md) distinguishes the current software/VM evidence from these limits.
