/* Preserve stock mutation/commit semantics. SQL notification reads current
 * state, including a pending stock transaction; never issues a SQL write. */
extern int stock_sqlite_exec(void*,const char*,int(*)(void*,int,char**,char**),void*,char**);
extern int stock_db_insert(void*,const char*,const char*,int);
extern int stock_db_delete(void*,const char*,const char*,int);
extern int stock_db_update(const char*,const char*);
extern int stock_db_delete_id(const char*,const char*,unsigned);
extern int stock_db_delete_first(const char*,const char*);
extern int stock_db_commit(void);
extern int stock_import_playlists(void);
extern int stock_playlist_exists(void*,unsigned);
extern int stock_playlist_delete_one(void*);
extern int stock_playlist_rename(unsigned,const char*);
extern int stock_sqlite_open(const char*,void**);
extern int stock_sqlite_close(void*);
extern int stock_file_access(const char*,int);
static volatile unsigned state_batch;static unsigned state_batch_owner,state_seen_generation,state_dirty;
static unsigned char state_close_lock[24] __attribute__((aligned(4)));
static int (*state_real_close)(void*);
extern int pa_call_foreign_close(void*,uintptr_t);
static unsigned state_lifetime_ready;
/* The pinned executable has one writable GOT entry for sqlite3_close. Guard
 * its connection lifetime, including query-error closes outside these hooks.
 * Clear the same global the stock callers clear, before it can become dangling. */
int pa_sqlite_close(void*db){int r;stock_mutex_lock(state_close_lock);if(db&&PTR32(0x809b54)==db)PTR32(0x809b54)=0;r=state_real_close?pa_call_foreign_close(db,(uintptr_t)state_real_close):0;stock_mutex_unlock(state_close_lock);return r;}
static int state_lifetime(void){uintptr_t target;if(state_lifetime_ready)return 1;
 if(stock_sqlite_close(0))return 0;target=*(uintptr_t*)(uintptr_t)0x8044d0;
 if(target<0x10000000u||target>=0x80000000u||(target&3))return 0;
 state_real_close=(int(*)(void*))target;__sync_synchronize();*(uintptr_t*)(uintptr_t)0x8044d0=(uintptr_t)pa_sqlite_close;__sync_synchronize();state_lifetime_ready=1;return 1;
}
static int relevant(const char*t){return t&&(!strcmp(t,"myfav")||!strcmp(t,"playlist"));}
static int contains(const char*s,const char*needle){unsigned n=(unsigned)strlen(needle);if(s)for(;*s;s++)if(!strncmp(s,needle,n))return 1;return 0;}
static void state_notify(void){void*db;if(state_batch||!initialized)return;stock_mutex_lock(io_lock);
 if(io_card.info_mode&&io_card.root>=0&&pa_card_current(&io_card)&&(!usb_owned)&&(io_card.info_mode!=2||inline_import_generation==generation)){
  if(!state_lifetime()){state_dirty=1;(void)pa_state_pending(&io_card);}else{stock_mutex_lock(state_close_lock);db=PTR32(0x809b54);
   if(db){if(!pa_state_export(&io_card,db,stock_sqlite_exec)){state_seen_generation=generation;state_dirty=0;}else state_dirty=1;}stock_mutex_unlock(state_close_lock);}
 }stock_mutex_unlock(io_lock);
}
static void state_mark(void){if(!managed_card())return;state_dirty=1;stock_mutex_lock(io_lock);(void)pa_state_pending(&io_card);stock_mutex_unlock(io_lock);}
static void state_initial(void){if((state_dirty||state_seen_generation!=generation)&&PTR32(0x809b54))state_notify();}
int pa_db_insert(void*record,const char*table,const char*db,int mode){int yes=relevant(table);if(yes)state_mark();int r=stock_db_insert(record,table,db,mode);
 if(yes){if(r==1&&record&&!strcmp(table,"playlist")&&*(unsigned*)record<100){stock_mutex_lock(io_lock);pa_playlist_created(&io_card,*(unsigned*)record,*(const char**)((char*)record+16));stock_mutex_unlock(io_lock);}state_notify();}return r;
}
int pa_db_delete(void*record,const char*table,const char*db,int mode){int yes=relevant(table);if(yes)state_mark();int r=stock_db_delete(record,table,db,mode);if(yes)state_notify();return r;}
int pa_db_update(const char*db,const char*sql){int yes=contains(sql,"playlist")||contains(sql,"myfav");if(yes)state_mark();int r=stock_db_update(db,sql);if(yes)state_notify();return r;}
int pa_db_delete_id(const char*table,const char*db,unsigned id){int yes=relevant(table);if(yes)state_mark();int r=stock_db_delete_id(table,db,id);
 if(yes){if(r==1&&id<100&&!strcmp(table,"playlist")){stock_mutex_lock(io_lock);pa_playlist_deleted(&io_card,id);stock_mutex_unlock(io_lock);}state_notify();}return r;}
int pa_db_delete_first(const char*table,const char*db){int yes=relevant(table);if(yes)state_mark();int r=stock_db_delete_first(table,db);if(yes)state_notify();return r;}
int pa_db_commit(void){if(state_dirty)state_notify();return stock_db_commit();}
int pa_import_playlists(void){int yes=managed_card();if(yes){inline_import_generation=generation;state_mark();state_batch_owner=pa_thread_id();__sync_add_and_fetch(&state_batch,1);}int r=stock_import_playlists();if(yes){void*db=0;int own=0;
  /* Stock import can close its connection after committing. Only this explicit
   * write action may open another connection; ordinary state export never does. */
  stock_mutex_lock(io_lock);if(state_lifetime()){stock_mutex_lock(state_close_lock);db=PTR32(0x809b54);
  if(!db&&!stock_file_access("/mnt/data/database.db",4)){int error=stock_sqlite_open("/mnt/data/database.db",&db);own=db!=0;if(error){if(db)pa_call_foreign_close(db,(uintptr_t)state_real_close);db=0;own=0;}}
  if(db){(void)pa_favorites_apply(&io_card,db,stock_sqlite_exec);
   if(!pa_state_export(&io_card,db,stock_sqlite_exec)){state_seen_generation=generation;state_dirty=0;}
   }if(own)pa_call_foreign_close(db,(uintptr_t)state_real_close);stock_mutex_unlock(state_close_lock);}stock_mutex_unlock(io_lock);
  __sync_sub_and_fetch(&state_batch,1);state_batch_owner=0;if(state_dirty)state_notify();}return r;}
int pa_playlist_exists(void*record,unsigned id){return state_batch&&state_batch_owner==pa_thread_id()?0:stock_playlist_exists(record,id);}
/* The stock importer removed duplicate paths, so its delete helper removed by
 * URL. Managed imports retain occurrences; delete the selected native row ID. */
int pa_playlist_delete_one(void*record){if(record&&*(unsigned*)record>100&&owned_music(*(const char**)((char*)record+12)))return pa_db_delete_id("playlist","/mnt/data/database.db",*(unsigned*)record);return stock_playlist_delete_one(record);}
int pa_playlist_rename(unsigned id,const char*name){int yes=managed_card();if(yes){state_mark();__sync_add_and_fetch(&state_batch,1);}int r=stock_playlist_rename(id,name);if(yes){if(r==1){stock_mutex_lock(io_lock);pa_playlist_renamed(&io_card,id,name);stock_mutex_unlock(io_lock);}__sync_sub_and_fetch(&state_batch,1);state_notify();}return r;}
