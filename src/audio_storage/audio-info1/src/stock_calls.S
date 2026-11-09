/* Original functions remain byte-identical. Set t9 for each PIC entry. */
.set noreorder
.text
.macro CALL name, addr
.globl \name
.type \name,@function
\name:
 lui $25, (\addr >> 16)
 ori $25, $25, (\addr & 0xffff)
 jr $25
 nop
.endm
CALL xm5_stock_open, 0x44fe04
CALL xm5_stock_control, 0x44fec8
CALL xm5_stock_play, 0x44fae0
CALL xm5_stock_uninit, 0x44fe9c
.globl xm5_stock_report
xm5_stock_report:
 addiu $29,$29,-56
 sw $16,36($29)
 j 0x42a61c
 nop
