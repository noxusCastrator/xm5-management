#define _GNU_SOURCE
#include "prepared_io.h"
#ifdef __mips__
#include <asm/unistd.h>
#include <asm/stat.h>
#include <linux/fcntl.h>
#include <linux/stat.h>
#include <asm/errno.h>
_Static_assert(sizeof(struct stat64) == 104, "O32 stat64");
_Static_assert(__builtin_offsetof(struct stat64, st_ino) == 16, "O32 inode");
_Static_assert(__builtin_offsetof(struct stat64, st_size) == 56, "O32 size");
static long call4(long nr, long a, long b, long c, long d) {
    register long v __asm__("$2") = nr, r4 __asm__("$4") = a, r5 __asm__("$5") = b,
                    r6 __asm__("$6") = c, r7 __asm__("$7") = d;
    __asm__ volatile("syscall"
                     : "+r"(v), "+r"(r7)
                     : "r"(r4), "r"(r5), "r"(r6)
                     : "$1", "$3", "$8", "$9", "$10", "$11", "$12", "$13", "$14", "$15", "$24",
                       "$25", "hi", "lo", "memory");
    return r7 ? -v : v;
}
/* O32 pread64 aligns the 64-bit offset at stack arguments 5/6. The leaf
 * assembly preserves that ABI and never changes the shared descriptor cursor. */
extern long pa_pread(int, void *, unsigned, unsigned, unsigned, unsigned);
__asm__(".text\n.set noreorder\n.globl pa_pread\npa_pread:\nli $2,4200\nsyscall\nbeqz "
        "$7,1f\nnop\nsubu $2,$0,$2\n1: jr $31\nnop\n");
