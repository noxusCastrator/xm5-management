OUTPUT_ARCH(mips)
ENTRY(_start)
SECTIONS {
 . = 0x00400000;
 .text : { *(.text.start) *(.text*) }
 .rodata : { *(.rodata*) }
 .data : { *(.data*) }
 .MIPS.abiflags : { *(.MIPS.abiflags) }
 . = ALIGN(16);
 .bss : { *(.bss*) *(COMMON) }
 /DISCARD/ : { *(.reginfo) *(.pdr) *(.comment) *(.gnu.attributes) *(.note*) }
}
