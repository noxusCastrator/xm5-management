/* Frozen PC/native metadata v2. All offsets are little-endian byte fields. */
static unsigned le32(const unsigned char *b) {
    return (unsigned)b[0] | (unsigned)b[1] << 8 | (unsigned)b[2] << 16 | (unsigned)b[3] << 24;
}
static int same(const void *a, const void *b, unsigned n) {
    const unsigned char *x = a, *y = b;
    while (n--)
        if (*x++ != *y++)
            return 0;
    return 1;
}
static int zeroes(const unsigned char *b, unsigned n) {
    while (n--)
        if (*b++)
            return 0;
    return 1;
}
static unsigned crc32(const unsigned char *b, unsigned n) {
    unsigned v = ~0u, i;
    while (n--) {
        v ^= *b++;
        for (i = 0; i < 8; i++)
            v = v >> 1 ^ (0xedb88320u & (0u - (v & 1)));
    }
    return ~v;
}
static int read_at(int fd, unsigned at, void *p, unsigned n) {
    unsigned char *b = p;
    int r;
    while (n) {
        r = (int)P_PREAD(fd, b, n, at);
        if (r <= 0)
            return 0;
        b += r;
        at += (unsigned)r;
        n -= (unsigned)r;
    }
    return 1;
}
static void hash_hex(const unsigned char *b, char out[65]) {
    unsigned i;
    for (i = 0; i < 32; i++) {
        out[2 * i] = "0123456789abcdef"[b[i] >> 4];
        out[2 * i + 1] = "0123456789abcdef"[b[i] & 15];
    }
    out[64] = 0;
}
static int bin_open(pa_card *c) {
    unsigned char ready[128], head[128];
    char digest[65];
    struct stat64 st;
    int fd;
    unsigned count, pool, bytes;
    fd = pa_regular_open(c->meta, "indexes/art.ready");
    if (fd < 0)
        return 0;
    int ok = read_at(fd, 0, ready, 128) && P_STAT(fd, &st) == 0 && st.st_size == 128;
    pa_close(fd);
    if (!ok)
        return 0;
    if (!same(ready, "XM5RDY2\0", 8) || le32(ready + 8) != 2 || le32(ready + 12) != 128 ||
        le32(ready + 84) != PA_SIZE_MASK || le32(ready + 88) != 3 || !zeroes(ready + 92, 32) ||
        le32(ready + 124) != crc32(ready, 124))
        return 0;
    bytes = le32(ready + 16);
    if (bytes < 128 || bytes > INDEX_LIMIT)
        return 0;
    c->index = pa_regular_open(c->meta, "indexes/art-index.bin");
    if (c->index < 0)
        return 0;
    if (!read_at(c->index, 0, head, 128) || P_STAT(c->index, &st) || st.st_size != bytes)
        return 0;
    if (!same(head, "XM5ART2\0", 8) || le32(head + 8) != 2 || le32(head + 12) != 128 ||
        le32(head + 16) != 112 || le32(head + 24) != 128 || le32(head + 72) != 3 ||
        le32(head + 76) != PA_SIZE_MASK || !zeroes(head + 112, 16) ||
        !same(head + 40, ready + 52, 16) || !same(head + 56, ready + 68, 16))
        return 0;
    count = le32(head + 20);
    pool = le32(head + 28);
    if (count > 1000000 || pool != 128 + count * 112 || pool > bytes ||
        le32(head + 32) != bytes - pool || le32(head + 36) != bytes)
        return 0;
    if (!digest_fd(c->index, INDEX_LIMIT, digest))
        return 0;
    hash_hex(ready + 20, c->index_hash);
    if (strcmp(digest, c->index_hash))
        return 0;
    c->records = count;
    c->pool_offset = pool;
    c->pool_bytes = bytes - pool;
    c->index_bytes = bytes;
    cp(c->index_identity, &st, sizeof(st));
    return 1;
}
static int index_current(const pa_card *c) {
    struct stat64 before, after;
    cp(&before, c->index_identity, sizeof(before));
    if (P_STAT(c->index, &after))
        return 0;
    if (before.st_dev != after.st_dev || before.st_ino != after.st_ino ||
        before.st_size != after.st_size || before.st_mtime != after.st_mtime ||
        before.st_ctime != after.st_ctime)
        return 0;
#ifdef __mips__
    return before.st_mtime_nsec == after.st_mtime_nsec &&
           before.st_ctime_nsec == after.st_ctime_nsec;
#else
    return before.st_mtim.tv_nsec == after.st_mtim.tv_nsec &&
           before.st_ctim.tv_nsec == after.st_ctim.tv_nsec;
#endif
}
int pa_card_open(pa_card *c, const char *root) {
    struct stat64 st;
    int flag;
    memset(c, 0, sizeof(*c));
    c->root = c->meta = c->index = c->lyrics = c->assets = -1;
    if (copystr(c->root_path, sizeof(c->root_path), root) || !root || *root != '/')
        return PA_IO;
    c->root = (int)P_OPENAT(AT_FDCWD, root, DIRFLAGS, 0);
    if (c->root < 0)
        return PA_IO;
    c->mount_id = fd_mount(c->root);
    if (!c->mount_id || P_STAT(c->root, &st))
        goto fail;
    cp(c->identity, &st, sizeof(st));
    int container = (int)P_OPENAT(c->root, "xm5", DIRFLAGS, 0);
    c->meta = container < 0 ? -1 : (int)P_OPENAT(container, "Struct", DIRFLAGS, 0);
    pa_close(container);
    if (c->meta < 0) {
        pa_card_close(c);
        return PA_UNMANAGED;
    }
    flag = (int)P_OPENAT(c->meta, "INCOMPLETE", O_PATH | O_NOFOLLOW | O_CLOEXEC, 0);
    if (flag >= 0) {
        pa_close(flag);
        goto fail;
    }
    flag = pa_regular_open(c->meta, "tracks.tsv");
    if (flag >= 0) {
        pa_close(flag);
        if (!inline_index_open(c) || !pa_card_current(c))
            goto fail;
        return 0;
    }
    flag = pa_regular_open(c->meta, "indexes/info.bin");
    if (flag >= 0) {
        pa_close(flag);
        if (!info_index_open(c) || !pa_card_current(c))
            goto fail;
        return 0;
    }
    flag = (int)P_OPENAT(c->meta, "indexes/art.ready", O_PATH | O_NOFOLLOW | O_CLOEXEC, 0);
    if (flag < 0) {
        pa_card_close(c);
        return PA_UNMANAGED;
    }
    pa_close(flag);
    if (!bin_open(c) || !pa_card_current(c))
        goto fail;
    return 0;
fail:
    pa_card_close(c);
    return XM5_ART_INVALID;
}
static int order(const unsigned char *r, const unsigned char *k, unsigned side) {
    unsigned i, v;
    for (i = 0; i < 32; i++)
        if (r[i] != k[i])
            return r[i] < k[i] ? -1 : 1;
    v = le32(r + 32);
    return v < side ? -1 : v > side ? 1 : 0;
}
static int record_read(pa_card *c, const unsigned char *key, unsigned side,
                       unsigned char out[112]) {
    unsigned lo = 0, hi = c->records, mid;
    int cmp;
    while (lo < hi) {
        mid = lo + (hi - lo) / 2;
        if (!read_at(c->index, 128 + mid * 112, out, 112))
            return -1;
        cmp = order(out, key, side);
        if (cmp < 0)
            lo = mid + 1;
        else
            hi = mid;
    }
    if (lo == c->records || !read_at(c->index, 128 + lo * 112, out, 112) || order(out, key, side))
        return 0;
    if (lo + 1 < c->records) {
        unsigned char next_record[112];
        if (!read_at(c->index, 128 + (lo + 1) * 112, next_record, 112) ||
            !order(next_record, key, side))
            return -1;
    }
    return 1;
}
static int pool_read(pa_card *c, unsigned off, unsigned len, char *out, unsigned cap) {
    if (off < c->pool_offset || off > c->index_bytes || len >= cap || len > c->index_bytes - off)
        return 0;
    if (!read_at(c->index, off, out, len))
        return 0;
    out[len] = 0;
    return strlen(out) == len && !strchr(out, '\t') && !strchr(out, '\n') && !strchr(out, '\r');
}
int pa_lookup_size(pa_card *c, const char *role, const char *subject, unsigned side,
                   xm5_art_record *out) {
    unsigned id = !strcmp(role, "directory")  ? 1
                  : !strcmp(role, "playback") ? 2
                  : !strcmp(role, "playlist") ? 3
                                              : 0;
    char hexkey[65], text[XM5_ART_PATH];
    unsigned char key[32], record[112], nul = 0;
    sha s;
    unsigned i, n, assetlen, flags;
    int found;
    if (c->info_mode == 2)
        return inline_lookup_size(c, role, subject, side, out);
    if (c->info_mode)
        return info_lookup_size(c, role, subject, side, out);
    if (!id || !subject || !*subject || strlen(subject) >= XM5_ART_PATH || pa_size_slot(side) < 0)
        return XM5_ART_INVALID;
    if (!pa_card_current(c))
        return PA_CHANGED;
    if (!index_current(c))
        return XM5_ART_INVALID;
    sha_init(&s);
    sha_add(&s, role, strlen(role));
    sha_add(&s, &nul, 1);
    sha_add(&s, subject, strlen(subject));
    sha_end(&s, hexkey);
    for (i = 0; i < 32; i++)
        key[i] = (unsigned char)(hex(hexkey[2 * i]) * 16 + hex(hexkey[2 * i + 1]));
    found = record_read(c, key, side, record);
    if (!found)
        found = record_read(c, key, 0, record);
    if (found < 0)
        return XM5_ART_INVALID;
    if (!found)
        return XM5_ART_MISS;
    if (le32(record + 100) != id || !zeroes(record + 104, 8) ||
        !pool_read(c, le32(record + 40), le32(record + 44), text, sizeof(text)) ||
        strcmp(text, subject))
        return XM5_ART_INVALID;
    memset(out, 0, sizeof(*out));
    flags = le32(record + 36);
    n = le32(record + 32);
    assetlen = le32(record + 52);
    if (flags == 1) {
        if (n || le32(record + 48) || assetlen || !zeroes(record + 56, 44))
            return XM5_ART_INVALID;
        out->no_art = 1;
        return XM5_ART_NONE;
    }
    if (flags || n != side || le32(record + 88) != side || le32(record + 92) != side ||
        le32(record + 96) != 64 + side * side * 4 ||
        !pool_read(c, le32(record + 48), assetlen, out->path, sizeof(out->path)) ||
        strncmp(out->path, "xm5/Struct/covers/", 18))
        return XM5_ART_INVALID;
    hash_hex(record + 56, out->sha256);
    out->side = side;
    if (!index_current(c))
        return XM5_ART_INVALID;
    return pa_card_current(c) ? XM5_ART_OK : PA_CHANGED;
}
int pa_lookup(pa_card *c, const char *role, const char *subject, xm5_art_record *out) {
    return pa_lookup_size(c, role, subject, !strcmp(role, "directory") ? 80 : 330, out);
}