#define P_PREAD(fd, p, n, off) pa_pread(fd, p, n, 0, off, 0)
#define P_SYMLINK(a, b) call4(__NR_symlink, (long)a, (long)b, 0, 0)
#define P_READ(fd, p, n) call4(__NR_read, fd, (long)p, n, 0)
#define P_WRITE(fd, p, n) call4(__NR_write, fd, (long)p, n, 0)
#define P_CLOSE(fd) call4(__NR_close, fd, 0, 0, 0)
#define P_OPENAT(fd, p, f, m) call4(__NR_openat, fd, (long)p, f, m)
#define P_STAT(fd, p) call4(__NR_fstat64, fd, (long)p, 0, 0)
#define P_SEEK(fd, n) call4(__NR_lseek, fd, n, 0, 0)
#define P_MKDIR(p) call4(__NR_mkdir, (long)p, 0700, 0, 0)
#define P_UNLINK(p) call4(__NR_unlink, (long)p, 0, 0, 0)
#define P_UNLINKAT(fd, p) call4(__NR_unlinkat, fd, (long)p, 0, 0)
#define P_RENAME(a, b) call4(__NR_rename, (long)a, (long)b, 0, 0)
#define P_GETPID() call4(__NR_getpid, 0, 0, 0, 0)
#define P_FSYNC(fd) call4(__NR_fsync, fd, 0, 0, 0)
#define P_MKDIRAT(fd, p) call4(__NR_mkdirat, fd, (long)p, 0700, 0)
#define P_RENAMEAT(fd, a, b) call4(__NR_renameat, fd, (long)a, fd, (long)b)
#else
#include <sys/stat.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>
#define P_READ read
#define P_PREAD(fd, p, n, off) pread64(fd, p, n, off)
#define P_SYMLINK symlink
#define P_WRITE write
#define P_CLOSE close
#define P_OPENAT openat
#define P_STAT fstat64
#define P_SEEK(fd, n) lseek(fd, n, SEEK_SET)
#define P_MKDIR(p) mkdir(p, 0700)
#define P_UNLINK unlink
#define P_UNLINKAT(fd, p) unlinkat(fd, p, 0)
#define P_RENAME rename
#define P_GETPID getpid
#define P_FSYNC fsync
#define P_MKDIRAT(fd, p) mkdirat(fd, p, 0700)
#define P_RENAMEAT(fd, a, b) renameat(fd, a, fd, b)
#include <stdio.h>
#endif
#ifndef O_LARGEFILE
#define O_LARGEFILE 0
#endif
#define DIRFLAGS (O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
#define READFLAGS (O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_NOCTTY | O_CLOEXEC | O_LARGEFILE)
#define MANIFEST_LIMIT (128u * 1024u * 1024u)
#define INDEX_LIMIT (256u * 1024u * 1024u)
#define ASSET_LIMIT (512u * 1024u)
typedef unsigned u32;
typedef unsigned char u8;
static void cp(void *d, const void *s, size_t n) {
    u8 *a = d;
    const u8 *b = s;
    while (n--)
        *a++ = *b++;
}
static int copystr(char *d, size_t cap, const char *s) {
    size_t n = 0;
    if (!s || !cap)
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
static int hash_ok(const char *s) {
    unsigned i;
    for (i = 0; i < 64; i++)
        if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f')))
            return 0;
    return s[64] == 0;
}
static const u32 K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
typedef struct {
    u32 h[8], bytes;
    unsigned used;
    u8 block[64];
} sha;
static u32 rr(u32 n, unsigned s) {
    return (n >> s) | (n << (32 - s));
}
static void transform(sha *s) {
    u32 w[64], a, b, c, d, e, f, g, h, x, y;
    unsigned i;
    for (i = 0; i < 16; i++)
        w[i] = (u32)s->block[4 * i] << 24 | (u32)s->block[4 * i + 1] << 16 |
               (u32)s->block[4 * i + 2] << 8 | s->block[4 * i + 3];
    for (; i < 64; i++) {
        x = w[i - 15];
        y = w[i - 2];
        w[i] = w[i - 16] + (rr(x, 7) ^ rr(x, 18) ^ (x >> 3)) + w[i - 7] +
               (rr(y, 17) ^ rr(y, 19) ^ (y >> 10));
    }
    a = s->h[0];
    b = s->h[1];
    c = s->h[2];
    d = s->h[3];
    e = s->h[4];
    f = s->h[5];
    g = s->h[6];
    h = s->h[7];
    for (i = 0; i < 64; i++) {
        x = h + (rr(e, 6) ^ rr(e, 11) ^ rr(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
        y = (rr(a, 2) ^ rr(a, 13) ^ rr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        h = g;
        g = f;
        f = e;
        e = d + x;
        d = c;
        c = b;
        b = a;
        a = x + y;
    }
    s->h[0] += a;
    s->h[1] += b;
    s->h[2] += c;
    s->h[3] += d;
    s->h[4] += e;
    s->h[5] += f;
    s->h[6] += g;
    s->h[7] += h;
}
static void sha_init(sha *s) {
    static const u32 h[] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                            0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    cp(s->h, h, sizeof(h));
    s->bytes = s->used = 0;
}
static void sha_add(sha *s, const void *p, size_t n) {
    const u8 *b = p;
    s->bytes += (u32)n;
    while (n--) {
        s->block[s->used++] = *b++;
        if (s->used == 64) {
            transform(s);
            s->used = 0;
        }
    }
}
static void sha_end(sha *s, char out[65]) {
    unsigned i, n = s->bytes;
    u8 pad = 0x80, zero = 0, bits[8];
    sha_add(s, &pad, 1);
    while (s->used != 56)
        sha_add(s, &zero, 1); /* explicit 64-bit bit length for inputs <512MiB */
    bits[0] = bits[1] = bits[2] = 0;
    bits[3] = (u8)(n >> 29);
    bits[4] = (u8)(n >> 21);
    bits[5] = (u8)(n >> 13);
    bits[6] = (u8)(n >> 5);
    bits[7] = (u8)(n << 3);
    sha_add(s, bits, 8);
    for (i = 0; i < 32; i++) {
        unsigned v = (s->h[i / 4] >> (24 - 8 * (i % 4))) & 255;
        out[2 * i] = "0123456789abcdef"[v >> 4];
        out[2 * i + 1] = "0123456789abcdef"[v & 15];
    }
    out[64] = 0;
}
void pa_sha256(const void *p, size_t n, char out[65]) {
    sha s;
    sha_init(&s);
    sha_add(&s, p, n);
    sha_end(&s, out);
}
unsigned pa_thread_id(void) {
#ifdef __mips__
    return (unsigned)call4(__NR_gettid, 0, 0, 0, 0);
#else
    return (unsigned)getpid();
#endif
}
int pa_close(int fd) {
    return fd < 0 ? 0 : (int)P_CLOSE(fd);
}
int pa_read(int fd, void *p, unsigned n) {
    int r;
    do {
        r = (int)P_READ(fd, p, n);
    } while (r < 0 &&
#ifdef __mips__
             r == -EINTR
#else
             errno == EINTR
#endif
    );
    return r;
}
static int isreg(int fd) {
    struct stat64 s;
    return P_STAT(fd, &s) == 0 && (s.st_mode & S_IFMT) == S_IFREG;
}
/* O_NOFOLLOW on EVERY component, not merely the final filename. */
int pa_regular_open(int root, const char *path) {
    char part[256];
    int dir = root, owned = 0, fd = -1;
    unsigned n;
    if (!path || !*path || *path == '/' || *path == '\\')
        return -1;
    while (*path) {
        n = 0;
        while (*path && *path != '/') {
            unsigned char c = *path++;
            if (c < 32 || c == 127 || c == '\\' || c == ':' || n == 255)
                goto done;
            part[n++] = (char)c;
        }
        part[n] = 0;
        if (!n || !strcmp(part, ".") || !strcmp(part, ".."))
            goto done;
        if (*path == '/') {
            path++;
            if (!*path)
                goto done;
            fd = (int)P_OPENAT(dir, part, DIRFLAGS, 0);
            if (fd < 0)
                goto done;
            if (owned)
                pa_close(dir);
            dir = fd;
            owned = 1;
            fd = -1;
        } else {
            fd = (int)P_OPENAT(dir, part, READFLAGS, 0);
            if (fd >= 0 && !isreg(fd)) {
                pa_close(fd);
                fd = -1;
            }
            break;
        }
    }
done:
    if (owned)
        pa_close(dir);
    return fd;
}
static unsigned decimal(char *out, unsigned n) {
    char t[12];
    unsigned len = 0, i;
    do {
        t[len++] = (char)('0' + n % 10);
        n /= 10;
    } while (n);
    for (i = 0; i < len; i++)
        out[i] = t[len - i - 1];
    out[len] = 0;
    return len;
}
static unsigned fd_mount(int fd) {
    char name[64] = "/proc/self/fdinfo/", buf[1024];
    unsigned i, v = 0;
    int f, n;
    decimal(name + 18, (unsigned)fd); /* prefix length checked below by unit test */
    f = (int)P_OPENAT(AT_FDCWD, name, READFLAGS, 0);
    if (f < 0)
        return 0;
    n = pa_read(f, buf, sizeof(buf) - 1);
    pa_close(f);
    if (n <= 0)
        return 0;
    buf[n] = 0;
    for (i = 0; i + 8 < (unsigned)n; i++)
        if ((i == 0 || buf[i - 1] == '\n') && !strncmp(buf + i, "mnt_id:", 7)) {
            i += 7;
            while (buf[i] == ' ' || buf[i] == '\t')
                i++;
            while (buf[i] >= '0' && buf[i] <= '9') {
                if (v > 400000000u)
                    return 0;
                v = v * 10 + (unsigned)(buf[i++] - '0');
            }
            return v;
        }
    return 0;
}
static int same_identity(const struct stat64 *a, const struct stat64 *b) {
    if (a->st_dev != b->st_dev || a->st_ino != b->st_ino || a->st_size != b->st_size ||
        a->st_mtime != b->st_mtime || a->st_ctime != b->st_ctime)
        return 0;
#ifdef __mips__
    return a->st_mtime_nsec == b->st_mtime_nsec && a->st_ctime_nsec == b->st_ctime_nsec;
#else
    return a->st_mtim.tv_nsec == b->st_mtim.tv_nsec && a->st_ctim.tv_nsec == b->st_ctim.tv_nsec;
#endif
}
int pa_card_current(const pa_card *c) {
    struct stat64 old, now;
    int fd, ok;
    if (c->root < 0 || !c->mount_id)
        return 0;
    fd = (int)P_OPENAT(AT_FDCWD, c->root_path, DIRFLAGS, 0);
    if (fd < 0)
        return 0;
    cp(&old, c->identity, sizeof(old));
    ok = P_STAT(fd, &now) == 0 && old.st_dev == now.st_dev && old.st_ino == now.st_ino &&
         fd_mount(fd) == c->mount_id;
    if (ok && c->meta >= 0) {
        int container = (int)P_OPENAT(fd, "xm5", DIRFLAGS, 0),
            meta = container < 0 ? -1 : (int)P_OPENAT(container, "Struct", DIRFLAGS, 0);
        struct stat64 pinned;
        ok = meta >= 0 && !P_STAT(c->meta, &pinned) && !P_STAT(meta, &now) &&
             pinned.st_dev == now.st_dev && pinned.st_ino == now.st_ino;
        if (ok) {
            int flag = (int)P_OPENAT(meta, "INCOMPLETE", O_PATH | O_NOFOLLOW | O_CLOEXEC, 0);
            if (flag >= 0) {
                ok = 0;
                pa_close(flag);
            }
        }
        pa_close(meta);
        pa_close(container);
    }
    /* Same-card replacement of the owned container is also a new generation. */
    if (ok && c->index >= 0) {
        int current = pa_regular_open(fd, c->info_mode == 2 ? "xm5/Struct/tracks.tsv"
                                          : c->info_mode    ? "xm5/Struct/indexes/info.bin"
                                                            : "xm5/Struct/indexes/art-index.bin");
        cp(&old, c->index_identity, sizeof(old));
        ok = current >= 0 && !P_STAT(current, &now) && same_identity(&old, &now);
        pa_close(current);
    }
    if (ok && c->assets >= 0) {
        int current = pa_regular_open(fd, "xm5/Struct/indexes/assets.bin");
        cp(&old, c->assets_identity, sizeof(old));
        ok = current >= 0 && !P_STAT(current, &now) && same_identity(&old, &now);
        pa_close(current);
    }
    pa_close(fd);
    return ok;
}
static void inline_reset(void);
void pa_card_close(pa_card *c) {
    pa_close(c->assets);
    pa_close(c->lyrics);
    pa_close(c->index);
    pa_close(c->meta);
    pa_close(c->root);
    c->root = c->meta = c->index = c->lyrics = c->assets = -1;
    pa_state_reset();
    inline_reset();
}
int pa_card_present(const char *path) {
    int root = (int)P_OPENAT(AT_FDCWD, path, DIRFLAGS, 0), meta, fd, music, result = 0;
    if (root < 0)
        return 0;
    int container = (int)P_OPENAT(root, "xm5", DIRFLAGS, 0);
    meta = container < 0 ? -1 : (int)P_OPENAT(container, "Struct", DIRFLAGS, 0);
    if (meta >= 0) {
        music = (int)P_OPENAT(container, "music", DIRFLAGS, 0);
        result = music >= 0;
        pa_close(music); /* Namespace ownership survives missing/rebuilding indexes. */
        if (!result) {
            fd = pa_regular_open(meta, "tracks.tsv");
            if (fd < 0)
                fd = pa_regular_open(meta, "indexes/info.bin");
            if (fd < 0)
                fd = pa_regular_open(meta, "indexes/art.ready");
            result = fd >= 0;
            pa_close(fd);
        }
        pa_close(meta);
    }
    pa_close(container);
    pa_close(root);
    return result;
}
static int hex(int c) {
    return c >= '0' && c <= '9'   ? c - '0'
           : c >= 'a' && c <= 'f' ? c - 'a' + 10
           : c >= 'A' && c <= 'F' ? c - 'A' + 10
                                  : -1;
}
static int digest_fd(int fd, unsigned cap, char out[65]) {
    sha s;
    u8 b[1024];
    int n;
    unsigned total = 0;
    sha_init(&s);
    if (P_SEEK(fd, 0) < 0)
        return 0;
    while ((n = pa_read(fd, b, sizeof(b))) > 0) {
        if ((unsigned)n > cap - total)
            return 0;
        total += (unsigned)n;
        sha_add(&s, b, (unsigned)n);
    }
    if (n < 0)
        return 0;
    sha_end(&s, out);
    return 1;
}
static int info_index_open(pa_card *);
static int assets_open(pa_card *);
static int info_lookup_size(pa_card *, const char *, const char *, unsigned, xm5_art_record *);
static int info_materialize(pa_card *, const xm5_art_record *, const char *, char *, size_t);
static int info_lyric(pa_card *, const char *, unsigned, char *, unsigned);
static int inline_index_open(pa_card *);
static int inline_lookup_size(pa_card *, const char *, const char *, unsigned, xm5_art_record *);
static int inline_asset(pa_card *, const char *, xm5_art_record *);
static int inline_matches(const char *, const void *, unsigned);
static int inline_read(pa_card *, const xm5_art_record *, void *, unsigned);
static int inline_check(pa_card *, const xm5_art_record *);
static int inline_gain(pa_card *, const char *, float *);
static int inline_lyric(pa_card *, const char *, unsigned, char *, unsigned);
#include "binary_reader.inc"
/* Fixed PC-generated top-down opaque BGRA32 profile. */
static int bmp(int fd, unsigned side) {
    unsigned char h[64];
    unsigned total = 64 + side * side * 4, i;
    struct stat64 st;
    if (!read_at(fd, 0, h, 64) || P_STAT(fd, &st) || st.st_size != total)
        return 0;
    if (h[0] != 'B' || h[1] != 'M' || le32(h + 2) != total || le32(h + 6) || le32(h + 10) != 64 ||
        le32(h + 14) != 40 || le32(h + 18) != side || le32(h + 22) != 0u - side ||
        le32(h + 26) != 0x00200001 || le32(h + 30) || le32(h + 34) != side * side * 4 ||
        !zeroes(h + 46, 18))
        return 0;
    unsigned char block[1024];
    int n;
    unsigned offset = 64;
    if (P_SEEK(fd, 64) < 0)
        return 0;
    while ((n = pa_read(fd, block, sizeof(block))) > 0) {
        for (i = 0; i < (unsigned)n; i++)
            if (((offset + i - 64) & 3) == 3 && block[i] != 255)
                return 0;
        offset += (unsigned)n;
    }
    return n == 0 && offset == total;
}
static void name64(char *out, const char *hash) {
    static const char a[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    unsigned i, n = 0, acc = 0, bits = 0;
    for (i = 0; i < 64; i += 2) {
        acc = (acc << 8) | (unsigned)(hex(hash[i]) * 16 + hex(hash[i + 1]));
        bits += 8;
        while (bits >= 6) {
            bits -= 6;
            out[n++] = a[(acc >> bits) & 63];
        }
    }
    if (bits)
        out[n++] = a[(acc << (6 - bits)) & 63];
    out[n] = 0;
}
static int writeall(int fd, const void *p, unsigned n) {
    const u8 *b = p;
    int r;
    while (n) {
        r = (int)P_WRITE(fd, b, n);
        if (r <= 0)
            return 0;
        b += r;
        n -= (unsigned)r;
    }
    return 1;
}
void pa_discard_ram(const char *uri) {
    unsigned i;
    int dir;
    if (!uri || strncmp(uri, "file:///tmp/xa/", 15) || strlen(uri) != 62 ||
        strcmp(uri + 58, ".bmp"))
        return;
    for (i = 15; i < 58; i++)
        if (!((uri[i] >= 'A' && uri[i] <= 'Z') || (uri[i] >= 'a' && uri[i] <= 'z') ||
              (uri[i] >= '0' && uri[i] <= '9') || uri[i] == '-' || uri[i] == '_'))
            return;
    dir = (int)P_OPENAT(AT_FDCWD, "/tmp/xa", DIRFLAGS, 0);
    if (dir < 0)
        return;
    (void)P_UNLINKAT(dir, uri + 15);
    pa_close(dir);
}
int pa_materialize(pa_card *c, const xm5_art_record *r, const char *ram, char *out, size_t cap) {
    char hash[65], name[48], target[256], tmp[256], pid[16];
    u8 buf[1024];
    int source = -1, dest = -1, dir = -1, n, result = XM5_ART_INVALID;
    unsigned len;
    if (r->encoding == 1)
        return pa_card_current(c) ? pa_native_uri(r, ram, out, cap) : PA_CHANGED;
    if (r->info_length)
        return info_materialize(c, r, ram, out, cap);
    if (r->no_art)
        return XM5_ART_NONE;
    if (!hash_ok(r->sha256) || pa_size_slot(r->side) < 0 ||
        strncmp(r->path, "xm5/Struct/covers/", 18))
        return XM5_ART_INVALID;
    if (!pa_card_current(c))
        return PA_CHANGED;
    source = pa_regular_open(c->root, r->path);
    if (source < 0)
        return PA_IO;
    if (!digest_fd(source, ASSET_LIMIT, hash) || strcmp(hash, r->sha256) || !bmp(source, r->side))
        goto done;
    if (!ram || ram[0] != '/' || strlen(ram) > 128)
        goto done;
    name64(name, r->sha256);
    copystr(target, sizeof(target), ram);
    len = (unsigned)strlen(target);
    target[len++] = '/';
    copystr(target + len, sizeof(target) - len, name);
    len = (unsigned)strlen(target);
    copystr(target + len, sizeof(target) - len, ".bmp");
    if (strlen(target) + 1 > cap)
        goto done;
    (void)P_MKDIR(ram);
    dir = (int)P_OPENAT(AT_FDCWD, ram, DIRFLAGS, 0);
    if (dir < 0)
        goto done; /* Existing immutable content is still checked, never trusted by basename. */
    copystr(name + 43, sizeof(name) - 43, ".bmp");
    dest = pa_regular_open(dir, name);
    if (dest >= 0) {
        int ok =
            digest_fd(dest, ASSET_LIMIT, hash) && !strcmp(hash, r->sha256) && bmp(dest, r->side);
        pa_close(dest);
        dest = -1;
        if (ok) {
            result = pa_card_current(c) ? XM5_ART_OK : PA_CHANGED;
            goto done;
        }
    }
    copystr(tmp, sizeof(tmp), target);
    decimal(pid, (unsigned)P_GETPID());
    len = (unsigned)strlen(tmp);
    copystr(tmp + len, sizeof(tmp) - len, ".");
    len++;
    copystr(tmp + len, sizeof(tmp) - len, pid);
    len = (unsigned)strlen(tmp);
    copystr(tmp + len, sizeof(tmp) - len, ".part");
    dest = (int)P_OPENAT(AT_FDCWD, tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (dest < 0)
        goto done;
    if (P_SEEK(source, 0) < 0)
        goto cleanup;
    while ((n = pa_read(source, buf, sizeof(buf))) > 0)
        if (!writeall(dest, buf, (unsigned)n))
            goto cleanup;
    pa_close(dest);
    dest = -1;
    if (n < 0 || !pa_card_current(c))
        goto cleanup;
    dest = (int)P_OPENAT(AT_FDCWD, tmp, READFLAGS, 0);
    if (dest < 0 || !digest_fd(dest, ASSET_LIMIT, hash) || strcmp(hash, r->sha256) ||
        !bmp(dest, r->side))
        goto cleanup;
    pa_close(dest);
    dest = -1;
    if (P_RENAME(tmp, target) < 0)
        goto cleanup;
    result = pa_card_current(c) ? XM5_ART_OK : PA_CHANGED;
    goto done;
cleanup:
    pa_close(dest);
    dest = -1;
    (void)P_UNLINK(tmp);
done:
    pa_close(dest);
    pa_close(dir);
    pa_close(source);
    if (result == XM5_ART_OK)
        copystr(out, cap, target);
    return result;
}

/* A single volatile directory alias replaces per-image RAM copies. Its target is
 * the pinned card root fd, not a freely retargetable /mnt/mmc pathname. */
int pa_bind_native(pa_card *c, const char *ram) {
    int a = -1, b = -1, d = -1, existing = -1, code = XM5_ART_INVALID;
    char target[160], tmp[160], fd[16], pid[16];
    struct stat64 st;
    if (c->info_mode == 2)
        return pa_card_current(c) ? 0 : PA_CHANGED;
    if (c->info_mode)
        return pa_card_current(c) && assets_open(c)
                   ? 0
                   : PA_CHANGED; /* Validate once in the worker; virtual content URIs use no RAM
                                    image files. */
    if (!pa_card_current(c) || !ram || ram[0] != '/' || strlen(ram) > 100)
        return PA_CHANGED;
    a = (int)P_OPENAT(c->meta, "covers", DIRFLAGS, 0);
    if (a < 0)
        goto done;
    b = (int)P_OPENAT(a, "native", DIRFLAGS, 0);
    if (b < 0)
        goto done;
    existing = (int)P_OPENAT(AT_FDCWD, ram, O_PATH | O_NOFOLLOW | O_CLOEXEC, 0);
    if (existing >= 0 && (P_STAT(existing, &st) || (st.st_mode & S_IFMT) != S_IFLNK))
        goto done;
    decimal(fd, (unsigned)c->root);
    decimal(pid, (unsigned)P_GETPID());
    copystr(target, sizeof(target), "/proc/self/fd/");
    copystr(target + strlen(target), sizeof(target) - strlen(target), fd);
    copystr(target + strlen(target), sizeof(target) - strlen(target), "/xm5/Struct/covers/native");
    copystr(tmp, sizeof(tmp), ram);
    copystr(tmp + strlen(tmp), sizeof(tmp) - strlen(tmp), ".");
    copystr(tmp + strlen(tmp), sizeof(tmp) - strlen(tmp), pid);
    copystr(tmp + strlen(tmp), sizeof(tmp) - strlen(tmp), ".link");
    /* No mkdir through this alias and no write/truncate/rename inside its target. */
    if (P_SYMLINK(target, tmp) < 0)
        goto done;
    if (!pa_card_current(c) || P_RENAME(tmp, ram) < 0) {
        P_UNLINK(tmp);
        goto done;
    }
    code = 0;
done:
    pa_close(d);
    pa_close(existing);
    pa_close(b);
    pa_close(a);
    return code;
}
int pa_native_uri(const xm5_art_record *r, const char *ram, char *out, size_t cap) {
    char shortname[48], expected[100];
    unsigned len;
    if (!r || r->no_art || !hash_ok(r->sha256) || pa_size_slot(r->side) < 0 || !ram)
        return XM5_ART_INVALID;
    name64(shortname, r->sha256);
    copystr(shortname + 43, sizeof(shortname) - 43, ".bmp");
    copystr(expected, sizeof(expected), "xm5/Struct/covers/native/");
    copystr(expected + strlen(expected), sizeof(expected) - strlen(expected), shortname);
    if ((!r->info_length && strcmp(expected, r->path)) || strlen(ram) + strlen(shortname) + 9 > cap)
        return XM5_ART_INVALID;
    copystr(out, cap, "file://");
    copystr(out + 7, cap - 7, ram);
    len = (unsigned)strlen(out);
    out[len++] = '/';
    copystr(out + len, cap - len, shortname);
    return 0;
}
int pa_asset_matches(const char *name, const void *data, unsigned bytes) {
    char digest[65], shortname[48];
    const char *last = name, *q;
    unsigned i;
    if (!name || !data || bytes > ASSET_LIMIT)
        return 0;
    if (inline_matches(name, data, bytes))
        return 1;
    for (q = name; *q; q++)
        if (*q == '/')
            last = q + 1;
    if (strlen(last) != 47 || strcmp(last + 43, ".bmp"))
        return 0;
    pa_sha256(data, bytes, digest);
    name64(shortname, digest);
    for (i = 0; i < 43; i++)
        if (last[i] != shortname[i])
            return 0;
    return 1;
}
/* Fixture-only verification of the exact same source alias the stock reader
 * opens. No card writes and no full-image allocation. */
int pa_check_native(pa_card *c, const xm5_art_record *r, const char *ram) {
    char uri[256], hash[65];
    int fd, ok;
    if (pa_native_uri(r, ram, uri, sizeof(uri)))
        return XM5_ART_INVALID;
    if (r->encoding == 1)
        return inline_check(c, r);
    fd = (int)P_OPENAT(AT_FDCWD, uri + 7, READFLAGS, 0);
    if (fd < 0)
        return PA_IO;
    ok = digest_fd(fd, ASSET_LIMIT, hash) && !strcmp(hash, r->sha256) && bmp(fd, r->side);
    pa_close(fd);
    return ok && pa_card_current(c) ? 0 : XM5_ART_INVALID;
}

#include "lyric_reader.inc"
#include "info_reader.inc"
#include "inline_reader.inc"
#include "state_io.inc"
#include "favorite_io.inc"

void pa_remove_playlist_stage(const char *p) {
    if (p && !strncmp(p, "/tmp/xm5-playlist-", 18) && !strchr(p + 18, '/'))
        (void)P_UNLINK(p);
}

#include "inline_state_io.inc"
#include "inline_favorite_io.inc"

int pa_medium_identity(const pa_card *c, char out[65]) {
    struct stat64 st;
    sha key;
    if (!c || c->root < 0 || c->meta < 0 || !c->mount_id || P_STAT(c->meta, &st))
        return 0;
    sha_init(&key);
    sha_add(&key, &c->mount_id, sizeof(c->mount_id));
    struct stat64 root;
    cp(&root, c->identity, sizeof(root));
    sha_add(&key, &root.st_dev, sizeof(root.st_dev));
    sha_add(&key, &root.st_ino, sizeof(root.st_ino));
    sha_add(&key, &st.st_dev, sizeof(st.st_dev));
    sha_add(&key, &st.st_ino, sizeof(st.st_ino));
    sha_end(&key, out);
    return 1;
}
