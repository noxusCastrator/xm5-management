/* Include in the audio prepared_io.c AFTER its existing read_at(),
 * same_identity(), locator and syscall wrappers. Replaces pa_inline_gain().
 * Configuration is optional; this opens/closes descriptors at track prepare,
 * never during sample processing and never changes the audio source. */
static int audio_setting(const char*s,unsigned n,const char*key,int*value){unsigned k=(unsigned)strlen(key),i;if(n<=k||strncmp(s,key,k)||s[k]!='=')return 0;i=k+1;int sign=1;if(s[i]=='-'){sign=-1;i++;}if(i==n)return 0;int v=0;for(;i<n;i++){if(s[i]<'0'||s[i]>'9'||v>100000)return 0;v=v*10+s[i]-'0';}*value=sign*v;return 1;}
int pa_inline_gain(const char*root,const char*subject,float*out){u8 h[50],level[4];struct stat64 before={0},after={0};struct xm5_block block;unsigned at,bytes;int directory=-1,fd=-1,result=XM5_ART_MISS,mid=0,lim=4000;*out=0;
 if(!root||!subject||strncmp(subject,"xm5/music/",10))return result;
 directory=(int)P_OPENAT(AT_FDCWD,root,DIRFLAGS,0);if(directory<0)return result;fd=pa_regular_open(directory,subject);if(fd<0)goto done;
 if(P_STAT(fd,&before)||!read_at(fd,0,h,sizeof(h)))goto done;
 if(!strncmp((const char*)h,"fLaC",4)){if(h[4]!=0||h[5]!=0||h[6]!=0||h[7]!=34||(h[42]&127)!=2||h[46]!='X'||h[47]!='M'||h[48]!='5'||h[49]!='I')goto done;bytes=(unsigned)h[43]<<16|(unsigned)h[44]<<8|h[45];at=143410;if(bytes<143372||bytes>143372+131072||before.st_size<46+(long long)bytes)goto done;}
 else {if(xm5_other_find(fd,&block)!=1||(uint64_t)block.header>0xffffffffu-143368u)goto done;at=(unsigned)block.header+4+143364;}
 if(!read_at(fd,at,level,4)||P_STAT(fd,&after)||!same_identity(&before,&after))goto done;int peak=(int)((unsigned)level[0]|(unsigned)level[1]<<8|(unsigned)level[2]<<16|(unsigned)level[3]<<24);if(peak==(-2147483647-1)){result=0;goto done;}if(peak< -240000||peak>24000)goto done;
 pa_close(fd);fd=pa_regular_open(directory,"xm5/Struct/settings.ini");if(fd>=0){char text[512];int n=pa_read(fd,text,sizeof(text));if(n>0&&n<(int)sizeof(text)){unsigned start=0,i;for(i=0;i<=(unsigned)n;i++)if(i==(unsigned)n||text[i]=='\n'){unsigned len=i-start;if(len&&text[start+len-1]=='\r')len--;int v;if(audio_setting(text+start,len,"gain_midpoint_mdb",&v)&&v>=-60000&&v<=0)mid=v;if(audio_setting(text+start,len,"gain_limit_mdb",&v)&&v>=0&&v<=12000)lim=v;start=i+1;}}}
 int gain=mid-peak;if(gain>lim)gain=lim;if(gain< -lim)gain=-lim;*out=(float)gain/1000.0f;result=0;
 done:pa_close(fd);pa_close(directory);return result;
}
