/* Shared node admission only. Stock execution remains <=48 kHz inside A4.
 * Return to the original conditional branches, preserving their delay slots.
 * No call: no stack, gp, ra or saved-register changes. */
.set noreorder
.set noat
.text
.globl xm5_gate_a
.ent xm5_gate_a
xm5_gate_a:
 lui $2,0xb0
 lw $2,-25040($2)
 beqz $2,1f
 nop
 lw $2,44($2)
 beqz $2,1f
 nop
 lw $2,60($2)
 lui $3,2
 ori $3,$3,0xee01 /* 192001 */
 slt $2,$2,$3
1:
 j 0x42ce44
 nop
.end xm5_gate_a
.globl xm5_gate_b
.ent xm5_gate_b
xm5_gate_b:
 lw $4,-25040($19)
 move $2,$0
 beqz $4,2f
 nop
 lw $2,44($4)
 beqz $2,2f
 nop
 lw $2,60($2)
 lui $3,2
 ori $3,$3,0xee01
 slt $2,$2,$3
2:
 j 0x42fad0
 nop
.end xm5_gate_b
