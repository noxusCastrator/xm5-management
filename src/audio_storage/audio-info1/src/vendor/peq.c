/* Equalizer APO preset parser and biquad DSP core.
 * Derived from q2-ringnav d127c6ea07af20bde73dd842df255ce58d52e5be patch/peq.c
 * (MIT, Copyright (c) 2026 Diamond Bond; see LICENSE). Changes: see ../COMPATIBILITY.md.
 * Filter design uses the RBJ Audio EQ Cookbook formulas; no Equalizer APO code is included. */
#include "peq.h"

static int between(double x, double lo, double hi) {
    return PEQ_ISFINITE(x) && x >= lo && x <= hi;
}

void peq_default(peq_preset *p) {
    static const double frequencies[10] = {31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000};
    memset(p, 0, sizeof(*p));
    p->count = 10;
    p->bypass = 1;
    for (int i = 0; i < PEQ_BANDS; ++i) {
        p->bands[i].frequency = i < 10 ? frequencies[i] : 1000;
        p->bands[i].q = 1.41; /* one octave wide, as graphic EQs use */
    }
}

static int valid_band(const peq_band *b) {
    return b->enabled >= 0 && b->enabled <= 3 && b->type >= 0 && b->type <= 2 &&
           between(b->frequency, 20, 20000) && between(b->gain, -24, 24) && between(b->q, 0.1, 10);
}

int peq_valid(const peq_preset *p) {
    if (!p || p->count < 0 || p->count > PEQ_BANDS || (p->bypass != 0 && p->bypass != 1) ||
        !between(p->preamp, -60, 24) || !between(p->balance, -12, 12)) return 0;
    for (int i = 0; i < p->count; ++i) if (!valid_band(&p->bands[i])) return 0;
    return 1;
}

static int error_at(peq_error *e, unsigned line, const char *reason) {
    if (e) { e->line = line; e->reason = reason; }
    return 0;
}

static int digit(char c) { return c >= '0' && c <= '9'; }
static int space(char c) { return c == ' ' || c == '\t'; }
static char lower(char c) { return c >= 'A' && c <= 'Z' ? (char)(c + 32) : c; }
static int numeric(char c) { return digit(c) || c == '+' || c == '-' || c == '.' || c == ',' || c == 'e' || c == 'E'; }

/* s[0..n) equals the NUL-terminated word w, exactly or ignoring ASCII case. */
static int same(const char *s, unsigned n, const char *w) {
    unsigned i = 0;
    for (; i < n && w[i]; ++i) if (s[i] != w[i]) return 0;
    return i == n && !w[i];
}
static int same_nocase(const char *s, unsigned n, const char *w) {
    unsigned i = 0;
    for (; i < n && w[i]; ++i) if (lower(s[i]) != w[i]) return 0;
    return i == n && !w[i];
}

/* Decimal only, '.' or ',' as the mark (APO converts ',' to '.'); no hex, NaN, infinity or garbage.
 * The validated token goes to strtod, so the result is correctly rounded. */
static int number_token(const char *s, unsigned n, double *out) {
    char buf[64], *end;
    unsigned i = 0, digits = 0;
    if (!n || n >= sizeof(buf)) return 0;
    if (s[i] == '+' || s[i] == '-') ++i;
    while (i < n && digit(s[i])) { ++i; ++digits; }
    if (i < n && (s[i] == '.' || s[i] == ',')) for (++i; i < n && digit(s[i]); ++i) ++digits;
    if (!digits) return 0;
    if (i < n && (s[i] == 'e' || s[i] == 'E')) {
        unsigned exponent = 0;
        if (++i < n && (s[i] == '+' || s[i] == '-')) ++i;
        for (; i < n && digit(s[i]); ++i) ++exponent;
        if (!exponent) return 0;
    }
    if (i != n) return 0;
    for (i = 0; i < n; ++i) buf[i] = s[i] == ',' ? '.' : s[i];
    buf[n] = 0;
    double v = PEQ_STRTOD(buf, &end);
    if (end != buf + n || !PEQ_ISFINITE(v)) return 0;
    *out = v;
    return 1;
}

int peq_number(const char *s, double *out) {
    unsigned n = 0;
    while (s[n]) ++n;
    return number_token(s, n, out);
}

