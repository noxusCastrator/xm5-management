/* PC-generated selected-LRC index. No lyric search/normalization on target. */
#define LRC_LIMIT (16u*1024u*1024u)
static int lrc_open(pa_card*c){unsigned char ready[64],h[64],art[16];char got[65],want[65];struct stat64 st;int fd;unsigned bytes,count,pool;
 if(c->lyrics>=0)return 1;
 fd=pa_regular_open(c->meta,"indexes/lyrics.ready");if(fd<0)return 0;
 int ok=read_at(fd,0,ready,64)&&!P_STAT(fd,&st)&&st.st_size==64;pa_close(fd);
 if(!ok||!same(ready,"XLRCRDY1",8)||le32(ready+8)!=1||le32(ready+12)!=64||le32(ready+52)!=1||le32(ready+56)||le32(ready+60)!=crc32(ready,60))return 0;
 bytes=le32(ready+16);if(bytes<64||bytes>LRC_LIMIT)return 0;
 fd=pa_regular_open(c->meta,"indexes/lyrics.bin");if(fd<0)return 0;
 if(!read_at(fd,0,h,64)||P_STAT(fd,&st)||st.st_size!=bytes||!same(h,"XM5LRC1\0",8)||le32(h+8)!=1||le32(h+12)!=64||le32(h+16)!=112||le32(h+24)!=64||!zeroes(h+56,8))goto fail;
 if(!read_at(c->index,40,art,16)||!same(h+40,art,16))goto fail;
 count=le32(h+20);pool=le32(h+28);if(count>100000||pool!=64+count*112||pool>bytes||le32(h+32)!=bytes-pool||le32(h+36)!=bytes)goto fail;
 if(!digest_fd(fd,LRC_LIMIT,got))goto fail;hash_hex(ready+20,want);if(strcmp(got,want))goto fail;
 c->lyrics=fd;c->lrc_records=count;c->lrc_pool=pool;c->lrc_bytes=bytes;cp(c->lyrics_identity,&st,sizeof(st));return 1;
 fail:pa_close(fd);return 0;
}
static int lrc_string(pa_card*c,unsigned off,unsigned len,char*out,unsigned cap){if(off<c->lrc_pool||off>c->lrc_bytes||len>=cap||len>c->lrc_bytes-off||!read_at(c->lyrics,off,out,len))return 0;out[len]=0;return strlen(out)==len&&!strchr(out,'\r')&&!strchr(out,'\n')&&!strchr(out,'\t');}
static int key_compare(const unsigned char*a,const unsigned char*b){unsigned i;for(i=0;i<32;i++)if(a[i]!=b[i])return a[i]<b[i]?-1:1;return 0;}
int pa_lyric_link(pa_card*c,const char*subject,unsigned slot,char*out,unsigned cap){unsigned char key[32],rec[112];char hash[65],text[1024],path[1024],wanted[65],target[1200],shortpath[32],temp[64],fdtext[16],pid[16];unsigned lo=0,hi,mid,i,bytes;struct stat64 before,now;int fd=-1,old=-1,result=XM5_ART_MISS;
 if(c->info_mode)return info_lyric(c,subject,slot,out,cap);
 if(slot>=8||!subject||strlen(subject)>=1024||cap<18||!pa_card_current(c)||!lrc_open(c))return XM5_ART_MISS;
 cp(&before,c->lyrics_identity,sizeof(before));if(P_STAT(c->lyrics,&now)||!same_identity(&before,&now))return XM5_ART_INVALID;
 pa_sha256(subject,strlen(subject),hash);for(i=0;i<32;i++)key[i]=(unsigned char)(hex(hash[2*i])*16+hex(hash[2*i+1]));hi=c->lrc_records;
 while(lo<hi){mid=lo+(hi-lo)/2;if(!read_at(c->lyrics,64+mid*112,rec,112))return XM5_ART_INVALID;if(key_compare(rec,key)<0)lo=mid+1;else hi=mid;}
 if(lo==c->lrc_records||!read_at(c->lyrics,64+lo*112,rec,112)||key_compare(rec,key))return XM5_ART_MISS;
 if(lo+1<c->lrc_records){unsigned char next[112];if(!read_at(c->lyrics,64+(lo+1)*112,next,112)||!key_compare(next,key))return XM5_ART_INVALID;}
 result=XM5_ART_INVALID;
 if(!lrc_string(c,le32(rec+32),le32(rec+36),text,sizeof(text))||strcmp(text,subject)||!lrc_string(c,le32(rec+40),le32(rec+44),path,sizeof(path))||strncmp(path,"xm5/Struct/lyrics/",18)||le32(rec+84)||!zeroes(rec+88,24))return XM5_ART_INVALID;
 if(strlen(path)<4||strcmp(path+strlen(path)-4,".lrc"))return XM5_ART_INVALID;
 bytes=le32(rec+80);if(!bytes||bytes>1024u*1024u)return XM5_ART_INVALID;
 fd=pa_regular_open(c->root,path);if(fd<0)return PA_IO;if(P_STAT(fd,&now)||now.st_size!=bytes||!digest_fd(fd,1024u*1024u,hash))goto done;hash_hex(rec+48,wanted);if(strcmp(hash,wanted)||!pa_card_current(c))goto done;
 copystr(shortpath,sizeof(shortpath),"/tmp/xm5lrc0.lrc");shortpath[11]=(char)('0'+slot);
 old=(int)P_OPENAT(AT_FDCWD,shortpath,O_PATH|O_NOFOLLOW|O_CLOEXEC,0);if(old>=0&&(P_STAT(old,&now)||(now.st_mode&S_IFMT)!=S_IFLNK))goto done;
 decimal(fdtext,(unsigned)c->root);copystr(target,sizeof(target),"/proc/self/fd/");copystr(target+strlen(target),sizeof(target)-strlen(target),fdtext);copystr(target+strlen(target),sizeof(target)-strlen(target),"/");copystr(target+strlen(target),sizeof(target)-strlen(target),path);
 decimal(pid,(unsigned)P_GETPID());copystr(temp,sizeof(temp),shortpath);copystr(temp+strlen(temp),sizeof(temp)-strlen(temp),".");copystr(temp+strlen(temp),sizeof(temp)-strlen(temp),pid);copystr(temp+strlen(temp),sizeof(temp)-strlen(temp),".link");
 if(P_SYMLINK(target,temp)<0)goto done;if(!pa_card_current(c)||P_RENAME(temp,shortpath)<0){P_UNLINK(temp);goto done;}copystr(out,cap,shortpath);result=0;
 done:pa_close(old);pa_close(fd);return result;
}
