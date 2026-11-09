/* High-rate PCM is processed internally, preserving AF input metadata.
 * Only verified signed little-endian S16/S24/S32 and float32 are admitted.
 * A valid DoP stream starts with alternating 05/FA markers in every channel.
 * Inspect those first frames BEFORE any PEQ conversion or modification.
 * Ambiguous one-frame starts pass unchanged until the next frame classifies it.
 * An intentional PCM signal indistinguishable from DoP is conservatively bypassed.
 */
#ifndef XM5_NATIVE_PCM_H
#define XM5_NATIVE_PCM_H
#define XM5_NATIVE_SAMPLES 2048
static int native_format(const af_data *d) {
 return (d->format==9&&d->bps==2)||(d->format==17&&d->bps==3)||
        (d->format==25&&d->bps==4)||(d->format==29&&d->bps==4);
}
static unsigned native_word(const unsigned char *p,int bps) {
 unsigned n=p[0]|((unsigned)p[1]<<8);
 if(bps>=3)n|=(unsigned)p[2]<<16;
 if(bps==4)n|=(unsigned)p[3]<<24;
 return n;
}
static float native_read(const unsigned char *p,int format,int bps) {
 if(format==29){float f;memcpy(&f,p,4);return f;}
 unsigned n=native_word(p,bps);
 double scale=bps==2?65536.0:bps==3?16777216.0:4294967296.0;
 double v=(double)n;if(n & (bps==2?0x8000u:bps==3?0x800000u:0x80000000u))v-=scale;
 return (float)(v/(scale*0.5));
}
static void native_write(unsigned char *p,int format,int bps,float f) {
 if(!__builtin_isfinite(f))f=0;
 if(format==29){memcpy(p,&f,4);return;}
 double scale=bps==2?32768.0:bps==3?8388608.0:2147483648.0;
 double v=(double)f*scale;
 if(v>=scale)v=scale-1;
 if(v< -scale)v= -scale;
 /* Cast toward zero, same policy as the existing float-to-integer AF path.
  * PEQ sanitizes nonfinite input before an active output reaches this cast. */
 unsigned n=(unsigned)(int)v;
 for(int i=0;i<bps;++i){p[i]=(unsigned char)n;n>>=8;}
}
static int dop_sample_marker(const unsigned char *p,int format,int bps) {
 if(bps<3)return 0;
 unsigned n;
 if(format==29){
  float f;memcpy(&f,p,4);
  if(!__builtin_isfinite(f)||f>=1||f< -1)return 0;
  n=(unsigned)(int)((double)f*2147483648.0);
 } else n=native_word(p,bps);
 unsigned hi=bps==3?n>>16:n>>24;
 if(hi==5||hi==250)return (int)hi;
 /* Some S32 containers put signed 24-bit DoP in the low three bytes. */
 if(bps==4&&(hi==0||hi==255)){
  hi=(n>>16)&255;if(hi==5||hi==250)return (int)hi;
 }
 return 0;
}
static int dop_frame_marker(const unsigned char *p,const af_data *d) {
 int marker=dop_sample_marker(p,d->format,d->bps);
 if(!marker)return 0;
 for(int ch=1;ch<d->nch;++ch)
  if(dop_sample_marker(p+ch*d->bps,d->format,d->bps)!=marker)return 0;
 return marker;
}
/* state: 0 not classified, 1 PCM, 2 DoP, 05/FA awaiting second frame.
 * Return 1 only after a PCM observation; unknown and DoP pass unchanged. */
static int classify_native_start(int *state,const af_data *d) {
 if(*state==1)return 1;
 if(*state==2)return 0;
 int frames=d->len/(d->nch*d->bps);
 const unsigned char *p=d->audio;
 for(int i=0;i<frames;++i,p+=d->nch*d->bps){
  int marker=dop_frame_marker(p,d);
  if(*state==0){
   if(!marker){*state=1;return 1;}
   *state=marker;
  } else {
   if(marker && marker!=*state){*state=2;return 0;}
   *state=1;return 1;
  }
 }
 return 0;
}
#endif
