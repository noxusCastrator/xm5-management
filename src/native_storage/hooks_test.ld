OUTPUT_ARCH(mips)
ENTRY(_start)
SECTIONS {
 . = 0x00400000; .text : { *(.text.start) *(.text*) }
 .rodata : { *(.rodata*) }
 .data : { *(.data*) }
 .MIPS.abiflags : { *(.MIPS.abiflags) }
 . = ALIGN(16); .bss : { *(.bss*) *(COMMON) }
 . = 0x007e2000; .native_class : { KEEP(*(.native_class)) }
 . = 0x00804000; .native_globals : { KEEP(*(.native_globals)) }
 . = 0x20000000; .fake_close : { KEEP(*(.fake_close)) }
 /DISCARD/ : { *(.reginfo) *(.pdr) *(.comment) *(.gnu.attributes) *(.note*) }
}
