/* A3: independent PEQ -> original ten-band cascade. No new amplitude processing. */
#include "xm5_af_abi.h"
#include "vendor/peq.h"
#include "layers.h"
extern int xm5_stock_open(af_instance *);
extern int xm5_stock_control(af_instance *, int, void *);
extern af_data *xm5_stock_play(af_instance *, af_data *);
extern void xm5_stock_uninit(af_instance *);

/* ---- libc the adapter itself needs -------------------------------------- *
 * The core (peq.c) declares its own names via vendor/peq_platform.h (host libc,
 * or xm5_platform.h on the target). Here we only declare the few the adapter
 * calls directly and the core does not. On the target these resolve to the stock
 * PLT/GOT stubs listed in manifests/import-manifest.json. */
#ifdef PEQ_HOST
#include <string.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#else
extern void *calloc(unsigned long, unsigned long);
extern void  free(void *);
extern int   strcmp(const char *, const char *);
extern int   strncmp(const char *, const char *, unsigned long);
/* memset/memcpy are declared by the core platform header (xm5_platform.h). */
extern int   open64(const char *, int, ...);
extern long  read(int, void *, unsigned long);
extern int   close(int);
#define O_RDONLY 0
#endif
#include "native_pcm.h"

/* ---- Trusted-process pointer chain (DSD/DoP static classifier) ----------- *
 * On XM5, mpctx lives at the fixed .data address 0x00af9e30 (G1B: the stock
 * code dereferences [0xaf9e30]->+0x2c (sh_audio)->+4 (codec)->+0x3d0 (name),
 * +0x3e0 (drv); sh+0xd4 is the vendor class enum). These are the SAME offsets
 * the stock binary itself uses, re-proven from XM5 instructions (G1B table 1).
 *
 * TRUSTED POINTER ASSUMPTION (unavoidable, documented): a null check proves a
 * pointer is non-null, NOT that it is readable. We only dereference these words
 * inside the hciplayer address space, where the stock player populates exactly
 * this chain before it reaches the AF reinit path; the stock EQ gate at
 * 0x42ce2c dereferences the same chain. We add null guards at every hop and
 * treat ANY null as "unknown => bypass". We cannot prove readability statically
 * and make no such claim. Host tests exercise this via the overridable
 * xm5_mpctx below.
 *
 * All loads/stores go through memcpy: the chain offsets (0x04, 0x2c, 0xd4, ...)
 * are 4-aligned for 4-byte target pointers, but a direct cast to a wider pointer
 * on a 64-bit host test would be a misaligned access (UBSan). memcpy is both
 * width- and alignment-correct and compiles to a plain load on aligned targets. */
#ifdef PEQ_HOST
unsigned char *xm5_mpctx = 0;            /* test-controlled */
#define XM5_MPCTX xm5_mpctx
#else
#define XM5_MPCTX (*(unsigned char *volatile *)0x00af9e30u)
#endif

#define XM5_SH_OFF_CODEC   0x2c  /* mpctx -> sh_audio            */
#define XM5_CODEC_OFF      0x04  /* sh_audio -> codec            */
#define XM5_NAME_OFF       0x3d0 /* codec -> name (const char *) */
#define XM5_DRV_OFF        0x3e0 /* codec -> drv  (const char *) */
#define XM5_CLASS_OFF      0xd4  /* sh_audio -> vendor class int */

static unsigned char *load_ptr(const unsigned char *base, unsigned off) {
    unsigned char *p;
    memcpy(&p, base + off, sizeof p);
    return p;
}

/* 1 => the stream is DSD/DoP/unknown and MUST NOT be float-converted (bypass).
 * 0 => classified as plain PCM by every static test available (not a proof of
 * DoP absence; the <=48 kHz rate gate in reinit is the other limited guard). */
static int codec_is_unsafe(void) {
    unsigned char *mp = XM5_MPCTX;
    if (!mp) return 1;
    unsigned char *sh = load_ptr(mp, XM5_SH_OFF_CODEC);
    if (!sh) return 1;
    unsigned char *codec = load_ptr(sh, XM5_CODEC_OFF);
    if (!codec) return 1;
    const char *name = (const char *)load_ptr(codec, XM5_NAME_OFF);
    const char *drv  = (const char *)load_ptr(codec, XM5_DRV_OFF);
    if (!name || !drv) return 1;                 /* unknown => bypass         */
    if (!strcmp(drv, "dsdiff")) return 1;        /* dsf / dff / SACD iso      */
    if (!strncmp(name, "ffdsd", 5)) return 1;    /* FFmpeg DSD decoders       */
    int cls;
    memcpy(&cls, sh + XM5_CLASS_OFF, sizeof cls);
    if (cls == 14 || cls == 15 || cls == 16) return 1; /* dsd / dsf / iso enum */
    return 0;
}

