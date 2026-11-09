/* XMI v1: bounded, read-only lookup. No music/tag/flash writes. */
#define XMI_LIMIT (16u*1024u*1024u)
static int memcmp32(const unsigned char*a,const unsigned char*b){unsigned i;for(i=0;i<32;i++)if(a[i]!=b[i])return a[i]<b[i]?-1:1;return 0;}
typedef struct {int fd;unsigned bytes,count,flags;unsigned char h[256],dir[1024];} xmi;
static int le64small(const unsigned char*p,unsigned*out){if(le32(p+4))return 0;*out=le32(p);return 1;}
static int range_hash(int fd,unsigned at,unsigned n,const unsigned char*want){sha s;unsigned char b[1024];char got[65],expected[65];unsigned k;sha_init(&s);while(n){k=n>sizeof(b)?sizeof(b):n;if(!read_at(fd,at,b,k))return 0;sha_add(&s,b,k);at+=k;n-=k;}sha_end(&s,got);hash_hex(want,expected);return !strcmp(got,expected);}
static int info_index_open(pa_card*c){unsigned char h[128],r[96],prev[96];struct stat64 st;unsigned i,n,bytes;int id;
 c->index=pa_regular_open(c->meta,"indexes/info.bin");if(c->index<0)return 0;
 if(!read_at(c->index,0,h,128)||P_STAT(c->index,&st)||!same(h,"XM5IXI1\0",8)||le32(h+8)!=1||le32(h+12)!=128||le32(h+20)!=96||!zeroes(h+80,48)||!le64small(h+72,&bytes))return 0;
 n=le32(h+16);if(n>1000000||bytes!=128+n*96||st.st_size!=bytes||!range_hash(c->index,128,n*96,h+40))return 0;
 memset(prev,0,sizeof(prev));
 for(i=0;i<n;i++){if(!read_at(c->index,128+i*96,r,96))return 0;id=le32(r);if(id<1||id>3||le32(r+4)||!zeroes(r+88,8))return 0;
  if(i){int v=memcmp32(prev+8,r+8);if(v>0||(!v&&le32(prev)>=le32(r)))return 0;}cp(prev,r,96);}
 c->info_mode=1;c->records=n;c->index_bytes=bytes;cp(c->index_identity,&st,sizeof(st));return 1;
}
static int info_find(pa_card*c,const char*role,const char*subject,unsigned char out[96]){unsigned id=!strcmp(role,"directory")?1:!strcmp(role,"playback")?2:!strcmp(role,"playlist")?3:0;unsigned lo=0,hi=c->records,mid;unsigned char k[32],nul=0;char hash[65];sha s;int cmp;unsigned i;
 if(!id||!subject||!*subject||strlen(subject)>=1024||!pa_card_current(c)||!index_current(c))return -1;
 sha_init(&s);sha_add(&s,role,strlen(role));sha_add(&s,&nul,1);sha_add(&s,subject,strlen(subject));sha_end(&s,hash);for(i=0;i<32;i++)k[i]=(unsigned char)(hex(hash[2*i])*16+hex(hash[2*i+1]));
 while(lo<hi){mid=lo+(hi-lo)/2;if(!read_at(c->index,128+mid*96,out,96))return -1;cmp=memcmp32(out+8,k);if(!cmp)cmp=le32(out)<id?-1:le32(out)>id?1:0;if(cmp<0)lo=mid+1;else hi=mid;}
 if(lo==c->records)return 0;if(!read_at(c->index,128+lo*96,out,96))return -1;return !memcmp32(out+8,k)&&le32(out)==id?1:0;
}
static int info_open(pa_card*c,const unsigned char*rec,const char*subject,xmi*x){char path[64]="info/",got[65],want[65],source[1024];struct stat64 st;unsigned i,po,pn,first,off,len,last,kind,key,oldkind=0,oldkey=0;sha s;
 memset(x,0,sizeof(*x));x->fd=-1;
 for(i=0;i<16;i++){path[5+i*2]="0123456789abcdef"[rec[40+i]>>4];path[6+i*2]="0123456789abcdef"[rec[40+i]&15];}copystr(path+37,sizeof(path)-37,".xmi");
 x->fd=pa_regular_open(c->meta,path);if(x->fd<0)return 0;
 if(!read_at(x->fd,0,x->h,256)||P_STAT(x->fd,&st)||!same(x->h,"XM5INF1\0",8)||le32(x->h+8)!=1||le32(x->h+12)!=256||!le64small(x->h+16,&x->bytes)||x->bytes<256||x->bytes>XMI_LIMIT||st.st_size!=x->bytes||!same(x->h+32,rec+40,16)||!zeroes(x->h+192,64)||le32(x->h+120)!=256||le32(x->h+124))goto fail;
 x->count=le32(x->h+28);x->flags=le32(x->h+24);if(x->count>16||(x->flags&~15u)||!le64small(x->h+112,&po))goto fail;
 pn=le32(x->h+108);if((!pn&&le32(rec)==2)||pn>=sizeof(source)||po!=256+x->count*64||po%64||pn>x->bytes-po||!read_at(x->fd,256,x->dir,x->count*64)||!read_at(x->fd,po,source,pn))goto fail;source[pn]=0;
 if(strlen(source)!=pn||!range_hash(x->fd,po,pn,x->h+128))goto fail;
 if(le32(rec)==2&&strcmp(source,subject))goto fail;
 first=(po+pn+63)&~63u;if(first>x->bytes)goto fail;last=first;
 for(i=0;i<x->count;i++){unsigned char*d=x->dir+i*64;kind=le32(d);key=le32(d+4);if(kind<1||kind>4||(kind==1?pa_size_slot(key)<0:key!=0)||!zeroes(d+56,8)||!le64small(d+8,&off)||!le64small(d+16,&len)||!len||off%64||off<last||off>x->bytes||len>x->bytes-off||(i&&(kind<oldkind||(kind==oldkind&&key<=oldkey))))goto fail;last=(off+len+63)&~63u;oldkind=kind;oldkey=key;}
 /* The header and directory are protected separately so gain lookup does not
  * hash every artwork pixel at each track transition. Selected payloads are
  * checked on use; complete-file hashes are verified by desktop card verify. */
 cp(want,x->h+160,32);memset(x->h+160,0,32);sha_init(&s);sha_add(&s,x->h,256);sha_add(&s,x->dir,x->count*64);
 for(off=po;off<first;){unsigned char b[1024];len=first-off;if(len>sizeof(b))len=sizeof(b);if(!read_at(x->fd,off,b,len))goto fail;sha_add(&s,b,len);off+=len;}sha_end(&s,got);hash_hex((const unsigned char*)want,source);if(strcmp(got,source))goto fail;cp(x->h+160,want,32);
 if(!pa_card_current(c))goto fail;return 1;
 fail:pa_close(x->fd);x->fd=-1;return 0;
}
static const unsigned char*info_entry(const xmi*x,unsigned kind,unsigned key){unsigned i;for(i=0;i<x->count;i++){const unsigned char*d=x->dir+64*i;if(le32(d)==kind&&le32(d+4)==key)return d;}return 0;}
static int info_lookup_size(pa_card*c,const char*role,const char*subject,unsigned side,xm5_art_record*out){unsigned char rec[96];const unsigned char*d;xmi x;int r;unsigned i;memset(out,0,sizeof(*out));r=info_find(c,role,subject,rec);if(r<=0)return r<0?XM5_ART_INVALID:XM5_ART_MISS;if(!info_open(c,rec,subject,&x))return XM5_ART_INVALID;d=info_entry(&x,1,side);if(!d){pa_close(x.fd);out->no_art=1;return XM5_ART_NONE;}
 copystr(out->path,sizeof(out->path),"xm5/Struct/info/");for(i=0;i<16;i++){out->path[16+i*2]="0123456789abcdef"[rec[40+i]>>4];out->path[17+i*2]="0123456789abcdef"[rec[40+i]&15];}copystr(out->path+48,sizeof(out->path)-48,".xmi");
 /* Prefix is 16 bytes, verified below by fixtures. */
 out->side=side;out->info_offset=le32(d+8);out->info_length=le32(d+16);hash_hex(d+24,out->sha256);pa_close(x.fd);return 0;
}
static int info_materialize(pa_card*c,const xm5_art_record*r,const char*ram,char*out,size_t cap){char target[256],name[48],tmp[256],got[65];unsigned char buf[1024];unsigned pos=0,n;int fd=-1,dst=-1,dir=-1,result=XM5_ART_INVALID;
 if(!pa_card_current(c)||!r->info_length||r->info_length!=64+r->side*r->side*4||r->info_length>ASSET_LIMIT||strcmp(ram,"/tmp/xa"))return XM5_ART_INVALID;
 fd=pa_regular_open(c->root,r->path);if(fd<0)return PA_IO;name64(name,r->sha256);copystr(name+43,sizeof(name)-43,".bmp");copystr(target,sizeof(target),ram);copystr(target+strlen(target),sizeof(target)-strlen(target),"/");copystr(target+strlen(target),sizeof(target)-strlen(target),name);
 (void)P_MKDIR(ram);dir=(int)P_OPENAT(AT_FDCWD,ram,DIRFLAGS,0);if(dir<0)goto done;
 dst=pa_regular_open(dir,name);if(dst>=0){int ok=digest_fd(dst,ASSET_LIMIT,got)&&!strcmp(got,r->sha256)&&bmp(dst,r->side);pa_close(dst);dst=-1;if(ok){result=0;goto done;}}
 copystr(tmp,sizeof(tmp),name);copystr(tmp+strlen(tmp),sizeof(tmp)-strlen(tmp),".part");dst=(int)P_OPENAT(dir,tmp,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(dst<0)goto done;
 while(pos<r->info_length){n=r->info_length-pos;if(n>sizeof(buf))n=sizeof(buf);if(!read_at(fd,r->info_offset+pos,buf,n)||!writeall(dst,buf,n))goto cleanup;pos+=n;}pa_close(dst);dst=-1;
 dst=pa_regular_open(dir,tmp);if(dst<0||!digest_fd(dst,ASSET_LIMIT,got)||strcmp(got,r->sha256)||!bmp(dst,r->side)||!pa_card_current(c))goto cleanup;pa_close(dst);dst=-1;
 {char fulltmp[256];copystr(fulltmp,sizeof(fulltmp),ram);copystr(fulltmp+strlen(fulltmp),sizeof(fulltmp)-strlen(fulltmp),"/");copystr(fulltmp+strlen(fulltmp),sizeof(fulltmp)-strlen(fulltmp),tmp);if(P_RENAME(fulltmp,target)<0)goto cleanup;}result=0;goto done;
 cleanup:pa_close(dst);dst=-1;(void)P_UNLINKAT(dir,tmp);
 done:pa_close(dst);pa_close(dir);pa_close(fd);if(!result){if(strlen(target)+8>cap)return XM5_ART_INVALID;copystr(out,cap,"file://");copystr(out+7,cap-7,target);}return result;
}
int pa_info_gain(pa_card*c,const char*subject,float*out){unsigned char rec[96];xmi x;float gain,mid,lim;double peak;struct stat64 st;int fd,r;*out=0;if(!c->info_mode)return XM5_ART_MISS;r=info_find(c,"playback",subject,rec);if(r<=0)return r<0?XM5_ART_INVALID:XM5_ART_MISS;if(!info_open(c,rec,subject,&x))return XM5_ART_INVALID;
 cp(&gain,x.h+104,4);cp(&mid,x.h+96,4);cp(&lim,x.h+100,4);cp(&peak,x.h+88,8);
 r=XM5_ART_INVALID;if(!(x.flags&2)){r=0;goto done;}if(!(x.flags&1)||!(peak>=0&&peak<1e12)||!(mid>=-60&&mid<=0)||!(lim>=0&&lim<=12)||!(gain>=-lim&&gain<=lim))goto done;
 fd=pa_regular_open(c->root,subject);if(fd<0)goto done;if(P_STAT(fd,&st)||st.st_size!=(long long)((uint64_t)le32(x.h+80)|((uint64_t)le32(x.h+84)<<32))){pa_close(fd);goto done;}pa_close(fd);*out=gain;r=0;
 done:pa_close(x.fd);return r;
}
static int info_lyric(pa_card*c,const char*subject,unsigned slot,char*out,unsigned cap){unsigned char rec[96],b[1024];xmi x;const unsigned char*d;char path[32]="/tmp/xm5lrc0.lrc",tmp[64];unsigned off,len,pos=0,n;int r,fd=-1;struct stat64 st;
 if(slot>=8||cap<17)return XM5_ART_INVALID;r=info_find(c,"playback",subject,rec);if(r<=0)return r<0?XM5_ART_INVALID:XM5_ART_MISS;if(!info_open(c,rec,subject,&x))return XM5_ART_INVALID;d=info_entry(&x,2,0);if(!d){pa_close(x.fd);return XM5_ART_MISS;}off=le32(d+8);len=le32(d+16);r=XM5_ART_INVALID;
 if(len<3||len>1024*1024||!read_at(x.fd,off,b,3)||b[0]!=239||b[1]!=187||b[2]!=191||!range_hash(x.fd,off,len,d+24))goto done;
 path[11]=(char)('0'+slot);copystr(tmp,sizeof(tmp),path);copystr(tmp+strlen(tmp),sizeof(tmp)-strlen(tmp),".part");
 /* An earlier failed extraction is ours only when it is a regular file. */
 fd=(int)P_OPENAT(AT_FDCWD,tmp,O_PATH|O_NOFOLLOW|O_CLOEXEC,0);if(fd>=0){int regular=!P_STAT(fd,&st)&&(st.st_mode&S_IFMT)==S_IFREG;pa_close(fd);fd=-1;if(!regular)goto done;(void)P_UNLINK(tmp);}
 fd=(int)P_OPENAT(AT_FDCWD,tmp,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(fd<0)goto done;
 while(pos<len){n=len-pos;if(n>sizeof(b))n=sizeof(b);if(!read_at(x.fd,off+pos,b,n)||!writeall(fd,b,n))goto cleanup;pos+=n;}pa_close(fd);fd=-1;
 if(!pa_card_current(c)||P_RENAME(tmp,path)<0)goto cleanup;copystr(out,cap,path);r=0;goto done;
 cleanup:pa_close(fd);fd=-1;(void)P_UNLINK(tmp);
 done:pa_close(fd);pa_close(x.fd);return r;
}
static int assets_open(pa_card*c){unsigned char h[128],ih[40],rec[96],old[96];struct stat64 st;unsigned bytes,n,i;int side;
 if(c->assets>=0)return 1;c->assets=pa_regular_open(c->meta,"indexes/assets.bin");if(c->assets<0)return 0;
 if(!read_at(c->assets,0,h,128)||P_STAT(c->assets,&st)||!same(h,"XM5IAC1\0",8)||le32(h+8)!=1||le32(h+12)!=128||le32(h+20)!=96||!zeroes(h+80,48)||!le64small(h+72,&bytes)||!read_at(c->index,0,ih,40)||!same(h+24,ih+24,16))goto fail;
 n=le32(h+16);if(n>1000000||bytes!=128+n*96||st.st_size!=bytes||!range_hash(c->assets,128,n*96,h+40))goto fail;
 for(i=0;i<n;i++){if(!read_at(c->assets,128+i*96,rec,96))goto fail;side=(int)le32(rec);if(pa_size_slot((unsigned)side)<0||le32(rec+4)||!zeroes(rec+88,8))goto fail;if(i){int cmp=memcmp32(old+8,rec+8);if(cmp>0||(!cmp&&le32(old)>=le32(rec)))goto fail;}cp(old,rec,96);}
 c->asset_records=n;cp(c->assets_identity,&st,sizeof(st));return 1;
 fail:pa_close(c->assets);c->assets=-1;return 0;
}
int pa_info_asset(pa_card*c,const char*name,xm5_art_record*out){unsigned char key[32],rec[96];unsigned acc=0,bits=0,n=0,i,lo=0,hi,mid;const char*a="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_",*base=name,*q,*p;int found;const unsigned char*d;xmi x;
 if(!c->info_mode||!name||!pa_card_current(c)||!assets_open(c))return XM5_ART_MISS;
 for(q=name;*q;q++)if(*q=='/')base=q+1;if(strlen(base)!=47||strcmp(base+43,".bmp"))return XM5_ART_INVALID;
 for(i=0;i<43;i++){p=strchr(a,base[i]);if(!p||!*p)return XM5_ART_INVALID;acc=(acc<<6)|(unsigned)(p-a);bits+=6;if(bits>=8){bits-=8;if(n>=32)return XM5_ART_INVALID;key[n++]=(unsigned char)(acc>>bits);}}if(n!=32||(acc&3))return XM5_ART_INVALID;
 hi=c->asset_records;while(lo<hi){mid=lo+(hi-lo)/2;if(!read_at(c->assets,128+mid*96,rec,96))return XM5_ART_INVALID;if(memcmp32(rec+8,key)<0)lo=mid+1;else hi=mid;}
 if(lo==c->asset_records||!read_at(c->assets,128+lo*96,rec,96)||memcmp32(rec+8,key))return XM5_ART_MISS;
 found=info_open(c,rec,"",&x);if(!found)return XM5_ART_INVALID;d=info_entry(&x,1,le32(rec));if(!d||!same(d+24,key,32)){pa_close(x.fd);return XM5_ART_INVALID;}
 memset(out,0,sizeof(*out));copystr(out->path,sizeof(out->path),"xm5/Struct/info/");for(i=0;i<16;i++){out->path[16+2*i]="0123456789abcdef"[rec[40+i]>>4];out->path[17+2*i]="0123456789abcdef"[rec[40+i]&15];}copystr(out->path+48,sizeof(out->path)-48,".xmi");
 out->side=le32(rec);out->info_offset=le32(d+8);out->info_length=le32(d+16);hash_hex(d+24,out->sha256);pa_close(x.fd);return pa_card_current(c)?0:PA_CHANGED;
}
int pa_info_read(pa_card*c,const xm5_art_record*r,void*buffer,unsigned bytes){int fd,ok;char got[65];unsigned i;unsigned char*b=buffer;
 if(!r||!buffer||!r->info_length||bytes!=r->info_length||bytes!=64+r->side*r->side*4||bytes>ASSET_LIMIT||!pa_card_current(c))return XM5_ART_INVALID;
 fd=pa_regular_open(c->root,r->path);if(fd<0)return PA_IO;ok=read_at(fd,r->info_offset,b,bytes);pa_close(fd);if(!ok)return PA_IO;pa_sha256(b,bytes,got);
 if(strcmp(got,r->sha256)||b[0]!='B'||b[1]!='M'||le32(b+2)!=bytes||le32(b+6)||le32(b+10)!=64||le32(b+14)!=40||le32(b+18)!=r->side||le32(b+22)!=0u-r->side||le32(b+26)!=0x00200001||le32(b+30)||le32(b+34)!=r->side*r->side*4||!zeroes(b+46,18))return XM5_ART_INVALID;
 for(i=67;i<bytes;i+=4)if(b[i]!=255)return XM5_ART_INVALID;return pa_card_current(c)?0:PA_CHANGED;
}
