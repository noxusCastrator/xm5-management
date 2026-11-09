/* Versioned, exact-width control state. Preset protocol remains unchanged. */
#ifndef XM5_LAYERS_H
#define XM5_LAYERS_H
#define XM5_LAYERS_SIZE 11
static int xm5_layers_parse(const char *p, unsigned n, int *stock, int *peq) {
    const char tag[] = "XM5EQ1 "; unsigned i;
    if (n != XM5_LAYERS_SIZE) return 0;
    for (i = 0; i < 7; ++i) if (p[i] != tag[i]) return 0;
    if ((p[7] != '0' && p[7] != '1') || p[8] != ' ' ||
        (p[9] != '0' && p[9] != '1') || p[10] != '\n') return 0;
    *stock = p[7] - '0'; *peq = p[9] - '0'; return 1;
}
static void xm5_layers_encode(char *p, int stock, int peq) {
    const char tag[] = "XM5EQ1 0 0\n"; unsigned i;
    for (i = 0; i < XM5_LAYERS_SIZE; ++i) p[i] = tag[i];
    p[7] = stock ? '1' : '0'; p[9] = peq ? '1' : '0';
}
#endif
