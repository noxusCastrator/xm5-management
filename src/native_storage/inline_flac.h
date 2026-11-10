#ifndef XM5_INLINE_FLAC_H
#define XM5_INLINE_FLAC_H

#include <stddef.h>
#include <stdint.h>
#ifdef XM5_FREESTANDING
typedef long long off_t;
#else
#include <sys/types.h>
#endif

#define XM5_APP_ID "XM5I"
#define XM5_INDEX "xm5/Struct/tracks.tsv"
#define XM5_PATH 1024
#define XM5_LYRIC_MAX (128U * 1024)
#define XM5_APP_FIXED (4U + 98304U + 32768U + 12288U + 8U)

struct xm5_block {
    off_t header;
    unsigned size;
};

/* A canonical lookup reads the first two metadata headers only. */
int xm5_find(int fd, int scan, struct xm5_block *block);
int xm5_find_media(int fd, int scan, struct xm5_block *block);
int xm5_slot(int fd, const struct xm5_block *block, unsigned side, off_t *position,
             unsigned *length);
int xm5_fields(int fd, const struct xm5_block *block, int32_t *peak, int32_t *crest,
               off_t *lyric_position, unsigned *lyric_length);
int xm5_validate(int fd, const struct xm5_block *block, int complete);
int xm5_read(int fd, void *buffer, size_t length, off_t position);
int xm5_write(int fd, const void *buffer, size_t length);
int xm5_organize(const char *path, const char *const art[3], const char *lyrics, int32_t peak,
                 int32_t crest);
int xm5_rebuild(const char *root);
int xm5_path(char *out, size_t capacity, const char *base, const char *leaf);

#endif