/* APO's getFreq multiplies Fc by 1000 when the token has 5+ characters, no exponent and a '.'
 * (after ',' -> '.') four characters from the end: "31.250" is 31250 Hz there. Reject such tokens. */
static int thousands_ambiguous(const char *s, unsigned n) {
    for (unsigned i = 0; i < n; ++i) if (s[i] == 'e' || s[i] == 'E') return 0;
    return n >= 5 && (s[n - 4] == '.' || s[n - 4] == ',');
}

typedef struct { const char *s; unsigned pos, end; } cursor;

static void skip(cursor *c) { while (c->pos < c->end && space(c->s[c->pos])) ++c->pos; }
static int at_end(cursor *c) { skip(c); return c->pos == c->end; }

/* Next whitespace-delimited word; returns its length (0 at the end). */
static unsigned word(cursor *c, const char **w) {
    skip(c);
    *w = c->s + c->pos;
    unsigned start = c->pos;
    while (c->pos < c->end && !space(c->s[c->pos])) ++c->pos;
    return c->pos - start;
}

static int expect(cursor *c, const char *w) {
    const char *t;
    unsigned n = word(c, &t);
    return same(t, n, w);
}

/* <number><unit> or <number> <unit>; unit NULL means the number must stand alone. */
static int number_unit(cursor *c, const char *unit, int nocase, const char **tok, unsigned *len, double *v) {
    skip(c);
    *tok = c->s + c->pos;
    unsigned start = c->pos;
    while (c->pos < c->end && numeric(c->s[c->pos])) ++c->pos;
    *len = c->pos - start;
    if (!number_token(*tok, *len, v)) return 0;
    if (!unit) return c->pos == c->end || space(c->s[c->pos]);
    skip(c); /* no-op for an attached unit */
    const char *t = c->s + c->pos;
    unsigned n = 0;
    for (; c->pos < c->end && !space(c->s[c->pos]) && c->s[c->pos] != '#'; ++c->pos) ++n;
    return nocase ? same_nocase(t, n, unit) : same(t, n, unit);
}

/* APO commands this importer does not implement; matched ignoring case, so case variants also reject. */
static const char *const unsupported[] = {
    "device", "if", "elseif", "else", "endif", "eval", "include", "stage", "iir", "biquad", "delay",
    "copy", "convolution", "graphiceq", "vstplugin", "loudnesscorrection", 0
};

static int command_word(const char *s, unsigned n) {
    if (same_nocase(s, n, "preamp") || same_nocase(s, n, "channel")) return 1;
    if (n >= 6 && same_nocase(s, 6, "filter")) {
        unsigned i = 6;
        while (i < n && space(s[i])) ++i;
        for (; i < n; ++i) if (!digit(s[i])) return 0;
        return 1;
    }
    for (int i = 0; unsupported[i]; ++i) if (same_nocase(s, n, unsupported[i])) return 1;
    return 0;
}

/* Structural UTF-8 check: no overlongs, surrogates or code points above U+10FFFF. */
static int utf8_valid(const unsigned char *s, unsigned n) {
    for (unsigned i = 0; i < n;) {
        unsigned c = s[i], more, cp;
        if (c < 0x80) { ++i; continue; }
        if (c >= 0xC2 && c <= 0xDF) { more = 1; cp = c & 0x1F; }
        else if (c >= 0xE0 && c <= 0xEF) { more = 2; cp = c & 0x0F; }
        else if (c >= 0xF0 && c <= 0xF4) { more = 3; cp = c & 0x07; }
        else return 0;
        if (n - i <= more) return 0;
        for (unsigned k = 1; k <= more; ++k) {
            if ((s[i + k] & 0xC0) != 0x80) return 0;
            cp = cp << 6 | (s[i + k] & 0x3F);
        }
        if ((more == 2 && (cp < 0x800 || (cp >= 0xD800 && cp <= 0xDFFF))) || (more == 3 && (cp < 0x10000 || cp > 0x10FFFF)))
            return 0;
        i += more + 1;
    }
    return 1;
}

static int ascii(const char *s, unsigned n) {
    for (unsigned i = 0; i < n; ++i) if ((unsigned char)s[i] >= 0x80) return 0;
    return 1;
}

