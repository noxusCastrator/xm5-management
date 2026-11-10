/* The stock importer requires canonical absolute names for its SQL lookup.
 * Its synchronous parser owns only a temporary RAM view, not the source list. */
extern int stock_parse_playlist(const char *);
/* libcstl iterator_t is a 28-byte value, not an integer position. MIPS O32
 * passes its return through a hidden destination argument and its value
 * parameter through registers plus stack. The normal C prototype is required
 * so the compiler supplies that ABI to the pinned PLT trampolines. */
typedef struct {
    unsigned words[7];
} pa_cstl_iterator;
_Static_assert(sizeof(pa_cstl_iterator) == 28, "pinned libcstl iterator ABI");
extern pa_cstl_iterator stock_deque_begin(const void *);
extern pa_cstl_iterator stock_iterator_next(pa_cstl_iterator);
extern pa_cstl_iterator stock_deque_erase(void *, pa_cstl_iterator);
extern char *stock_std_strdup(const char *);
extern void stock_std_free(void *);
extern int stock_deque_clear(void *);
int pa_parse_playlist(const char *path, unsigned id) {
    char staged[80], display[256];
    int mode = 0, code = PA_UNMANAGED;
    if (path && local(path) && !strncmp(path + 9, "xm5/Struct/playlists/", 21)) {
        stock_mutex_lock(io_lock);
        mode = io_card.root >= 0 && io_card.info_mode == 2 && !usb_owned;
        if (mode) {
            code = inline_playlist_stage(&io_card, path + 9, staged, sizeof(staged));
            if (!code) {
                if (inline_playlist_display(&io_card, path + 9, display, sizeof(display)))
                    text(display, sizeof(display), "Playlist");
            }
        }
        stock_mutex_unlock(io_lock);
    }
    if (!mode)
        return stock_parse_playlist(path);
    if (code) {
        if (PTR32(0x804d50))
            stock_deque_clear(PTR32(0x804d50));
        stock_mutex_lock(io_lock);
        inline_playlist_abandon(id);
        stock_mutex_unlock(io_lock);
        return -1;
    }
    int r = stock_parse_playlist(staged);
    stock_mutex_lock(io_lock);
    if (r >= 0)
        (void)inline_playlist_bind(&io_card, id, display, path + 9);
    else
        inline_playlist_abandon(id);
    inline_playlist_discard(staged);
    stock_mutex_unlock(io_lock);
    if (r < 0 && PTR32(0x804d50))
        stock_deque_clear(PTR32(0x804d50));
    return r;
}
static int inline_list(void *deque) {
    unsigned i = 0;
    char display[256];
    while (i < stock_deque_size(deque)) {
        void *r = stock_deque_at(deque, i);
        const char *path = r ? *(const char **)((char *)r + 12) : 0;
        if (!path || !local(path)) {
            i++;
            continue;
        }
        const char *base = path;
        for (const char *p = path; *p; p++)
            if (*p == '/')
                base = p + 1;
        if (!strcmp(base, "Favorites.m3u8")) {
            pa_cstl_iterator position = stock_deque_begin(deque);
            for (unsigned j = 0; j < i; j++)
                position = stock_iterator_next(position);
            (void)stock_deque_erase(deque, position);
            continue;
        }
        stock_mutex_lock(io_lock);
        int code = inline_playlist_display(&io_card, path + 9, display, sizeof(display));
        stock_mutex_unlock(io_lock);
        if (!code) {
            char *name = stock_std_strdup(display);
            if (name) {
                stock_std_free(*(void **)((char *)r + 8));
                *(char **)((char *)r + 8) = name;
            }
        }
        i++;
    }
    return 0;
}
