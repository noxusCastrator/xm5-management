OUTPUT_ARCH(mips)
ENTRY(_start)
SECTIONS {
 . = 0x400000; .text : { *(.text.start) *(.text*) }
 .rodata : { *(.rodata*) }
 .data : { *(.data*) }
 .MIPS.abiflags : { *(.MIPS.abiflags) }
 . = ALIGN(16); .bss : { *(.bss*) *(COMMON) }
 . = 0x7fd4e0; .probe_got : { *(.probe_got) }
 . = 0x20001000; .probe_stub : { *(.probe_stub) }
 . = 0x20002000; .probe_resolver : { *(.probe_resolver) }
 /DISCARD/ : { *(.reginfo) *(.pdr) *(.comment) *(.gnu.attributes) *(.note*) }
}
