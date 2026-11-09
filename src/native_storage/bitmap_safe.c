/* Narrow failure-atomic replacement at the ordinary image loader's decode/add
 * call sites. Non-prepared names use the original routines unchanged. */
#include "managed_image.h"
#define LIMIT (16u*1024u*1024u)
#define SCRATCH_RESERVE (2u*1024u*1024u)
extern int pa_locked_asset_matches(const char*,const void*,unsigned);
extern int stock_image_add(void*,const char*,void*);
extern int stock_image_decode(void*,void*);
extern unsigned stock_bitmap_size(void*);
extern int stock_bitmap_destroy(void*);
extern void*stock_calloc(unsigned,unsigned,const char*,unsigned);
extern char*stock_strdup(const char*);
extern void stock_free(void*,const char*,unsigned);
extern uint64_t stock_time(void);
extern int stock_array_push(void*,void*);
extern int stock_asset_unref(void*,void*);
static void copy(void*d,const void*s,unsigned n){unsigned char*a=d;const unsigned char*b=s;while(n--)*a++=*b++;}
static int managed(const char*n){return n&&(!strncmp(n,"file:///tmp/xa/",15)||!strncmp(n,"/tmp/xa/",8));}
static unsigned capacity(unsigned*m){return m[7]&&m[7]<LIMIT?m[7]:LIMIT;}
static void clear(void*b){memset(b,0,72);}
static void dispose(void*b){stock_bitmap_destroy(b);clear(b);}
extern void*stock_graphic_data(void*,unsigned,unsigned,unsigned);
extern int stock_asset_ref(void*);
extern int stock_asset_info_unref(void*);
extern int stock_bitmap_line(void*,unsigned);
static unsigned rd32(const unsigned char*p){return (unsigned)p[0]|(unsigned)p[1]<<8|(unsigned)p[2]<<16|(unsigned)p[3]<<24;}
static void asset_owner_release(void*bitmap){unsigned*b=bitmap;void*asset=(void*)(uintptr_t)b[14];unsigned*m=(unsigned*)(uintptr_t)b[17];b[14]=b[15]=0;if(asset){if(m)stock_asset_unref((void*)(uintptr_t)m[5],asset);else stock_asset_info_unref(asset);}}
int pa_decode(void*asset,void*bitmap,const char*name,unsigned*manager){unsigned*info=asset,*out=bitmap;const unsigned char*p;unsigned side,bytes;void*graphic;int code=1;
 if(!managed(name))return stock_image_decode(asset,bitmap);clear(bitmap);
 if(!manager||manager[6]>capacity(manager)||capacity(manager)-manager[6]<SCRATCH_RESERVE||!asset)goto failed;
 bytes=info[1];p=(void*)(uintptr_t)info[11];if(!p||bytes<64||bytes>512*1024||!pa_locked_asset_matches(name,p,bytes))goto failed;
 if(bytes>=4&&p[0]==255&&p[1]==216){
  /* pa_info_read and pa_asset_matches admitted an exact-size prepared JPEG.
   * The stock JPEG decoder owns its new pixel buffer, not the asset bytes. */
  code=stock_image_decode(asset,bitmap);
  if(code)goto failed;
  if(!out[0]||out[0]!=out[1]||(out[0]!=80&&out[0]!=160&&out[0]!=330)){code=1;goto failed;}
  pa_note_manager(manager);return 0;
 }
 side=rd32(p+18);if(pa_size_slot(side)<0||p[0]!='B'||p[1]!='M'||rd32(p+2)!=bytes||rd32(p+10)!=64||rd32(p+14)!=40||rd32(p+22)!=0u-side||rd32(p+26)!=0x00200001||rd32(p+30)||rd32(p+34)!=side*side*4||bytes!=64+side*side*4)goto failed;
 /* The PC has already composed, oriented, resized and converted these pixels.
  * The asset buffer is retained; no JPEG/BMP codec or pixel copy is called. */
 graphic=stock_graphic_data((void*)(p+64),side,side,3);if(!graphic)goto failed;
 out[0]=out[1]=side;((unsigned short*)out)[6]=1;((unsigned short*)out)[7]=3;out[5]=(unsigned)(uintptr_t)graphic;((unsigned char*)out)[48]=1;out[17]=(unsigned)(uintptr_t)manager;stock_bitmap_line(out,side*4);
 if(stock_asset_ref(asset)){dispose(bitmap);goto failed;}
 out[14]=(unsigned)(uintptr_t)asset;out[15]=(unsigned)(uintptr_t)asset_owner_release;pa_note_manager(manager);return 0;
 failed:dispose(bitmap);if(manager&&asset)stock_asset_unref((void*)(uintptr_t)manager[5],asset);return code;
}
int pa_add(void*opaque,const char*name,void*bitmap){unsigned*m=opaque,*item,*array,bytes=0,i;char*owned_name;uint64_t now;unsigned char*b=bitmap;
 if(!managed(name))return stock_image_add(opaque,name,bitmap);
 if(!m||!bitmap)return 16;
 array=(unsigned*)(uintptr_t)m[2];
 for(i=0;i<m[0];i++){unsigned*e=(unsigned*)(uintptr_t)array[i];if(!e)continue;
  /* A borrowed alias must never free the cache's existing graphic buffer. */
  if(e[5]&&e[5]==((unsigned*)bitmap)[5]){clear(bitmap);return 1;}
  if(e[18]&&!strcmp((const char*)(uintptr_t)e[18],name)){dispose(bitmap);return 0;}
 }
 if(b[48])bytes=stock_bitmap_size(bitmap);
 if(bytes>capacity(m)||m[6]>capacity(m)-bytes){dispose(bitmap);return 1;}
 item=stock_calloc(1,96,"xm5_art",0);if(!item){dispose(bitmap);return 1;}
 owned_name=stock_strdup(name);if(!owned_name){stock_free(item,"xm5_art",0);dispose(bitmap);return 1;}
 copy(item,bitmap,72);((unsigned char*)item)[49]=0;
 item[4]=item[18]=(unsigned)(uintptr_t)owned_name;item[17]=(unsigned)(uintptr_t)m;item[19]=1;
 now=stock_time();item[20]=item[22]=(unsigned)now;item[21]=item[23]=(unsigned)(now>>32);
 if(stock_array_push(m,item)){stock_free(owned_name,"xm5_art",0);stock_free(item,"xm5_art",0);dispose(bitmap);return 1;}
 /* Commit has no remaining allocation. Existing native eviction destroys this
  * unique owner and subtracts the same bitmap size from manager+24. */
 m[6]+=bytes;pa_note_manager(m);clear(bitmap);return 0;
}
