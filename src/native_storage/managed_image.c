/* GUI-thread ownership by URI. No raw borrowed bitmap survives a paint call. */
#include "managed_image.h"
#define OWNERS 256
#define MANAGERS 16
#define PAINTS 8
typedef struct {void*widget;void*manager;unsigned generation,sequence,play_bound;char uri[64];} reference;
typedef struct {void*widget;unsigned generation,sequence;} paint_frame;
static reference refs[OWNERS];
static void*managers[MANAGERS];
static unsigned pending[MANAGERS];
static paint_frame frames[PAINTS];
static unsigned painting,timer_id,idle_id;
extern const char*stock_get_image_prop(void*,const char*,const char*);
extern void*stock_widget_image_manager(void*);
extern int stock_set_image(void*,const char*);
extern int stock_image_destroy(void*);
extern int stock_manager_destroy(void*);
extern int stock_image_paint(void*,void*);
extern int stock_need_transform(void*);
extern int stock_canvas_image_ex(void*,void*,unsigned,const int*);
extern int stock_canvas_image(void*,void*,const int*,const int*);
extern int stock_owner_drop(void*);
extern unsigned stock_timer_add(void*,void*,unsigned);
extern unsigned stock_idle_add(void*,void*);
int pa_managed_name(const char*n){return n&&(!strncmp(n,"file:///tmp/xa/",15)||!strncmp(n,"/tmp/xa/",8));}
static void copy_name(char*out,const char*s){unsigned n=0;while(s&&s[n]&&n<63){out[n]=s[n];n++;}out[n]=0;}
/* Fields verified in widget_set_visible_only/widget_get_window and paint code:
 * x/y/w/h at0/4/8/12, visible bit2 at36, parent at68. */
