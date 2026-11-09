/* The stock detector enables the mass-storage gadget before opening its page.
 * Quiesce at that first command, not at the later page callback. */
static char pending_storage_start[160];
static void card_quiesce(void){
 state_notify();
 stock_mutex_lock(io_lock);
 usb_owned=1;
 take();generation++;play_seq++;memset(ready,0,sizeof(ready));
 memset(&view_card,0,sizeof(view_card));
 view_card.root=view_card.meta=view_card.index=view_card.lyrics=view_card.assets=-1;
 playing[0]=0;release();
 if(initialized)pa_card_close(&io_card);
 stock_mutex_unlock(io_lock);
 pa_collect();
}
int pa_usb_system(const char*command){
 if(!command)return stock_system(command);
 if(!strncmp(command,"storage_config.sh start ",sizeof("storage_config.sh start ")-1)||
    !strncmp(command,"/usr/bin/storage_config.sh start ",sizeof("/usr/bin/storage_config.sh start ")-1)){
  /* Stop the isolated writer and its decoder children before unmount. A failed
   * stop/unmount refuses writable host exposure; the original exit still works. */
  if(stock_system("/tmp/xm5-organize stop"))return 256;
  if(text(pending_storage_start,sizeof(pending_storage_start),command))return 256;
  card_quiesce();
  if(stock_system("umount /mnt/mmc"))return 256;
  int r=stock_system(command);if(!r)pending_storage_start[0]=0;return r;
 }
 if(!strcmp(command,"umount /mnt/mmc")){
  if(stock_system("/tmp/xm5-organize stop"))return 256;
  card_quiesce();
  int r=stock_system(command);
  /* The original early detector ignores system()'s result. Its later page
   * stops playback and retries unmount. Finish the deferred first start there. */
  if(!r&&pending_storage_start[0]){
   r=stock_system(pending_storage_start);if(!r)pending_storage_start[0]=0;
  }
  return r;
 }
 return stock_system(command);
}
extern int stock_usb_exit(void);
int pa_usb_exit(void){
 int r=stock_usb_exit();
 /* The next acquisition checks the current mount, index and INCOMPLETE marker;
  * no old descriptor or decoded-owner generation is reused. */
 stock_mutex_lock(io_lock);usb_owned=0;pending_storage_start[0]=0;stock_mutex_unlock(io_lock);
 return r;
}
extern int stock_load_all(const char*,void*,int);
int pa_load_all(const char*path,void*table,int check){
 if(local(path)){
  const char*p=path+9;
  if(!strchr(p,'/')&&(!strncmp(p,".xm5-before-",12)||
      !strncmp(p,".xm5-library-",13)||!strncmp(p,".xm5-export-",12)))return -1;
 }
 return stock_load_all(path,table,check);
}
extern int stock_scan_music(void*,void*,void*,void*);
int pa_scan_music(void*a,void*b,void*c,void*d){
 int r=stock_scan_music(a,b,c,d);
 if(!usb_owned&&pa_card_present(ROOT))(void)pa_import_playlists();
 if(!usb_owned&&stock_play_status()!=1&&!stock_file_access(ROOT "/xm5/Struct",4))
  (void)stock_system("/tmp/xm5-organize start >/tmp/xm5-organize-launch.log 2>&1 &");
 return r;
}
