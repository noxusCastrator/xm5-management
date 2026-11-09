#define _GNU_SOURCE
#include "inline_flac.h"
#include "xm5_stat.h"
#include "formats/other_container.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>

static const unsigned sides[] = {330, 160, 80};
static const unsigned capacities[] = {98304, 32768, 12288};
enum { FIRST_BLOCK = 42 };

struct metadata {
    unsigned char header[4];
    unsigned size;
    unsigned kind;
    int last;
};

int xm5_read(int fd, void *buffer, size_t length, off_t position)
{
    unsigned char *bytes = buffer;
    while (length) {
        ssize_t count = pread(fd, bytes, length, position);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0)
            return -1;
        bytes += count;
        length -= (size_t)count;
        position += count;
    }
    return 0;
}

int xm5_write(int fd, const void *buffer, size_t length)
{
    const unsigned char *bytes = buffer;
    while (length) {
        ssize_t count = write(fd, bytes, length);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0)
            return -1;
        bytes += count;
        length -= (size_t)count;
    }
    return 0;
}

int xm5_path(char *out, size_t capacity, const char *base, const char *leaf)
{
    int count = snprintf(out, capacity, "%s/%s", base, leaf);
    return count < 0 || (size_t)count >= capacity ? -1 : 0;
}

static unsigned le32(const unsigned char p[4])
{
    return (unsigned)p[0] | ((unsigned)p[1] << 8) |
           ((unsigned)p[2] << 16) | ((unsigned)p[3] << 24);
}

/* Read segment headers only and admit the exact baseline JPEG profile. */
static int jpeg_size_ok(int fd, off_t start, unsigned size, unsigned side)
{
    unsigned char marker[7];
    unsigned at = 2;
    if (size < 4 || xm5_read(fd, marker, 2, start) || marker[0] != 0xff || marker[1] != 0xd8)
        return 0;
    while (at <= size - 4) {
        if (xm5_read(fd, marker, 4, start + at) || marker[0] != 0xff)
            return 0;
        if (marker[1] == 0xff) {
            at++;
            continue;
        }
        unsigned length = ((unsigned)marker[2] << 8) | marker[3];
        if (length < 2 || length > size - at - 2 || marker[1] == 0xda)
            return 0;
        if (marker[1] == 0xc0) {
            if (length < 8 || xm5_read(fd, marker, 7, start + at + 4))
                return 0;
            return marker[0] == 8 && (((unsigned)marker[1] << 8) | marker[2]) == side &&
                   (((unsigned)marker[3] << 8) | marker[4]) == side;
        }
        at += length + 2;
    }
    return 0;
}

static int write_le32(int fd, uint32_t value)
{
    unsigned char p[4] = {value, value >> 8, value >> 16, value >> 24};
    return xm5_write(fd, p, sizeof(p));
}

static int metadata_at(int fd, off_t position, off_t end, struct metadata *item)
{
    unsigned char *p = item->header;
    if (position > end - 4 || xm5_read(fd, p, 4, position))
        return -1;
    item->size = ((unsigned)p[1] << 16) | ((unsigned)p[2] << 8) | p[3];
    item->kind = p[0] & 127;
    item->last = (p[0] & 128) != 0;
    return item->kind == 127 || (off_t)item->size > end - position - 4 ? -1 : 0;
}

static int streaminfo(int fd, off_t end, struct metadata *item)
{
    unsigned char signature[4];
    if (end <= FIRST_BLOCK || xm5_read(fd, signature, 4, 0) ||
        memcmp(signature, "fLaC", 4) || metadata_at(fd, 4, end, item))
        return -1;
    return item->kind == 0 && item->size == 34 ? 0 : -1;
}