int peq_parse(const char *text, unsigned size, peq_preset *out, peq_error *error) {
    peq_preset p;
    peq_default(&p);
    p.count = 0;
    p.bypass = 0;
    unsigned pos = 0, line = 0, preamp_line = 1;
    int scope = 1; /* APO Channel: 1 both, 2 left, 3 right, as peq_band.enabled */
    double side[4] = {0}; /* Preamp under Channel: L or R */
    if (!text) return error_at(error, 0, "no configuration");
    if (size > PEQ_FILE_LIMIT) return error_at(error, 1, "file exceeds 16384 bytes");
    if (size >= 3 && (unsigned char)text[0] == 239 && (unsigned char)text[1] == 187 &&
        (unsigned char)text[2] == 191) pos = 3;
    while (pos < size) {
        const char *s = text + pos;
        unsigned length = 0;
        ++line;
        while (pos < size && text[pos] != '\n') {
            if (length == PEQ_LINE_LIMIT) return error_at(error, line, "line exceeds 512 bytes");
            if (!text[pos]) return error_at(error, line, "embedded NUL (UTF-16 is not supported)");
            ++length; ++pos;
        }
        if (pos < size) ++pos;
        if (length && s[length - 1] == '\r') --length; /* CRLF */
        for (unsigned i = 0; i < length; ++i)
            if (s[i] == '\r') return error_at(error, line, "CR without LF (CR-only line ends are not supported)");
        if (!utf8_valid((const unsigned char *)s, length)) return error_at(error, line, "invalid UTF-8");
        unsigned start = 0;
        while (start < length && space(s[start])) ++start;
        if (start == length || s[start] == '#') continue; /* blank or comment */
        for (unsigned i = start; i < length; ++i)
            if (s[i] == '`') return error_at(error, line, "inline expressions (backticks) are not supported");
        unsigned colon = start;
        while (colon < length && s[colon] != ':') ++colon;
        if (colon == length) {
            /* APO ignores lines without a colon (exporter headers such as "Filter Settings file"),
             * but a command word followed by parameters is a broken command, not a header. */
            unsigned n = start;
            while (n < length && !space(s[n])) ++n;
            if (command_word(s + start, n - start)) {
                cursor h = {s, n, length};
                const char *t;
                unsigned k;
                while ((k = word(&h, &t))) {
                    int number = 0;
                    for (unsigned i = 0; i < k; ++i) number |= digit(t[i]);
                    if (number || same_nocase(t, k, "on") || same_nocase(t, k, "off"))
                        return error_at(error, line, "command without ':'");
                }
            }
            continue;
        }
        unsigned key_end = colon;
        while (key_end > start && space(s[key_end - 1])) --key_end;
        const char *key = s + start;
        unsigned key_length = key_end - start;
        cursor c = {s, colon + 1, length};
        if (same(key, key_length, "Preamp")) {
            const char *tok;
            unsigned n;
            double gain;
            if (!ascii(s, length)) return error_at(error, line, "non-ASCII character in command");
            if (!number_unit(&c, "db", 1, &tok, &n, &gain)) return error_at(error, line, "expected Preamp: <gain> dB");
            skip(&c);
            if (c.pos != c.end && s[c.pos] != '#') return error_at(error, line, "expected Preamp: <gain> dB");
            if (!between(gain, -60, 24)) return error_at(error, line, "preamp outside -60..24 dB");
            if (scope == 1) p.preamp += gain;
            else side[scope] += gain;
            preamp_line = line;
            continue;
        }
        if (same(key, key_length, "Channel")) {
            int l = 0, r = 0, any = 0;
            if (!ascii(s, length)) return error_at(error, line, "non-ASCII character in command");
            /* APO splits on U+0020 only: a tab or '#' would become part of a channel name. */
            for (unsigned i = colon + 1; i < length;) {
                if (s[i] == ' ') { ++i; continue; }
                unsigned n = i;
                while (n < length && s[n] != ' ') ++n;
                if (same_nocase(s + i, n - i, "l")) l = 1;
                else if (same_nocase(s + i, n - i, "r")) r = 1;
                else if (same_nocase(s + i, n - i, "all")) l = r = 1;
                else return error_at(error, line, "only channels L, R and all are supported");
                any = 1;
                i = n;
            }
            if (!any) return error_at(error, line, "expected Channel: L, R or all");
            scope = l && r ? 1 : l ? 2 : 3;
            continue;
        }
        if (key_length >= 6 && same(key, 6, "Filter")) {
            unsigned i = 6;
            while (i < key_length && space(key[i])) ++i;
            for (; i < key_length; ++i) if (!digit(key[i])) return error_at(error, line, "invalid filter label");
            if (!ascii(s, length)) return error_at(error, line, "non-ASCII character in command");
            const char *t;
            unsigned n = word(&c, &t);
            /* APO applies a filter only when the parameters start with "ON <type>"; anything after OFF is ignored. */
            if (same_nocase(t, n, "off")) continue;
            if (!same(t, n, "ON")) return error_at(error, line, "expected ON or OFF");
            n = word(&c, &t);
            if (same(t, n, "None")) {
                if (!at_end(&c)) return error_at(error, line, "unexpected text after None");
                continue;
            }
            peq_band b = {scope, 0, 0, 0, 0};
            int corner = 0;
            if (same(t, n, "PK") || same(t, n, "PEQ")) b.type = 0;
            else if (same(t, n, "LS") || same(t, n, "LSC")) b.type = 1;
            else if (same(t, n, "HS") || same(t, n, "HSC")) b.type = 2;
            else return error_at(error, line, "unsupported filter type");
            corner = b.type && n == 2; /* APO: LS/HS with Q take a corner frequency, LSC/HSC the centre */
            const char *ftok, *tok;
            unsigned flen, len;
            if (!expect(&c, "Fc") || !number_unit(&c, "Hz", 0, &ftok, &flen, &b.frequency) ||
                !expect(&c, "Gain") || !number_unit(&c, "dB", 0, &tok, &len, &b.gain))
                return error_at(error, line, "expected Fc <Hz> Hz Gain <dB> dB [Q <q>]");
            if (thousands_ambiguous(ftok, flen))
                return error_at(error, line, "ambiguous Fc: APO reads 3 decimals as a thousands separator");
            int has_q = 0;
            skip(&c);
            if (c.pos < c.end && s[c.pos] != '#') {
                if (!expect(&c, "Q") || !number_unit(&c, 0, 0, &tok, &len, &b.q))
                    return error_at(error, line, "expected Q <number>");
                has_q = 1;
            }
            skip(&c);
            if (c.pos < c.end) {
                /* APO has no inline comments: its regexes search the rest of the line for Q and BW. */
                if (s[c.pos] != '#') return error_at(error, line, "unsupported filter form");
                for (i = c.pos; i < length; ++i)
                    if (s[i] == 'Q' || (i + 1 < length && ((s[i] == 'B' && s[i + 1] == 'W') || (s[i] == 'F' && s[i + 1] == 'c'))) ||
                        (i + 3 < length && same(s + i, 4, "Gain")))
                        return error_at(error, line, "'#' after a filter is not a comment in APO; its text could set parameters");
            }
            if (!has_q && !b.type) return error_at(error, line, "peaking filter requires Q");
            if (!has_q) b.q = 1; /* placeholder for the range check below */
            if (!valid_band(&b)) return error_at(error, line, "range: 20..20000 Hz, -24..24 dB, Q 0.1..10");
            if (b.type) {
                double a = pow(10, b.gain / 40), ab = a + 1 / a;
                /* No Q: RBJ shelf slope S = 0.9 at Fc, expressed as the equivalent Q. */
                if (!has_q) b.q = 1 / sqrt(ab * (1 / 0.9 - 1) + 2);
                else if (corner) {
                    /* Corner to centre: Fc * 10^(+-|G| / 80 / S) with 1/S = (1/Q^2 - 2) / (A + 1/A) + 1. */
                    double f = pow(10, fabs(b.gain) / 80 * ((1 / (b.q * b.q) - 2) / ab + 1));
                    b.frequency = b.type == 1 ? b.frequency * f : b.frequency / f;
                    if (!between(b.frequency, 20, 20000))
                        return error_at(error, line, "LS/HS corner shift moves the shelf centre outside 20..20000 Hz");
                }
            }
            if (p.count == PEQ_BANDS) return error_at(error, line, "more than 30 active bands");
            p.bands[p.count++] = b;
            continue;
        }
        if (command_word(key, key_length))
            return error_at(error, line, same_nocase(key, key_length, "preamp") || same_nocase(key, key_length, "channel") ||
                                         (key_length >= 6 && same_nocase(key, 6, "filter"))
                                         ? "command names are case-sensitive (APO would ignore this line)"
                                         : "unsupported command");
        /* Any other key ("Dated:", "Notes:", "Equaliser:") is not an APO command and is ignored, as in APO. */
    }
    /* Per-channel preamps become the louder one's common preamp plus a balance that turns the other down. */
    p.preamp += side[2] > side[3] ? side[2] : side[3];
    p.balance = side[3] - side[2];
    if (!between(p.preamp, -60, 24)) return error_at(error, preamp_line, "summed preamp outside -60..24 dB");
    if (!between(p.balance, -12, 12)) return error_at(error, preamp_line, "L/R preamp differ by more than 12 dB");
    *out = p; /* Commit only after the complete file validates. */
    return 1;
}

