/* xm5_platform.h -- freestanding platform header for the pinned P1 core on the
 * XM5 target. vendor/peq_platform.h includes this when built with
 * -DPEQ_PLATFORM_HEADER='"xm5_platform.h"'. It provides NO libc; it declares
 * exactly the names the core references so they resolve to the stock hciplayer
 * dynamic imports (see manifests/import-manifest.json).
 *
 * The P1 core (peq.c) needs: memset, memcpy, pow, sin, cos, sqrt, fabs, strtod,
 * isfinite. Of these:
 *   - memset, memcpy, sin, cos, strtod  -> stock PLT/GOT imports (declared here);
 *   - pow                               -> mapped to __pow_finite (plain pow is
 *                                          NOT a stock import);
 *   - sqrt, fabs                        -> emitted as FPU sqrt.d/abs.d, no call;
 *   - isfinite                          -> __builtin_isfinite, no call.
 *
 * NaN note (T1): the stock libc is legacy-NaN FPXX while the shippable object is
 * built nan2008; the core depends only on IEEE isfinite, not on NaN bit patterns,
 * and uses no legacy libm/libgcc helper. Target NaN2008 linking is a separate gate.
 */
#ifndef XM5_PLATFORM_H
#define XM5_PLATFORM_H

#ifdef __FAST_MATH__
#error "peq.c relies on IEEE isfinite and strict FP; do not use -ffast-math"
#endif

/* Tell peq_platform.h not to pull in isfinite/strtod from a hosted libc. */
#define PEQ_ISFINITE(x) __builtin_isfinite(x)
#define PEQ_STRTOD strtod

/* mem (stock imports: memset, memcpy) */
void *memset(void *, int, __SIZE_TYPE__);
void *memcpy(void *, const void *, __SIZE_TYPE__);

/* number parsing (stock import: strtod, GOT 0xaf9ab8) */
double strtod(const char *, char **);

/* math: sin/cos are stock PLT imports; pow exists only as __pow_finite. */
double sin(double);
double cos(double);
double __pow_finite(double, double);
#define pow(x, y) __pow_finite((x), (y))
/* Force the FPU inline (sqrt.d / abs.d): __builtin_* never emit a library call,
 * so no sqrt/fabs import is created. (A plain prototype was not enough on this
 * MIPS target -- GCC still emitted out-of-line calls.) */
#define sqrt(x) __builtin_sqrt(x)
#define fabs(x) __builtin_fabs(x)

#endif /* XM5_PLATFORM_H */
