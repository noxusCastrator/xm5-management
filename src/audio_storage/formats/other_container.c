#include "other_container.h"

#ifdef XM5_FREESTANDING
extern off_t xm5_file_bytes(int);
static int inline_compare(const void *a, const void *b, size_t length)
{
    const unsigned char *x = a, *y = b;
    for (size_t i = 0; i < length; i++)
        if (x[i] != y[i])
            return x[i] < y[i] ? -1 : 1;
    return 0;
}
#define memcmp inline_compare
#else
#include <string.h>
#include <unistd.h>
#endif

enum { TAG_LIMIT = 32 * 1024 * 1024, HEADER_LIMIT = 65536 };
static const char private_owner[] = "org.xm5.inline";
static const unsigned char box_id[16] = {
    0xfb, 0xc2, 0x49, 0x71, 0xc0, 0xc1, 0x49, 0xd2,
    0x9f, 0xec, 0xb8, 0x99, 0xb4, 0xc8, 0xbe, 0xf2
};

static uint32_t be32(const unsigned char *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 |
           (uint32_t)p[2] << 8 | p[3];
}

static uint32_t le32(const unsigned char *p)
{
    return (uint32_t)p[3] << 24 | (uint32_t)p[2] << 16 |
           (uint32_t)p[1] << 8 | p[0];
}

static int file_at(int fd, off_t end, off_t pos, void *bytes, size_t count)
{
    if (pos < 0 || pos > end || count > (uint64_t)(end - pos))
        return -1;
    return xm5_read(fd, bytes, count, pos);
}

static int set_payload(int fd, off_t end, off_t pos, uint64_t size,
                       struct xm5_block *block)
{
    unsigned char marker[4];
    if (block->size || pos < 4 || pos > end || size < XM5_APP_FIXED ||
        size > XM5_APP_FIXED + XM5_LYRIC_MAX || size > (uint64_t)(end - pos) ||
        file_at(fd, end, pos, marker, sizeof(marker)) ||
        memcmp(marker, XM5_APP_ID, sizeof(marker)))
        return -1;
    block->header = pos - 4;
    block->size = (unsigned)size;
    return 0;
}

static int find_chunked(int fd, off_t end, const unsigned char *first, int aiff,
                        struct xm5_block *block)
{
    uint32_t (*number)(const unsigned char *) = aiff ? be32 : le32;
    const char *format = aiff ? "COMM" : "fmt ";
    const char *audio = aiff ? "SSND" : "data";
    const char *private = aiff ? "APPL" : XM5_APP_ID;
    int saw_format = 0, saw_audio = 0;
    unsigned count = 0;
    if (number(first + 4) != (uint64_t)end - 8)
        return -1;
    for (off_t pos = 12; pos < end;) {
        unsigned char header[8], field[4];
        if (++count > HEADER_LIMIT || file_at(fd, end, pos, header, sizeof(header)))
            return -1;
        pos += (off_t)sizeof(header);
        uint32_t size = number(header + 4);
        uint64_t padded = (uint64_t)size + (size & 1U);
        if (padded > (uint64_t)(end - pos))
            return -1;
        if (!memcmp(header, format, 4)) {
            if (saw_format || size < (aiff ? 18U : 16U))
                return -1;
            saw_format = 1;
        } else if (!memcmp(header, audio, 4)) {
            if (!saw_format || saw_audio ||
                (aiff && (size < 8 || file_at(fd, end, pos, field, 4) ||
                          be32(field) > size - 8)))
                return -1;
            saw_audio = 1;
        } else if (!memcmp(header, private, 4)) {
            int match = !aiff;
            if (aiff && size >= 4) {
                if (file_at(fd, end, pos, field, 4))
                    return -1;
                match = !memcmp(field, XM5_APP_ID, 4);
            }
            if (match && set_payload(fd, end, pos, size, block))
                return -1;
        }
        pos += (off_t)padded;
    }
    return saw_audio ? block->size != 0 : -1;
}

static int syncsafe(const unsigned char *bytes, uint32_t *size)
{
    if ((bytes[0] | bytes[1] | bytes[2] | bytes[3]) & 128U)
        return -1;
    *size = (uint32_t)bytes[0] << 21 | (uint32_t)bytes[1] << 14 |
            (uint32_t)bytes[2] << 7 | bytes[3];
    return 0;
}

static int mp3_frame(int fd, off_t end, off_t pos)
{
    static const unsigned rates[] = {44100, 48000, 32000};
    static const unsigned v1[] = {0, 32, 40, 48, 56, 64, 80, 96, 112, 128,
                                 160, 192, 224, 256, 320};
    static const unsigned v2[] = {0, 8, 16, 24, 32, 40, 48, 56, 64, 80,
                                 96, 112, 128, 144, 160};
    unsigned char header[4];
    if (file_at(fd, end, pos, header, sizeof(header)))
        return -1;
    unsigned version = (header[1] >> 3) & 3U;
    unsigned bitrate = header[2] >> 4;
    unsigned rate = (header[2] >> 2) & 3U;
    if (header[0] != 255 || (header[1] & 224U) != 224U ||
        (header[1] & 6U) != 2U || version == 1 || !bitrate || bitrate == 15 || rate == 3)
        return -1;
    unsigned sample_rate = rates[rate] >> (version == 3 ? 0 : version == 2 ? 1 : 2);
    unsigned length = (version == 3 ? 144000U * v1[bitrate] : 72000U * v2[bitrate]) /
                      sample_rate + ((header[2] >> 1) & 1U);
    return length <= (uint64_t)(end - pos) ? 0 : -1;
}

