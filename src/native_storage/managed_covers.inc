/* Native local consumers. Index +12 is the complete music source URL, never
 * the displayed title or a basename. Managed misses are explicit no-art. */
extern int stock_album_row(void*,const char*,int);
extern int stock_album_header(void*);
extern int stock_album_detail(unsigned,void*);
extern int stock_album_track(void*,int);
extern int stock_class_track(void*,int);
extern int stock_playlist_track(void*,int);
extern int stock_all_track(void*,void*);
extern int stock_all_album(void*,void*);
extern int stock_search_cover(void*,void*);
extern int stock_cover_info(int,char*,char*);
static char selected_album_path[1024];
static int owned_music(const char*p){return p&&!strncmp(p,ROOT "/xm5/music/",19)&&prepared_namespace(p);}
static void*list_record(void*d,int index){if(!d||index<0||(unsigned)index>=stock_deque_size(d))return 0;return stock_deque_at(d,(unsigned)index);}
static int source_record(void*r,char path[1024]){return r&&!text(path,1024,*(char**)((char*)r+12))&&owned_music(path);}
static int prepared_widget(void*w,const char*path,const char*def){char uri[64];unsigned g;int code;const char*was;
 if(!w)return 16;unsigned side=widget_side(w);
 if(!side)return pa_image_set(w,def);
 prepare(path,"playback");code=cached(path,side,uri,&g);was=stock_get_image_prop(w,"image","");
 const char*next=code==XM5_ART_OK?uri:def;if(was&&!strcmp(was,next)){if(code==XM5_ART_OK)return pa_image_set(w,next);return 0;}
 int result=pa_image_set(w,next);if(!result&&code==XM5_ART_OK)stock_draw_type(w,4);return result;
}
static int playlist_widget(void*parent,const char*name,void*r){char subject[129],uri[64];xm5_art_record rec;int code;void*w;
 if(!r||*(unsigned*)(uintptr_t)0x7e2bfc!=0xf00b||*(unsigned*)r>=100||!managed_card())return -99;
 w=stock_lookup(parent,name,1);if(!w)return 16;unsigned side=widget_side(w);if(!side)return -99;
 stock_mutex_lock(io_lock);code=pa_playlist_subject(&io_card,*(unsigned*)r,*(const char**)((char*)r+16),subject,sizeof(subject));
 if(!code){code=pa_lookup_size(&io_card,"playlist",subject,side,&rec);if(!code)code=pa_native_uri(&rec,RAM,uri,sizeof(uri));}stock_mutex_unlock(io_lock);
 if(code==XM5_ART_MISS)return -99;int result=pa_image_set(w,code==0?uri:"default_album_big");if(!result&&!code)stock_draw_type(w,4);return result;
}
/* The ordinary custom-playlist list has its own anonymous image constructor.
 * Its deque uses the original 88-byte file record: native playlist id at +64,
 * display name at +16. It does not pass through the album-row consumer. */
int pa_custom_playlist_cover(void*w,unsigned index){char subject[129],uri[64];xm5_art_record rec;int code=XM5_ART_MISS;
 void*r=list_record(PTR32(0x805dd0),(int)index);
 if(w&&r&&managed_card()){
  unsigned side=widget_side(w);
  if(side){
   stock_mutex_lock(io_lock);
   code=pa_playlist_subject(&io_card,*(unsigned*)((char*)r+64),*(const char**)((char*)r+16),subject,sizeof(subject));
   if(!code){code=pa_lookup_size(&io_card,"playlist",subject,side,&rec);if(!code)code=pa_native_uri(&rec,RAM,uri,sizeof(uri));}
   stock_mutex_unlock(io_lock);
  }
 }
 int result=pa_image_set(w,code==XM5_ART_OK?uri:"icon_music");
 if(!result&&code==XM5_ART_OK)stock_draw_type(w,4);return result;
}
static int managed_row(void*parent,const char*name,int index){char path[1024];void*r=list_record(PTR32(0x805de4),index);
 if(!source_record(r,path))return playlist_widget(parent,name,r);return prepared_widget(stock_lookup(parent,name,1),path,"default_track_small");}