int xm5_find(int fd, int scan, struct xm5_block *block)
{
    off_t end = lseek(fd, 0, SEEK_END);
    off_t position = FIRST_BLOCK;
    struct metadata item;
    int found = 0;

    if (streaminfo(fd, end, &item))
        return -1;
    while (!item.last) {
        unsigned char id[4];
        if (metadata_at(fd, position, end, &item) || item.kind == 0)
            return -1;
        if (item.kind == 2) {
            if (item.size < sizeof(id) || xm5_read(fd, id, sizeof(id), position + 4))
                return -1;
            if (!memcmp(id, XM5_APP_ID, sizeof(id))) {
                if (found || item.size < XM5_APP_FIXED || item.size > XM5_APP_FIXED + XM5_LYRIC_MAX)
                    return -1;
                *block = (struct xm5_block){position, item.size};
                found = 1;
            }
        }
        if (!scan)
            break;
        position += 4 + item.size;
    }
    return found;
}

int xm5_find_media(int fd, int scan, struct xm5_block *block)
{
    char signature[4];
    if (xm5_read(fd, signature, sizeof(signature), 0))
        return -1;
    if (!memcmp(signature, "fLaC", sizeof(signature)))
        return xm5_find(fd, scan, block);
    return xm5_other_find(fd, block);
}

int xm5_slot(int fd, const struct xm5_block *block, unsigned side,
             off_t *position, unsigned *length)
{
    off_t offset = block->header + 8;
    for (unsigned i = 0; i < 3; i++) {
        if (side == sides[i]) {
            unsigned char size[4];
            if (xm5_read(fd, size, 4, offset))
                return -1;
            *length = le32(size);
            *position = offset + 4;
            if (!*length)
                return 0;
            if (*length > capacities[i] - 4 || !jpeg_size_ok(fd, *position, *length, side))
                return -1;
            return 1;
        }
        offset += capacities[i];
    }
    return -1;
}

int xm5_fields(int fd, const struct xm5_block *block, int32_t *peak, int32_t *crest,
               off_t *lyric_position, unsigned *lyric_length)
{
    unsigned char levels[8];
    if (block->size < XM5_APP_FIXED || block->size > XM5_APP_FIXED + XM5_LYRIC_MAX)
        return -1;
    *lyric_position = block->header + 4 + XM5_APP_FIXED;
    *lyric_length = block->size - XM5_APP_FIXED;
    if (xm5_read(fd, levels, sizeof(levels), *lyric_position - 8))
        return -1;
    *peak = (int32_t)le32(levels);
    *crest = (int32_t)le32(levels + 4);
    return *peak > 0 || (*crest < 0 && *crest != INT32_MIN) ? -1 : 0;
}

static int zero_range(int fd, off_t position, unsigned length)
{
    unsigned char buffer[4096];
    while (length) {
        unsigned size = length < sizeof(buffer) ? length : sizeof(buffer);
        if (xm5_read(fd, buffer, size, position))
            return -1;
        for (unsigned i = 0; i < size; i++)
            if (buffer[i])
                return -1;
        position += size;
        length -= size;
    }
    return 0;
}

static int utf8_range(int fd, off_t position, unsigned length)
{
    unsigned char buffer[4096];
    unsigned remaining = 0, scalar = 0, minimum = 0;
    while (length) {
        unsigned size = length < sizeof(buffer) ? length : sizeof(buffer);
        if (xm5_read(fd, buffer, size, position))
            return -1;
        for (unsigned i = 0; i < size; i++) {
            unsigned byte = buffer[i];
            if (remaining) {
                if ((byte & 0xc0) != 0x80)
                    return -1;
                scalar = (scalar << 6) | (byte & 63);
                if (!--remaining && (scalar < minimum || scalar > 0x10ffff ||
                                     (scalar >= 0xd800 && scalar <= 0xdfff)))
                    return -1;
            } else if (byte >= 0xc2 && byte <= 0xf4) {
                remaining = byte < 0xe0 ? 1 : byte < 0xf0 ? 2 : 3;
                minimum = remaining == 1 ? 0x80 : remaining == 2 ? 0x800 : 0x10000;
                scalar = byte & (remaining == 1 ? 31 : remaining == 2 ? 15 : 7);
            } else if (byte >= 0x80) {
                return -1;
            }
        }
        position += size;
        length -= size;
    }
    return remaining ? -1 : 0;
}