/* Poles of 1 + a1 z^-1 + a2 z^-2 strictly inside the unit circle (Jury), all coefficients finite. */
int peq_biquad_stable(const peq_coeff *c) {
    return PEQ_ISFINITE(c->b0) && PEQ_ISFINITE(c->b1) && PEQ_ISFINITE(c->b2) && PEQ_ISFINITE(c->a1) &&
           PEQ_ISFINITE(c->a2) && fabs(c->a2) < 1 && fabs(c->a1) < 1 + c->a2;
}

/* RBJ peaking and Q-form shelves. All design work is outside the PCM callback. A band that cannot
 * be designed at this rate fails the whole preset; it is never skipped on its own. */
int peq_compile(const peq_preset *p, int rate, peq_engine *out, peq_error *error) {
    peq_engine e;
    memset(&e, 0, sizeof(e));
    if (!peq_valid(p)) return error_at(error, 0, "invalid preset");
    if (rate < 8000 || rate > 384000) return error_at(error, 0, "sample rate outside 8000..384000 Hz");
    for (int ch = 0; ch < PEQ_CHANNELS; ++ch) e.gain[ch] = pow(10, p->preamp / 20);
    if (p->balance > 0) e.gain[0] *= pow(10, -p->balance / 20);
    if (p->balance < 0) e.gain[1] *= pow(10, p->balance / 20);
    e.bypass = p->bypass;
    for (int i = 0; i < PEQ_BANDS; ++i) {
        e.c[i].b0 = 1;
        if (i >= p->count || !p->bands[i].enabled || !p->bands[i].gain) continue;
        e.used = i + 1;
        if (p->bands[i].enabled > 1) e.only[i] = p->bands[i].enabled - 1;
        const peq_band *b = &p->bands[i];
        if (!(b->frequency < rate * 0.5)) return error_at(error, i + 1, "band frequency at or above Nyquist for this sample rate");
        double a = pow(10, b->gain / 40), w = 6.283185307179586 * b->frequency / rate;
        double c = cos(w), alpha = sin(w) / (2 * b->q), r = 2 * sqrt(a) * alpha;
        double b0, b1, b2, a0, a1, a2;
        if (!b->type) {
            b0 = 1 + alpha * a; b1 = -2 * c; b2 = 1 - alpha * a;
            a0 = 1 + alpha / a; a1 = -2 * c; a2 = 1 - alpha / a;
        } else if (b->type == 1) {
            b0 = a * ((a+1) - (a-1)*c + r); b1 = 2*a*((a-1) - (a+1)*c);
            b2 = a*((a+1) - (a-1)*c - r); a0 = (a+1) + (a-1)*c + r;
            a1 = -2*((a-1) + (a+1)*c); a2 = (a+1) + (a-1)*c - r;
        } else {
            b0 = a*((a+1) + (a-1)*c + r); b1 = -2*a*((a-1) + (a+1)*c);
            b2 = a*((a+1) + (a-1)*c - r); a0 = (a+1) - (a-1)*c + r;
            a1 = 2*((a-1) - (a+1)*c); a2 = (a+1) - (a-1)*c - r;
        }
        e.c[i] = (peq_coeff){b0/a0, b1/a0, b2/a0, a1/a0, a2/a0};
        if (!peq_biquad_stable(&e.c[i])) return error_at(error, i + 1, "unstable or non-finite coefficients");
    }
    /* Nothing to do: pass samples through bit-exactly (no NaN zeroing or clipping). */
    if (!e.used && !p->preamp && !p->balance) e.bypass = 1;
    *out = e;
    return 1;
}