int pa_album_row(void*parent,const char*name,int index){int r=managed_row(parent,name,index);return r==-99?stock_album_row(parent,name,index):r;}
int pa_album_track(void*parent,int index){int r=managed_row(parent,"img_icon",index);return r==-99?stock_album_track(parent,index):r;}
int pa_class_track(void*parent,int index){int r=managed_row(parent,"img_icon",index);return r==-99?stock_class_track(parent,index):r;}
int pa_playlist_track(void*parent,int index){int r=managed_row(parent,"img_icon",index);return r==-99?stock_playlist_track(parent,index):r;}
int pa_album_detail(unsigned mode,void*r){selected_album_path[0]=0;if(r&&!source_record(r,selected_album_path))selected_album_path[0]=0;return stock_album_detail(mode,r);}
int pa_album_header(void*parent){if(!selected_album_path[0])return stock_album_header(parent);return prepared_widget(stock_lookup(parent,"img_albuminfo",1),selected_album_path,"default_album_big");}
static int widget_index(void*w,const char*prefix){const char*n=w?*(const char**)((char*)w+16):0;unsigned i=0,index=0;if(!n)return -1;
 while(prefix[i]&&prefix[i]==n[i])i++;if(prefix[i]||n[i]!='_')return -1;i++;
 if(n[i]<'0'||n[i]>'9')return -1;while(n[i]>='0'&&n[i]<='9'){if(index>1000000)return -1;index=index*10+(unsigned)(n[i++]-'0');}
 return n[i]?-1:(int)index;
}
static int managed_named_widget(void*w,void*d,const char*prefix){char path[1024];int index=widget_index(w,prefix);void*r=list_record(d,index);if(!source_record(r,path))return -99;return prepared_widget(w,path,"default_track_small");}
int pa_all_track(void*context,void*w){int r=managed_named_widget(w,PTR32(0x805de4),"imgtrack");return r==-99?stock_all_track(context,w):r;}
int pa_all_album(void*context,void*w){int r=managed_named_widget(w,PTR32(0x805de4),"imgalbum");return r==-99?stock_all_album(context,w):r;}
int pa_search_cover(void*context,void*w){int r=managed_named_widget(w,PTR32(0x805dd4),"imgtrack");if(r==-99)r=managed_named_widget(w,PTR32(0x805dd4),"imgalbum");return r==-99?stock_search_cover(context,w):r;}
/* Called before the worker hashes/copies a title or starts a converter. Copy
 * ordinary entries with bounds too: stock strcpy overflowed its 256-byte key. */
int pa_cover_info(int index,char*key,char*source){void*r;int result=0;key[0]=source[0]=0;
 stock_mutex_lock((void*)(uintptr_t)0x805d9c);r=list_record(PTR32(0x805ddc),index);
 if(r){const char*p=*(const char**)((char*)r+8),*k=*(const char**)((char*)r+4);
  if(!prepared_namespace(p)&&!text(key,256,k)&&!text(source,1024,p))result=1;
 }
 if(!result)key[0]=source[0]=0;stock_mutex_unlock((void*)(uintptr_t)0x805d9c);return 0;
}
/* Additional mini-player/screen-saver widgets use the same stock playing image.
 * Resolve by actual current track and widget dimensions, not the fixed filename. */
int pa_play_image_name(void*w,const char*name,char out[64]){char path[1024];unsigned seq,g,side;if(!name||!w)return 0;
 if(strcmp(name,"file:///tmp/coverpic.jpg")&&strcmp(name,"/tmp/coverpic.jpg"))return 0;
 take();text(path,sizeof(path),playing);seq=play_seq;release();if(!owned_music(path))return 0;
 side=widget_side(w);if(!side){text(out,64,"default_cover");return 1;}
 prepare(path,"playback");int code=cached(path,side,out,&g);if(!pa_render_valid(g,seq)||code!=XM5_ART_OK)text(out,64,"default_cover");return 1;
}
extern int stock_widget_load(void*,const char*,void*);
int pa_widget_load(void*w,const char*name,void*bitmap){char prepared[64];if(pa_play_image_name(w,name,prepared))name=prepared;return stock_widget_load(w,name,bitmap);}
/* Backstop for an unmanaged parent directory containing managed children.
 * Do not extract a managed track or re-encode an original managed picture if
 * an older producer reaches these helpers before its consumer is replaced. */
extern int stock_legacy_extract(const char*,const char*,int,int);
extern int stock_legacy_jpeg(const char*,const char*,int,int);
extern int stock_legacy_png(const char*,const char*,int,int);
int pa_legacy_extract(const char*src,const char*dst,int w,int h){return prepared_namespace(src)?0:stock_legacy_extract(src,dst,w,h);}
int pa_legacy_jpeg(const char*src,const char*dst,int w,int h){return prepared_namespace(src)?0:stock_legacy_jpeg(src,dst,w,h);}
int pa_legacy_png(const char*src,const char*dst,int w,int h){return prepared_namespace(src)?0:stock_legacy_png(src,dst,w,h);}