/* Format/rate gate. Plain integer/float PCM only; rate kept 8k..48k (stock
 * external gate preserved -- this candidate does not raise it). */
static int stream_unsafe(const af_data *in) {
    if (!in) return 1;
    if (in->nch < 1 || in->nch > PEQ_CHANNELS) return 1;
    if (in->rate < 8000 || in->rate > 192000) return 1;
    if (in->rate > 48000 && !native_format(in)) return 1;
    if (in->format & ~0x3f) return 1;            /* non-PCM mpaf class => bypass */
    return codec_is_unsafe();
}

/* ---- Player state (allocated once in open, freed in uninit) -------------- */
typedef struct {
    peq_dsp dsp;
    af_instance stock;
    int stock_on, peq_on;
    int native_high, dop_state;
    float native_scratch[XM5_NATIVE_SAMPLES];
    peq_preset requested;
    int requested_valid;
    int      bypass;                 /* 1 => play() is byte-identical passthrough */
    char     text[PEQ_FILE_LIMIT + 2]; /* reused config buffer (no per-call alloc) */
} player_state;

/* Config path: the persistent active file (UI-PLAYER-CONTRACT). It is APO text;
 * its "# xm5-peq 1 source=..." header line is a comment to the shared parser. */
#ifdef PEQ_HOST
extern const char *xm5_active_path;    /* tests point this at a scratch file */
#define XM5_ACTIVE_PATH xm5_active_path
#else
#define XM5_ACTIVE_PATH "/mnt/data/peq/active.txt"
#endif

/* Read the active file into s->text. Returns 1 with *out_len set on success.
 * Oversized (> PEQ_FILE_LIMIT), unreadable or missing => 0. Control context
 * only; never called from play(). */
static int read_active(player_state *s, unsigned *out_len) {
    int fd = open64(XM5_ACTIVE_PATH, O_RDONLY);
    if (fd < 0) return 0;
    unsigned n = 0;
    long r = 0;
    while (n <= PEQ_FILE_LIMIT) {
        r = read(fd, s->text + n, (PEQ_FILE_LIMIT + 1) - n);
        if (r <= 0) break;
        n += (unsigned)r;
    }
    close(fd);
    if (r < 0) return 0;               /* read error              */
    if (n > PEQ_FILE_LIMIT) return 0;  /* oversized => reject      */
    *out_len = n;
    return 1;
}

/* Parse the active file into *p via the shared parser. Missing/invalid/oversized
 * => 0 (caller selects identity). */
static int load_active_preset(player_state *s, peq_preset *p) {
    unsigned n = 0;
    if (!read_active(s, &n) || n == 0) return 0;
    peq_error err;
    return peq_parse(s->text, n, p, &err);   /* peq_parse validates ranges */
}

/* Compare parsed meaning, not structure padding or file comments. Stock slider
 * IPC must not restart an unchanged PEQ crossfade or discard its history. */
static int same_preset(const peq_preset *a, const peq_preset *b) {
    if (a->count != b->count || a->bypass != b->bypass ||
        a->preamp != b->preamp || a->balance != b->balance) return 0;
    for (int i = 0; i < a->count; ++i) {
        const peq_band *x = &a->bands[i], *y = &b->bands[i];
        if (x->enabled != y->enabled || x->type != y->type ||
            x->frequency != y->frequency || x->gain != y->gain || x->q != y->q)
            return 0;
    }
    return 1;
}

static void refresh_peq(player_state *s, int reset) {
    peq_preset p; peq_error err;
    int have = load_active_preset(s, &p);
    int changed = have != s->requested_valid ||
                  (have && !same_preset(&p, &s->requested));
    if (reset) peq_reset(&s->dsp, s->dsp.rate, s->dsp.channels, have ? &p : 0, &err);
    else if (changed) peq_update(&s->dsp, have ? &p : 0, &err);
    s->requested_valid = have;
    if (have) s->requested = p;
}

