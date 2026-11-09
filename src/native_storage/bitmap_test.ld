OUTPUT_ARCH(mips)
ENTRY(_start)
SECTIONS {
 . = 0x00400000; .text : { *(.text.start) *(.text*) }
 .rodata : { *(.rodata*) }
 .data : { *(.data*) }
 .MIPS.abiflags : { *(.MIPS.abiflags) }
 . = ALIGN(16); .bss : { *(.bss*) *(COMMON) }
 . = 0x0063ab44; .owner_bitmap : { *(.owner_bitmap) }
 . = 0x0063d9c4; .owner_size : { *(.owner_size) }
 . = 0x0064e320; .owner_graphic : { *(.owner_graphic) }
 . = 0x006501cc; .native_owner : { *(.native_owner) }
 . = 0x007fd000; .owner_got : { *(.owner_got) }
 /DISCARD/ : { *(.reginfo) *(.pdr) *(.comment) *(.gnu.attributes) *(.note*) }
}
