#include "prepared_io.h"
#ifndef __mips__
#include <stdio.h>
#include <fcntl.h>
#include <unistd.h>
#else
#include <asm/unistd.h>
static long raw(long nr, long a, long b, long c) {
    register long v __asm__("$2") = nr, r4 __asm__("$4") = a, r5 __asm__("$5") = b,
                    r6 __asm__("$6") = c, r7 __asm__("$7") = 0;
    __asm__ volatile("syscall" : "+r"(v), "+r"(r7) : "r"(r4), "r"(r5), "r"(r6) : "memory");
    return r7 ? -v : v;
}
#endif
static void say(const char *s) {
#ifndef __mips__
    fputs(s, stdout);
#else
    raw(__NR_write, 1, (long)s, (long)strlen(s));
#endif
}
static unsigned char lyric_buffer[1024 * 1024];
static void number(unsigned n) {
    char b[16];
    unsigned i = 0, j;
    do {
        b[i++] = (char)('0' + n % 10);
        n /= 10;
    } while (n);
    for (j = 0; j < i / 2; j++) {
        char t = b[j];
        b[j] = b[i - j - 1];
        b[i - j - 1] = t;
    }
    b[i] = 0;
    say(b);
}
int cli_main(int argc, char **argv) {
    pa_card c;
    xm5_art_record r;
    char path[256], hash[65];
    int code;
    if (argc == 3 && !strcmp(argv[1], "sha")) {
        pa_sha256(argv[2], strlen(argv[2]), hash);
        say(hash);
        say("\n");
        return 0;
    }
    if (argc == 4 && !strcmp(argv[1], "gain")) {
        float gain;
        unsigned bits;
        code = pa_card_open(&c, argv[2]);
        if (code)
            return code;
        code = pa_info_gain(&c, argv[3], &gain);
        memcpy(&bits, &gain, 4);
        say("gain_bits=");
        number(bits);
        say("\n");
        pa_card_close(&c);
        return code;
    }
    if (argc == 4 && !strcmp(argv[1], "asset")) {
        code = pa_card_open(&c, argv[2]);
        if (code)
            return code;
        code = pa_info_asset(&c, argv[3], &r);
        if (!code) {
            code = pa_info_read(&c, &r, lyric_buffer, r.info_length);
            if (!code) {
                pa_sha256(lyric_buffer, r.info_length, hash);
                say("sha256=");
                say(hash);
                say("\nbytes=");
                number(r.info_length);
                say("\n");
            }
        }
        pa_card_close(&c);
        return code;
    }
    if (argc == 4 && !strcmp(argv[1], "generation")) {
        code = pa_card_open(&c, argv[2]);
        if (code)
            return code;
        char active[1200], old[1200];
        strcpy(active, argv[2]);
        strcpy(active + strlen(active), "/xm5");
        strcpy(old, argv[2]);
        strcpy(old + strlen(old), "/old-xm5");
#ifdef __mips__
        if (raw(__NR_rename, (long)active, (long)old, 0) ||
            raw(__NR_rename, (long)argv[3], (long)active, 0))
            return 99;
#else
        if (rename(active, old) || rename(argv[3], active))
            return 99;
#endif
        code = pa_card_current(&c) ? 98 : 0;
        pa_card_close(&c);
        return code;
    }
    if (argc == 4 && !strcmp(argv[1], "lrc")) {
        code = pa_card_open(&c, argv[2]);
        if (code)
            return code;
        code = pa_lyric_link(&c, argv[3], 0, path, sizeof(path));
        say("lyric=");
        number((unsigned)code);
        say("\n");
        if (!code) {
#ifdef __mips__
            int fd = (int)raw(__NR_open, (long)path, 0, 0);
#else
            int fd = open(path, O_RDONLY);
#endif
            if (fd < 0) {
                pa_card_close(&c);
                return 22;
            }
            unsigned total = 0;
            int n;
            while (total < sizeof(lyric_buffer) &&
                   (n = pa_read(fd, lyric_buffer + total, sizeof(lyric_buffer) - total)) > 0)
                total += (unsigned)n;
            pa_close(fd);
            pa_sha256(lyric_buffer, total, hash);
            say("sha256=");
            say(hash);
            say("\nalias=");
            say(path);
            say("\n");
        }
        pa_card_close(&c);
        return code;
    }
    if (argc != 6 && argc != 7)
        return 90;
    unsigned side = 0;
    for (const char *q = argv[4]; *q; q++) {
        if (*q < '0' || *q > '9')
            return 90;
        side = side * 10 + (unsigned)(*q - '0');
    }
    code = pa_card_open(&c, argv[1]);
    say("open=");
    number((unsigned)code);
    say("\n");
    if (code)
        return code;
    code = pa_lookup_size(&c, argv[2], argv[3], side, &r);
    say("lookup=");
    number((unsigned)code);
    say("\n");
    if (!code) {
        say("asset=");
        say(r.path);
        say("\nsha256=");
        say(r.sha256);
        say("\nside=");
        number(r.side);
        say("\n");
        if (argc == 7 && !strcmp(argv[6], "direct")) {
            code = pa_bind_native(&c, argv[5]);
            if (!code)
                code = pa_check_native(&c, &r, argv[5]);
            if (!code) {
                pa_native_uri(&r, argv[5], path, sizeof(path));
                memcpy(path, path + 7, strlen(path + 7) + 1);
            }
        } else
            code = pa_materialize(&c, &r, argv[5], path, sizeof(path));
        say("materialize=");
        number((unsigned)code);
        say("\n");
        if (!code) {
            say("file=");
            say(path);
            say("\n");
        }
    }
    pa_card_close(&c);
    return code;
}
#ifndef __mips__
int main(int argc, char **argv) {
    return cli_main(argc, argv);
}
#else
void start_c(unsigned *sp) {
    int code = cli_main((int)sp[0], (char **)(sp + 1));
    raw(__NR_exit, code, 0, 0);
    for (;;) {
    }
}
#endif
