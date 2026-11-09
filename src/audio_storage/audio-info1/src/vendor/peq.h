/* Equalizer APO preset parser and biquad DSP core.
 * Derived from q2-ringnav d127c6ea07af20bde73dd842df255ce58d52e5be patch/peq.h
 * (MIT, Copyright (c) 2026 Diamond Bond; see LICENSE). Changes: see ../COMPATIBILITY.md. */
#ifndef PEQ_H
#define PEQ_H

#include "peq_platform.h"

#define PEQ_BANDS 30 /* active (ON) bands; OFF and None lines do not use a slot */
#define PEQ_CHANNELS 8
#define PEQ_FILE_LIMIT 16384
#define PEQ_LINE_LIMIT 512

/* Same layout as upstream Q2PEQ02 so stored presets stay readable (peq_store.c).
 * enabled: 0 off, 1 both channels, 2 left only, 3 right only. type: 0 peaking, 1 low shelf, 2 high shelf.
 * frequency is always the RBJ design (centre) frequency: the APO LS/HS corner shift is applied by the parser.
 * balance: dB, above 0 turns the left channel down, below 0 the right. */
typedef struct { int enabled, type; double frequency, gain, q; } peq_band;
typedef struct { int count, bypass; double preamp; peq_band bands[PEQ_BANDS]; double balance; } peq_preset;
/* Parser errors: line is the 1-based text line. Compile errors: line is the 1-based band, 0 for the preset. */
typedef struct { unsigned line; const char *reason; } peq_error;
typedef struct { double b0, b1, b2, a1, a2; } peq_coeff;
typedef struct {
    peq_coeff c[PEQ_BANDS];
    double z[PEQ_CHANNELS][PEQ_BANDS][2], gain[PEQ_CHANNELS];
    int bypass, used, only[PEQ_BANDS]; /* used: bands past it are identity with zero memory; only: 0 every channel, else channel only-1 */
} peq_engine;
typedef struct {
    peq_engine current, next, pending;
    int rate, channels, ramp, ramp_length, waiting;
    int failed; /* 1 when the last requested preset was rejected and bypass was selected instead */
} peq_dsp;

void peq_default(peq_preset *p);
int peq_valid(const peq_preset *p);
int peq_number(const char *s, double *out);
int peq_parse(const char *text, unsigned size, peq_preset *out, peq_error *error);
int peq_biquad_stable(const peq_coeff *c);
int peq_compile(const peq_preset *p, int rate, peq_engine *out, peq_error *error);
/* Control side, never from the PCM callback. p == NULL selects bypass. On failure the DSP selects
 * bypass (no previous filter stays active), d->failed is set and 0 is returned. */
int peq_reset(peq_dsp *d, int rate, int channels, const peq_preset *p, peq_error *error);
int peq_update(peq_dsp *d, const peq_preset *p, peq_error *error);
int peq_apply_text(peq_dsp *d, const char *text, unsigned size, peq_preset *out, peq_error *error);
/* PCM callback: interleaved float, no allocation or file access. */
void peq_process(peq_dsp *d, float *audio, unsigned frames);

#endif