int xm5_validate(int fd, const struct xm5_block *block, int complete)
{
    off_t position;
    unsigned length, lyric_length;
    int32_t peak, crest;
    if (xm5_fields(fd, block, &peak, &crest, &position, &lyric_length))
        return -1;
    if (complete && utf8_range(fd, position, lyric_length))
        return -1;
    for (unsigned i = 0; i < 3; i++) {
        int found = xm5_slot(fd, block, sides[i], &position, &length);
        if (found < 0)
            return -1;
        if (complete) {
            unsigned char ending[2];
            if (found && (xm5_read(fd, ending, 2, position + length - 2) ||
                          ending[0] != 0xff || ending[1] != 0xd9))
                return -1;
            if (zero_range(fd, position + length, capacities[i] - 4 - length))
                return -1;
        }
    }
    return 0;
}

static int copy_range(int input, int output, off_t offset, off_t bytes)
{
    unsigned char buffer[8192];
    while (bytes) {
        size_t chunk = bytes < (off_t)sizeof(buffer) ? (size_t)bytes : sizeof(buffer);
        if (xm5_read(input, buffer, chunk, offset) || xm5_write(output, buffer, chunk))
            return -1;
        offset += (off_t)chunk;
        bytes -= (off_t)chunk;
    }
    return 0;
}

static int write_zeroes(int fd, unsigned length)
{
    static const unsigned char zero[4096];
    while (length) {
        unsigned chunk = length < sizeof(zero) ? length : sizeof(zero);
        if (xm5_write(fd, zero, chunk))
            return -1;
        length -= chunk;
    }
    return 0;
}

static int optional_input(const char *path, int *fd, unsigned *length, unsigned limit)
{
    *fd = -1;
    *length = 0;
    if (!path || !strcmp(path, "-"))
        return 0;
    *fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (*fd < 0)
        return -1;
    off_t size = lseek(*fd, 0, SEEK_END);
    if (size < 0 || (unsigned long)size > limit)
        return -1;
    *length = (unsigned)size;
    return 0;
}

static int write_application(int output, int last, const int art[3], const unsigned length[3],
                             int lyrics, unsigned lyric_size, int32_t peak, int32_t crest)
{
    unsigned size = XM5_APP_FIXED + lyric_size;
    unsigned char header[4] = {2 | (last ? 128 : 0), size >> 16, size >> 8, size};
    if (xm5_write(output, header, sizeof(header)) || xm5_write(output, XM5_APP_ID, 4))
        return -1;
    for (unsigned i = 0; i < 3; i++) {
        if (length[i] && !jpeg_size_ok(art[i], 0, length[i], sides[i]))
            return -1;
        if (write_le32(output, length[i]) || copy_range(art[i], output, 0, length[i]) ||
            write_zeroes(output, capacities[i] - 4 - length[i]))
            return -1;
    }
    return write_le32(output, (uint32_t)peak) || write_le32(output, (uint32_t)crest) ||
           copy_range(lyrics, output, 0, lyric_size) ? -1 : 0;
}

static int copy_application(int input, int output, const struct xm5_block *app, int last)
{
    unsigned size = app->size;
    unsigned char header[4] = {2 | (last ? 128 : 0), size >> 16, size >> 8, size};
    return xm5_write(output, header, 4) ||
           copy_range(input, output, app->header + 4, size) ? -1 : 0;
}

