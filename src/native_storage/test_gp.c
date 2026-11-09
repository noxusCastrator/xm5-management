/* Actual production THUNK macro, with an exact MIPS import-stub shape. */
#include <asm/unistd.h>
extern int gp_probe(void);
extern void gp_resolver(void);
static long raw(long n,long a,long b,long c){register long v __asm__("$2")=n,r4 __asm__("$4")=a,r5 __asm__("$5")=b,r6 __asm__("$6")=c,r7 __asm__("$7")=0;__asm__ volatile("syscall":"+r"(v),"+r"(r7):"r"(r4),"r"(r5),"r"(r6):"memory");return r7?-v:v;}
void start_c(unsigned*sp){(void)sp;*(unsigned*)0x7fd4e0=(unsigned)gp_resolver;
 __asm__ volatile("lui $28,0x1234\nori $28,$28,0x5678":::"memory");
 if(gp_probe()!=123||gp_probe()!=123)raw(__NR_exit,91,0,0);
 static const char msg[]="MIPS import THUNK: canonical gp restored across two foreign-gp returns PASS\n";raw(__NR_write,1,(long)msg,sizeof(msg)-1);raw(__NR_exit,0,0,0);for(;;){}
}
