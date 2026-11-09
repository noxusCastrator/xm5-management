#include "prepared_io.h"
#include <asm/unistd.h>
extern int pa_add(void*,const char*,void*);
extern int pa_decode(void*,void*,const char*,unsigned*);
extern void native_owner_drop(void*);
static long raw(long nr,long a,long b,long c){register long v __asm__("$2")=nr,r4 __asm__("$4")=a,r5 __asm__("$5")=b,r6 __asm__("$6")=c,r7 __asm__("$7")=0;__asm__ volatile("syscall":"+r"(v),"+r"(r7):"r"(r4),"r"(r5),"r"(r6):"memory");return r7?-v:v;}
static unsigned manager[8],items[4],entry[24],pixels,meta_free,name_free,push_failure,allocation_failure,name_failure,asset_unref,decode_failure,decode_called;
static unsigned char rawbmp[64+80*80*4],bitmap[72];static unsigned asset[12],graphic[6],refs,wrappers;static char name[96];
static void req(int yes,const char*s){if(!yes){raw(__NR_write,2,(long)s,strlen(s));raw(__NR_exit,1,0,0);}}
static void input(void){memset(bitmap,0,sizeof(bitmap));bitmap[48]=1;((unsigned*)bitmap)[5]=0x1234;}
static void reset(void){memset(manager,0,sizeof(manager));memset(items,0,sizeof(items));manager[2]=(unsigned)items;pixels=meta_free=name_free=push_failure=allocation_failure=name_failure=asset_unref=decode_failure=decode_called=0;input();}
int stock_image_add(void*a,const char*b,void*c){(void)a;(void)b;(void)c;return 42;}
int stock_image_decode(void*a,void*b){(void)a;decode_called++;input();memcpy(b,bitmap,72);return decode_failure?1:0;}
unsigned stock_bitmap_size(void*b){(void)b;return 100;}
int test_graphic_destroy(void*b){if(b){pixels++;if(b==graphic)wrappers++;}return 0;}
void*stock_calloc(unsigned a,unsigned b,const char*c,unsigned d){(void)a;(void)b;(void)c;(void)d;memset(entry,0,sizeof(entry));return allocation_failure?0:entry;}
char*stock_strdup(const char*s){strcpy(name,s);return name_failure?0:name;}
void stock_free(void*p,const char*a,unsigned b){(void)a;(void)b;if(p==entry)meta_free++;if(p==name)name_free++;}
uint64_t stock_time(void){return 1234;}
int stock_array_push(void*p,void*e){unsigned*m=p;if(push_failure)return 7;items[m[0]++]=(unsigned)e;return 0;}
int stock_asset_unref(void*a,void*b){(void)a;asset_unref++;if(b==asset&&refs)refs--;return 0;}
int stock_asset_info_unref(void*b){return stock_asset_unref(0,b);}
int stock_asset_ref(void*b){if(b==asset)refs++;return 0;}
void pa_note_manager(void*m){(void)m;}
void*stock_graphic_data(void*p,unsigned w,unsigned h,unsigned fmt){req(p==rawbmp+64&&w==80&&h==80&&fmt==3,"zero-copy graphic arguments wrong\n");graphic[1]=(unsigned)p;return graphic;}
int stock_bitmap_line(void*b,unsigned n){((unsigned*)b)[2]=n;return 0;}
static void raw_input(void){reset();memset(rawbmp,0,sizeof(rawbmp));rawbmp[0]='B';rawbmp[1]='M';*(unsigned*)(rawbmp+2)=sizeof(rawbmp);*(unsigned*)(rawbmp+10)=64;*(unsigned*)(rawbmp+14)=40;*(unsigned*)(rawbmp+18)=80;*(unsigned*)(rawbmp+22)=0u-80;*(unsigned*)(rawbmp+26)=0x00200001;*(unsigned*)(rawbmp+34)=80*80*4;memset(asset,0,sizeof(asset));asset[1]=sizeof(rawbmp);asset[11]=(unsigned)rawbmp;refs=1;wrappers=0;for(unsigned i=67;i<sizeof(rawbmp);i+=4)rawbmp[i]=255;}

void start_c(unsigned*sp){(void)sp;const char*uri="file:///tmp/xa/-dtxmE7TavKPJiZL94dzoJkWzY3oA45sWQMYSfhddXw.bmp";unsigned i;
 for(i=0;i<3;i++){reset();allocation_failure=i==0;name_failure=i==1;push_failure=i==2;req(pa_add(manager,uri,bitmap)!=0,"injected failure accepted\n");req(!manager[0]&&!manager[6]&&pixels==1,"failure lost pixel ownership/accounting\n");req(meta_free==(i>0)&&name_free==(i==2),"private metadata leaked\n");req(bitmap[48]==0,"failed bitmap not cleared\n");}
 reset();req(pa_add(manager,uri,bitmap)==0,"insertion failed\n");req(manager[0]==1&&manager[6]==100&&pixels==0&&bitmap[48]==0,"success did not transfer exactly once\n");req(((unsigned char*)entry)[48]==1&&((unsigned char*)entry)[49]==0&&entry[18]==(unsigned)name&&entry[17]==(unsigned)manager,"native cache entry layout wrong\n");req(entry[19]==1&&entry[20]==1234&&entry[22]==1234,"native cache timestamps wrong\n");
 input();req(pa_add(manager,"file:///tmp/xa/alias.bmp",bitmap)!=0&&pixels==0&&manager[0]==1,"borrowed alias freed existing pixels\n");
 input();((unsigned*)bitmap)[5]=0x4321;req(pa_add(manager,uri,bitmap)==0&&pixels==1&&manager[0]==1,"duplicate name lost unique new pixels\n");
 manager[0]=0;native_owner_drop(entry);req(manager[6]==0&&pixels==2&&meta_free==1&&name_free==1,"actual stock destructor rejected new entry layout\n");
 reset();manager[7]=50;req(pa_add(manager,uri,bitmap)!=0&&pixels==1&&!manager[0]&&!manager[6],"oversize first image admitted\n");
 reset();manager[6]=15*1024*1024;req(pa_decode((void*)1,bitmap,uri,manager)!=0&&decode_called==0&&asset_unref==1,"scratch admission did not fail before decode\n");
 raw_input();rawbmp[0]=0;req(pa_decode(asset,bitmap,uri,manager)!=0&&asset_unref==1&&refs==0,"invalid raw BMP leaked asset\n");
 raw_input();req(pa_decode(asset,bitmap,uri,manager)==0&&decode_called==0&&refs==2&&((unsigned*)bitmap)[5]==(unsigned)graphic,"raw BMP did not bypass decoder\n");
 req(pa_add(manager,uri,bitmap)==0,"zero-copy cache insertion failed\n");stock_asset_unref(0,asset);req(refs==1,"source asset was not retained\n");manager[0]=0;native_owner_drop(entry);req(refs==0&&wrappers==1&&manager[6]==0,"real destructors did not release retained source exactly once\n");
 raw_input();req(pa_decode(asset,bitmap,uri,manager)==0,"raw decode failed\n");allocation_failure=1;req(pa_add(manager,uri,bitmap)!=0&&refs==1&&wrappers==1,"failed insertion lost retained asset\n");stock_asset_unref(0,asset);req(refs==0,"caller asset reference leaked\n");
 reset();req(pa_add(manager,"stock_icon",bitmap)==42&&bitmap[48]==1,"unmanaged insertion changed\n");
 const char*message="MIPS failure-atomic bitmap adapter + original destructor: PASS\n";raw(__NR_write,1,(long)message,strlen(message));raw(__NR_exit,0,0,0);for(;;){}
}
