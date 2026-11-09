/* Owned card exchange only. All paths are relative to pinned Struct/device.
 * One SELECT observes both tables in one SQLite read statement; this module
 * never opens, changes or commits the native database. */
#define STATE_LIMIT (16u*1024u*1024u)
#define BIND_MAX 100
static int state_cmp(const void*a,const void*b,unsigned n){const u8*x=a,*y=b;while(n--)if(*x++!=*y++)return 1;return 0;}
typedef struct {unsigned id;char pc[129],exported[256],display[256];} binding;
static binding bindings[BIND_MAX];static unsigned binding_count;
static u8 binding_build[16];static unsigned binding_loaded;
static void put32(u8*p,u32 x){unsigned i;for(i=0;i<4;i++){p[i]=(u8)x;x>>=8;}}
static void put64(u8*p,unsigned long long x){put32(p,(u32)x);put32(p+4,(u32)(x>>32));}
static void raw_hash(const char*s,u8*out){unsigned i;for(i=0;i<32;i++)out[i]=(u8)(hex(s[i*2])*16+hex(s[i*2+1]));}
static int build_id(pa_card*c,u8 out[16]){u8 h[128];if(!c->info_mode||!read_at(c->index,0,h,128)||state_cmp(h,"XM5IXI1\0",8))return 0;cp(out,h+24,16);return 1;}
static int state_dir(pa_card*c){if(!pa_card_current(c)||!c->info_mode)return -1;
 int d=(int)P_OPENAT(c->meta,"device",DIRFLAGS,0);if(d<0){(void)P_MKDIRAT(c->meta,"device");d=(int)P_OPENAT(c->meta,"device",DIRFLAGS,0);}return d;
}
static int pool_string(int fd,unsigned pool,unsigned bytes,unsigned off,unsigned len,char*out,unsigned cap){unsigned i;if(len>=cap||off>bytes||len>bytes-off||!read_at(fd,pool+off,out,len))return 0;for(i=0;i<len;i++)if(!out[i]||(unsigned char)out[i]<32)return 0;out[len]=0;return 1;}
static int bindings_read(pa_card*c,const char*path,int overlay){int fd=pa_regular_open(c->meta,path);u8 h[128],r[32],build[16];struct stat64 st;char hash[65];unsigned n,pool,bytes,i,j;sha sh;u8 block[1024];
 if(fd<0)return 0;if(!build_id(c,build)||!read_at(fd,0,h,128)||P_STAT(fd,&st)||state_cmp(h,"XM5PLB1\0",8)||le32(h+8)!=1||le32(h+12)!=128||le32(h+20)||le32(h+28)!=32||state_cmp(h+32,build,16)||!zeroes(h+84,44))goto fail;
 n=le32(h+24);bytes=le32(h+80);pool=128+n*32;if(n>BIND_MAX||bytes>128*1024||(unsigned long long)st.st_size!=(unsigned long long)pool+bytes||le32(h+16)!=(unsigned)st.st_size)goto fail;
 sha_init(&sh);for(i=128;i<(unsigned)st.st_size;){j=(unsigned)st.st_size-i;if(j>sizeof(block))j=sizeof(block);if(!read_at(fd,i,block,j))goto fail;sha_add(&sh,block,j);i+=j;}sha_end(&sh,hash);raw_hash(hash,block);if(state_cmp(block,h+48,32))goto fail;
 for(i=0;i<n;i++){binding x;memset(&x,0,sizeof(x));if(!read_at(fd,128+i*32,r,32)||!pool_string(fd,pool,bytes,le32(r+8),le32(r+12),x.pc,sizeof(x.pc))||!pool_string(fd,pool,bytes,le32(r+16),le32(r+20),x.exported,sizeof(x.exported))||!pool_string(fd,pool,bytes,le32(r+24),le32(r+28),x.display,sizeof(x.display)))goto fail;
  if(!x.pc[0]||!x.exported[0]||!(le32(r+4)==0||(le32(r)==~0u&&le32(r+4)==~0u)))goto fail;x.id=le32(r);
  if(!overlay){bindings[binding_count++]=x;}else for(j=0;j<binding_count;j++)if(!strcmp(bindings[j].pc,x.pc)&&!strcmp(bindings[j].exported,x.exported)){bindings[j].id=x.id;copystr(bindings[j].display,sizeof(bindings[j].display),x.display);break;}
 }
 pa_close(fd);return 1;fail:pa_close(fd);return 0;
}
static int bindings_load(pa_card*c){u8 build[16];if(!build_id(c,build))return 0;if(binding_loaded&&!state_cmp(build,binding_build,16))return 1;
 binding_count=0;binding_loaded=0;if(!bindings_read(c,"indexes/playlists.bin",0)){binding_count=0;return 0;}/* Overlay failure cannot poison prepared records. */
 binding backup[BIND_MAX];cp(backup,bindings,sizeof(bindings));if(!bindings_read(c,"device/playlists.bind",1))cp(bindings,backup,sizeof(bindings));
 cp(binding_build,build,16);binding_loaded=1;return 1;
}
void pa_state_reset(void){inline_state_reset();binding_loaded=binding_count=0;}
static binding*binding_find(pa_card*c,unsigned id,const char*name){unsigned i;binding*found=0;if(!bindings_load(c))return 0;
 if(id!=~0u)for(i=0;i<binding_count;i++)if(bindings[i].id==id){if(!name||!strcmp(name,bindings[i].display))return &bindings[i];bindings[i].id=~0u;}
 if(name)for(i=0;i<binding_count;i++)if(bindings[i].id==~0u&&!strcmp(bindings[i].exported,name)){if(found)return 0;found=&bindings[i];}
 return found;
}
int pa_playlist_subject(pa_card*c,unsigned id,const char*name,char*out,unsigned cap){if(c->info_mode==2)return inline_playlist_subject(c,id,name,out,cap);binding*b=binding_find(c,id,name);return b&&!copystr(out,cap,b->pc)?0:XM5_ART_MISS;}
void pa_playlist_deleted(pa_card*c,unsigned id){if(c->info_mode==2){inline_playlist_deleted(c,id);return;}unsigned i;if(bindings_load(c))for(i=0;i<binding_count;i++)if(bindings[i].id==id)bindings[i].id=~0u;}
void pa_playlist_renamed(pa_card*c,unsigned id,const char*name){if(c->info_mode==2){inline_playlist_renamed(c,id,name);return;}unsigned i;if(name&&bindings_load(c))for(i=0;i<binding_count;i++)if(bindings[i].id==id)copystr(bindings[i].display,sizeof(bindings[i].display),name);}
void pa_playlist_created(pa_card*c,unsigned id,const char*name){if(c->info_mode==2){inline_playlist_created(c,id,name);return;}pa_playlist_deleted(c,id);binding*b=binding_find(c,~0u,name);if(b&&name&&!strcmp(b->exported,name)){b->id=id;copystr(b->display,sizeof(b->display),name);}}
typedef struct {pa_card*c;int fd;unsigned count,used,failed;u8 buffer[4096];sha hash;} sink;
static int flush(sink*s){if(s->failed)return 0;if(s->used){if(!writeall(s->fd,s->buffer,s->used)){s->failed=1;return 0;}sha_add(&s->hash,s->buffer,s->used);s->used=0;}return 1;}
static int emit(sink*s,const void*p,unsigned n){const u8*b=p;if(n>STATE_LIMIT-128-s->count){s->failed=1;return 0;}s->count+=n;while(n--){s->buffer[s->used++]=*b++;if(s->used==sizeof(s->buffer)&&!flush(s))return 0;}return !s->failed;}
static void literal(sink*s,const char*p){emit(s,p,(unsigned)strlen(p));}
static int state_text(const char*s,unsigned cap){unsigned n=0,need=0;u32 cp=0,min=0;if(!s)return 1;for(;s[n];n++){unsigned c=(u8)s[n];if(n>=cap)return 0;if(need){if((c&0xc0)!=0x80)return 0;cp=(cp<<6)|(c&63);if(!--need&&(cp<min||cp>0x10ffff||(cp>=0xd800&&cp<=0xdfff)))return 0;}else if(c<128)continue;else if(c>=0xc2&&c<=0xdf){cp=c&31;need=1;min=0x80;}else if(c>=0xe0&&c<=0xef){cp=c&15;need=2;min=0x800;}else if(c>=0xf0&&c<=0xf4){cp=c&7;need=3;min=0x10000;}else return 0;}return !need;}
static void json_string(sink*s,const char*p){unsigned i;char esc[6]={'\\','u','0','0',0,0};literal(s,"\"");if(p)for(i=0;p[i];i++){unsigned char c=(unsigned char)p[i];if(c=='\"'||c=='\\'){char pair[2]={'\\',(char)c};emit(s,pair,2);}else if(c<32){esc[4]="0123456789abcdef"[c>>4];esc[5]="0123456789abcdef"[c&15];emit(s,esc,6);}else emit(s,&c,1);if(i>16384){s->failed=1;break;}}literal(s,"\"");}
static void number(sink*s,unsigned n){char b[12];decimal(b,n);literal(s,b);}
typedef struct {sink s;int playlists,opened,comma;unsigned current;} state_writer;
static int state_row(void*opaque,int n,char**v,char**columns){state_writer*w=opaque;sink*s=&w->s;unsigned band,kind,id,pl;(void)columns;if(n!=6||!v[0]||!v[1]||!v[2]||!v[3])return 1;
 if(!state_text(v[4],4096)||!state_text(v[5],1024))return 1;
 band=(unsigned)(v[0][0]-'0');kind=(unsigned)(v[2][0]-'0');pl=0;id=0;const char*p=v[1];while(*p>='0'&&*p<='9')pl=pl*10+(unsigned)(*p++-'0');p=v[3];while(*p>='0'&&*p<='9')id=id*10+(unsigned)(*p++-'0');
 if(band&& !w->playlists){literal(s,"],\"playlists\":[");w->playlists=1;w->comma=0;}
 if(band&&kind==0){const char*name=v[5]&&v[5][0]?v[5]:"Unnamed playlist";if(strchr(name,'\r')||strchr(name,'\n'))return 1;binding*b=binding_find(s->c,pl,name);if(w->opened)literal(s,"]},");literal(s,"{\"native_id\":");number(s,pl);literal(s,",\"name\":");json_string(s,name);
  if(b){b->id=pl;copystr(b->display,sizeof(b->display),v[5]?v[5]:"");literal(s,",\"pc_id\":");json_string(s,b->pc);literal(s,",\"source_export_name\":");json_string(s,b->exported);}
  literal(s,",\"entries\":[");w->opened=1;w->current=pl;w->comma=0;
 }else{if(band&&(!w->opened||w->current!=pl))return 1;if(w->comma)literal(s,",");literal(s,"{\"path\":");p=v[4]?v[4]:"";if(!strncmp(p,"/mnt/mmc/",9))p+=9;json_string(s,p);literal(s,",\"native_track_id\":");number(s,id);literal(s,"}");w->comma=1;}
 return s->failed?1:0;
}
static const char state_query[]=
 "SELECT band,pl,kind,id,fileurl,name FROM ("
 "SELECT 0 band,0 pl,0 kind,id,fileurl,'' name FROM myfav UNION ALL "
 "SELECT 1,id,0,id,'',coalesce(name,filename,'') FROM playlist WHERE id<100 UNION ALL "
 "SELECT 1,cd_num,0,cd_num,'','Orphaned playlist' FROM playlist p WHERE id>100 AND NOT EXISTS(SELECT 1 FROM playlist h WHERE h.id=p.cd_num AND h.id<100) GROUP BY cd_num UNION ALL "
 "SELECT 1,cd_num,1,id,fileurl,'' FROM playlist WHERE id>100) ORDER BY band,pl,kind,id";
