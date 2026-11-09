/* Inline metadata v1. The TSV is a replaceable hint, never media authority.
 * Persistent state is bounded to 32 artwork records and the card FDs. */
#include "inline_flac.h"
off_t xm5_file_bytes(int fd){struct stat64 st;return P_STAT(fd,&st)?-1:(off_t)st.st_size;}
int xm5_read(int fd,void*p,size_t n,off_t at){return at<0||(uint64_t)at>0xffffffffu||n>0xffffffffu-(unsigned)at||!read_at(fd,(unsigned)at,p,(unsigned)n)?-1:0;}
#define le32 inline_container_le32
#define be32 inline_container_be32
#include "inline-formats/other_container.c"
#undef le32
#undef be32
#define INLINE_RECORDS 32
static xm5_art_record inline_records[INLINE_RECORDS];
static unsigned inline_next;
static void inline_reset(void){memset(inline_records,0,sizeof(inline_records));inline_next=0;}
static void inline_store(const xm5_art_record*r){unsigned i;for(i=0;i<INLINE_RECORDS;i++)if(inline_records[i].encoding&&!strcmp(inline_records[i].sha256,r->sha256)){inline_records[i]=*r;return;}inline_records[inline_next++%INLINE_RECORDS]=*r;}
static int inline_path(const char*p){unsigned n=0,part=0;if(!p||!*p||*p=='/')return 0;for(;p[n];n++){unsigned c=(u8)p[n];if(n>=1023||c<32||c==127||c=='\\'||c==':')return 0;if(c=='/'){if(!part||(part==1&&p[n-1]=='.')||(part==2&&p[n-1]=='.'&&p[n-2]=='.'))return 0;part=0;}else part++;}return part&&!(part==1&&p[n-1]=='.')&&!(part==2&&p[n-1]=='.'&&p[n-2]=='.');}
static int inline_hint_line(const char*p){return (p[0]=='0'||p[0]=='1')&&p[1]=='\t'&&inline_path(p+2)&&!strncmp(p+2,"xm5/music/",10);}
static void inline_stat_key(sha*s,const struct stat64*st){sha_add(s,&st->st_dev,sizeof(st->st_dev));sha_add(s,&st->st_ino,sizeof(st->st_ino));sha_add(s,&st->st_size,sizeof(st->st_size));sha_add(s,&st->st_mtime,sizeof(st->st_mtime));sha_add(s,&st->st_ctime,sizeof(st->st_ctime));
#ifdef __mips__
 sha_add(s,&st->st_mtime_nsec,sizeof(st->st_mtime_nsec));sha_add(s,&st->st_ctime_nsec,sizeof(st->st_ctime_nsec));
#else
 sha_add(s,&st->st_mtim.tv_nsec,sizeof(st->st_mtim.tv_nsec));sha_add(s,&st->st_ctim.tv_nsec,sizeof(st->st_ctim.tv_nsec));
#endif
}
typedef struct {int fd;unsigned pos,used,next,total;u8 buffer[4096];} inline_lines;
static int inline_line(inline_lines*s,char*out,unsigned cap){unsigned n=0;for(;;){if(s->next==s->used){if(s->pos==s->total)return n?-1:0;unsigned k=s->total-s->pos;if(k>sizeof(s->buffer))k=sizeof(s->buffer);if(!read_at(s->fd,s->pos,s->buffer,k))return -1;s->pos+=k;s->used=k;s->next=0;}unsigned c=s->buffer[s->next++];if(c=='\n'){out[n]=0;return 1;}if(c==0||c=='\r'||n+1>=cap)return -1;out[n++]=(char)c;}}
static unsigned inline_cursor(const inline_lines*s){return s->pos-(s->used-s->next);}
/* Binary search line boundaries; directory lookup does not rescan a 20k-track
 * list. A short sequential suffix handles missing/unprepared representatives. */
