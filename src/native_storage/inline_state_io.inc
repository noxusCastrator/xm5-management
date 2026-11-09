/* Inline-card playlist exchange. Include after state_io.inc. The source M3U8
 * files are authoritative; native-playlists.tsv is only an id/path binding.
 * All mutations are relative to the pinned Struct descriptor. No audio files
 * or unrelated card files are opened for writing. */
#define IP_MAX 100
#define IP_FILE_LIMIT (16u*1024u*1024u)
typedef struct {
 unsigned id, deleted, seen, bound;
 char file[256], name[256], digest[65];
} ip_binding;
static ip_binding ip_bindings[IP_MAX];
static unsigned ip_count, ip_loaded;
static unsigned ip_favorite_bound;
static char ip_favorite_digest[65];
static char ip_stage_live[80];
static int ip_playlist_dir(pa_card*c){int d;
 if(!pa_card_current(c)||c->info_mode!=2)return -1;
 d=(int)P_OPENAT(c->meta,"playlists",DIRFLAGS,0);
 if(d<0){(void)P_MKDIRAT(c->meta,"playlists");d=(int)P_OPENAT(c->meta,"playlists",DIRFLAGS,0);}
 return d;
}
static int ip_name(const char*s){unsigned i,n;if(!s||!(n=(unsigned)strlen(s))||n>255||!state_text(s,255))return 0;
 for(i=0;i<n;i++)if((u8)s[i]<32||s[i]==127||s[i]=='/'||s[i]=='\\'||s[i]==':')return 0;
 return strcmp(s,".")&&strcmp(s,"..");
}
static int ip_label(const char*s){unsigned i,n;if(!s||!(n=(unsigned)strlen(s))||n>255||!state_text(s,255))return 0;for(i=0;i<n;i++)if((u8)s[i]<32||s[i]==127)return 0;return 1;}
static int ip_file(const char*s){unsigned n;if(!ip_name(s)||(n=(unsigned)strlen(s))<6||s[0]=='.'||strcmp(s+n-5,".m3u8"))return 0;return 1;}
/* Canonicalize a relative path against the playlist parent, rejecting escape.
 * This is textual only: missing tracks remain editable and are not dropped. */