static int rewrite(int input, int output, const struct xm5_block *app, int found,
                   const int art[3], const unsigned length[3], int lyrics,
                   unsigned lyric_size, int32_t peak, int32_t crest)
{
    off_t end = lseek(input, 0, SEEK_END);
    struct metadata item;
    if (streaminfo(input, end, &item))
        return -1;
    int streaminfo_last = item.last;
    if (xm5_write(output, "fLaC\0\0\0\042", 8) || copy_range(input, output, 8, 34))
        return -1;
    int last = streaminfo_last || (found && app->header == FIRST_BLOCK &&
                                   !metadata_at(input, FIRST_BLOCK, end, &item) && item.last);
    if (found ? copy_application(input, output, app, last)
              : write_application(output, last, art, length, lyrics, lyric_size, peak, crest))
        return -1;

    off_t position = FIRST_BLOCK;
    off_t last_header = -1;
    if (!streaminfo_last) {
        do {
            if (metadata_at(input, position, end, &item))
                return -1;
            if (!found || app->header != position) {
                last_header = lseek(output, 0, SEEK_CUR);
                if (last_header < 0 || copy_range(input, output, position, 4 + item.size))
                    return -1;
            }
            position += 4 + item.size;
        } while (!item.last);
    }
    if (found && !last && last_header >= 0) {
        unsigned char type;
        if (xm5_read(output, &type, 1, last_header))
            return -1;
        type |= 128;
        if (pwrite(output, &type, 1, last_header) != 1)
            return -1;
    }
    return copy_range(input, output, position, end - position);
}