/* Equivalent to re-adding the original stock node: open -> REINIT with zero
 * gains -> EQ_SET. Reinitializing old gains would change stock gain_factor. */
static int restart_stock(af_instance *af, player_state *s) {
    af_instance fresh; memset(&fresh, 0, sizeof fresh); fresh.info = af->info;
    int rc = xm5_stock_open(&fresh);
    if (rc == XM5_AF_OK) {
        af_data input = *af->data;
        rc = xm5_stock_control(&fresh, XM5_AF_CONTROL_REINIT, &input);
    }
    if (rc != XM5_AF_OK && rc != XM5_AF_FALSE) {
        if (fresh.setup || fresh.data) xm5_stock_uninit(&fresh);
        return XM5_AF_ERROR;
    }
    xm5_stock_uninit(&s->stock);
    s->stock = fresh;
    return XM5_AF_OK;
}

#ifdef PEQ_HOST
extern const char *xm5_layers_path;
#define XM5_LAYERS_PATH xm5_layers_path
#else
#define XM5_LAYERS_PATH "/mnt/data/peq/layers.conf"
#endif
/* Control context only. Malformed/missing state disables both stages. */
static void load_layers(player_state *s) {
    char b[XM5_LAYERS_SIZE + 1]; unsigned n = 0; long r = 0;
    s->stock_on = s->peq_on = 0;
    int fd = open64(XM5_LAYERS_PATH, O_RDONLY);
    if (fd < 0) return;
    while (n < sizeof b) {
        r = read(fd, b + n, sizeof b - n);
        if (r <= 0) break;
        n += (unsigned)r;
    }
    close(fd);
    if (r >= 0) xm5_layers_parse(b, n, &s->stock_on, &s->peq_on);
}

/* ---- AF callbacks -------------------------------------------------------- */

static int control(af_instance *af, int cmd, void *arg) {
    player_state *s = (player_state *)af->setup;
    if (!s) return XM5_AF_ERROR;

    if (cmd == XM5_AF_CONTROL_REINIT) {
        af_data *in = (af_data *)arg;
        s->bypass = 1;
        s->native_high = 0; s->dop_state = 0;
        load_layers(s);
        peq_reset(&s->dsp, 0, 0, 0, 0);      /* drop the previous stream's DSP */
        if (!in) return XM5_AF_ERROR;        /* stock never passes NULL; no deref */
        /* Output metadata = input metadata: the host inserts no conversion and
         * play() leaves the bytes alone. Never DETACH (see header, R9 F1/F2). */
        *af->data = *in;
        if (stream_unsafe(in)) return XM5_AF_OK;       /* DSD/DoP/unknown/rate */

        /* Safe PCM: compile the active config at this rate. Missing/invalid =>
         * NULL preset => identity; Nyquist-invalid bands => peq_reset fails and
         * selects bypass (identity). Either way the DSP is never left stale. */
        peq_preset p;
        peq_error err;
        int have = load_active_preset(s, &p);
        peq_reset(&s->dsp, in->rate, in->nch, have ? &p : 0, &err);
        s->requested_valid = have;
        if (have) s->requested = p;
        /* High-rate PEQ only. Retain decoder format: inspect valid DoP markers
         * in play BEFORE our conversion. Stock coefficients are untouched and
         * its saved preference remains intact, but the stage cannot execute. */
        if (in->rate > 48000) {
            s->native_high = 1; s->bypass = 0; af->delay = 0;
            return XM5_AF_OK;
        }
        /* Initialize actual stock filter even if currently skipped, so later enabling
         * works without a second external node or stale rate coefficients. */
        af_data negotiated = *in;
        int rc = xm5_stock_control(&s->stock, cmd, &negotiated);
        if (rc != XM5_AF_OK && rc != XM5_AF_FALSE) return XM5_AF_ERROR;
        s->bypass = 0;
        af->delay = s->stock_on ? s->stock.delay : 0;

        af->data->format = XM5_AF_FORMAT_FLOAT_NE;
        af->data->bps = 4;
        if (in->format != XM5_AF_FORMAT_FLOAT_NE || in->bps != 4) {
            *in = *af->data;                 /* ask host to feed us float       */
            return XM5_AF_FALSE;
        }
        return XM5_AF_OK;
    }

    if (cmd == XM5_AF_CONTROL_EQ_SET || cmd == XM5_AF_CONTROL_EQ_GET) {
        struct { float *gain; int channel; } *ext = arg;
        if (!ext || !ext->gain || ext->channel < 0 || ext->channel >= 8)
            return XM5_AF_ERROR;
        if (cmd == XM5_AF_CONTROL_EQ_SET && ext->channel == 0) {
            int old_stock = s->stock_on, old_peq = s->peq_on;
            load_layers(s);
            if (!s->bypass) {
                if (!s->native_high && !old_stock && s->stock_on && restart_stock(af, s) != XM5_AF_OK) {
                    s->stock_on = 0; af->delay = 0;
                    return XM5_AF_ERROR;
                }
                refresh_peq(s, !old_peq && s->peq_on);
                af->delay = s->stock_on && !s->native_high ? s->stock.delay : 0;
            }
        }
        return xm5_stock_control(&s->stock, cmd, arg);
    }
    if (cmd == XM5_AF_CONTROL_EQ_STR)
        return xm5_stock_control(&s->stock, cmd, arg);

    return -1;                               /* unsupported command            */
}

