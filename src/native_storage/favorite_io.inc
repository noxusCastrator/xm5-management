/* PC favorites are applied only by the explicit native import action. Source
 * files stay untouched. A savepoint preserves stock outer-transaction timing. */
typedef struct {
    pa_card *c;
    void *db;
    pa_sql_exec exec;
    int insert;
    unsigned count;
} fav_context;
static int favorite_found(void *context, int n, char **v, char **names) {
    (void)names;
    if (n == 1 && v[0] && v[0][0] == '1')
        *(int *)context = 1;
    return 0;
}
static int favorite_line(fav_context *f, const char *line) {
    char sql[9000];
    unsigned n = 0, i;
    int found = 0, fd;
    if (!line[0] || line[0] == '#')
        return 1;
    if (strncmp(line, "/mnt/mmc/xm5/music/", 19) || !state_text(line, 4096) || ++f->count > 100000)
        return 0;
    fd = pa_regular_open(f->c->root, line + 9);
    if (fd < 0)
        return 0;
    pa_close(fd);
    const char *prefix = f->insert ? "INSERT INTO myfav SELECT "
                                     "NULL,filename,fileurl,album,songer,style,name,composer,"
                                     "albumsonger,year,track,cd_num,time,time_end,time_create,"
                                     "Initial,bitdepth,samprate FROM songtable WHERE fileurl='"
                                   : "SELECT 1 FROM songtable WHERE fileurl='";
    copystr(sql, sizeof(sql), prefix);
    n = (unsigned)strlen(sql);
    for (i = 0; line[i]; i++) {
        if (n + 32 >= sizeof(sql))
            return 0;
        sql[n++] = line[i];
        if (line[i] == '\'')
            sql[n++] = '\'';
    }
    copystr(sql + n, sizeof(sql) - n, "' LIMIT 1");
    if (f->insert)
        return f->exec(f->db, sql, 0, 0, 0) == 0;
    return f->exec(f->db, sql, favorite_found, &found, 0) == 0 && found;
}
static int favorite_lines(int fd, fav_context *f) {
    char line[4097];
    u8 chunk[4096];
    unsigned used = 0, total = 0, i;
    int n;
    f->count = 0;
    if (P_SEEK(fd, 0) < 0)
        return 0;
    while ((n = pa_read(fd, chunk, sizeof(chunk))) > 0) {
        if ((unsigned)n > STATE_LIMIT - total)
            return 0;
        for (i = 0; i < (unsigned)n; i++, total++) {
            unsigned char ch = chunk[i];
            if (total < 3) {
                static const u8 bom[] = {0xef, 0xbb, 0xbf};
                if (ch != bom[total])
                    return 0;
                continue;
            }
            if (ch == '\n') {
                if (used && line[used - 1] == '\r')
                    used--;
                line[used] = 0;
                if (!favorite_line(f, line))
                    return 0;
                used = 0;
            } else {
                if (!ch || used == 4096)
                    return 0;
                line[used++] = (char)ch;
            }
        }
    }
    if (n < 0 || total < 3)
        return 0;
    if (used) {
        if (line[used - 1] == '\r')
            used--;
        line[used] = 0;
        if (!favorite_line(f, line))
            return 0;
    }
    return 1;
}
int pa_favorites_apply(pa_card *c, void *db, pa_sql_exec exec) {
    if (c->info_mode == 2)
        return inline_favorites_apply(c, db, exec);
    int fd = -1, d = -1, success = 0, begun = 0;
    struct stat64 before, after;
    fav_context f = {c, db, exec, 0, 0};
    char oldhash[65], newhash[65];
    if (!db || !exec || !pa_card_current(c) || !c->info_mode)
        return PA_UNMANAGED;
    fd = pa_regular_open(c->meta, "favorites.m3u8");
    if (fd < 0)
        return PA_UNMANAGED;
    if (P_STAT(fd, &before) || !digest_fd(fd, STATE_LIMIT, oldhash) || !favorite_lines(fd, &f) ||
        !pa_card_current(c))
        goto done;
    if (exec(db, "SAVEPOINT xm5_managed_favorites", 0, 0, 0))
        goto done;
    begun = 1;
    if (exec(db, "DELETE FROM myfav WHERE substr(fileurl,1,19)='/mnt/mmc/xm5/music/'", 0, 0, 0))
        goto done;
    f.insert = 1;
    if (!favorite_lines(fd, &f) || P_STAT(fd, &after) || !same_identity(&before, &after) ||
        !digest_fd(fd, STATE_LIMIT, newhash) || strcmp(oldhash, newhash) || !pa_card_current(c))
        goto done;
    if (exec(db, "RELEASE xm5_managed_favorites", 0, 0, 0))
        goto done;
    begun = 0;
    success = 1;
done:
    if (begun) {
        (void)exec(db, "ROLLBACK TO xm5_managed_favorites", 0, 0, 0);
        (void)exec(db, "RELEASE xm5_managed_favorites", 0, 0, 0);
    }
    pa_close(fd);
    d = state_dir(c);
    if (d >= 0) {
        char status[320];
        u8 build[16];
        unsigned i, n = 0;
        const char *prefix = "{\"schema\":\"xm5.native-import\",\"version\":1,\"build_id\":\"";
        copystr(status, sizeof(status), prefix);
        n = (unsigned)strlen(status);
        if (build_id(c, build)) {
            for (i = 0; i < 16; i++) {
                status[n++] = "0123456789abcdef"[build[i] >> 4];
                status[n++] = "0123456789abcdef"[build[i] & 15];
            }
        }
        copystr(
            status + n, sizeof(status) - n,
            success
                ? "\",\"favorites\":\"applied\"}\n"
                : "\",\"favorites\":\"failed\",\"action\":\"scan_library_then_import_again\"}\n");
        (void)atomic_file(c, d, ".import.part", "import.status", status, (unsigned)strlen(status));
        pa_close(d);
    }
    return success ? 0 : PA_IO;
}