static int active(void*widget){int x=0,y=0,w=0,h=0;unsigned depth=0;unsigned char*p=widget;
 if(!p)return 0;w=*(int*)(p+8);h=*(int*)(p+12);if(w<=0||h<=0)return 0;
 for(;p&&depth++<64;p=*(void**)(p+68)){
  if(!(*(unsigned short*)(p+36)&4))return 0;
  if(depth>1&&*(int*)(p+8)>0&&*(int*)(p+12)>0&&(x>=*(int*)(p+8)||y>=*(int*)(p+12)||x+w<=0||y+h<=0))return 0;
  x+=*(int*)p;y+=*(int*)(p+4);
 }
 return !p;
}
static int idle_gc(void*info){(void)info;if(painting)return 8;idle_id=0;pa_collect();return 0;}
static int timer_gc(void*info){(void)info;pa_collect();return 8;}
static void schedule(void){if(!idle_id)idle_id=stock_idle_add((void*)idle_gc,0);if(!timer_id)timer_id=stock_timer_add((void*)timer_gc,0,200);}
void pa_note_manager(void*m){unsigned i;if(!m)return;for(i=0;i<MANAGERS;i++)if(managers[i]==m)return;for(i=0;i<MANAGERS;i++)if(!managers[i]){managers[i]=m;schedule();return;}}
static int forget_widget(void*w){unsigned i;int found=0;for(i=0;i<OWNERS;i++)if(refs[i].widget==w){memset(&refs[i],0,sizeof(refs[i]));found=1;}return found;}
static int remember(void*w,const char*uri,int play_bound){unsigned i,slot=OWNERS;void*m=stock_widget_image_manager(w);
 for(i=0;i<OWNERS;i++){if(refs[i].widget==w){slot=i;break;}if(!refs[i].widget)slot=i;}
 if(slot==OWNERS)for(i=0;i<OWNERS;i++)if(!active(refs[i].widget)){slot=i;break;}
 if(slot==OWNERS)return 1;refs[slot].widget=w;refs[slot].manager=m;refs[slot].play_bound=(unsigned)play_bound;pa_render_stamp(&refs[slot].generation,&refs[slot].sequence);copy_name(refs[slot].uri,uri);pa_note_manager(m);return 0;
}
extern int pa_play_image_name(void*,const char*,char[64]);
static int assign_image(void*w,const char*name,int play_bound){
 if(pa_managed_name(name)){const char*old=stock_get_image_prop(w,"image","");if(old&&!strcmp(old,name)){if(remember(w,name,play_bound))return stock_set_image(w,"default_cover");return 0;}}
 int result=stock_set_image(w,name);if(result)return result;
 if(pa_managed_name(name)){if(remember(w,name,play_bound))return stock_set_image(w,"default_cover");}
 else if(!forget_widget(w))return 0;schedule();return 0;
}
int pa_image_set(void*w,const char*name){char prepared[64];int bound=pa_play_image_name(w,name,prepared);if(bound)name=prepared;return assign_image(w,name,bound);}
int pa_play_image_set(void*w,const char*name){return assign_image(w,name,1);}
int pa_image_destroy(void*w){if(forget_widget(w))schedule();return stock_image_destroy(w);}
int pa_manager_destroy(void*m){unsigned i;if(painting){for(i=0;i<MANAGERS;i++)if(managers[i]==m){pending[i]=1;schedule();return 0;}}for(i=0;i<OWNERS;i++)if(refs[i].manager==m)memset(&refs[i],0,sizeof(refs[i]));for(i=0;i<MANAGERS;i++)if(managers[i]==m){managers[i]=0;pending[i]=0;}return stock_manager_destroy(m);}
int pa_collect(void){unsigned k,i,j,n,g,s;int used;if(painting)return 8;pa_render_stamp(&g,&s);int card_valid=pa_render_valid(g,s);
 for(k=0;k<MANAGERS;k++){unsigned*m=managers[k];if(!m)continue;if(pending[k]){pa_manager_destroy(m);continue;}i=0;
  while(i<m[0]){unsigned*items=(unsigned*)(uintptr_t)m[2];unsigned*entry=(unsigned*)(uintptr_t)items[i];const char*name=entry?(const char*)(uintptr_t)entry[18]:0;
   if(!pa_managed_name(name)){i++;continue;}used=0;
   for(j=0;j<OWNERS;j++)if(card_valid&&refs[j].generation==g&&(!refs[j].play_bound||refs[j].sequence==s)&&refs[j].manager==m&&refs[j].widget&&!strcmp(refs[j].uri,name)&&active(refs[j].widget)){used=1;break;}
   if(used){i++;continue;}n=m[0];for(j=i+1;j<n;j++)items[j-1]=items[j];m[0]=n-1;items[n-1]=0;stock_owner_drop(entry);
  }
 }
 return 0;
}
int pa_image_paint(void*w,void*canvas){const char*name=stock_get_image_prop(w,"image","");int result;unsigned g,s,i;reference*r=0;
 if(!pa_managed_name(name))return stock_image_paint(w,canvas);if(painting==PAINTS)return 1;
 pa_render_stamp(&g,&s);for(i=0;i<OWNERS;i++)if(refs[i].widget==w&&!strcmp(refs[i].uri,name)){r=&refs[i];break;}
 if(!r||r->generation!=g||(r->play_bound&&r->sequence!=s)||!pa_render_valid(g,s)){forget_widget(w);stock_set_image(w,"default_cover");schedule();return 0;/* Setter invalidates; next ordinary paint draws the placeholder. */}
 frames[painting].widget=w;frames[painting].generation=g;frames[painting].sequence=s;painting++;
 result=stock_image_paint(w,canvas);painting--;if(!painting)schedule();return result;
}
int pa_need_transform(void*w){return pa_managed_name(stock_get_image_prop(w,"image",""))?0:stock_need_transform(w);}
int pa_canvas_image(void*canvas,void*bitmap,unsigned type,const int*rect){unsigned*w=bitmap;const char*name=(const char*)(uintptr_t)w[4];int src[4],dst[4];
 if(!pa_managed_name(name))return stock_canvas_image_ex(canvas,bitmap,type,rect);
 if(!painting||!pa_render_valid(frames[painting-1].generation,frames[painting-1].sequence))return 1;
 if(pa_size_slot(w[0])<0||w[1]!=w[0])return 1;
 int fitting=rect[2]<rect[3]?rect[2]:rect[3];
 if((int)w[0]!=fitting)return stock_canvas_image_ex(canvas,bitmap,type,rect);
 src[0]=src[1]=0;src[2]=src[3]=(int)w[0];dst[0]=rect[0]+(rect[2]-src[2])/2;dst[1]=rect[1]+(rect[3]-src[3])/2;dst[2]=src[2];dst[3]=src[3];
 /* Identical source/destination pixel dimensions: no scale or resampling call. */
 return stock_canvas_image(canvas,bitmap,src,dst);
}
