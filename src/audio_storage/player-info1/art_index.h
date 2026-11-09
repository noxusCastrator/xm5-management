#ifndef XM5_ART_INDEX_H
#define XM5_ART_INDEX_H
#include <stddef.h>
#ifdef XM5_FREESTANDING
typedef __UINT64_TYPE__ uint64_t;
typedef __UINTPTR_TYPE__ uintptr_t;
#define UINT64_MAX ((uint64_t)-1)
void *memset(void *,int,size_t);
size_t strlen(const char *);
int strcmp(const char *,const char *);
int strncmp(const char *,const char *,size_t);
char *strchr(const char *,int);
char *strcpy(char *,const char *);
#else
#include <stdint.h>
#include <string.h>
#endif
#define XM5_ART_PATH 1024
enum { XM5_ART_OK=0, XM5_ART_MISS=1, XM5_ART_NONE=2, XM5_ART_STALE=3,
       XM5_ART_INVALID=4, XM5_ART_BUSY=5, XM5_ART_BUDGET=6 };
typedef struct { char path[XM5_ART_PATH],sha256[65]; unsigned side; int no_art; unsigned info_offset,info_length; } xm5_art_record;

#endif
