/* Runs the actual target-compiled player_hooks.o. Vendor boundaries and card
 * IO are explicit stubs. Real card IO is exercised separately by check_io.py. */
#include "managed_image.h"
#include <asm/unistd.h>
extern int pa_worker_folder(const char*);
extern int pa_worker_play(const char*,const char*,int,int);
extern int pa_worker_same(const char*,const char*);
extern int pa_set_play(const char*,const char*,int);
extern int pa_folder_row(void*,int);
extern int pa_folder_play(void*);
extern int pa_jump_play(void*,int);
extern int pa_power_play(void*);
static long raw(long nr,long a,long b,long c){register long v __asm__("$2")=nr,r4 __asm__("$4")=a,r5 __asm__("$5")=b,r6 __asm__("$6")=c,r7 __asm__("$7")=0;__asm__ volatile("syscall":"+r"(v),"+r"(r7):"r"(r4),"r"(r5),"r"(r6):"memory");return r7?-v:v;}
static unsigned epoch=10,present=1,fallback,loads,unloads,change_track,change_mount,load_error;
static char shown[128],route[128];static void*passed_owner;static unsigned system_calls,linked=1;
extern int pa_lyric_path(char*,unsigned,const char*,const char*);
extern int pa_playlist_list(const char*,void*);
extern int pa_playlist_save(const char*,const char*,unsigned);
extern int pa_playlist_mkdir(const char*);
extern int pa_nf_init(void*,void*,const char*,const char*,void*);
static unsigned record[14],info_enabled,info_fail,asset_drops;
static unsigned asset_words[16];static unsigned char asset_pixels[25664];
extern void *pa_loader_load(void*,unsigned,unsigned,const char*,const char*);
extern int pa_loader_exist(void*,const char*);
extern int pa_set_eq(float,float,float,float,float,float,float,float,float,float,int);
void *stock_loader_load(void*a,unsigned b,unsigned c,const char*d,const char*e){(void)a;(void)b;(void)c;(void)d;(void)e;return (void*)42;}
int stock_loader_exist(void*a,const char*b){(void)a;(void)b;return 1;}
void *stock_asset_create(unsigned a,unsigned b,const char*c,unsigned d){(void)c;if(a!=3||b!=4||d!=sizeof(asset_pixels))return 0;memset(asset_words,0,sizeof(asset_words));asset_words[1]=d;asset_words[11]=(unsigned)asset_pixels;return asset_words;}
int stock_asset_destroy(void*a){(void)a;asset_drops++;return 0;}
int pa_info_asset(pa_card*c,const char*p,xm5_art_record*r){(void)c;(void)p;memset(r,0,sizeof(*r));r->side=80;r->info_length=sizeof(asset_pixels);return info_enabled?0:1;}
int pa_info_read(pa_card*c,const xm5_art_record*r,void*p,unsigned n){(void)c;(void)r;memset(p,123,n);return (int)info_fail;}
int pa_info_gain(pa_card*c,const char*p,float*out){(void)c;(void)p;*out=info_enabled?4:0;return info_enabled?0:1;}
int stock_play_status(void){return 1;}
int stock_set_eq(float a,float b,float c,float d,float e,float f,float g,float h,float i,float j,int state){(void)a;(void)b;(void)c;(void)d;(void)e;(void)f;(void)g;(void)h;(void)i;(void)j;if(a!=1.25f||j!=10.25f)return 99;return state;}
static void require(int yes,const char*s){if(!yes){raw(__NR_write,2,(long)s,strlen(s));raw(__NR_exit,1,0,0);}}
int pa_card_open(pa_card*c,const char*p){(void)p;memset(c,0,sizeof(*c));c->root=present?3:-1;c->meta=present?4:-1;c->mount_id=epoch;c->info_mode=info_enabled;return present?0:20;}
void pa_card_close(pa_card*c){c->root=c->meta=-1;}
int pa_card_current(const pa_card*c){return c->mount_id==epoch;}
int pa_card_present(const char*p){(void)p;return (int)present;}
void pa_discard_ram(const char*p){(void)p;}
int pa_lookup_size(pa_card*c,const char*role,const char*subject,unsigned side,xm5_art_record*r){(void)c;(void)role;memset(r,0,sizeof(*r));r->side=side;if(!strcmp(subject,"Music/none.wav")){r->no_art=1;return 2;}if(!strncmp(subject,"Outside/",8))return 1;strcpy(r->path,".xm5/art/fake.bmp");memset(r->sha256,strchr(subject,'B')?'b':'a',64);return 0;}
int pa_bind_native(pa_card*c,const char*r){(void)c;(void)r;return 0;}
int pa_native_uri(const xm5_art_record*r,const char*d,char*out,size_t cap){(void)d;(void)cap;strcpy(out,r->sha256[0]=='a'?"file:///tmp/xa/aaaa.bmp":"file:///tmp/xa/bbbb.bmp");return 0;}
int pa_materialize(pa_card*c,const xm5_art_record*r,const char*d,char*out,size_t cap){(void)c;(void)d;(void)cap;strcpy(out,r->sha256[0]=='a'?"/tmp/xa/aaaa.bmp":"/tmp/xa/bbbb.bmp");return 0;}
int stock_tfstatus(void){fallback++;return 1;}
int stock_album_cover(const char*a,const char*b,int c,int d){(void)a;(void)b;(void)c;(void)d;fallback++;return 42;}
int stock_set_cover(const char*a,const char*b,int c){(void)a;(void)b;(void)c;return 0;}
int stock_folder_row(void*a,int b){(void)a;(void)b;fallback++;return 42;}
int stock_folder_play(void*a){(void)a;fallback++;return 42;}
int stock_jump_play(void*a,int b){(void)a;(void)b;fallback++;return 42;}
int stock_power_play(void*a){(void)a;fallback++;return 42;}
void*stock_lookup(void*a,const char*b,int c){(void)b;(void)c;return a;}
unsigned stock_deque_size(void*a){(void)a;return 1;}
void*stock_deque_at(void*a,unsigned b){(void)a;(void)b;return record;}
int stock_load_image(void*a,const char*b,void*c){(void)a;(void)b;(void)c;loads++;if(change_track){change_track=0;pa_set_play("/mnt/mmc/Music/B.wav",0,0);}if(change_mount){change_mount=0;epoch++;}return (int)load_error;}
int pa_image_set(void*a,const char*b){(void)a;strcpy(shown,b);return 0;}
int stock_unload_image(void*a,void*b){(void)a;(void)b;unloads++;return 0;}
int stock_draw_type(void*a,int b){(void)a;(void)b;return 0;}
int stock_destroy_children(void*a){(void)a;return 0;}
const char*stock_get_image_prop(void*a,const char*b,const char*c){(void)a;(void)b;(void)c;return shown;}
int stock_folder_timer(void*a){(void)a;return 0;}
int stock_mutex_lock(void*a){(void)a;return 0;}
int stock_mutex_unlock(void*a){(void)a;return 0;}
void start_c(unsigned*sp){(void)sp;unsigned object[40]={0};void*widget=object;object[2]=object[3]=80;
 *(unsigned*)0x805de4=1;*(unsigned char*)0x807271=1;record[13]=4;record[3]=(unsigned)"/mnt/mmc/Music/A";
 require(pa_worker_folder("/mnt/mmc/Music/A")==0,"directory worker not bypassed\n");pa_folder_row(widget,0);require(!strcmp(shown,"file:///tmp/xa/aaaa.bmp"),"directory did not show prepared URI\n");
 pa_set_play("/mnt/mmc/Music/A.wav",0,0);require(pa_worker_play("/mnt/mmc/Music/A.wav","/tmp/coverpic.jpg",330,330)==1,"play worker not bypassed\n");object[2]=object[3]=90;pa_folder_play(widget);require(!strcmp(shown,"file:///tmp/xa/aaaa.bmp"),"play A missing\n");require(loads==0,"UI eagerly decoded image\n");
 pa_set_play("/mnt/mmc/Music/B.wav",0,0);pa_worker_play("/mnt/mmc/Music/B.wav","x",330,330);object[2]=object[3]=330;pa_jump_play(widget,1);require(!strcmp(shown,"file:///tmp/xa/bbbb.bmp"),"per-track image mixed\n");object[2]=object[3]=150;pa_power_play(widget);require(!strcmp(shown,"file:///tmp/xa/bbbb.bmp"),"power cover not redirected\n");
 pa_set_play("/mnt/mmc/Music/none.wav",0,0);pa_worker_play("/mnt/mmc/Music/none.wav","x",330,330);pa_folder_play(widget);require(!strcmp(shown,"default_cover"),"no-art did not clear\n");
 unsigned stamp_g,stamp_s;pa_render_stamp(&stamp_g,&stamp_s);pa_set_play("/mnt/mmc/Music/A.wav",0,0);require(!pa_render_valid(stamp_g,stamp_s),"late paint generation accepted\n");
 epoch++;object[2]=object[3]=90;pa_folder_play(widget);require(!strcmp(shown,"default_cover"),"old mount published\n");
 pa_worker_play("/mnt/mmc/Music/A.wav","x",330,330);pa_folder_play(widget);require(!strcmp(shown,"file:///tmp/xa/aaaa.bmp"),"new mount did not republish\n");
 pa_set_play("/mnt/mmc/Outside/song.wav",0,0);require(pa_worker_play("/mnt/mmc/Outside/song.wav","x",330,330)==42,"unmanaged audio fallback broken\n");require(pa_folder_play(widget)==42,"unmanaged UI fallback broken\n");
 require(pa_worker_same("/mnt/mmc/Music/A.wav","/mnt/mmc/Music/A.wav")!=0,"legacy same-path shortcut remains\n");present=0;require(pa_worker_same("/mnt/mmc/Music/A.wav","/mnt/mmc/Music/A.wav")==0,"unmanaged shortcut changed\n");
 pa_set_play("http://example.invalid/a",0,1);require(pa_folder_play(widget)==42,"remote playback changed\n");require(loads==unloads,"bitmap borrow not balanced\n");
 present=1;epoch++;pa_playlist_list("legacy",0);require(!strcmp(route,"/mnt/mmc/xm5/Struct/playlists"),"managed playlist list route\n");
 pa_playlist_save("legacy","name",7);require(!strcmp(route,"/mnt/mmc/xm5/Struct/playlists"),"managed playlist save route\n");
 unsigned old_calls=system_calls;pa_playlist_mkdir("mkdir legacy");require(system_calls==old_calls,"managed playlist mkdir ran shell\n");
 pa_nf_init(0,0,"legacy","active",(void*)0x12345678);require(!strcmp(route,"/mnt/mmc/xm5/Struct/peq")&&passed_owner==(void*)0x12345678,"managed PEQ route/fifth argument\n");
 char lrc[128];linked=0;pa_lyric_path(lrc,sizeof(lrc),"%s","/mnt/mmc/xm5/music/song.wav");require(!strcmp(lrc,"/tmp/xm5lrc0.lrc"),"managed lyric not selected\n");
 linked=1;pa_lyric_path(lrc,sizeof(lrc),"%s","/mnt/mmc/xm5/music/song.wav");require(!strcmp(lrc,"/mnt/mmc/xm5/music/song.wav"),"missing lyric fallback\n");
 present=0;epoch++;pa_playlist_list("legacy",0);require(!strcmp(route,"legacy"),"legacy playlist route changed\n");pa_playlist_save("legacy","name",7);require(!strcmp(route,"legacy"),"legacy save route changed\n");pa_playlist_mkdir("mkdir legacy");require(system_calls==old_calls+1,"legacy mkdir missing\n");pa_nf_init(0,0,"legacy","active",(void*)0x2345);require(!strcmp(route,"legacy")&&passed_owner==(void*)0x2345,"legacy PEQ route/owner\n");
 present=info_enabled=1;epoch++;pa_set_play("/mnt/mmc/Music/A.wav",0,0);pa_worker_play("/mnt/mmc/Music/A.wav","x",330,330);
 require(pa_loader_exist(0,"file:///tmp/xa/fake.bmp")==1,"virtual asset existence missing\n");
 require(pa_loader_load(0,3,4,"file:///tmp/xa/fake.bmp","fake")==asset_words&&asset_pixels[0]==123,"XMI loader did not fill native asset buffer\n");
 info_fail=1;require(pa_loader_load(0,3,4,"file:///tmp/xa/fake.bmp","fake")==0&&asset_drops==1,"failed XMI asset owner leak\n");info_fail=0;
 require(pa_loader_load(0,3,4,"legacy","fake")== (void*)42,"legacy loader changed\n");
 require(pa_set_eq(1.25f,2,3,4,5,6,7,8,9,10.25f,0)==2,"gain-only node was not armed or float ABI changed\n");
 require(pa_set_eq(1.25f,2,3,4,5,6,7,8,9,10.25f,1)==1,"existing EQ state changed\n");
 info_enabled=0;require(pa_set_eq(1.25f,2,3,4,5,6,7,8,9,10.25f,0)==0,"unmanaged gain node armed\n");
 const char*message="Target compiled hook boundary cases: PASS\n";raw(__NR_write,1,(long)message,strlen(message));raw(__NR_exit,0,0,0);for(;;){}
}

int pa_lyric_link(pa_card*c,const char*s,unsigned n,char*out,unsigned cap){(void)c;(void)s;(void)n;(void)cap;if(!linked)strcpy(out,"/tmp/xm5lrc0.lrc");return (int)linked;}


int stock_snprintf(char*d,unsigned cap,const char*f,const char*s){(void)cap;(void)f;strcpy(d,s);return (int)strlen(s);}
int stock_list_m3u(const char*p,void*d){(void)d;strcpy(route,p);return 0;}
int stock_save_playlist(const char*p,const char*n,unsigned id){(void)n;(void)id;strcpy(route,p);return 0;}
int stock_system(const char*p){(void)p;system_calls++;return 0;}
int stock_nf_init(void*b,void*p,const char*i,const char*a,void*o){(void)b;(void)p;(void)a;strcpy(route,i);passed_owner=o;return 0;}