int xm5_organize(const char *path, const char *const art[3], const char *lyrics,
                 int32_t peak, int32_t crest)
{
    int input = -1, output = -1, lyric = -1;
    int images[3] = {-1, -1, -1};
    unsigned length[3], lyric_size;
    struct xm5_block app, written;
    xm5_stat initial, current;
    char temporary[XM5_PATH];
    int staged = 0, result = -1;

    input = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (input < 0 || xm5_fstat(input, &initial) || !xm5_is_regular(initial.mode))
        goto done;
    int found = xm5_find(input, 1, &app);
    if (found < 0 || (found && xm5_validate(input, &app, 1)))
        goto done;
    if (found && app.header == FIRST_BLOCK) {
        result = 0;
        goto done;
    }
    if (!found) {
        for (unsigned i = 0; i < 3; i++)
            if (optional_input(art[i], &images[i], &length[i], capacities[i] - 4))
                goto done;
        if (optional_input(lyrics, &lyric, &lyric_size, XM5_LYRIC_MAX))
            goto done;
    } else {
        memset(length, 0, sizeof(length));
        lyric_size = 0;
    }

    int count = snprintf(temporary, sizeof(temporary), "%s.xm5-tmp-XXXXXX", path);
    if (count < 0 || (size_t)count >= sizeof(temporary))
        goto done;
    output = mkstemp(temporary);
    if (output < 0)
        goto done;
    staged = 1;
    if (rewrite(input, output, &app, found, images, length, lyric, lyric_size, peak, crest) ||
        xm5_find(output, 1, &written) != 1 || written.header != FIRST_BLOCK ||
        xm5_validate(output, &written, 1) || fsync(output))
        goto done;
    if (xm5_lstat(path, &current) || !xm5_is_regular(current.mode) ||
        current.dev != initial.dev || current.ino != initial.ino ||
        current.size != initial.size || current.mtime != initial.mtime ||
        current.mtime_nsec != initial.mtime_nsec || current.ctime != initial.ctime ||
        current.ctime_nsec != initial.ctime_nsec)
        goto done;
    if (fchmod(output, initial.mode & 0777) || rename(temporary, path))
        goto done;
    staged = 0;
    /* The rename is the commit. Persist its parent directory on filesystems
     * which support directory fsync; FAT may report EINVAL after a valid commit. */
    char parent[XM5_PATH];
    strcpy(parent, path);
    char *slash = strrchr(parent, '/');
    if (slash) {
        *slash = 0;
        int directory = open(parent, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (directory >= 0) { (void)fsync(directory); close(directory); }
    }
    result = 1;
done:
    if (output >= 0)
        close(output);
    if (staged)
        unlink(temporary);
    if (input >= 0)
        close(input);
    if (lyric >= 0)
        close(lyric);
    for (unsigned i = 0; i < 3; i++)
        if (images[i] >= 0)
            close(images[i]);
    return result;
}

static int name_order(const void *left, const void *right)
{
    const struct dirent *a = *(const struct dirent *const *)left;
    const struct dirent *b = *(const struct dirent *const *)right;
    size_t i = 0, an = strlen(a->d_name), bn = strlen(b->d_name);
    for (;;) {
        unsigned ac = i < an ? (unsigned char)a->d_name[i] :
            (i == an && a->d_type == DT_DIR ? '/' : 0);
        unsigned bc = i < bn ? (unsigned char)b->d_name[i] :
            (i == bn && b->d_type == DT_DIR ? '/' : 0);
        if (ac != bc) return ac < bc ? -1 : 1;
        if (!ac) return 0;
        i++;
    }
}

static int audio_name(const char *name)
{
    const char *suffix = strrchr(name, '.');
    if (!suffix)
        return 0;
    return !strcasecmp(suffix, ".flac") || !strcasecmp(suffix, ".mp3") ||
           !strcasecmp(suffix, ".wav") || !strcasecmp(suffix, ".aif") ||
           !strcasecmp(suffix, ".aiff") || !strcasecmp(suffix, ".m4a");
}

static int scan_directory(const char *root, const char *relative, FILE *index, unsigned depth)
{
    char path[XM5_PATH];
    if (depth > 24 || xm5_path(path, sizeof(path), root, relative))
        return -1;
    struct dirent **entries;
    int total = scandir(path, &entries, NULL, NULL);
    if (total < 0)
        return -1;
    for (int i = 0; i < total; i++) {
        if (entries[i]->d_type == DT_UNKNOWN) {
            char child[XM5_PATH];
            xm5_stat st;
            if (!xm5_path(child, sizeof(child), path, entries[i]->d_name) && !xm5_lstat(child, &st))
                entries[i]->d_type = xm5_is_directory(st.mode) ? DT_DIR : xm5_is_regular(st.mode) ? DT_REG : DT_UNKNOWN;
        }
    }
    qsort(entries, (size_t)total, sizeof(*entries), name_order);
    int result = 0;
    for (int i = 0; i < total; i++) {
        const struct dirent *entry = entries[i];
        const char *name = entry->d_name;
        char next[XM5_PATH];
        if (!result && strcmp(name, ".") && strcmp(name, "..") && !strpbrk(name, "\r\n\t")) {
            if (xm5_path(next, sizeof(next), relative, name)) {
                result = -1;
            } else if (entry->d_type == DT_DIR) {
                result = scan_directory(root, next, index, depth + 1);
            } else if (entry->d_type == DT_REG && audio_name(name)) {
                if (xm5_path(path, sizeof(path), root, next)) {
                    result = -1;
                } else {
                    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
                    struct xm5_block app;
                    if (fd < 0) {
                        result = -1;
                    } else {
                        int found = xm5_find_media(fd, 1, &app);
                        if (found == 1 && xm5_validate(fd, &app, 0))
                            found = -1;
                        close(fd);
                        if (found < 0)
                            fprintf(stderr, "XM5_INLINE_SKIP %s\n", next);
                        else if (fprintf(index, "%d\t%s\n", found, next) < 0)
                            result = -1;
                    }
                }
            }
        }
        free(entries[i]);
    }
    free(entries);
    return result;
}

int xm5_rebuild(const char *root)
{
    char path[XM5_PATH], temporary[XM5_PATH];
    if (xm5_path(path, sizeof(path), root, XM5_INDEX) ||
        xm5_path(temporary, sizeof(temporary), root, XM5_INDEX ".tmp-XXXXXX"))
        return -1;
    int fd = mkstemp(temporary);
    if (fd < 0)
        return -1;
    FILE *index = fdopen(fd, "w");
    int result = -1;
    if (index) {
        result = scan_directory(root, "xm5/music", index, 0);
        if (fflush(index) || fsync(fd))
            result = -1;
        if (fclose(index))
            result = -1;
    } else {
        close(fd);
    }
    if (!result && !rename(temporary, path))
        return 0;
    unlink(temporary);
    return -1;
}
