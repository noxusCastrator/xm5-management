.set noreorder
.option pic0
.section .text.start,"ax",@progbits
.globl _start
_start:
 move $4,$29
 addiu $29,$29,-32
 jal start_c
 nop
1: b 1b
 nop
