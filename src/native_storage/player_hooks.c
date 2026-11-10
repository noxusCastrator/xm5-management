/* PEQ08 adapter. Workers select bounded prepared artwork; the GUI assigns a
 * URI. Large original covers never enter the managed display path. */
#include "managed_image.h"
#define ROOT "/mnt/mmc"
#define RAM "/tmp/xa"
#define ENTRIES 8
#define PTR32(a) (*(void **)(uintptr_t)(a))
#define BYTE(a) (*(volatile unsigned char *)(uintptr_t)(a))
extern int stock_tfstatus(void);
extern int stock_album_cover(const char *, const char *, int, int);
extern int stock_set_cover(const char *, const char *, int);
extern int stock_folder_row(void *, int);
extern int stock_folder_play(void *);
extern int stock_jump_play(void *, int);
extern int stock_power_play(void *);
extern void *stock_lookup(void *, const char *, int);
extern unsigned stock_deque_size(void *);
extern void *stock_deque_at(void *, unsigned);
extern int stock_draw_type(void *, int);
extern int stock_destroy_children(void *);
extern const char *stock_get_image_prop(void *, const char *, const char *);
extern int stock_mutex_lock(void *);
extern int stock_mutex_unlock(void *);
static unsigned char io_lock[24] __attribute__((aligned(4)));
static volatile unsigned lock;
static unsigned generation, serial, play_seq;
static char playing[1024];
static pa_card io_card, view_card;
static int initialized;
static volatile unsigned usb_owned;
static unsigned inline_import_generation;
static void state_initial(void);
static void state_notify(void);
static int inline_list(void *);
static int managed_card(void);
typedef struct {
    unsigned generation, used;
    int managed, code[PA_SIZES];
    char path[1024], uri[PA_SIZES][64];
} entry;
static entry ready[ENTRIES];
static void take(void) {
    while (__sync_lock_test_and_set(&lock, 1)) {
    }
    __sync_synchronize();
}
static void release(void) {
    __sync_synchronize();
    __sync_lock_release(&lock);
}
static int text(char *d, unsigned cap, const char *s) {
    unsigned n = 0;
    if (!s)
        return -1;
    while (s[n]) {
        if (n + 1 >= cap)
            return -1;
        d[n] = s[n];
        n++;
    }
    d[n] = 0;
    return 0;
}
/* Stock row layout expands an 80-square image widget to 80x120. Select the
 * image by its fitting side; the painting adapter centers exact-size pixels. */
static unsigned widget_side(const void *w) {
    if (!w)
        return 0;
    int width = *(const int *)((const char *)w + 8), height = *(const int *)((const char *)w + 12);
    if (width <= 0 || height <= 0 || width > 2048 || height > 2048)
        return 0;
    unsigned side = (unsigned)(width < height ? width : height);
    return pa_size_slot(side) >= 0 ? side : 0;
}
static int local(const char *p) {
    return p && !strncmp(p, ROOT "/", 9) && p[9];
}
static int reserved_card_path(const char *p) {
    if (!local(p))
        return 0;
    p += 9;
    return !strncmp(p, ".xm5-before-", 12) || !strncmp(p, ".xm5-library-", 13) ||
           !strncmp(p, ".xm5-export-", 12);
}
static int prepared_namespace(const char *p) {
    return !usb_owned && p &&
           (!strcmp(p, ROOT "/xm5") || !strncmp(p, ROOT "/xm5/", 13) || reserved_card_path(p)) &&
           pa_card_present(ROOT);
}
static int current_card(pa_card *copy, unsigned *g) {
    if (usb_owned)
        return 0;
    take();
    *copy = view_card;
    *g = generation;
    release();
    if (!*g || !pa_card_current(copy))
        return 0;
    take();
    int ok = *g == generation;
    release();
    return ok;
}
void pa_render_stamp(unsigned *g, unsigned *s) {
    take();
    *g = generation;
    *s = play_seq;
    release();
}
int pa_render_valid(unsigned g, unsigned s) {
    int yes;
    pa_card card;
    take();
    yes = g == generation && s == play_seq;
    card = view_card;
    release();
    if (!yes || !g || !pa_card_current(&card))
        return 0;
    take();
    yes = g == generation && s == play_seq;
    release();
    return yes;
}
/* The RAM alias names the card directory; no raw image file is copied into RAM.
 * The small path cache is only an optimization. Eviction does not hide artwork. */