static void select_bypass(peq_engine *e) {
    memset(e, 0, sizeof(*e));
    e->bypass = 1;
}

int peq_reset(peq_dsp *d, int rate, int channels, const peq_preset *p, peq_error *error) {
    memset(d, 0, sizeof(*d));
    d->rate = rate;
    d->channels = channels;
    d->ramp_length = rate / 50 > 0 ? rate / 50 : 1; /* 20 ms */
    select_bypass(&d->current);
    if (p && !peq_compile(p, rate, &d->current, error)) d->failed = 1;
    return !d->failed;
}

int peq_update(peq_dsp *d, const peq_preset *p, peq_error *error) {
    d->failed = 0;
    if (!p) select_bypass(&d->pending);
    else if (!peq_compile(p, d->rate, &d->pending, error)) {
        select_bypass(&d->pending); /* never leave the previous filter running in place of a rejected one */
        d->failed = 1;
    }
    d->waiting = 1; /* Latest complete update wins; never interrupt an in-flight crossfade. */
    return !d->failed;
}

int peq_apply_text(peq_dsp *d, const char *text, unsigned size, peq_preset *out, peq_error *error) {
    peq_preset p;
    if (!peq_parse(text, size, &p, error)) {
        peq_update(d, 0, 0);
        d->failed = 1;
        return 0;
    }
    if (!peq_update(d, &p, error)) return 0;
    if (out) *out = p;
    return 1;
}

