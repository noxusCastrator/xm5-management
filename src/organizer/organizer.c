#define _GNU_SOURCE
#include "inline_flac.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/file.h>
#include <sys/resource.h>
#include "xm5_stat.h"
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/* Invoked by the stock library scan after it completes, never by a picture
 * paint callback. No audio frame, tag or original picture is removed. */
static volatile sig_atomic_t stopped;
static const char *root;
static char scratch[1024];
static unsigned prepared, skipped, failed;
static void stopping(int signal){(void)signal;stopped=1;}
static int path(char out[1024],const char*a,const char*b){return xm5_path(out,1024,a,b);}
static int regular(const char*p){xm5_stat st;return !xm5_lstat(p,&st)&&xm5_is_regular(st.mode);}
static int child(char*const argv[],const char*log){
 if(stopped)return -1;
 pid_t pid=fork();if(pid<0)return -1;
 if(!pid){
  struct rlimit memory={32u*1024u*1024u,32u*1024u*1024u},cpu={600,600};
  (void)setrlimit(RLIMIT_DATA,&memory);(void)setrlimit(RLIMIT_CPU,&cpu);
  int fd=open(log,O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW|O_CLOEXEC,0600);
  if(fd<0)_exit(127);dup2(fd,1);dup2(fd,2);close(fd);
  unsetenv("LD_PRELOAD");unsetenv("XM5_ORGANIZE_MODE");
  execv(argv[0],argv);_exit(127);
 }
 int status=0;
 while(waitpid(pid,&status,0)<0){if(errno!=EINTR)return -1;if(stopped)kill(pid,SIGTERM);}
 return !stopped&&WIFEXITED(status)&&WEXITSTATUS(status)==0?0:-1;
}
static int image_ok(const char*file){
 int fd=open(file,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return 0;
 unsigned char h[4];int good=xm5_read(fd,h,4,0)==0&&h[0]==255&&h[1]==216;
 close(fd);return good;
}
static void levels(const char*file,int32_t*peak,int32_t*crest){
 *peak=*crest=INT32_MIN;
 char log[1024];if(path(log,scratch,"levels.log"))return;
 char*args[]={"/usr/bin/ffmpeg","-hide_banner","-nostats","-threads","1","-i",(char*)file,
  "-map","0:a:0","-vn","-af","astats=metadata=0:reset=0","-f","null","-",NULL};
 if(child(args,log))return;
 FILE*f=fopen(log,"r");if(!f)return;char line[1024];double p=1,r=1;
 while(fgets(line,sizeof(line),f)){
  char*s=strstr(line,"Peak level dB: ");if(s){char*e;p=strtod(s+15,&e);if(e==s+15)p=1;}
  s=strstr(line,"RMS level dB: ");if(s){char*e;r=strtod(s+14,&e);if(e==s+14)r=1;}
 }fclose(f);
 if(p<=0&&p>=-200&&r<=p&&r>=-300){*peak=(int32_t)(p*1000);*crest=(int32_t)((p-r)*1000);}
}
static int prepare(const char*file){
 int fd=open(file,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return -1;
 struct xm5_block block;int found=xm5_find(fd,1,&block);
 if(found==1){int bad=xm5_validate(fd,&block,1);close(fd);return bad?-1:0;}
 close(fd);if(found<0)return -1;
 char jpg[3][1024],log[1024],lyric[1024];const char*art[3]={NULL,NULL,NULL};
 if(path(jpg[0],scratch,"330.jpg")||path(jpg[1],scratch,"160.jpg")||
    path(jpg[2],scratch,"80.jpg")||path(log,scratch,"image.log"))return -1;
 for(unsigned i=0;i<3;i++)unlink(jpg[i]);
 char*args[]={"/usr/bin/ffmpeg","-hide_banner","-loglevel","error","-y","-threads","1",
  "-filter_complex_threads","1","-i",(char*)file,"-filter_complex",
  "[0:v:0]split=3[a][b][c];[a]scale=330:330[x];[b]scale=160:160[y];[c]scale=80:80[z]",
  "-map","[x]","-frames:v","1","-threads","1","-c:v","mjpeg",jpg[0],
  "-map","[y]","-frames:v","1","-threads","1","-c:v","mjpeg",jpg[1],
  "-map","[z]","-frames:v","1","-threads","1","-c:v","mjpeg",jpg[2],NULL};
 if(!child(args,log))for(unsigned i=0;i<3;i++)if(image_ok(jpg[i]))art[i]=jpg[i];
 int32_t peak,crest;levels(file,&peak,&crest);
 if(strlen(file)+5>sizeof(lyric))return -1;strcpy(lyric,file);
 char*extension=strrchr(lyric,'.');if(!extension)return -1;strcpy(extension,".lrc");
 const char*lrc=regular(lyric)?lyric:NULL;
 if(stopped)return -1;
 return xm5_organize(file,art,lrc,peak,crest);
}
static int walk(const char*directory,unsigned depth){
 if(depth>24||stopped)return -1;DIR*d=opendir(directory);if(!d)return -1;
 struct dirent*e;int result=0;
 while(!stopped&&(e=readdir(d))){
  if(e->d_name[0]=='.'||strpbrk(e->d_name,"\r\n\t\\"))continue;
  char file[1024];xm5_stat st;if(path(file,directory,e->d_name)||xm5_lstat(file,&st)){result=-1;continue;}
  if(xm5_is_directory(st.mode)){if(walk(file,depth+1))result=-1;}
  else if(xm5_is_regular(st.mode)){
   const char*ext=strrchr(file,'.');if(!ext||strcasecmp(ext,".flac"))continue;
   int r=prepare(file);if(r>0)prepared++;else if(!r)skipped++;else{failed++;result=-1;}
  }
 }closedir(d);return stopped?-1:result;
}
static int root_ok(const char*p){
 char canonical[PATH_MAX],s[1024];xm5_stat st;
 if(!realpath(p,canonical)||strcmp(canonical,p)||path(s,p,"xm5")||xm5_lstat(s,&st)||!xm5_is_directory(st.mode))return 0;
 if(path(s,p,"xm5/Struct")||xm5_lstat(s,&st)||!xm5_is_directory(st.mode))return 0;
 if(path(s,p,"xm5/music")||xm5_lstat(s,&st)||!xm5_is_directory(st.mode))return 0;
 /* Legacy INFO02 cards have a different transaction owner. Reading them stays
  * supported; an ordinary stock scan must not migrate their files silently. */
 const char*legacy[]={"xm5/Struct/manifest.json","xm5/Struct/manifest.sha256","xm5/Struct/indexes/info.bin"};
 for(unsigned i=0;i<3;i++){
  if(path(s,p,legacy[i])||xm5_lstat(s,&st)==0||errno!=ENOENT)return 0;
 }
 if(path(s,p,"xm5/Struct/INCOMPLETE"))return 0;
 return xm5_lstat(s,&st)<0&&errno==ENOENT;
}
static int run(const char*mode){
 root=getenv("XM5_ORGANIZE_ROOT");if(!root)root="/mnt/mmc";
 if(!root_ok(root)){fprintf(stderr,"XM5_ORGANIZE_REFUSED card-not-ready-or-legacy errno=%d\n",errno);return 2;}
 if(!strcmp(mode,"rebuild"))return xm5_rebuild(root)?1:0;
 if(strcmp(mode,"run"))return 2;
 int lock=open("/tmp/xm5-organize.lock",O_RDWR|O_CREAT|O_CLOEXEC|O_NOFOLLOW,0600);
 if(lock<0||flock(lock,LOCK_EX|LOCK_NB)){if(lock>=0)close(lock);return 3;}
 /* A dedicated process group permits the USB boundary to stop decoder and
  * analysis children as well as their coordinator. */
 (void)setsid();signal(SIGTERM,stopping);signal(SIGINT,stopping);
 int pidfile=open("/var/run/xm5-organize.pid",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW,0600);
 if(pidfile<0){close(lock);return 3;}
 char number[32];int n=snprintf(number,sizeof(number),"%ld\n",(long)getpid());
 if(write(pidfile,number,(size_t)n)!=n){close(pidfile);unlink("/var/run/xm5-organize.pid");close(lock);return 1;}close(pidfile);
 strcpy(scratch,"/tmp/xm5-organize-XXXXXX");
 int result=1;if(mkdtemp(scratch)){
  char music[1024];if(!path(music,root,"xm5/music"))result=walk(music,0)?1:0;
  if(!stopped&&xm5_rebuild(root))result=1;
  const char*names[]={"330.jpg","160.jpg","80.jpg","levels.log","image.log"};
  for(unsigned i=0;i<5;i++){char file[1024];if(!path(file,scratch,names[i]))unlink(file);}rmdir(scratch);
 }
 unlink("/var/run/xm5-organize.pid");close(lock);
 fprintf(stderr,"XM5_ORGANIZE prepared=%u skipped=%u failed=%u stopped=%d\n",prepared,skipped,failed,(int)stopped);
 return stopped?4:result;
}
__attribute__((constructor))static void entry(void){
 const char*mode=getenv("XM5_ORGANIZE_MODE");if(mode){int result=run(mode);fprintf(stderr,"XM5_ORGANIZE_RESULT %d\n",result);exit(result);}
}