int inline_playlist_resolve(const char*reference,char*out,unsigned cap){
 char tmp[XM5_ART_PATH];unsigned n=0,i=0,start,len;
 if(!reference||!reference[0]||reference[0]=='/'||reference[0]=='\\'||!state_text(reference,XM5_ART_PATH-1))return 0;
 if(copystr(tmp,sizeof(tmp),"xm5/Struct/playlists"))return 0;n=20;
 while(reference[i]){start=i;while(reference[i]&&reference[i]!='/'&&reference[i]!='\\'){u8 ch=(u8)reference[i];if(ch<32||ch==127||ch==':')return 0;i++;}len=i-start;
  if(!len||(len==1&&reference[start]=='.')){}
  else if(len==2&&reference[start]=='.'&&reference[start+1]=='.'){if(!n)return 0;while(n&&tmp[n-1]!='/')n--;if(n)n--;tmp[n]=0;}
  else {if(n+!!n+len>=sizeof(tmp))return 0;if(n)tmp[n++]='/';cp(tmp+n,reference+start,len);n+=len;tmp[n]=0;}
  if(reference[i])i++;
 }
 return n&&!copystr(out,cap,tmp);
}
static int ip_track(const char*in,char*out,unsigned cap){const char*p=in;unsigned i;if(!p)return 0;
 if(!strncmp(p,"file://",7))p+=7;
 if(!strncmp(p,"/mnt/mmc/",9))p+=9;
 else if(p[0]=='A'&&p[1]==':'&&(p[2]=='/'||p[2]=='\\'))p+=3;
 if(!p[0]||p[0]=='/'||!state_text(p,XM5_ART_PATH-1)||copystr(out,cap,p))return 0;
 for(i=0;out[i];i++){if(out[i]=='\\')out[i]='/';if((u8)out[i]<32||out[i]==127||out[i]==':')return 0;}
 /* Reject noncanonical/escaping SQL paths. No renaming or guessing. */
 p=out;while(*p){const char*begin=p;while(*p&&*p!='/')p++;unsigned n=(unsigned)(p-begin);if(!n||(n==1&&begin[0]=='.')||(n==2&&begin[0]=='.'&&begin[1]=='.'))return 0;if(*p)p++;}
 return 1;
}
static int ip_line(int fd,char*out,unsigned cap,unsigned*offset,unsigned limit){unsigned n=0,first=*offset==0;u8 chunk[256];int r;unsigned i,want;
 while(*offset<limit){want=limit-*offset;if(want>sizeof(chunk))want=sizeof(chunk);r=(int)P_PREAD(fd,chunk,want,*offset);if(r<=0)return -1;
  for(i=0;i<(unsigned)r;i++){u8 ch=chunk[i];(*offset)++;if(ch=='\n')goto complete;if(!ch||n+1>=cap)return -1;out[n++]=(char)ch;}}
 if(!n)return 0;
 complete:if(n&&out[n-1]=='\r')n--;if(first&&n>=3&&(u8)out[0]==0xef&&(u8)out[1]==0xbb&&(u8)out[2]==0xbf){cp(out,out+3,n-3);n-=3;}out[n]=0;return 1;
}
static ip_binding*ip_find(unsigned id){unsigned i;for(i=0;i<ip_count;i++)if(ip_bindings[i].id==id)return ip_bindings+i;return 0;}
void inline_playlist_abandon(unsigned id){unsigned i,j;for(i=0;i<ip_count;i++)if(ip_bindings[i].id==id){for(j=i+1;j<ip_count;j++)ip_bindings[j-1]=ip_bindings[j];ip_count--;return;}}
void inline_state_reset(void){ip_count=ip_loaded=ip_favorite_bound=0;ip_favorite_digest[0]=0;if(ip_stage_live[0]){(void)P_UNLINK(ip_stage_live);ip_stage_live[0]=0;}}
static void ip_load(pa_card*c){int d,fd;struct stat64 st;unsigned off=0;char line[700];if(ip_loaded)return;ip_loaded=1;ip_count=0;
 d=state_dir(c);if(d<0)return;fd=pa_regular_open(d,"native-playlists.tsv");pa_close(d);if(fd<0)return;
 if(P_STAT(fd,&st)||st.st_size>70000||ip_line(fd,line,sizeof(line),&off,(unsigned)st.st_size)!=1||strcmp(line,"XM5PL2"))goto done;
 while(ip_count<IP_MAX&&ip_line(fd,line,sizeof(line),&off,(unsigned)st.st_size)==1){ip_binding b;char *p=line,*q;unsigned id=0;memset(&b,0,sizeof(b));
  if(p[0]=='F'&&p[1]=='\t'&&hash_ok(p+2)){copystr(ip_favorite_digest,sizeof(ip_favorite_digest),p+2);ip_favorite_bound=1;continue;}
  if(*p<'0'||*p>'9')break;while(*p>='0'&&*p<='9'){if(id>10000000u)goto done;id=id*10+(unsigned)(*p++-'0');}if(*p++!='\t')break;b.id=id;
  q=strchr(p,'\t');if(!q)break;*q++=0;if(!hash_ok(p))break;copystr(b.digest,sizeof(b.digest),p);p=q;
  q=strchr(p,'\t');if(!q)break;*q++=0;if(!ip_file(p)||!strcmp(p,"Favorites.m3u8")||!ip_label(q))break;
  if(ip_find(id))break;copystr(b.file,sizeof(b.file),p);copystr(b.name,sizeof(b.name),q);b.bound=1;ip_bindings[ip_count++]=b;
 }
 done:pa_close(fd);
}
static int ip_map_save(pa_card*c){int d,fd=-1,ok=0;char tmp[64]=".native-map-",pid[16];unsigned i;ip_binding*b;
 d=state_dir(c);if(d<0)return 0;decimal(pid,(unsigned)P_GETPID());copystr(tmp+12,sizeof(tmp)-12,pid);copystr(tmp+strlen(tmp),sizeof(tmp)-strlen(tmp),".part");
 fd=(int)P_OPENAT(d,tmp,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(fd<0)goto done;
 if(!writeall(fd,"XM5PL2\n",7))goto done;
 if(ip_favorite_bound&&(!writeall(fd,"F\t",2)||!writeall(fd,ip_favorite_digest,64)||!writeall(fd,"\n",1)))goto done;
 for(i=0;i<ip_count;i++){char num[16];b=ip_bindings+i;if(b->deleted||!b->bound||!hash_ok(b->digest))continue;decimal(num,b->id);
  if(!writeall(fd,num,(unsigned)strlen(num))||!writeall(fd,"\t",1)||!writeall(fd,b->digest,64)||!writeall(fd,"\t",1)||!writeall(fd,b->file,(unsigned)strlen(b->file))||!writeall(fd,"\t",1)||!writeall(fd,b->name,(unsigned)strlen(b->name))||!writeall(fd,"\n",1))goto done;
 }
 if(P_FSYNC(fd)||!pa_card_current(c)||P_RENAMEAT(d,tmp,"native-playlists.tsv"))goto done;(void)P_FSYNC(d);ok=1;
 done:pa_close(fd);if(!ok&&fd>=0)(void)P_UNLINKAT(d,tmp);pa_close(d);return ok;
}
/* Called only after the stock importer has associated this exact source file
 * with a native id. Matching display names alone must never create a binding. */
int inline_playlist_bind(pa_card*c,unsigned id,const char*name,const char*path){const char*p;int d,fd;ip_binding*b;char digest[65];unsigned i;
 if(!c||c->info_mode!=2||!pa_card_current(c)||!ip_label(name)||!path||strncmp(path,"xm5/Struct/playlists/",21))return PA_UNMANAGED;
 p=path+21;if(!ip_file(p)||!strcmp(p,"Favorites.m3u8"))return XM5_ART_INVALID;ip_load(c);b=ip_find(id);
 for(i=0;i<ip_count;i++)if(ip_bindings[i].id!=id&&!strcmp(ip_bindings[i].file,p))return XM5_ART_INVALID;
 d=ip_playlist_dir(c);if(d<0)return PA_IO;fd=pa_regular_open(d,p);pa_close(d);if(fd<0)return PA_IO;
 int ok=digest_fd(fd,IP_FILE_LIMIT,digest)&&pa_card_current(c);pa_close(fd);if(!ok)return PA_CHANGED;
 if(!b){if(ip_count==IP_MAX)return PA_LIMIT;b=ip_bindings+ip_count++;memset(b,0,sizeof(*b));b->id=id;}
 copystr(b->file,sizeof(b->file),p);copystr(b->name,sizeof(b->name),name);copystr(b->digest,sizeof(b->digest),digest);b->bound=1;b->deleted=0;
 return ip_map_save(c)?0:PA_IO;
}
void inline_playlist_created(pa_card*c,unsigned id,const char*name){ip_binding*b;char hash[65],num[16];if(!ip_label(name)||!pa_card_current(c))return;ip_load(c);b=ip_find(id);
 if(!b){if(ip_count==IP_MAX)return;b=ip_bindings+ip_count++;}memset(b,0,sizeof(*b));b->id=id;copystr(b->name,sizeof(b->name),name);
 pa_sha256(name,strlen(name),hash);hash[16]=0;decimal(num,id);copystr(b->file,sizeof(b->file),"Native-");copystr(b->file+7,sizeof(b->file)-7,num);copystr(b->file+strlen(b->file),sizeof(b->file)-strlen(b->file),"-");copystr(b->file+strlen(b->file),sizeof(b->file)-strlen(b->file),hash);copystr(b->file+strlen(b->file),sizeof(b->file)-strlen(b->file),".m3u8");
}
void inline_playlist_deleted(pa_card*c,unsigned id){if(!pa_card_current(c))return;ip_load(c);ip_binding*b=ip_find(id);if(b)b->deleted=1;}
void inline_playlist_renamed(pa_card*c,unsigned id,const char*name){if(!ip_label(name)||!pa_card_current(c))return;ip_load(c);ip_binding*b=ip_find(id);if(b)copystr(b->name,sizeof(b->name),name);}
int inline_playlist_subject(pa_card*c,unsigned id,const char*name,char*out,unsigned cap){ip_load(c);ip_binding*b=ip_find(id);(void)name;if(!b||b->deleted||!b->bound)return XM5_ART_MISS;unsigned n=(unsigned)strlen(b->file)-5;if(n>=cap)return XM5_ART_MISS;cp(out,b->file,n);out[n]=0;return 0;}
static const char ip_pending_text[]="{\"schema\":\"xm5.inline-state.pending\",\"version\":1}\n";
int inline_state_pending(pa_card*c){int d,fd,ok=0;char data[sizeof(ip_pending_text)];struct stat64 st;
 if(c->info_mode!=2)return PA_UNMANAGED;if(!pa_card_current(c))return PA_CHANGED;d=state_dir(c);if(d<0)return PA_IO;
 fd=pa_regular_open(d,"state.pending");if(fd>=0){ok=!P_STAT(fd,&st)&&st.st_size==sizeof(ip_pending_text)-1&&read_at(fd,0,data,sizeof(ip_pending_text)-1)&&!state_cmp(data,ip_pending_text,sizeof(ip_pending_text)-1);pa_close(fd);pa_close(d);return ok?0:PA_IO;}
 fd=(int)P_OPENAT(d,"state.pending",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(fd>=0){ok=writeall(fd,ip_pending_text,sizeof(ip_pending_text)-1)&&!P_FSYNC(fd)&&pa_card_current(c);pa_close(fd);(void)P_FSYNC(d);}pa_close(d);return ok?0:PA_IO;
}

typedef struct {ip_binding*b;char file[256],tmp[80],expected[65],digest[65];unsigned existed,owned;} ip_output;
typedef struct {pa_card*c;void*db;pa_sql_exec exec;int dir,fd,failed;unsigned current,has_current,skip_current,favorites,count,total;ip_output files[IP_MAX+1];sha hash;} ip_writer;
static int ip_expect(int d,const char*file,char digest[65]){int fd=pa_regular_open(d,file);if(fd<0){/* Distinguish an absent name from a symlink/special node. */int node=(int)P_OPENAT(d,file,O_PATH|O_NOFOLLOW|O_CLOEXEC,0);if(node>=0){pa_close(node);return -1;}return 0;}int ok=digest_fd(fd,IP_FILE_LIMIT,digest);pa_close(fd);return ok?1:-1;}
static int ip_emit(ip_writer*w,const char*s){unsigned n=(unsigned)strlen(s);if(w->total+n>IP_FILE_LIMIT||!writeall(w->fd,s,n)){w->failed=1;return 0;}w->total+=n;sha_add(&w->hash,s,n);return 1;}
static int ip_finish(ip_writer*w){if(w->fd<0)return 1;if(P_FSYNC(w->fd)){w->failed=1;return 0;}sha_end(&w->hash,w->files[w->count-1].digest);pa_close(w->fd);w->fd=-1;return 1;}
static int ip_begin(ip_writer*w,ip_binding*b){ip_output*o;char num[16],pid[16];unsigned i;if(!ip_finish(w)||w->count>=IP_MAX+1)return 0;o=w->files+w->count;memset(o,0,sizeof(*o));o->b=b;
 copystr(o->file,sizeof(o->file),b?b->file:"Favorites.m3u8");int exists=ip_expect(w->dir,o->file,o->expected);if(exists<0)return 0;o->existed=(unsigned)exists;
 if(!b&&ip_favorite_bound){if(!exists||strcmp(o->expected,ip_favorite_digest))return 0;}
 else if(!b&&exists)return 0; /* Import PC favorites before the first export. */
 if(b&&b->bound&&(!exists||strcmp(o->expected,b->digest))){
  /* Keep the computer copy, publish the native snapshot as a separate list.
   * It remains an ordinary visible M3U8 so desktop conflict handling can offer
   * both versions. No attempt is made to auto-merge user edits. */
  char stem[256],id[16];decimal(id,b->id);copystr(stem,sizeof(stem),"NativeConflict-");copystr(stem+strlen(stem),sizeof(stem)-strlen(stem),id);copystr(stem+strlen(stem),sizeof(stem)-strlen(stem),"-");
  const char*change=exists?o->expected:b->digest;unsigned at=(unsigned)strlen(stem);cp(stem+at,change,16);stem[at+16]=0;
  for(i=0;i<1000;i++){copystr(o->file,sizeof(o->file),stem);if(i){decimal(num,i);copystr(o->file+strlen(o->file),sizeof(o->file)-strlen(o->file),"-");copystr(o->file+strlen(o->file),sizeof(o->file)-strlen(o->file),num);}copystr(o->file+strlen(o->file),sizeof(o->file)-strlen(o->file),".m3u8");exists=ip_expect(w->dir,o->file,o->expected);if(exists<0)return 0;if(!exists)break;}if(exists)return 0;o->existed=0;
 }else if(b&&!b->bound&&exists){
  /* A brand-new native list never adopts an unrelated PC file. A collision
   * uses a free deterministic suffix. Favorites are bound on first export. */
  if(b){char stem[256];copystr(stem,sizeof(stem),o->file);unsigned n=(unsigned)strlen(stem);stem[n-5]=0;
   for(i=1;i<1000;i++){decimal(num,i);copystr(o->file,sizeof(o->file),stem);copystr(o->file+strlen(o->file),sizeof(o->file)-strlen(o->file),"-");copystr(o->file+strlen(o->file),sizeof(o->file)-strlen(o->file),num);copystr(o->file+strlen(o->file),sizeof(o->file)-strlen(o->file),".m3u8");exists=ip_expect(w->dir,o->file,o->expected);if(exists<0)return 0;if(!exists)break;}if(exists)return 0;o->existed=0;}
 }
 decimal(pid,(unsigned)P_GETPID());decimal(num,w->count);copystr(o->tmp,sizeof(o->tmp),".xm5-native-");copystr(o->tmp+12,sizeof(o->tmp)-12,pid);copystr(o->tmp+strlen(o->tmp),sizeof(o->tmp)-strlen(o->tmp),"-");copystr(o->tmp+strlen(o->tmp),sizeof(o->tmp)-strlen(o->tmp),num);copystr(o->tmp+strlen(o->tmp),sizeof(o->tmp)-strlen(o->tmp),".part");
 w->fd=(int)P_OPENAT(w->dir,o->tmp,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(w->fd<0)return 0;o->owned=1;w->count++;w->total=0;sha_init(&w->hash);
 if(!ip_emit(w,"#EXTM3U\n#PLAYLIST:")||!ip_emit(w,b?b->name:"Favorites")||!ip_emit(w,"\n"))return 0;return 1;
}
static int ip_row(void*context,int n,char**v,char**columns){ip_writer*w=context;unsigned band,kind,pl=0;const char*p;char track[XM5_ART_PATH];(void)columns;
 if(n!=6||!v[0]||!v[1]||!v[2]||!v[3]||!v[4]||!v[5]||!state_text(v[4],XM5_ART_PATH-1)||!state_text(v[5],255))return w->failed=1;
 band=(unsigned)(v[0][0]-'0');kind=(unsigned)(v[2][0]-'0');p=v[1];while(*p>='0'&&*p<='9'){if(pl>10000000u)return w->failed=1;pl=pl*10+(unsigned)(*p++-'0');}if(*p||band>1||kind>1)return w->failed=1;
 if(band&&kind==0){ip_binding*b=ip_find(pl);w->current=pl;w->has_current=1;w->skip_current=!b;if(!b)return 0;b->seen=1;b->deleted=0;if(ip_label(v[5])&&!copystr(b->name,sizeof(b->name),v[5])){}else if(v[5][0])return w->failed=1;
  if(!ip_begin(w,b))return w->failed=1;w->current=pl;w->has_current=1;return 0;
 }
 if(band&&(!w->has_current||w->current!=pl))return w->failed=1;
 if((band&&w->skip_current)||(!band&&!w->favorites))return 0;
 if(!ip_track(v[4],track,sizeof(track)))return w->failed=1;
 if(!band){/* Deduplicate favorites using the already staged file, not a
            * fixed-size hash table with a silent overflow limit. */
  int fd=pa_regular_open(w->dir,w->files[0].tmp);if(fd<0)return w->failed=1;char line[XM5_ART_PATH+20];unsigned off=0;struct stat64 st;int duplicate=0;if(P_STAT(fd,&st)){pa_close(fd);return w->failed=1;}
  while(ip_line(fd,line,sizeof(line),&off,(unsigned)st.st_size)>0)if(!strncmp(line,"../../../",9)&&!strcmp(line+9,track)){duplicate=1;break;}pa_close(fd);if(duplicate)return 0;
 }
 return ip_emit(w,"../../../")&&ip_emit(w,track)&&ip_emit(w,"\n")?0:(w->failed=1);
}
static int ip_publish(ip_writer*w,ip_output*o){char now[65];int exists=ip_expect(w->dir,o->file,now);if(exists<0||exists!=(int)o->existed||(exists&&strcmp(now,o->expected))||!pa_card_current(w->c))return 0;
 if(P_RENAMEAT(w->dir,o->tmp,o->file))return 0;o->owned=0;if(o->b){copystr(o->b->file,sizeof(o->b->file),o->file);copystr(o->b->digest,sizeof(o->b->digest),o->digest);o->b->bound=1;}else {ip_favorite_bound=1;copystr(ip_favorite_digest,sizeof(ip_favorite_digest),o->digest);}
 return 1;
}
/* The native scanner omits missing references. Keep those original occurrences
 * in the editing file, at their previous ordinal positions. Existing native
 * rows keep the new native order; native deletion of a present song still wins.
 * This runs over bounded streams rather than retaining the entire playlist. */
static int ip_spool_contains(int fd,unsigned bytes,const char*path){unsigned off=0;char line[XM5_ART_PATH+20],canonical[XM5_ART_PATH];int r;
 while((r=ip_line(fd,line,sizeof(line),&off,bytes))>0)if(line[0]&&line[0]!='#'&&inline_playlist_resolve(line,canonical,sizeof(canonical))&&!strcmp(canonical,path))return 1;return r<0?-1:0;
}
static int ip_song_found(void*context,int n,char**v,char**columns){(void)columns;if(n==1&&v[0]&&v[0][0]=='1')*(int*)context=1;return 0;}
/* A real file which has not reached songtable is just as unavailable to the
 * native importer as a missing file. Preserve its source reference until a
 * later scan, rather than mistaking the importer's omission for a user edit. */
static int ip_song_available(ip_writer*w,const char*path){char sql[2300],absolute[XM5_ART_PATH+16];unsigned n,i;int found=0;
 int fd=pa_regular_open(w->c->root,path);if(fd<0)return 0;pa_close(fd);
 if(!w->db||!w->exec||copystr(absolute,sizeof(absolute),"/mnt/mmc/")||copystr(absolute+9,sizeof(absolute)-9,path))return -1;
 copystr(sql,sizeof(sql),"SELECT 1 FROM songtable WHERE fileurl='");n=(unsigned)strlen(sql);
 for(i=0;absolute[i];i++){if(n+16>=sizeof(sql))return -1;sql[n++]=absolute[i];if(absolute[i]=='\'')sql[n++]='\'';}
 copystr(sql+n,sizeof(sql)-n,"' LIMIT 1");return w->exec(w->db,sql,ip_song_found,&found,0)?-1:found;
}
static int ip_restore_missing(ip_writer*w,ip_output*o){int old=-1,spool=-1,dest=-1,ok=0,r;struct stat64 oldst,newst;unsigned oldoff=0,newoff=0,position=0,emitted=0,changed=0,created=0;char oldline[XM5_ART_PATH+20],newline[XM5_ART_PATH+20],canonical[XM5_ART_PATH],temp[100],hash[65];
 if(!o->existed)return 1;old=pa_regular_open(w->dir,o->file);spool=pa_regular_open(w->dir,o->tmp);if(old<0||spool<0||P_STAT(old,&oldst)||P_STAT(spool,&newst)||oldst.st_size>IP_FILE_LIMIT||newst.st_size>IP_FILE_LIMIT||!digest_fd(old,IP_FILE_LIMIT,hash)||strcmp(hash,o->expected))goto done;
 copystr(temp,sizeof(temp),o->tmp);copystr(temp+strlen(temp),sizeof(temp)-strlen(temp),".merge");dest=(int)P_OPENAT(w->dir,temp,O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(dest<0)goto done;created=1;
 /* Copy the generated header first, leaving its first entry pending. */
 unsigned pending=0;while((r=ip_line(spool,newline,sizeof(newline),&newoff,(unsigned)newst.st_size))>0){if(newline[0]&&newline[0]!='#'){pending=1;break;}if(!writeall(dest,newline,(unsigned)strlen(newline))||!writeall(dest,"\n",1))goto done;}if(r<0)goto done;
 while((r=ip_line(old,oldline,sizeof(oldline),&oldoff,(unsigned)oldst.st_size))>0){char*p=oldline;if(oldoff==(unsigned)strlen(oldline)+1&&strlen(oldline)>=3&&(u8)p[0]==0xef&&(u8)p[1]==0xbb&&(u8)p[2]==0xbf)p+=3;
  if(!p[0]||p[0]=='#')continue;if(!inline_playlist_resolve(p,canonical,sizeof(canonical)))goto done;int available=ip_song_available(w,canonical);if(available<0)goto done;if(available){position++;continue;}
  int present=ip_spool_contains(spool,(unsigned)newst.st_size,canonical);if(present<0)goto done;if(!present&&!o->b){struct stat64 merged;if(P_STAT(dest,&merged))goto done;present=ip_spool_contains(dest,(unsigned)merged.st_size,canonical);if(present<0)goto done;}if(present){position++;continue;}
  while(emitted<position&&pending){if(!writeall(dest,newline,(unsigned)strlen(newline))||!writeall(dest,"\n",1))goto done;emitted++;r=ip_line(spool,newline,sizeof(newline),&newoff,(unsigned)newst.st_size);if(r<0)goto done;pending=r>0;}
  if(!writeall(dest,"../../../",9)||!writeall(dest,canonical,(unsigned)strlen(canonical))||!writeall(dest,"\n",1))goto done;emitted++;position++;changed=1;
 }
 if(r<0)goto done;while(pending){if(!writeall(dest,newline,(unsigned)strlen(newline))||!writeall(dest,"\n",1))goto done;r=ip_line(spool,newline,sizeof(newline),&newoff,(unsigned)newst.st_size);if(r<0)goto done;pending=r>0;}
 if(!digest_fd(old,IP_FILE_LIMIT,hash)||strcmp(hash,o->expected)||!pa_card_current(w->c)||P_FSYNC(dest))goto done;pa_close(dest);dest=-1;
 if(changed){if(P_RENAMEAT(w->dir,temp,o->tmp))goto done;int f=pa_regular_open(w->dir,o->tmp);if(f<0)goto done;ok=digest_fd(f,IP_FILE_LIMIT,o->digest);pa_close(f);}else ok=1;
 done:pa_close(dest);pa_close(spool);pa_close(old);if(created)(void)P_UNLINKAT(w->dir,temp);return ok;
}
static int ip_hide_deleted(pa_card*c,int d,ip_binding*b){char hash[65],hidden[100]=".xm5-deleted-",num[16];if(!b->bound)return 1;int exists=ip_expect(d,b->file,hash);if(!exists)return 1;if(exists<0||strcmp(hash,b->digest)||!pa_card_current(c))return 0;
 decimal(num,b->id);copystr(hidden+13,sizeof(hidden)-13,num);copystr(hidden+strlen(hidden),sizeof(hidden)-strlen(hidden),"-");cp(hidden+strlen(hidden),b->digest,16);hidden[strlen(".xm5-deleted-")+strlen(num)+1+16]=0;copystr(hidden+strlen(hidden),sizeof(hidden)-strlen(hidden),".deleted");
 int node=(int)P_OPENAT(d,hidden,O_PATH|O_NOFOLLOW|O_CLOEXEC,0);if(node>=0){pa_close(node);return 0;}
 return P_RENAMEAT(d,b->file,hidden)==0;
}
int inline_state_export(pa_card*c,void*db,pa_sql_exec exec){static ip_writer w;unsigned i;int ok=0,d=-1;if(!db||!exec||c->info_mode!=2||!pa_card_current(c))return PA_UNMANAGED;if(inline_state_pending(c))return PA_IO;
 ip_load(c);d=ip_playlist_dir(c);if(d<0)return PA_IO;memset(&w,0,sizeof(w));w.c=c;w.db=db;w.exec=exec;w.dir=d;w.fd=-1;for(i=0;i<ip_count;i++)ip_bindings[i].seen=0;
 /* An unbound myfav table may belong to the previously inserted card. Only
  * explicit Favorites import arms its export for this card generation. */
 if(ip_favorite_bound){if(!ip_begin(&w,0))goto done;w.favorites=1;}
 if(exec(db,state_query,ip_row,&w,0)||w.failed||!ip_finish(&w)||!pa_card_current(c))goto done;
 for(i=0;i<w.count;i++)if(!ip_restore_missing(&w,w.files+i))goto done;
 /* Prepare every output before publishing any. Concurrent host changes are
  * detected by the expected SHA256 and retain their contents on failure. */
 for(i=0;i<w.count;i++){char now[65];ip_output*o=w.files+i;int e=ip_expect(d,o->file,now);if(e<0||e!=(int)o->existed||(e&&strcmp(now,o->expected)))goto done;}
 for(i=0;i<w.count;i++)if(!ip_publish(&w,w.files+i))goto done;
 for(i=0;i<ip_count;i++){ip_binding*b=ip_bindings+i;if(!b->seen&&b->bound)b->deleted=1;if(b->deleted&&!ip_hide_deleted(c,d,b))goto done;}
 (void)P_FSYNC(d);if(!ip_map_save(c))goto done;ok=pa_card_current(c);if(ok){int device=state_dir(c);if(device>=0){(void)P_UNLINKAT(device,"state.pending");(void)P_FSYNC(device);pa_close(device);}}
 done:pa_close(w.fd);for(i=0;i<w.count;i++)if(w.files[i].owned)(void)P_UNLINKAT(d,w.files[i].tmp);pa_close(d);return ok?0:PA_IO;
}

/* Returns a caller-owned temporary filename. Caller must discard it after the
 * synchronous stock parser returns. Original playlist is never rewritten. */
int inline_playlist_stage(pa_card*c,const char*path,char*out,unsigned cap){int source=-1,dest=-1,result=PA_IO;struct stat64 before,after;char file[80]="/tmp/xm5-playlist-",pid[16],num[16],line[XM5_ART_PATH+20],canonical[XM5_ART_PATH],digest[65],check[65];unsigned off=0;static unsigned sequence;
 if(ip_stage_live[0])return XM5_ART_BUSY;
 if(!c||c->info_mode!=2||!pa_card_current(c)||!path||strncmp(path,"xm5/Struct/playlists/",21)||!ip_file(path+21))return PA_UNMANAGED;
 source=pa_regular_open(c->root,path);if(source<0)goto done;if(P_STAT(source,&before)||before.st_size>IP_FILE_LIMIT||!digest_fd(source,IP_FILE_LIMIT,digest))goto done;
 decimal(pid,(unsigned)P_GETPID());decimal(num,++sequence);copystr(file+18,sizeof(file)-18,pid);copystr(file+strlen(file),sizeof(file)-strlen(file),"-");copystr(file+strlen(file),sizeof(file)-strlen(file),num);copystr(file+strlen(file),sizeof(file)-strlen(file),".m3u8");if(strlen(file)>=cap)goto done;
 dest=(int)P_OPENAT(AT_FDCWD,file,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(dest<0)goto done;if(!writeall(dest,"\xef\xbb\xbf#EXTM3U\n",11))goto done;
 int r;while((r=ip_line(source,line,sizeof(line),&off,(unsigned)before.st_size))>0){char*p=line;if(off==(unsigned)strlen(line)+1&&strlen(line)>=3&&(u8)p[0]==0xef&&(u8)p[1]==0xbb&&(u8)p[2]==0xbf)p+=3;if(!p[0]||p[0]=='#')continue;if(!inline_playlist_resolve(p,canonical,sizeof(canonical))||!writeall(dest,"/mnt/mmc/",9)||!writeall(dest,canonical,(unsigned)strlen(canonical))||!writeall(dest,"\n",1))goto done;}
 if(r<0||P_STAT(source,&after)||!same_identity(&before,&after)||!digest_fd(source,IP_FILE_LIMIT,check)||strcmp(digest,check)||!pa_card_current(c)||P_FSYNC(dest))goto done;copystr(out,cap,file);copystr(ip_stage_live,sizeof(ip_stage_live),file);result=0;
 done:pa_close(dest);pa_close(source);if(result&&dest>=0)(void)P_UNLINK(file);return result;
}
void inline_playlist_discard(const char*path){if(path&&ip_stage_live[0]&&!strcmp(path,ip_stage_live)){(void)P_UNLINK(ip_stage_live);ip_stage_live[0]=0;}}

int inline_playlist_display(pa_card*c,const char*path,char*out,unsigned cap){int fd;struct stat64 st;char line[1100];unsigned off=0;const char*base;int r;
 if(!c||c->info_mode!=2||!pa_card_current(c)||!path||strncmp(path,"xm5/Struct/playlists/",21)||!ip_file(path+21))return PA_UNMANAGED;
 base=path+21;unsigned n=(unsigned)strlen(base)-5;if(n>=cap)return PA_LIMIT;cp(out,base,n);out[n]=0;
 fd=pa_regular_open(c->root,path);if(fd<0)return PA_IO;if(P_STAT(fd,&st)||st.st_size>IP_FILE_LIMIT){pa_close(fd);return PA_LIMIT;}
 unsigned limit=(unsigned)st.st_size;if(limit>8192)limit=8192;
 while((r=ip_line(fd,line,sizeof(line),&off,limit))>0){if(!strncmp(line,"#PLAYLIST:",10)&&ip_label(line+10)){if(copystr(out,cap,line+10)){pa_close(fd);return PA_LIMIT;}break;}if(line[0]&&line[0]!='#')break;}
 pa_close(fd);return pa_card_current(c)?0:PA_CHANGED;
}