static double sample(peq_engine *e, double x, int ch) {
    if (e->bypass) return x;
    for (int i = 0; i < e->used; ++i) {
        const peq_coeff *c = &e->c[i];
        double *z = e->z[ch][i];
        /* The other channel's band: drop memory carried from a both-channel version. */
        if (e->only[i] && e->only[i] != ch + 1) { z[0] = z[1] = 0; continue; }
        /* A band left out with settled memory passes x through: most presets leave several. */
        if (c->b0 == 1 && !c->b1 && !c->b2 && !c->a1 && !c->a2 && !z[0] && !z[1]) continue;
        /* Flush |y| < ~2e-34 to zero: decaying tails would otherwise reach denormals, which MIPS FPUs trap on. */
        double y = c->b0 * x + z[0] + 1e-18 - 1e-18;
        z[0] = c->b1 * x - c->a1 * y + z[1];
        z[1] = c->b2 * x - c->a2 * y;
        x = y;
    }
    return x * e->gain[ch]; /* after the cascade, so filter memory is independent of preamp */
}

void peq_process(peq_dsp *d, float *audio, unsigned frames) {
    if (!audio || d->channels < 1 || d->channels > PEQ_CHANNELS) return;
    for (unsigned i = 0; i < frames; ++i) {
        if (!d->ramp && d->waiting && d->current.bypass && d->pending.bypass) {
            d->current = d->pending; /* bypass to bypass: no fade, so output stays bit-exact */
            d->waiting = 0;
        }
        if (!d->ramp && d->waiting) {
            d->next = d->pending;
            d->waiting = 0;
            d->ramp = d->ramp_length;
            /* Carry filter memory over so unchanged bands and preamp-only edits crossfade without a transient. */
            if (!d->current.bypass) memcpy(d->next.z, d->current.z, sizeof(d->next.z));
            /* Bands dropped from the end still run until their carried memory drains. */
            for (int b = d->next.used; b < d->current.used; ++b)
                for (int ch = 0; ch < PEQ_CHANNELS; ++ch) if (d->next.z[ch][b][0] || d->next.z[ch][b][1]) d->next.used = b + 1;
        }
        for (int ch = 0; ch < d->channels; ++ch, ++audio) {
            if (!d->ramp && d->current.bypass) continue; /* bit-exact steady bypass */
            /* Active policy: nonfinite input becomes 0, output is hard-clipped to [-1, 1]. */
            double x = *audio;
            if (!PEQ_ISFINITE(x)) x = 0;
            double y = sample(&d->current, x, ch);
            if (d->ramp) {
                double wet = sample(&d->next, x, ch);
                double mix = (double)(d->ramp_length - d->ramp + 1) / d->ramp_length;
                y += (wet - y) * mix;
            }
            *audio = (float)(y > 1 ? 1 : y < -1 ? -1 : y);
        }
        if (d->ramp && !--d->ramp) d->current = d->next;
    }
}
