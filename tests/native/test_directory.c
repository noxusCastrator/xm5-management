/* Actual storage reader, synthetic filesystem only; no vendor executable. */
#define _GNU_SOURCE
static unsigned fixture_seconds = 10;
#define XM5_DIRECTORY_NOW() fixture_seconds
#include "../../src/native_storage/prepared_io.c"
#include <stdarg.h>
#include <stdlib.h>

static unsigned probes;
int __real_openat(int, const char *, int, ...);
int __wrap_openat(int dir, const char *path, int flags, ...) {
    int mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = va_arg(args, int);
        va_end(args);
    }
    size_t n = strlen(path);
    if (n >= 5 && !strcmp(path + n - 5, ".flac"))
        probes++;
    return __real_openat(dir, path, flags, mode);
}
static void check(int yes, const char *message) {
    if (!yes) {
        fprintf(stderr, "%s\n", message);
        exit(1);
    }
}
int main(int argc, char **argv) {
    check(argc == 2, "fixture root required");
    const char *cases[] = {"noart", "late", "stalezero", "zeroslot"};
    for (unsigned i = 0; i < 4; i++) {
        char root[1024];
        pa_card card;
        xm5_art_record rec;
        snprintf(root, sizeof(root), "%s/%s", argv[1], cases[i]);
        check(!pa_card_open(&card, root) && card.info_mode == 2, "fixture admission");
        unsigned calls = 0, total = 0, max = 0;
        int code;
        do {
            probes = 0;
            code = pa_lookup_size(&card, "directory", "xm5/music/Album", 80, &rec);
            check(probes <= 64, "directory slice exceeds 64 media probes");
            total += probes;
            if (probes > max)
                max = probes;
            check(++calls < 20, "scan failed to progress");
        } while (code == XM5_ART_BUSY);
        check(code == ((i == 1 || i == 2) ? XM5_ART_OK : XM5_ART_NONE), "scan result changed");
        check(total == 129, "scan lost or repeated a candidate");
        if (!code)
            check(!strcmp(rec.path, "xm5/music/Album/00128.flac"), "first valid descendant");
        probes = 0;
        check(pa_lookup_size(&card, "directory", "xm5/music/Album", 80, &rec) == code,
              "cache result");
        check(probes <= 1, "completed directory repeats entire scan");
        if (i == 0 || i == 2) {
            char from[1200], to[1200];
            snprintf(from, sizeof(from), "%s/prepared.flac", root);
            snprintf(to, sizeof(to), "%s/xm5/music/Album/00000.flac", root);
            check(!rename(from, to), "owned fixture media replacement");
            fixture_seconds += 6;
            if (i == 0) {
                for (unsigned j = 0; j < 128; j++) {
                    probes = 0;
                    check(pa_lookup_size(&card, "directory", "xm5/music/Album", 80, &rec) ==
                              XM5_ART_NONE,
                          "negative cache expired before two minutes");
                    check(!probes, "frequent paint retried a completed no-art subtree");
                }
            }
            fixture_seconds += 115;
            check(!pa_lookup_size(&card, "directory", "xm5/music/Album", 80, &rec),
                  "expired hint miss did not recover");
            check(!strcmp(rec.path, "xm5/music/Album/00000.flac"), "earlier new art ignored");
        }
        printf("%s: %u candidates, %u slices, max %u per slice\n", cases[i], total, calls, max);
        pa_card_close(&card);
    }
    /* Replacing the hint index invalidates an unfinished old-generation scan. */
    char root[1024], old[1200], replacement[1200];
    pa_card card;
    xm5_art_record rec;
    snprintf(root, sizeof(root), "%s/zeroslot", argv[1]);
    check(!pa_card_open(&card, root), "generation admission");
    fixture_seconds += 6;
    check(pa_lookup_size(&card, "directory", "xm5/music/Album", 80, &rec) == XM5_ART_BUSY,
          "pending generation");
    snprintf(old, sizeof(old), "%s/xm5/Struct/tracks.tsv", root);
    snprintf(replacement, sizeof(replacement), "%s/replacement.tsv", root);
    check(!rename(replacement, old), "owned index replacement");
    probes = 0;
    check(pa_lookup_size(&card, "directory", "xm5/music/Album", 80, &rec) != XM5_ART_OK,
          "stale generation accepted");
    check(!probes, "old generation opened media");
    pa_card_close(&card);
    check(!pa_card_open(&card, root), "new generation admission");
    check(pa_lookup_size(&card, "directory", "xm5/music/Album", 80, &rec) == XM5_ART_BUSY,
          "cursor was not reset");
    pa_card_close(&card);
    /* The actual directory reader must share one budget across prepare's
     * discovery + 80/160 lookups, not replenish it on each inner call. */
    check(!pa_card_open(&card, root), "shared scope admission");
    probes = 0;
    pa_directory_begin();
    pa_lookup_size(&card, "directory", "xm5/music/Album", 80, &rec);
    pa_lookup_size(&card, "directory", "xm5/music/Album", 80, &rec);
    pa_lookup_size(&card, "directory", "xm5/music/Album", 160, &rec);
    pa_directory_end();
    check(probes == 64, "shared scope did not cap the whole prepare sequence");
    pa_card_close(&card);
    return 0;
}
