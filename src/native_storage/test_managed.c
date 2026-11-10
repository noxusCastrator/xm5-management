/* Actual stock image paint bytes + target-compiled lifecycle adapter.
 * The display, widget/heap boundaries are controlled, not physical hardware. */
#include "managed_image.h"
#include <asm/unistd.h>
extern int native_image_paint(void *, void *);
static long raw(long nr, long a, long b, long c) {
    register long v __asm__("$2") = nr, r4 __asm__("$4") = a, r5 __asm__("$5") = b,
                    r6 __asm__("$6") = c, r7 __asm__("$7") = 0;
    __asm__ volatile("syscall" : "+r"(v), "+r"(r7) : "r"(r4), "r"(r5), "r"(r6) : "memory");
    return r7 ? -v : v;
}
static void req(int y, const char *s) {
    if (!y) {
        raw(__NR_write, 2, (long)s, strlen(s));
        raw(__NR_exit, 1, 0, 0);
    }
}
typedef struct {
    unsigned mem[40];
    char image[64];
} widget;
static widget root, a, b;
static unsigned manager[8], items[8], entries[8][24];
static unsigned g = 1, seq = 1, freed, draws, ex_calls, destroys, manager_freed, paint_gc,
                kill_manager, stale, transforms;
static int (*idle_fn)(void *), (*timer_fn)(void *);
static const char *uri = "file:///tmp/xa/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.bmp";
void pa_render_stamp(unsigned *x, unsigned *y) {
    *x = g;
    *y = seq;
}
int pa_render_valid(unsigned x, unsigned y) {
    return x == g && y == seq;
}
const char *stock_get_image_prop(void *w, const char *p, const char *d) {
    (void)p;
    (void)d;
    return ((widget *)w)->image;
}
void *stock_widget_image_manager(void *w) {
    (void)w;
    return manager;
}
int stock_set_image(void *w, const char *n) {
    widget *v = w;
    strcpy(v->image, n);
    v->mem[30] = (unsigned)v->image;
    return 0;
}
int stock_image_destroy(void *w) {
    (void)w;
    destroys++;
    return 0;
}
int stock_owner_drop(void *e) {
    (void)e;
    freed++;
    return 0;
}
int stock_manager_destroy(void *m) {
    unsigned *v = m;
    while (v[0])
        stock_owner_drop((void *)items[--v[0]]);
    manager_freed++;
    return 0;
}
unsigned stock_idle_add(void *f, void *c) {
    (void)c;
    idle_fn = f;
    return 1;
}
unsigned stock_timer_add(void *f, void *c, unsigned ms) {
    (void)c;
    req(ms == 200, "GC polling interval wrong\n");
    timer_fn = f;
    return 2;
}
int stock_need_transform(void *w) {
    (void)w;
    transforms++;
    return 1;
}
int stock_canvas_image_ex(void *c, void *b, unsigned t, const int *r) {
    (void)c;
    (void)b;
    (void)t;
    (void)r;
    ex_calls++;
    return 0;
}
int stock_canvas_image(void *c, void *b, const int *s, const int *d) {
    (void)c;
    unsigned *v = b;
    req(s[0] == 0 && s[1] == 0 && s[2] == (int)v[0] && s[3] == (int)v[1] && s[2] == d[2] &&
            s[3] == d[3],
        "native paint reached a resampling rectangle\n");
    draws++;
    return 0;
}
void *test_image_cast(void *w) {
    return w;
}
void *test_vg(void *c) {
    (void)c;
    return (void *)1;
}
int test_helper(void *w, void *c, void *bitmap, void *d) {
    (void)w;
    (void)c;
    (void)bitmap;
    (void)d;
    return 0;
}
int test_load(void *w, const char *n, unsigned *bmap) {
    widget *v = w;
    memset(bmap, 0, 72);
    bmap[0] = bmap[1] = v->mem[2];
    bmap[4] = (unsigned)n;
    if (paint_gc)
        req(pa_collect() == 8, "collection ran while stock paint borrowed pixels\n");
    if (kill_manager)
        req(pa_manager_destroy(manager) == 0, "manager destruction was not deferred\n");
    if (stale)
        g++;
    return 0;
}
int *test_rect(int *r, int x, int y, int w, int h) {
    r[0] = x;
    r[1] = y;
    r[2] = w;
    r[3] = h;
    return r;
}
int stock_image_paint(void *w, void *c) {
    return native_image_paint(w, c);
}
static void init_widget(widget *w, unsigned side, widget *parent) {
    memset(w, 0, sizeof(*w));
    w->mem[2] = w->mem[3] = side;
    ((unsigned short *)w->mem)[18] = 4;
    w->mem[17] = (unsigned)parent;
}
static void insert(void) {
    unsigned n = manager[0];
    memset(entries[n], 0, 96);
    entries[n][18] = (unsigned)uri;
    items[n] = (unsigned)entries[n];
    manager[0]++;
    pa_note_manager(manager);
}
void start_c(unsigned *sp) {
    (void)sp;
    unsigned i, before;
    init_widget(&root, 400, 0);
    init_widget(&a, 80, &root);
    init_widget(&b, 80, &root);
    manager[2] = (unsigned)items;
    pa_image_set(&a, uri);
    pa_image_set(&b, uri);
    insert();
    pa_collect();
    req(!freed && manager[0] == 1, "visible shared bitmap collected\n");
    ((unsigned short *)a.mem)[18] = 0;
    pa_collect();
    req(!freed, "one hidden sharer freed visible owner\n");
    ((unsigned short *)root.mem)[18] = 0;
    pa_collect();
    req(freed == 1 && !manager[0], "hidden ancestor retained pixels\n");
    ((unsigned short *)root.mem)[18] = 4;
    ((unsigned short *)a.mem)[18] = 4;
    insert();
    for (i = 0; i < PA_SIZES; i++) {
        a.mem[2] = a.mem[3] = pa_sizes[i];
        before = draws;
        paint_gc = 1;
        pa_image_paint(&a, (void *)1);
        req(draws == before + 1, "actual stock paint did not call unit blit\n");
    }
    req(!transforms && !ex_calls, "managed image used ordinary transform/scale path\n");
    a.mem[2] = 80;
    a.mem[3] = 120;
    before = draws;
    pa_image_paint(&a, (void *)1);
    req(draws == before + 1 && !ex_calls,
        "stock rectangular row resampled exact80-square artwork\n");
    a.mem[3] = 80;
    before = draws;
    stale = 1;
    pa_image_paint(&a, (void *)1);
    stale = 0;
    req(draws == before, "stale paint reached display\n");
    pa_image_set(&a, "stock_icon");
    pa_image_set(&b, "stock_icon");
    req(idle_fn && timer_fn, "lifecycle release not scheduled\n");
    idle_fn(0);
    req(freed == 2 && !manager[0], "idle did not release unused image\n");
    pa_image_set(&a, uri);
    insert();
    pa_image_destroy(&a);
    timer_fn(0);
    req(freed == 3 && destroys == 1, "widget destroy did not release owner\n");
    pa_image_set(&a, uri);
    insert();
    kill_manager = 1;
    pa_image_paint(&a, (void *)1);
    kill_manager = 0;
    req(!manager_freed, "manager freed borrowed image inside paint\n");
    idle_fn(0);
    req(manager_freed == 1 && freed == 4, "deferred manager destruction did not finish\n");
    req(pa_need_transform(&b) == 1 && transforms == 1, "ordinary icon transformation changed\n");
    paint_gc = 0;
    pa_play_image_set(&a, uri);
    insert();
    seq++;
    before = draws;
    pa_image_paint(&a, (void *)1);
    req(draws == before && !strcmp(a.image, "default_cover"),
        "old track image was retagged during paint\n");
    pa_collect();
    pa_image_set(&a, uri);
    insert();
    seq++;
    before = draws;
    pa_image_paint(&a, (void *)1);
    req(draws == before + 1, "library thumbnail invalidated by unrelated track change\n");
    g++;
    before = draws;
    pa_image_paint(&a, (void *)1);
    req(draws == before && !strcmp(a.image, "default_cover"),
        "old card image rendered under a new paint stamp\n");
    pa_collect();
    pa_play_image_set(&a, uri);
    insert();
    seq++;
    pa_play_image_set(&a, uri);
    before = draws;
    pa_image_paint(&a, (void *)1);
    req(draws == before + 1, "same-content next track did not rebind\n");
    const char *msg =
        "Actual MIPS stock paint:9 sizes unit blit; shared/hidden/destroy/paint GC: PASS\n";
    raw(__NR_write, 1, (long)msg, strlen(msg));
    raw(__NR_exit, 0, 0, 0);
    for (;;) {
    }
}

int pa_play_image_name(void *w, const char *n, char out[64]) {
    (void)w;
    (void)n;
    (void)out;
    return 0;
}
