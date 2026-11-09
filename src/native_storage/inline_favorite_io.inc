/* Include after inline_state_io.inc and the legacy favorite helpers. Explicit
 * import is transactional; missing songs are skipped for native playback. The
 * source text remains intact, including references absent from songtable. */
typedef struct {pa_card*c;void*db;pa_sql_exec exec;unsigned insert,count;} ip_favorite_context;
static int ip_favorite_path(ip_favorite_context*f,const char*reference){char path[XM5_ART_PATH],absolute[XM5_ART_PATH+16],sql[9000];unsigned i,n;
 if(!reference[0]||reference[0]=='#')return 1;
 if(++f->count>100000||!inline_playlist_resolve(reference,path,sizeof(path)))return 0;
 if(!f->insert)return 1;
 copystr(absolute,sizeof(absolute),"/mnt/mmc/");copystr(absolute+9,sizeof(absolute)-9,path);
 copystr(sql,sizeof(sql),"INSERT INTO myfav SELECT NULL,filename,fileurl,album,songer,style,name,composer,albumsonger,year,track,cd_num,time,time_end,time_create,Initial,bitdepth,samprate FROM songtable WHERE fileurl='");n=(unsigned)strlen(sql);
 for(i=0;absolute[i];i++){if(n+64>=sizeof(sql))return 0;sql[n++]=absolute[i];if(absolute[i]=='\'')sql[n++]='\'';}
 copystr(sql+n,sizeof(sql)-n,"' AND NOT EXISTS(SELECT 1 FROM myfav WHERE myfav.fileurl=songtable.fileurl) LIMIT 1");
 return f->exec(f->db,sql,0,0,0)==0;
}
static int ip_favorite_lines(int fd,ip_favorite_context*f){struct stat64 st;unsigned off=0;char line[XM5_ART_PATH+16];int r;f->count=0;
 if(P_STAT(fd,&st)||st.st_size<0||st.st_size>IP_FILE_LIMIT)return 0;
 while((r=ip_line(fd,line,sizeof(line),&off,(unsigned)st.st_size))>0){char*p=line;if(off==(unsigned)strlen(line)+1&&strlen(line)>=3&&(u8)line[0]==0xef&&(u8)line[1]==0xbb&&(u8)line[2]==0xbf)p+=3;if(!state_text(p,XM5_ART_PATH-1)||!ip_favorite_path(f,p))return 0;}
 return r==0;
}
int inline_favorites_apply(pa_card*c,void*db,pa_sql_exec exec){int d=-1,fd=-1,begun=0,ok=0;struct stat64 before,after;char digest[65],check[65];ip_favorite_context f={c,db,exec,0,0};
 if(!db||!exec||c->info_mode!=2||!pa_card_current(c))return PA_UNMANAGED;ip_load(c);d=ip_playlist_dir(c);if(d<0)return PA_IO;fd=pa_regular_open(d,"Favorites.m3u8");if(fd<0)goto done;
 if(P_STAT(fd,&before)||!digest_fd(fd,IP_FILE_LIMIT,digest)||!ip_favorite_lines(fd,&f)||!pa_card_current(c))goto done;
 if(exec(db,"SAVEPOINT xm5_inline_favorites",0,0,0))goto done;begun=1;
 if(exec(db,"DELETE FROM myfav WHERE substr(fileurl,1,9)='/mnt/mmc/'",0,0,0))goto done;
 f.insert=1;if(!ip_favorite_lines(fd,&f)||P_STAT(fd,&after)||!same_identity(&before,&after)||!digest_fd(fd,IP_FILE_LIMIT,check)||strcmp(digest,check)||!pa_card_current(c))goto done;
 if(exec(db,"RELEASE xm5_inline_favorites",0,0,0))goto done;begun=0;ip_favorite_bound=1;copystr(ip_favorite_digest,sizeof(ip_favorite_digest),digest);ok=ip_map_save(c);
 done:if(begun){(void)exec(db,"ROLLBACK TO xm5_inline_favorites",0,0,0);(void)exec(db,"RELEASE xm5_inline_favorites",0,0,0);}pa_close(fd);pa_close(d);return ok?0:PA_IO;
}
