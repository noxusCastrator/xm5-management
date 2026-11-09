/* xm5_af_abi.h -- MPlayer libaf instance/data ABI as proven on XM5 hciplayer.
 *
 * Offsets are taken from G1 player-map.json (open() @0x44fe04, play() @0x44fae0),
 * independently re-derived from the stock binary, not quoted from Q2. The static
 * asserts below fail the build if the layout does not match that evidence.
 *
 * This file contains no third-party code; it only names a binary ABI.
 */
#ifndef XM5_AF_ABI_H
#define XM5_AF_ABI_H

/* af_data: the per-chunk audio descriptor. 24 bytes (G1: af_data_size=24). */
typedef struct af_data {
    void *audio;   /* +0  interleaved sample buffer                      */
    int   len;     /* +4  valid bytes in audio                          */
    int   rate;    /* +8  sample rate (Hz)                              */
    int   nch;     /* +12 channel count                                 */
    int   format;  /* +16 mpaf format id (0x1d == AF_FORMAT_FLOAT_NE)    */
    int   bps;     /* +20 bytes per sample (4 for float)                */
} af_data;

/* af_instance: the filter node. G1: control+4, uninit+8, play+12,
 * setup+16, data+20, mul(double)+40. */
typedef struct af_instance {
    const void *info;                                   /* +0  */
    int  (*control)(struct af_instance *, int, void *); /* +4  */
    void (*uninit)(struct af_instance *);               /* +8  */
    af_data *(*play)(struct af_instance *, af_data *);  /* +12 */
    void *setup;                                        /* +16 */
    af_data *data;                                      /* +20 */
    struct af_instance *next, *prev;                    /* +24,+28 */
    double delay;                                       /* +32 */
    double mul;                                         /* +40 */
} af_instance;

/* mpaf format / control command ids used by the stock EQ node (G1 control_commands). */
#define XM5_AF_FORMAT_FLOAT_NE 0x1d
#define XM5_AF_CONTROL_REINIT  0x10000100 /* format negotiation / (re)compile */
#define XM5_AF_CONTROL_EQ_SET  0x40001d00 /* AF_CONTROL_EQUALIZER_GAIN set     */
#define XM5_AF_CONTROL_EQ_GET  0x40001d01 /* AF_CONTROL_EQUALIZER_GAIN get     */
#define XM5_AF_CONTROL_EQ_STR  0x20000300 /* ten colon-separated dB gains      */

/* af_reinit return codes (G1B af_reinit_semantics, callers at 0x44bf38). */
#define XM5_AF_OK       1  /* already in the wanted format; keep filter      */
#define XM5_AF_FALSE    0  /* host inserts a conversion filter in front of us */
#define XM5_AF_DETACH   2  /* remove us (KEPT if (cfg.force&3)==3)            */
#define XM5_AF_ERROR  (-2) /* reinit failed (af_reinit propagates -2)         */

/* The ABI is the 32-bit MIPS o32 layout (4-byte pointers): these asserts are
 * the real gate on the target (and any -m32) build. On a 64-bit host build the
 * structs widen, so the asserts are skipped there; host tests do not depend on
 * exact af_instance offsets (they use named members), only the MIPS build does. */
#if defined(__GNUC__) && defined(__SIZEOF_POINTER__) && __SIZEOF_POINTER__ == 4
#define XM5_SASSERT(e, m) _Static_assert(e, m)
#define XM5_OFFSET(t, f)  __builtin_offsetof(t, f)
XM5_SASSERT(sizeof(af_data) == 24, "af_data must be 24 bytes (G1)");
XM5_SASSERT(XM5_OFFSET(af_instance, control) == 4,  "control@+4");
XM5_SASSERT(XM5_OFFSET(af_instance, uninit)  == 8,  "uninit@+8");
XM5_SASSERT(XM5_OFFSET(af_instance, play)    == 12, "play@+12");
XM5_SASSERT(XM5_OFFSET(af_instance, setup)   == 16, "setup@+16");
XM5_SASSERT(XM5_OFFSET(af_instance, data)    == 20, "data@+20");
XM5_SASSERT(XM5_OFFSET(af_instance, mul)     == 40, "mul(double)@+40");
#endif

#endif /* XM5_AF_ABI_H */
