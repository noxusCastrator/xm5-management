.section .native_globals,"aw",@progbits
.space 0x9000
.section .native_class,"aw",@progbits
.space 0x1000
.section .fake_close,"ax",@progbits
.set noreorder
.globl native_fake_close
native_fake_close:
 lui $8,0x2000
 bne $25,$8,1f
 nop
 lui $25,%hi(native_close_cb)
 addiu $25,$25,%lo(native_close_cb)
 jr $25
 nop
1:
 li $2,4001
 li $4,77
 syscall