static int check_padding(int fd, off_t end, off_t pos)
{
    unsigned char bytes[4096];
    while (pos < end) {
        size_t count = end - pos < (off_t)sizeof(bytes) ? (size_t)(end - pos) : sizeof(bytes);
        if (file_at(fd, end, pos, bytes, count))
            return -1;
        for (size_t i = 0; i < count; ++i)
            if (bytes[i])
                return -1;
        pos += (off_t)count;
    }
    return 0;
}

static int find_mp3(int fd, off_t end, const unsigned char *first, struct xm5_block *block)
{
    if (memcmp(first, "ID3", 3))
        return mp3_frame(fd, end, 0);
    uint32_t tag_size;
    unsigned version = first[3], count = 0;
    if ((version != 3 && version != 4) || first[4] || first[5] ||
        syncsafe(first + 6, &tag_size) || tag_size > TAG_LIMIT ||
        mp3_frame(fd, end, (off_t)tag_size + 10))
        return -1;
    off_t tag_end = (off_t)tag_size + 10;
    for (off_t pos = 10; pos < tag_end;) {
        unsigned char header[10], owner[sizeof(private_owner)];
        uint32_t size;
        if (++count > HEADER_LIMIT || file_at(fd, tag_end, pos, header, 1))
            return -1;
        if (!header[0]) {
            if (check_padding(fd, tag_end, pos))
                return -1;
            break;
        }
        if (file_at(fd, tag_end, pos, header, sizeof(header)))
            return -1;
        for (unsigned i = 0; i < 4; ++i)
            if (!(header[i] >= 'A' && header[i] <= 'Z') &&
                !(header[i] >= '0' && header[i] <= '9'))
                return -1;
        if (version == 4) {
            if (syncsafe(header + 4, &size))
                return -1;
        } else {
            size = be32(header + 4);
        }
        pos += (off_t)sizeof(header);
        if (!size || size > (uint64_t)(tag_end - pos))
            return -1;
        if (!memcmp(header, "CHAP", 4) || !memcmp(header, "ASPI", 4) ||
            !memcmp(header, "SEEK", 4) || !memcmp(header, "RBUF", 4))
            return -1;
        if (!memcmp(header, "PRIV", 4)) {
            if (header[9])
                return -1;
            if (size >= sizeof(owner)) {
                if (file_at(fd, tag_end, pos, owner, sizeof(owner)))
                    return -1;
                if (!memcmp(owner, private_owner, sizeof(owner)) &&
                    set_payload(fd, tag_end, pos + (off_t)sizeof(owner),
                                size - sizeof(owner), block))
                    return -1;
            }
        }
        pos += (off_t)size;
    }
    return block->size != 0;
}

static int find_mp4(int fd, off_t end, struct xm5_block *block)
{
    int saw_movie = 0, saw_media = 0;
    unsigned count = 0;
    for (off_t pos = 0; pos < end;) {
        unsigned char header[8], extra[16];
        if (++count > HEADER_LIMIT || file_at(fd, end, pos, header, sizeof(header)))
            return -1;
        uint64_t size = be32(header);
        unsigned prefix = sizeof(header);
        if (size == 1) {
            if (file_at(fd, end, pos + prefix, extra, 8))
                return -1;
            size = (uint64_t)be32(extra) << 32 | be32(extra + 4);
            prefix += 8;
        }
        int uuid = !memcmp(header + 4, "uuid", 4);
        unsigned minimum = prefix + (uuid ? (unsigned)sizeof(box_id) : 0U);
        if (size < minimum || size > (uint64_t)(end - pos) ||
            (!pos && size < minimum + 8))
            return -1;
        if (!memcmp(header + 4, "moov", 4)) {
            if (saw_movie)
                return -1;
            saw_movie = 1;
        } else if (!memcmp(header + 4, "mdat", 4)) {
            saw_media = 1;
        } else if (!memcmp(header + 4, "moof", 4) || !memcmp(header + 4, "mfra", 4) ||
                   !memcmp(header + 4, "sidx", 4)) {
            return -1;
        } else if (uuid) {
            if (file_at(fd, end, pos + prefix, extra, sizeof(box_id)))
                return -1;
            if (!memcmp(extra, box_id, sizeof(box_id)) &&
                set_payload(fd, end, pos + minimum, size - minimum, block))
                return -1;
        }
        pos += (off_t)size;
    }
    return saw_movie && saw_media ? block->size != 0 : -1;
}

int xm5_other_find(int fd, struct xm5_block *block)
{
    struct xm5_block span = {0, 0};
    unsigned char first[12];
    int result = -1;
#ifdef XM5_FREESTANDING
    off_t end = xm5_file_bytes(fd);
#else
    off_t original = lseek(fd, 0, SEEK_CUR);
    if (original < 0)
        return -1;
    off_t end = lseek(fd, 0, SEEK_END);
    if (lseek(fd, original, SEEK_SET) != original)
        return -1;
#endif
    if (end < 0 || file_at(fd, end, 0, first, sizeof(first)))
        return -1;
    if (!memcmp(first, "RIFF", 4) && !memcmp(first + 8, "WAVE", 4))
        result = find_chunked(fd, end, first, 0, &span);
    else if (!memcmp(first, "FORM", 4) && !memcmp(first + 8, "AIFF", 4))
        result = find_chunked(fd, end, first, 1, &span);
    else if (!memcmp(first + 4, "ftyp", 4))
        result = find_mp4(fd, end, &span);
    else if (!memcmp(first, "ID3", 3) || first[0] == 255)
        result = find_mp3(fd, end, first, &span);
    if (result == 1)
        *block = span;
    return result;
}
