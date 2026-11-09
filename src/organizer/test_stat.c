#define _GNU_SOURCE
#include "xm5_stat.h"
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void require(int yes, const char *message)
{
    if (!yes) { fprintf(stderr, "%s\n", message); exit(1); }
}
int main(void)
{
    char directory[]="build/stat-host-XXXXXX", song[256], link[256], missing[256];
    require(mkdtemp(directory)!=NULL,"mkdtemp failed");
    snprintf(song,sizeof(song),"%s/song.flac",directory);
    snprintf(link,sizeof(link),"%s/link.flac",directory);
    snprintf(missing,sizeof(missing),"%s/missing.flac",directory);
    int fd=open(song,O_RDWR|O_CREAT|O_EXCL,0600);
    require(fd>=0&&!ftruncate(fd,65537),"fixture creation failed");
    xm5_stat a,b;
    require(!xm5_fstat(fd,&a)&&xm5_is_regular(a.mode)&&a.size==65537,"fstat regular/size failed");
    require(!xm5_lstat(song,&b)&&a.dev==b.dev&&a.ino==b.ino&&a.size==b.size,"lstat identity differs from fstat");
    require(a.mtime==b.mtime&&a.mtime_nsec==b.mtime_nsec&&a.ctime==b.ctime&&a.ctime_nsec==b.ctime_nsec,"timestamp copy differs");
    require(!xm5_lstat(directory,&b)&&xm5_is_directory(b.mode),"directory mode failed");
    require(!symlink("song.flac",link),"fixture symlink failed");
    require(!xm5_lstat(link,&b)&&!xm5_is_regular(b.mode)&&(b.mode&0170000u)==0120000u,"lstat followed symlink");
    errno=0;require(xm5_lstat(missing,&b)==-1&&errno==ENOENT,"ENOENT contract failed");
    errno=0;require(xm5_fstat(-1,&b)==-1&&errno==EBADF,"EBADF contract failed");
    close(fd);unlink(link);unlink(song);rmdir(directory);
    puts("Host stat compatibility: 7 checks passed");
    return 0;
}
