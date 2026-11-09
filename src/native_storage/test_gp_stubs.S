.set noreorder
.option pic0
.section .probe_stub,"ax",@progbits
 lw $25,-32752($28)
 move $15,$31
 jalr $25
 li $24,7127
.section .probe_resolver,"ax",@progbits
.globl gp_resolver
gp_resolver:
 lui $8,0x80
 ori $8,$8,0x54d0
 bne $28,$8,1f
 nop
 lui $28,0x1234
 ori $28,$28,0x5678
 li $2,123
 jr $15
 nop
1:
 li $2,4001
 li $4,92
 syscall
.section .probe_got,"aw",@progbits
.word 0