static int prepare(const char *path, const char *requested) {
    xm5_art_record rec;
    entry next;
    const char *role = requested;
    unsigned i, slot = ENTRIES, oldest = ~0u, g;
    int code, managed = 0;
    if (!local(path))
        return 0;
    stock_mutex_lock(io_lock);
    if (usb_owned) {
        stock_mutex_unlock(io_lock);
        return 1;
    }
    if (!initialized) {
        memset(&io_card, 0, sizeof(io_card));
        io_card.root = io_card.meta = io_card.index = io_card.lyrics = io_card.assets = -1;
        initialized = 1;
    }
    if (io_card.root < 0 || !pa_card_current(&io_card)) {
        char old_medium[65], new_medium[65];
        int carry = io_card.info_mode == 2 && inline_import_generation == generation &&
                    pa_medium_identity(&io_card, old_medium);
        take();
        generation++;
        view_card.root = view_card.meta = view_card.index = -1;
        release();
        take();
        memset(ready, 0, sizeof(ready));
        release();
        pa_card_close(&io_card);
        code = pa_card_open(&io_card, ROOT);
        if (code) {
            stock_mutex_unlock(io_lock);
            return code == PA_UNMANAGED ? 0 : 1;
        }
        if (pa_bind_native(&io_card, RAM)) {
            pa_card_close(&io_card);
            stock_mutex_unlock(io_lock);
            return 1;
        }
        if (carry && io_card.info_mode == 2 && pa_medium_identity(&io_card, new_medium) &&
            !strcmp(old_medium, new_medium))
            inline_import_generation = generation;
        take();
        view_card = io_card;
        release();
    }
    take();
    g = generation;
    for (i = 0; i < ENTRIES; i++)
        if (ready[i].generation == g && !strcmp(ready[i].path, path)) {
            int found = ready[i].managed;
            ready[i].used = ++serial;
            release();
            stock_mutex_unlock(io_lock);
            return found;
        }
    release();
    memset(&next, 0, sizeof(next));
    text(next.path, sizeof(next.path), path);
    next.generation = g;
    code = pa_lookup_size(&io_card, role, path + 9, !strcmp(role, "directory") ? 80 : 330, &rec);
    if (code == XM5_ART_MISS && !strcmp(role, "directory")) {
        role = "playback";
        code = pa_lookup_size(&io_card, role, path + 9, 330, &rec);
    }
    managed = code != XM5_ART_MISS;
    next.managed = managed;
    /* Metadata LRU only. PC outputs remain on the card. */
    take();
    for (i = 0; i < ENTRIES; i++) {
        if (ready[i].generation != g) {
            slot = i;
            break;
        }
        if (ready[i].used < oldest) {
            oldest = ready[i].used;
            slot = i;
        }
    }
    memset(&ready[slot], 0, sizeof(entry));
    release();
    for (i = 0; i < PA_SIZES; i++) {
        next.code[i] = managed ? code : PA_UNMANAGED;
        if (!managed)
            continue;
        if (!strcmp(role, "directory") && pa_sizes[i] != 80 && pa_sizes[i] != 160) {
            next.code[i] = XM5_ART_NONE;
            continue;
        }
        next.code[i] = pa_lookup_size(&io_card, role, path + 9, pa_sizes[i], &rec);
        if (next.code[i] == XM5_ART_OK) {
            next.code[i] = pa_native_uri(&rec, RAM, next.uri[i], 64);
        }
    }
    if (!pa_card_current(&io_card)) {
        for (i = 0; i < PA_SIZES; i++)
            next.code[i] = PA_CHANGED;
    }
    take();
    if (g == generation) {
        next.used = ++serial;
        ready[slot] = next;
    }
    release();
    stock_mutex_unlock(io_lock);
    BYTE(0x806454) = 1;
    return managed;
}
static int cached_unlocked(const char *path, unsigned side, char uri[64], unsigned *g) {
    pa_card c;
    xm5_art_record rec;
    unsigned i;
    int n = pa_size_slot(side), code = PA_IO;
    uri[0] = 0;
    if (n < 0)
        return XM5_ART_INVALID;
    if (!current_card(&c, g))
        return PA_CHANGED;
    take();
    if (*g == generation)
        for (i = 0; i < ENTRIES; i++)
            if (ready[i].generation == *g && !strcmp(ready[i].path, path)) {
                code = ready[i].code[n];
                text(uri, 64, ready[i].uri[n]);
                ready[i].used = ++serial;
                break;
            }
    release();
    if (code != PA_IO && !c.info_mode)
        return code;
    /* Cold directory scans advance at most one bounded candidate slice. A pending
     * result remains managed and requests a later paint, without stock decoding. */
    /* An inline directory with no descendants is NONE, not MISS. Try the actual
     * media file first, so that a directory miss cannot hide its valid picture. */
    code = pa_lookup_size(&c, "playback", path + 9, side, &rec);
    if (code == XM5_ART_MISS)
        code = pa_lookup_size(&c, "directory", path + 9, side, &rec);
    if (code == XM5_ART_MISS)
        return PA_UNMANAGED;
    if (code == XM5_ART_OK)
        code = pa_native_uri(&rec, RAM, uri, 64);
    if (code == XM5_ART_BUSY)
        BYTE(0x806454) = 1;
    take();
    if (*g != generation)
        code = PA_CHANGED;
    release();
    return code;
}
static int cached(const char *path, unsigned side, char uri[64], unsigned *g) {
    stock_mutex_lock(io_lock);
    int r = usb_owned ? PA_CHANGED : cached_unlocked(path, side, uri, g);
    stock_mutex_unlock(io_lock);
    return r;
}
static int show(void *parent, const char *widget_name, const char *path, const char *def,
                unsigned seq, int playback, int code, const char *uri) {
    void *w = stock_lookup(parent, widget_name, 1);
    int valid;
    const char *previous;
    if (!w)
        return 16;
    unsigned side = widget_side(w);
    if (!side)
        return pa_image_set(w, def);
    previous = stock_get_image_prop(w, "image", "");
    if (previous && !strcmp(previous, code == XM5_ART_OK ? uri : def)) {
        if (code == XM5_ART_OK) {
            if (playback)
                pa_play_image_set(w, uri);
            else
                pa_image_set(w, uri);
        }
        return 0;
    }
    if (playback) {
        take();
        valid = seq == play_seq && !strcmp(playing, path);
        release();
        if (!valid)
            code = PA_CHANGED;
        stock_destroy_children(w);
    }
    if (code == XM5_ART_OK) {
        if (playback)
            pa_play_image_set(w, uri);
        else
            pa_image_set(w, uri);
        stock_draw_type(w, 4);
    } else
        pa_image_set(w, def);
    return 0;
}
static int unavailable(int code) {
    return code == PA_UNMANAGED || (code == PA_CHANGED && !pa_card_present(ROOT));
}
int pa_worker_folder(const char *path) {
    if (!local(path))
        return stock_tfstatus();
    if (prepare(path, "directory") || prepared_namespace(path))
        return 0;
    return stock_tfstatus();
}
int pa_worker_same(const char *path, const char *saved) {
    return local(path) && pa_card_present(ROOT) &&
                   (prepare(path, "playback") || prepared_namespace(path))
               ? 1
               : strcmp(path, saved);
}
int pa_worker_play(const char *path, const char *dst, int w, int h) {
    if (w == 330 && h == 330 && (prepare(path, "playback") || prepared_namespace(path)))
        return 1;
    return stock_album_cover(path, dst, w, h);
}
int pa_set_play(const char *path, const char *extra, int type) {
    take();
    play_seq++;
    if (text(playing, sizeof(playing), path))
        playing[0] = 0;
    release();
    int r = stock_set_cover(path, extra, type);
    state_initial();
    return r;
}
int pa_folder_row(void *parent, int index) {
    void *d = PTR32(0x805de4);
    void *record;
    char path[1024], uri[64];
    unsigned g;
    int code;
    if (index < 0 || (unsigned)index >= stock_deque_size(d))
        return 16;
    record = stock_deque_at(d, (unsigned)index);
    if (!record)
        return 16;
    unsigned kind = *(unsigned *)((char *)record + 52);
    if ((kind != 4 && kind != 8) || !BYTE(0x807271))
        return stock_folder_row(parent, index);
    if (text(path, sizeof(path), *(char **)((char *)record + 12)) || !local(path))
        return stock_folder_row(parent, index);
    code = cached(path, 80, uri, &g);
    if (unavailable(code) && !prepared_namespace(path))
        return stock_folder_row(parent, index);
    return show(parent, "img_icon", path, kind == 4 ? "icon_folder" : "icon_music", 0, 0, code,
                uri);
}
static int play_view(void *parent, const char *widget, const char *def, unsigned side, int mode,
                     int force) {
    char path[1024], uri[64];
    unsigned seq, g;
    int code;
    if (mode == 1 && !force && !BYTE(0x806453))
        return 0;
    take();
    text(path, sizeof(path), playing);
    seq = play_seq;
    release();
    if (!local(path))
        code = PA_UNMANAGED;
    else
        code = cached(path, side, uri, &g);
    if (unavailable(code) && !prepared_namespace(path)) {
        if (mode == 0)
            return stock_folder_play(parent);
        if (mode == 1)
            return stock_jump_play(parent, force);
        return stock_power_play(parent);
    }
    BYTE(0x806453) = 0;
    return show(parent, widget, path, def, seq, 1, code, uri);
}
int pa_folder_play(void *parent) {
    return play_view(parent, "img_album", "default_cover", 90, 0, 0);
}
int pa_jump_play(void *parent, int force) {
    return play_view(parent, "img_cover", "default_cover", 330, 1, force);
}
int pa_power_play(void *parent) {
    return play_view(parent, "img_playcover", "defaultcover", 150, 2, 0);
}