static int atomic_file(pa_card*c,int d,const char*tmp,const char*name,const void*p,unsigned n){(void)P_UNLINKAT(d,tmp);int fd=(int)P_OPENAT(d,tmp,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600),ok=0;if(fd<0)return 0;
 if(writeall(fd,p,n)&&P_FSYNC(fd)==0&&pa_card_current(c)&&P_RENAMEAT(d,tmp,name)==0){(void)P_FSYNC(d);ok=pa_card_current(c);}pa_close(fd);if(!ok)(void)P_UNLINKAT(d,tmp);return ok;
}
int pa_state_pending(pa_card*c){if(c->info_mode==2)return inline_state_pending(c);int d=state_dir(c);static const char p[]="{\"schema\":\"xm5.device-state.pending\",\"version\":1}\n";if(d<0)return PA_IO;
 int fd=pa_regular_open(d,"state.pending");if(fd>=0){pa_close(fd);pa_close(d);return 0;}
 int ok=atomic_file(c,d,".pending.part","state.pending",p,sizeof(p)-1);pa_close(d);return ok?0:PA_IO;
}
static int bindings_save(pa_card*c,int d){u8 h[128]={0},r[32];unsigned i,j,pool=0,off=0;sha hash;char digest[65];int fd;
 if(!bindings_load(c))return 0;for(i=0;i<binding_count;i++)pool+=(unsigned)(strlen(bindings[i].pc)+strlen(bindings[i].exported)+strlen(bindings[i].display));
 (void)P_UNLINKAT(d,".bindings.part");fd=(int)P_OPENAT(d,".bindings.part",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(fd<0)return 0;cp(h,"XM5PLB1\0",8);put32(h+8,1);put32(h+12,128);put64(h+16,128+binding_count*32+pool);put32(h+24,binding_count);put32(h+28,32);cp(h+32,binding_build,16);put32(h+80,pool);sha_init(&hash);
 if(!writeall(fd,h,128))goto fail;for(i=0;i<binding_count;i++){binding*b=&bindings[i];memset(r,0,32);put32(r,b->id);if(b->id==~0u)put32(r+4,~0u);const char*parts[]={b->pc,b->exported,b->display};for(j=0;j<3;j++){unsigned n=(unsigned)strlen(parts[j]);put32(r+8+j*8,off);put32(r+12+j*8,n);off+=n;}if(!writeall(fd,r,32))goto fail;sha_add(&hash,r,32);}
 for(i=0;i<binding_count;i++){binding*b=&bindings[i];const char*parts[]={b->pc,b->exported,b->display};for(j=0;j<3;j++){unsigned n=(unsigned)strlen(parts[j]);if(!writeall(fd,parts[j],n))goto fail;sha_add(&hash,parts[j],n);}}
 sha_end(&hash,digest);raw_hash(digest,h+48);if(P_SEEK(fd,0)<0||!writeall(fd,h,128)||P_FSYNC(fd)||!pa_card_current(c)||P_RENAMEAT(d,".bindings.part","playlists.bind"))goto fail;pa_close(fd);(void)P_FSYNC(d);return 1;
 fail:pa_close(fd);(void)P_UNLINKAT(d,".bindings.part");return 0;
}
int pa_state_export(pa_card*c,void*db,pa_sql_exec exec){if(c->info_mode==2)return inline_state_export(c,db,exec);int d=-1,fd=-1,old=-1,ok=0;u8 h[128]={0},build[16];char digest[65];unsigned long long seq=1;state_writer w;
 if(!db||!exec||!build_id(c,build)||(d=state_dir(c))<0)return PA_IO;if(pa_state_pending(c))goto done;
 old=pa_regular_open(d,"state.xms");if(old>=0){u8 prev[128];if(read_at(old,0,prev,128)&&!state_cmp(prev,"XM5STA1\0",8)&&!state_cmp(prev+32,build,16)){seq=((unsigned long long)le32(prev+28)<<32|le32(prev+24))+1;if(!seq||seq>>63)seq=1;}pa_close(old);old=-1;}
 (void)P_UNLINKAT(d,".state.part");fd=(int)P_OPENAT(d,".state.part",O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(fd<0)goto done;
 memset(&w,0,sizeof(w));w.s.c=c;w.s.fd=fd;sha_init(&w.s.hash);if(!writeall(fd,h,128))goto done;
 literal(&w.s,"{\"schema\":\"xm5.device-state\",\"version\":1,\"complete\":true,\"favorites\":[");
 if(exec(db,state_query,state_row,&w,0))goto done;if(!w.playlists)literal(&w.s,"],\"playlists\":[");if(w.opened)literal(&w.s,"]}");literal(&w.s,"]}");if(!flush(&w.s))goto done;
 cp(h,"XM5STA1\0",8);put32(h+8,1);put32(h+12,128);put64(h+16,128+w.s.count);put64(h+24,seq);cp(h+32,build,16);sha_end(&w.s.hash,digest);raw_hash(digest,h+48);put64(h+80,w.s.count);
 if(P_SEEK(fd,0)<0||!writeall(fd,h,128)||P_FSYNC(fd)||!pa_card_current(c))goto done;
 if(binding_count&&!bindings_save(c,d))goto done;if(P_RENAMEAT(d,".state.part","state.xms"))goto done;(void)P_FSYNC(d);if(!pa_card_current(c))goto done;
 (void)P_UNLINKAT(d,"state.pending");(void)P_FSYNC(d);ok=1;
 done:pa_close(old);pa_close(fd);if(!ok&&d>=0)(void)P_UNLINKAT(d,".state.part");pa_close(d);return ok?0:PA_IO;
}