static af_data *play(af_instance *af, af_data *data) {
    player_state *s = (player_state *)af->setup;
    if (s && !s->bypass && s->native_high && data && data->audio &&
        data->len > 0 && native_format(data) &&
        data->rate == af->data->rate && data->nch == af->data->nch &&
        data->format == af->data->format && data->bps == af->data->bps &&
        !(data->len % (data->bps * data->nch))) {
        if (!s->peq_on || !classify_native_start(&s->dop_state, data)) return data;
        if (!s->dsp.ramp && s->dsp.current.bypass &&
            (!s->dsp.waiting || s->dsp.pending.bypass)) return data;
        unsigned char *p = data->audio;
        unsigned frames = (unsigned)data->len / (unsigned)(data->bps * data->nch);
        while (frames) {
            unsigned chunk = XM5_NATIVE_SAMPLES / (unsigned)data->nch;
            if (chunk > frames) chunk = frames;
            unsigned samples = chunk * (unsigned)data->nch;
            for (unsigned i=0;i<samples;++i)
                s->native_scratch[i] = native_read(p+i*data->bps,data->format,data->bps);
            peq_process(&s->dsp,s->native_scratch,chunk);
            for (unsigned i=0;i<samples;++i)
                native_write(p+i*data->bps,data->format,data->bps,s->native_scratch[i]);
            p += samples * (unsigned)data->bps; frames -= chunk;
        }
        return data;
    }
    if (s && !s->bypass && !s->native_high && data && data->len > 0 &&
        data->format == XM5_AF_FORMAT_FLOAT_NE && data->bps == 4 &&
        data->nch == s->dsp.channels && data->rate == s->dsp.rate &&
        data->nch > 0 && !(data->len % (4 * data->nch))) {
        if (s->peq_on) peq_process(&s->dsp, (float *)data->audio,
                    (unsigned)data->len / (unsigned)(4 * data->nch));
        if (s->stock_on) return xm5_stock_play(&s->stock, data);
    }
    return data;                             /* bypass / odd length => identical */
}

static void uninit(af_instance *af) {
    player_state *s = (player_state *)af->setup;
    if (s) {
        /* Verified stock uninit frees BOTH data and setup, but does not null them. */
        if (s->stock.setup || s->stock.data) xm5_stock_uninit(&s->stock);
        s->stock.data = 0; s->stock.setup = 0;
    }
    if (af->data) { free(af->data); af->data = 0; }
    if (af->setup) { free(af->setup); af->setup = 0; }
}

/* open(): installed in place of the stock af_info->open at 0x44fe04.
 * Returns 1 on success, -2 on allocation failure (the host then calls uninit,
 * mirroring the stock open's -2/ uninit contract -- G1 alloc_failure). */
int xm5_peq_open(af_instance *af) {
    af->control = control;
    af->uninit  = uninit;
    af->play    = play;
    af->delay   = 0;
    af->mul     = 1;                         /* stock EQ sets mul=1.0 (double)  */
    af->data  = (af_data *)calloc(1, sizeof(af_data));
    af->setup = calloc(1, sizeof(player_state));
    if (!af->data || !af->setup) return XM5_AF_ERROR;
    player_state *s = (player_state *)af->setup;
    s->stock.info = af->info;
    if (xm5_stock_open(&s->stock) != XM5_AF_OK) return XM5_AF_ERROR;
    s->bypass = 1; /* safe until first reinit         */
    return 1;
}
