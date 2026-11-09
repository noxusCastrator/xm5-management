/* Portable platform interface for peq.c (MIT; see LICENSE and UPSTREAM.md).
 *
 * The core only needs memset/memcpy, pow/sin/cos/sqrt/fabs, strtod and isfinite. A target without
 * a hosted libc defines PEQ_PLATFORM_HEADER (e.g. -DPEQ_PLATFORM_HEADER='"xm5_platform.h"') and
 * provides those names there. strtod must use '.' as the decimal mark (the "C" LC_NUMERIC locale):
 * peq.c converts ',' to '.' before calling it. */
#ifndef PEQ_PLATFORM_H
#define PEQ_PLATFORM_H

#ifdef PEQ_PLATFORM_HEADER
#include PEQ_PLATFORM_HEADER
#else
#include <math.h>
#include <stdlib.h>
#include <string.h>
#endif

#ifndef PEQ_ISFINITE
#define PEQ_ISFINITE(x) isfinite(x)
#endif
#ifndef PEQ_STRTOD
#define PEQ_STRTOD strtod
#endif

#ifdef __FAST_MATH__
#error "peq.c relies on IEEE isfinite and on y + 1e-18 - 1e-18 not being reassociated; do not use -ffast-math"
#endif

#endif
