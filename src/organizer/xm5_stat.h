#ifndef XM5_ORGANIZER_STAT_H
#define XM5_ORGANIZER_STAT_H

#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/stat.h>

/* Private, stable representation. No libc stat type or token is redefined.
 * In particular newer cross headers may redirect lstat/fstat to time64
 * entry points which the player's original glibc 2.28 does not export. */
typedef struct {
    uint64_t dev, ino;
    int64_t size, mtime, ctime;
    uint32_t mode, mtime_nsec, ctime_nsec;
} xm5_stat;

static inline int xm5_is_regular(uint32_t mode) { return (mode & 0170000u) == 0100000u; }
static inline int xm5_is_directory(uint32_t mode) { return (mode & 0170000u) == 0040000u; }

#ifdef __mips__
#include <asm/sgidefs.h>
#include <asm/unistd.h>
#if _MIPS_SIM != _MIPS_SIM_ABI32
#error "XM5 native stat compatibility requires the MIPS O32 ABI"
#endif

/* Linux MIPS O32 stat64 syscall layout, as published by asm/stat.h.
 * Fixed-width fields avoid both glibc's struct stat ABI and its timestamp
 * field macros. This is the same 104-byte layout used by prepared_io.c. */
typedef struct {
    uint32_t dev, pad0[3];
    uint64_t ino;
    uint32_t mode, nlink, uid, gid, rdev, pad1[3];
    int64_t size;
    int32_t atime;
    uint32_t atime_nsec;
    int32_t mtime;
    uint32_t mtime_nsec;
    int32_t ctime;
    uint32_t ctime_nsec;
    uint32_t blksize, pad2;
    int64_t blocks;
} xm5_kernel_stat64;

_Static_assert(sizeof(xm5_kernel_stat64) == 104, "XM5 O32 stat64 size");
_Static_assert(offsetof(xm5_kernel_stat64, ino) == 16, "XM5 O32 inode offset");
_Static_assert(offsetof(xm5_kernel_stat64, mode) == 24, "XM5 O32 mode offset");
_Static_assert(offsetof(xm5_kernel_stat64, size) == 56, "XM5 O32 size offset");
_Static_assert(offsetof(xm5_kernel_stat64, mtime) == 72, "XM5 O32 mtime offset");
_Static_assert(offsetof(xm5_kernel_stat64, ctime) == 80, "XM5 O32 ctime offset");
_Static_assert(sizeof(void *) == 4 && sizeof(long) == 4, "XM5 O32 word sizes");

static inline int xm5_stat_call(long number, long argument, xm5_stat *out)
{
    xm5_kernel_stat64 st;
    register long result __asm__("$2") = number;
    register long a0 __asm__("$4") = argument;
    register long a1 __asm__("$5") = (long)&st;
    register long a2 __asm__("$6") = 0;
    register long error __asm__("$7") = 0;
    __asm__ volatile("syscall"
        : "+r"(result), "+r"(error)
        : "r"(a0), "r"(a1), "r"(a2)
        : "$1", "$3", "$8", "$9", "$10", "$11", "$12", "$13", "$14",
          "$15", "$24", "$25", "hi", "lo", "memory");
    if (error) {
        /* errno expands to *__errno_location(). That call returns its pointer
         * in $v0, the same fixed register as result. Capture an ordinary C
         * value first so it survives the call used to evaluate errno's LHS. */
        int saved_errno = (int)result;
        errno = saved_errno;
        return -1;
    }
    out->dev = st.dev;
    out->ino = st.ino;
    out->mode = st.mode;
    out->size = st.size;
    out->mtime = st.mtime;
    out->mtime_nsec = st.mtime_nsec;
    out->ctime = st.ctime;
    out->ctime_nsec = st.ctime_nsec;
    return 0;
}

static inline int xm5_lstat(const char *path, xm5_stat *out)
{
    return xm5_stat_call(__NR_lstat64, (long)path, out);
}
static inline int xm5_fstat(int fd, xm5_stat *out)
{
    return xm5_stat_call(__NR_fstat64, fd, out);
}
#else
static inline void xm5_stat_copy(xm5_stat *out, const struct stat *st)
{
    out->dev = st->st_dev;
    out->ino = st->st_ino;
    out->mode = st->st_mode;
    out->size = st->st_size;
    out->mtime = st->st_mtime;
    out->ctime = st->st_ctime;
    out->mtime_nsec = st->st_mtim.tv_nsec;
    out->ctime_nsec = st->st_ctim.tv_nsec;
}
static inline int xm5_lstat(const char *path, xm5_stat *out)
{
    struct stat st;
    if (lstat(path, &st)) return -1;
    xm5_stat_copy(out, &st);
    return 0;
}
static inline int xm5_fstat(int fd, xm5_stat *out)
{
    struct stat st;
    if (fstat(fd, &st)) return -1;
    xm5_stat_copy(out, &st);
    return 0;
}
#endif
#endif