extern int stock_snprintf(char *, unsigned, const char *, const char *);
extern int stock_list_m3u(const char *, void *);
extern int stock_save_playlist(const char *, const char *, unsigned);
extern int stock_system(const char *);
extern int stock_nf_init(void *, void *, const char *, const char *, void *);
const char pa_peq_dir[] = "/mnt/mmc/xm5/Struct/peq";
const char pa_playlist_dir[] = "/mnt/mmc/xm5/Struct/playlists";
static int managed_card(void) {
    int ok;
    prepare(ROOT "/xm5", "directory");
    stock_mutex_lock(io_lock);
    ok = io_card.root >= 0 && pa_card_current(&io_card);
    stock_mutex_unlock(io_lock);
    return ok;
}
int pa_lyric_path(char *out, unsigned cap, const char *format, const char *source) {
    char linked[32];
    int code;
    if (!source || !out || !local(source) || cap < 18)
        return stock_snprintf(out, cap, format, source);
    prepare(source, "playback");
    stock_mutex_lock(io_lock);
    code = pa_lyric_link(&io_card, source + 9, play_seq & 7, linked, sizeof(linked));
    stock_mutex_unlock(io_lock);
    if (!code) {
        text(out, cap, linked);
        return (int)strlen(linked);
    }
    return stock_snprintf(out, cap, format, source);
}
int pa_playlist_list(const char *path, void *deque) {
    int yes = managed_card();
    int r = stock_list_m3u(yes ? pa_playlist_dir : path, deque);
    if (yes && io_card.info_mode == 2)
        inline_list(deque);
    return r;
}
int pa_playlist_save(const char *path, const char *name, unsigned id) {
    int yes = managed_card();
    if (yes && io_card.info_mode == 2) {
        state_notify();
        return 1;
    }
    return stock_save_playlist(yes ? pa_playlist_dir : path, name, id);
}
int pa_playlist_mkdir(const char *command) {
    return managed_card() ? 0 : stock_system(command);
}
int pa_nf_init(void *backend, void *platform, const char *import_dir, const char *active,
               void *owner) {
    int r =
        stock_nf_init(backend, platform, managed_card() ? pa_peq_dir : import_dir, active, owner);
    state_initial();
    return r;
}
extern int stock_set_eq(float, float, float, float, float, float, float, float, float, float, int);
extern int stock_play_status(void);
int pa_set_eq(float a, float b, float c, float d, float e, float f, float g, float h, float i,
              float j, int state) {
    if (!state) {
        char path[1024];
        float db = 0;
        take();
        text(path, sizeof(path), playing);
        release();
        if (local(path)) {
            prepare(path, "playback");
            stock_mutex_lock(io_lock);
            int enabled = io_card.root >= 0 && !pa_info_gain(&io_card, path + 9, &db) && db != 0;
            stock_mutex_unlock(io_lock);
            if (enabled)
                state = stock_play_status() == 1 ? 2 : 1;
        }
    }
    return stock_set_eq(a, b, c, d, e, f, g, h, i, j, state);
}
extern void *stock_loader_load(void *, unsigned, unsigned, const char *, const char *);
extern int stock_loader_exist(void *, const char *);
extern void *stock_asset_create(unsigned, unsigned, const char *, unsigned);
extern int stock_asset_destroy(void *);
static int virtual_name(const char *p) {
    return p && (!strncmp(p, "/tmp/xa/", 8) || !strncmp(p, "file:///tmp/xa/", 15));
}
int pa_loader_exist(void *loader, const char *path) {
    xm5_art_record r;
    int code;
    if (!virtual_name(path) || !initialized || !io_card.info_mode)
        return stock_loader_exist(loader, path);
    stock_mutex_lock(io_lock);
    code = pa_info_asset(&io_card, path, &r);
    stock_mutex_unlock(io_lock);
    return code == 0;
}
void *pa_loader_load(void *loader, unsigned type, unsigned subtype, const char *path,
                     const char *name) {
    xm5_art_record r;
    void *asset = 0;
    int code;
    if (!virtual_name(path) || !initialized || !io_card.info_mode)
        return stock_loader_load(loader, type, subtype, path, name);
    if (!name)
        return 0;
    stock_mutex_lock(io_lock);
    code = pa_info_asset(&io_card, path, &r);
    if (!code) {
        asset = stock_asset_create(type, subtype, name, r.info_length);
        if (asset) {
            unsigned *a = asset;
            if (a[1] != r.info_length || !a[11] ||
                pa_info_read(&io_card, &r, (void *)(uintptr_t)a[11], r.info_length)) {
                stock_asset_destroy(asset);
                asset = 0;
            }
        }
    }
    stock_mutex_unlock(io_lock);
    return asset;
}
extern void *stock_manager_load_file(void *, unsigned, const char *);
void *pa_manager_load_file(void *manager, unsigned type, const char *path) {
    /* file:// sources take this direct filesystem route before the generic
     * asset loader. Virtual slices have no physical JPEG file to stat. */
    if (!virtual_name(path) || !initialized || !io_card.info_mode)
        return stock_manager_load_file(manager, type, path);
    return pa_loader_load(0, type, io_card.info_mode == 2 ? 4 : 2, path, path);
}
#include "managed_covers.inc"
#include "state_hooks.inc"

#include "playlist_hooks.inc"
#include "usb_hooks.inc"

int pa_locked_asset_matches(const char *n, const void *p, unsigned bytes) {
    stock_mutex_lock(io_lock);
    int ok = pa_asset_matches(n, p, bytes);
    stock_mutex_unlock(io_lock);
    return ok;
}
