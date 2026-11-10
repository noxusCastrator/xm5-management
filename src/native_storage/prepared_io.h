#ifndef XM5_PREPARED_IO_H
#define XM5_PREPARED_IO_H
/* Descriptor-based prepared-media reads and atomic playlist/favorite exchange.
 * No raw device, mount, shell or image-resampling commands are implemented. */
#include "art_index.h"
#include "art_profile.h"
#ifdef XM5_FREESTANDING
void *memcpy(void *, const void *, size_t);
#endif
enum { PA_UNMANAGED = 20, PA_CHANGED = 21, PA_IO = 22, PA_LIMIT = 23 };
typedef struct {
    int root, meta, index, lyrics, assets;
    unsigned info_mode;
    unsigned mount_id;
    unsigned char identity[192];
    unsigned char index_identity[192];
    char root_path[XM5_ART_PATH];
    char index_hash[65];
    unsigned records, pool_offset, pool_bytes, index_bytes;
    unsigned lrc_records, lrc_pool, lrc_bytes;
    unsigned char lyrics_identity[192];
    unsigned asset_records;
    unsigned char assets_identity[192];
} pa_card;
int pa_card_open(pa_card *, const char *);
int pa_card_present(const char *);
void pa_card_close(pa_card *);
int pa_card_current(const pa_card *);
int pa_lookup(pa_card *, const char *, const char *, xm5_art_record *);
int pa_lookup_size(pa_card *, const char *, const char *, unsigned, xm5_art_record *);
/* Legacy fixed BMPs use immutable RAM files. Inline JPEGs use verified bounded
 * slices of their original song container, without staging separate images. */
int pa_materialize(pa_card *, const xm5_art_record *, const char *, char *, size_t);
void pa_discard_ram(const char *);
int pa_bind_native(pa_card *, const char *);
int pa_native_uri(const xm5_art_record *, const char *, char *, size_t);
int pa_asset_matches(const char *, const void *, unsigned);
int pa_check_native(pa_card *, const xm5_art_record *, const char *);
int pa_info_gain(pa_card *, const char *, float *);
int pa_info_asset(pa_card *, const char *, xm5_art_record *);
int pa_info_read(pa_card *, const xm5_art_record *, void *, unsigned);
int pa_lyric_link(pa_card *, const char *, unsigned, char *, unsigned);
void pa_sha256(const void *, size_t, char[65]);
int pa_regular_open(int, const char *);
int pa_read(int, void *, unsigned);
int pa_close(int);
typedef int (*pa_sql_exec)(void *, const char *, int (*)(void *, int, char **, char **), void *,
                           char **);
int pa_state_export(pa_card *, void *, pa_sql_exec);
int pa_state_pending(pa_card *);
int pa_playlist_subject(pa_card *, unsigned, const char *, char *, unsigned);
void pa_playlist_created(pa_card *, unsigned, const char *);
void pa_playlist_deleted(pa_card *, unsigned);
void pa_playlist_renamed(pa_card *, unsigned, const char *);
void pa_state_reset(void);
int pa_favorites_apply(pa_card *, void *, pa_sql_exec);
unsigned pa_thread_id(void);
int inline_playlist_bind(pa_card *, unsigned, const char *, const char *);
int inline_playlist_stage(pa_card *, const char *, char *, unsigned);
int inline_playlist_display(pa_card *, const char *, char *, unsigned);
void pa_remove_playlist_stage(const char *);
void inline_state_reset(void);
int inline_state_pending(pa_card *);
int inline_state_export(pa_card *, void *, pa_sql_exec);
int inline_favorites_apply(pa_card *, void *, pa_sql_exec);
void inline_playlist_created(pa_card *, unsigned, const char *);
void inline_playlist_deleted(pa_card *, unsigned);
void inline_playlist_renamed(pa_card *, unsigned, const char *);
int inline_playlist_subject(pa_card *, unsigned, const char *, char *, unsigned);
void inline_playlist_discard(const char *);
void inline_playlist_abandon(unsigned);
int pa_medium_identity(const pa_card *, char[65]);
#endif
