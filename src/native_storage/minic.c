#include "prepared_io.h"
void *memset(void*d,int c,size_t n){unsigned char*p=d;while(n--)*p++=(unsigned char)c;return d;}
void *memcpy(void*d,const void*s,size_t n){unsigned char*p=d;const unsigned char*q=s;while(n--)*p++=*q++;return d;}
size_t strlen(const char*s){size_t n=0;while(s[n])n++;return n;}
int strcmp(const char*a,const char*b){while(*a&&*a==*b){a++;b++;}return (unsigned char)*a-(unsigned char)*b;}
int strncmp(const char*a,const char*b,size_t n){while(n&&*a&&*a==*b){a++;b++;n--;}return n?(unsigned char)*a-(unsigned char)*b:0;}
char *strchr(const char*s,int c){for(;;s++){if(*s==(char)c)return (char*)s;if(!*s)return 0;}}
char *strcpy(char*d,const char*s){char*p=d;while((*p++=*s++)){}return d;}