static unsigned inline_lower(pa_card*c,const char*prefix){unsigned lo=0,hi=c->index_bytes,best=c->index_bytes;char line[1027];
 while(lo<hi){unsigned mid=lo+(hi-lo)/2;inline_lines s;memset(&s,0,sizeof(s));s.fd=c->index;s.total=c->index_bytes;s.pos=mid;if(mid){u8 previous;if(!read_at(c->index,mid-1,&previous,1))return c->index_bytes;if(previous!='\n'&&inline_line(&s,line,sizeof(line))<=0){hi=mid;continue;}}
 unsigned at=inline_cursor(&s);if(inline_line(&s,line,sizeof(line))<=0){hi=mid;continue;}if(!inline_hint_line(line))return c->index_bytes;if(strcmp(line+2,prefix)>=0){if(at<best)best=at;hi=mid;}else lo=inline_cursor(&s);
 }return best;
}
static int inline_index_open(pa_card*c){struct stat64 st;char line[1027],previous[1024]={0};unsigned count=0;int r;
 c->index=pa_regular_open(c->meta,"tracks.tsv");if(c->index<0||P_STAT(c->index,&st)||st.st_size<0||st.st_size>MANIFEST_LIMIT)return 0;
 inline_lines s;memset(&s,0,sizeof(s));s.fd=c->index;s.total=(unsigned)st.st_size;
 while((r=inline_line(&s,line,sizeof(line)))>0){if((line[0]!='0'&&line[0]!='1')||line[1]!='\t'||!inline_path(line+2)||strncmp(line+2,"xm5/music/",10)||(*previous&&strcmp(previous,line+2)>=0)||++count>1000000)return 0;copystr(previous,sizeof(previous),line+2);}
 if(r<0)return 0;c->info_mode=2;c->records=count;c->index_bytes=(unsigned)st.st_size;cp(c->index_identity,&st,sizeof(st));return 1;
}
static int inline_find(int fd,struct xm5_block*b){u8 h[42],next[8];struct stat64 st;if(P_STAT(fd,&st)||st.st_size<12||!read_at(fd,0,h,12))return 0;
 if(!same(h,"fLaC",4))return xm5_other_find(fd,b);
 if(st.st_size<42||!read_at(fd,0,h,42)||(h[4]&127)!=0||h[5]||h[6]||h[7]!=34)return -1;
 if(h[4]&128)return 0;if(st.st_size<50||!read_at(fd,42,next,8))return -1;if((next[0]&127)!=2||!same(next+4,"XM5I",4))return 0;
 unsigned n=(unsigned)next[1]<<16|(unsigned)next[2]<<8|next[3];if(n<XM5_APP_FIXED||n>XM5_APP_FIXED+XM5_LYRIC_MAX||(uint64_t)n>(uint64_t)st.st_size-46)return -1;b->header=42;b->size=n;return 1;
}
/* Baseline JPEG only; inspect marker bounds before entering stock decoder. */
static int inline_jpeg_header(int fd,unsigned at,unsigned bytes,unsigned side){u8 h[10];unsigned pos=2;int found=0;if(bytes<4||!read_at(fd,at,h,2)||h[0]!=255||h[1]!=216||!read_at(fd,at+bytes-2,h,2)||h[0]!=255||h[1]!=217)return 0;
 while(pos<bytes-2){if(!read_at(fd,at+pos,h,2)||h[0]!=255)return 0;unsigned marker=h[1];pos+=2;while(marker==255){if(pos>=bytes||!read_at(fd,at+pos,h,1))return 0;marker=h[0];pos++;}if(marker==218)return found;if(marker==217||marker==0||marker==216||marker==1||(marker>=208&&marker<=215))return 0;
 if(pos>bytes-2||!read_at(fd,at+pos,h,2))return 0;unsigned n=(unsigned)h[0]<<8|h[1];if(n<2||n>bytes-pos)return 0;
 if((marker>=192&&marker<=195)||(marker>=197&&marker<=199)||(marker>=201&&marker<=203)||(marker>=205&&marker<=207)){if(marker!=192||found||n<8||!read_at(fd,at+pos,h,8)||h[2]!=8||((unsigned)h[3]<<8|h[4])!=side||((unsigned)h[5]<<8|h[6])!=side||h[7]!=3)return 0;found=1;}pos+=n;
 }return 0;
}
static int inline_span_hash(int fd,unsigned at,unsigned bytes,char out[65]){sha s;u8 b[1024];sha_init(&s);while(bytes){unsigned n=bytes>sizeof(b)?sizeof(b):bytes;if(!read_at(fd,at,b,n))return 0;sha_add(&s,b,n);at+=n;bytes-=n;}sha_end(&s,out);return 1;}
static int inline_record(pa_card*c,const char*path,unsigned side,xm5_art_record*out){int fd=-1,result=XM5_ART_INVALID;struct stat64 before={0},after={0};struct xm5_block block;u8 size[4];unsigned slot,capacity,bytes,at;sha key;
 memset(out,0,sizeof(*out));if(!inline_path(path)||strncmp(path,"xm5/music/",10)||!pa_card_current(c))return XM5_ART_MISS;
 fd=pa_regular_open(c->root,path);if(fd<0)return XM5_ART_MISS;if(P_STAT(fd,&before))goto done;int r=inline_find(fd,&block);if(r<0)goto done;if(!r){result=XM5_ART_NONE;out->no_art=1;goto done;}
 if(side<=80){side=80;slot=131076;capacity=12288;}else if(side<=160){side=160;slot=98308;capacity=32768;}else {side=330;slot=4;capacity=98304;}
 if(block.header<0||(uint64_t)block.header+4+slot>0xffffffffu)goto done;at=(unsigned)block.header+4+slot;if(!read_at(fd,at,size,4))goto done;bytes=le32(size);if(!bytes){out->no_art=1;result=XM5_ART_NONE;goto done;}if(bytes>capacity-4||!inline_jpeg_header(fd,at+4,bytes,side)||!inline_span_hash(fd,at+4,bytes,out->content_sha256))goto done;
 if(P_STAT(fd,&after)||!same_identity(&before,&after)||!pa_card_current(c))goto done;
 copystr(out->path,sizeof(out->path),path);out->side=side;out->info_offset=at+4;out->info_length=bytes;out->encoding=1;cp(out->source_identity,&before,sizeof(before));
 sha_init(&key);sha_add(&key,c->root_path,strlen(c->root_path)+1);sha_add(&key,&c->mount_id,sizeof(c->mount_id));sha_add(&key,c->identity,sizeof(struct stat64));sha_add(&key,path,strlen(path)+1);sha_add(&key,&side,sizeof(side));inline_stat_key(&key,&before);sha_add(&key,out->content_sha256,64);sha_end(&key,out->sha256);
 inline_store(out);result=0;
 done:pa_close(fd);return result;
}
static int inline_playlist_cover(pa_card*c,const char*stem,unsigned side,xm5_art_record*out){char path[1024]="xm5/Struct/playlist-covers/",number[16];struct stat64 before={0},after={0};sha key;int fd,result=XM5_ART_INVALID;
 memset(out,0,sizeof(*out));if(!inline_path(stem)||strchr(stem,'/')||strlen(stem)>800)return XM5_ART_INVALID;side=side<=80?80:side<=160?160:330;copystr(path+strlen(path),sizeof(path)-strlen(path),stem);copystr(path+strlen(path),sizeof(path)-strlen(path),".");decimal(number,side);copystr(path+strlen(path),sizeof(path)-strlen(path),number);copystr(path+strlen(path),sizeof(path)-strlen(path),".jpg");
 fd=pa_regular_open(c->root,path);if(fd<0){out->no_art=1;return XM5_ART_NONE;}if(P_STAT(fd,&before)||before.st_size<4||before.st_size>(side==80?12284:side==160?32764:98300)||!inline_jpeg_header(fd,0,(unsigned)before.st_size,side)||!inline_span_hash(fd,0,(unsigned)before.st_size,out->content_sha256)||P_STAT(fd,&after)||!same_identity(&before,&after)||!pa_card_current(c))goto done;
 copystr(out->path,sizeof(out->path),path);out->side=side;out->info_offset=0;out->info_length=(unsigned)before.st_size;out->encoding=1;cp(out->source_identity,&before,sizeof(before));sha_init(&key);sha_add(&key,c->root_path,strlen(c->root_path)+1);sha_add(&key,&c->mount_id,sizeof(c->mount_id));sha_add(&key,c->identity,sizeof(struct stat64));sha_add(&key,path,strlen(path)+1);sha_add(&key,&side,sizeof(side));inline_stat_key(&key,&before);sha_add(&key,out->content_sha256,64);sha_end(&key,out->sha256);inline_store(out);result=0;
 done:pa_close(fd);return result;
}
static int inline_lookup_size(pa_card*c,const char*role,const char*subject,unsigned side,xm5_art_record*out){if(!subject||pa_size_slot(side)<0||!pa_card_current(c))return XM5_ART_INVALID;
 if(!strcmp(role,"playback"))return inline_record(c,subject,side,out);
 if(!strcmp(role,"playlist"))return inline_playlist_cover(c,subject,side,out);
 if(!strcmp(role,"directory")){char line[1027],prefix[1024];if(!inline_path(subject)||copystr(prefix,sizeof(prefix),subject))return XM5_ART_INVALID;unsigned n=(unsigned)strlen(prefix);if(n+1>=sizeof(prefix))return XM5_ART_INVALID;prefix[n++]='/';prefix[n]=0;
  inline_lines s;memset(&s,0,sizeof(s));s.fd=c->index;s.total=c->index_bytes;s.pos=inline_lower(c,prefix);int r;while((r=inline_line(&s,line,sizeof(line)))>0){if(!inline_hint_line(line))return XM5_ART_INVALID;if(strncmp(line+2,prefix,n))break;int result=inline_record(c,line+2,side,out);if(result==0)return 0;}if(r<0)return XM5_ART_INVALID;memset(out,0,sizeof(*out));out->no_art=1;return XM5_ART_NONE;}
 memset(out,0,sizeof(*out));return XM5_ART_MISS;
}
static int inline_asset(pa_card*c,const char*name,xm5_art_record*out){unsigned i;char base[48];const char*p=name,*q;if(!name||!pa_card_current(c))return XM5_ART_MISS;for(q=name;*q;q++)if(*q=='/')p=q+1;if(strlen(p)!=47||strcmp(p+43,".bmp"))return XM5_ART_INVALID;
 for(i=0;i<INLINE_RECORDS;i++){xm5_art_record*r=&inline_records[i];if(!r->encoding)continue;name64(base,r->sha256);if(!strncmp(p,base,43)){*out=*r;return 0;}}return XM5_ART_MISS;
}
static int inline_matches(const char*name,const void*data,unsigned bytes){const char*p=name,*q;char base[48],hash[65];unsigned i;if(!name)return 0;for(q=name;*q;q++)if(*q=='/')p=q+1;if(strlen(p)!=47||strcmp(p+43,".bmp"))return 0;
 for(i=0;i<INLINE_RECORDS;i++){xm5_art_record*r=&inline_records[i];if(r->encoding&&r->info_length==bytes){name64(base,r->sha256);if(!strncmp(base,p,43)){pa_sha256(data,bytes,hash);return !strcmp(hash,r->content_sha256);}}}return 0;
}
static int inline_read(pa_card*c,const xm5_art_record*r,void*buffer,unsigned bytes){struct stat64 before,after;int fd,ok;char hash[65];if(!r||r->encoding!=1||!buffer||bytes!=r->info_length||bytes>98300||!pa_card_current(c))return XM5_ART_INVALID;
 fd=pa_regular_open(c->root,r->path);if(fd<0)return PA_IO;cp(&before,r->source_identity,sizeof(before));ok=!P_STAT(fd,&after)&&same_identity(&before,&after)&&inline_jpeg_header(fd,r->info_offset,bytes,r->side)&&read_at(fd,r->info_offset,buffer,bytes)&&!P_STAT(fd,&after)&&same_identity(&before,&after);pa_close(fd);if(!ok)return XM5_ART_INVALID;pa_sha256(buffer,bytes,hash);return !strcmp(hash,r->content_sha256)&&pa_card_current(c)?0:PA_CHANGED;
}
static int inline_check(pa_card*c,const xm5_art_record*r){struct stat64 before,after;char hash[65];int fd,ok;if(!r||r->encoding!=1||!pa_card_current(c))return XM5_ART_INVALID;fd=pa_regular_open(c->root,r->path);if(fd<0)return PA_IO;cp(&before,r->source_identity,sizeof(before));ok=!P_STAT(fd,&after)&&same_identity(&before,&after)&&inline_jpeg_header(fd,r->info_offset,r->info_length,r->side)&&inline_span_hash(fd,r->info_offset,r->info_length,hash)&&!strcmp(hash,r->content_sha256)&&!P_STAT(fd,&after)&&same_identity(&before,&after);pa_close(fd);return ok&&pa_card_current(c)?0:PA_CHANGED;}
static int inline_fields(pa_card*c,const char*subject,int*source,struct xm5_block*b,struct stat64*st){if(!inline_path(subject)||strncmp(subject,"xm5/music/",10)||!pa_card_current(c))return 0;int fd=pa_regular_open(c->root,subject);if(fd<0)return 0;if(P_STAT(fd,st)||inline_find(fd,b)!=1||(uint64_t)b->header+b->size+4>0xffffffffu){pa_close(fd);return 0;}*source=fd;return 1;}
static int inline_setting(const char*s,unsigned n,const char*key,int*value){unsigned k=(unsigned)strlen(key),i;if(n<=k||strncmp(s,key,k)||s[k]!='=')return 0;i=k+1;int sign=1;if(s[i]=='-'){sign=-1;i++;}if(i==n)return 0;int v=0;for(;i<n;i++){if(s[i]<'0'||s[i]>'9'||v>100000)return 0;v=v*10+s[i]-'0';}*value=sign*v;return 1;}
static int inline_gain(pa_card*c,const char*subject,float*out){struct xm5_block b;struct stat64 before,after;u8 level[4];int fd=-1,mid=0,lim=4000;*out=0;if(!inline_fields(c,subject,&fd,&b,&before))return XM5_ART_MISS;int ok=read_at(fd,(unsigned)b.header+4+143364,level,4)&&!P_STAT(fd,&after)&&same_identity(&before,&after);pa_close(fd);if(!ok)return XM5_ART_INVALID;int peak=(int)le32(level);if(peak==(-2147483647-1))return 0;if(peak< -240000||peak>24000)return XM5_ART_INVALID;
 fd=pa_regular_open(c->meta,"settings.ini");if(fd>=0){char text[512];int n=pa_read(fd,text,sizeof(text));pa_close(fd);if(n>0&&n<(int)sizeof(text)){unsigned start=0,i;for(i=0;i<=(unsigned)n;i++)if(i==(unsigned)n||text[i]=='\n'){unsigned len=i-start;if(len&&text[start+len-1]=='\r')len--;int v;if(inline_setting(text+start,len,"gain_midpoint_mdb",&v)&&v>=-60000&&v<=0)mid=v;if(inline_setting(text+start,len,"gain_limit_mdb",&v)&&v>=0&&v<=12000)lim=v;start=i+1;}}}
 int gain=mid-peak;if(gain>lim)gain=lim;if(gain< -lim)gain=-lim;*out=(float)gain/1000.0f;return pa_card_current(c)?0:PA_CHANGED;
}
static int inline_utf8(const u8*b,unsigned n,unsigned*need,unsigned*code,unsigned*minimum){unsigned i;for(i=0;i<n;i++){unsigned c=b[i];if(*need){if((c&0xc0)!=0x80)return 0;*code=(*code<<6)|(c&63);if(!--*need&&(*code<*minimum||*code>0x10ffff||(*code>=0xd800&&*code<=0xdfff)))return 0;}else if(!c)return 0;else if(c<128)continue;else if(c>=0xc2&&c<=0xdf){*need=1;*code=c&31;*minimum=0x80;}else if(c>=0xe0&&c<=0xef){*need=2;*code=c&15;*minimum=0x800;}else if(c>=0xf0&&c<=0xf4){*need=3;*code=c&7;*minimum=0x10000;}else return 0;}return 1;}
static int inline_lyric(pa_card*c,const char*subject,unsigned slot,char*out,unsigned cap){struct xm5_block b;struct stat64 before,after;u8 buffer[1024],bom[3]={239,187,191};char path[32]="/tmp/xm5lrc0.lrc",tmp[40];unsigned at,n,pos=0,need=0,code=0,minimum=0;int source=-1,dest=-1,result=XM5_ART_INVALID;
 if(slot>=8||cap<17||!inline_fields(c,subject,&source,&b,&before))return XM5_ART_MISS;n=b.size-XM5_APP_FIXED;if(!n){pa_close(source);return XM5_ART_MISS;}if(n>XM5_LYRIC_MAX)goto done;at=(unsigned)b.header+4+XM5_APP_FIXED;path[11]=(char)('0'+slot);copystr(tmp,sizeof(tmp),path);copystr(tmp+strlen(tmp),sizeof(tmp)-strlen(tmp),".part");
 /* Only replace our own regular staging file, never follow a link. */
 dest=(int)P_OPENAT(AT_FDCWD,tmp,O_PATH|O_NOFOLLOW|O_CLOEXEC,0);if(dest>=0){int reg=!P_STAT(dest,&after)&&(after.st_mode&S_IFMT)==S_IFREG;pa_close(dest);dest=-1;if(!reg)goto done;(void)P_UNLINK(tmp);}
 dest=(int)P_OPENAT(AT_FDCWD,tmp,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(dest<0)goto done;
 if(!read_at(source,at,buffer,n<3?n:3))goto cleanup;if(n<3||!same(buffer,bom,3))if(!writeall(dest,bom,3))goto cleanup;
 while(pos<n){unsigned k=n-pos;if(k>sizeof(buffer))k=sizeof(buffer);if(!read_at(source,at+pos,buffer,k)||!inline_utf8(buffer,k,&need,&code,&minimum)||!writeall(dest,buffer,k))goto cleanup;pos+=k;}
 if(need||P_STAT(source,&after)||!same_identity(&before,&after)||!pa_card_current(c))goto cleanup;pa_close(dest);dest=-1;if(P_RENAME(tmp,path)<0)goto cleanup;copystr(out,cap,path);result=0;goto done;
 cleanup:pa_close(dest);dest=-1;(void)P_UNLINK(tmp);
 done:pa_close(source);pa_close(dest);return result;
}
