OUTPUT_ARCH(mips)
ENTRY(_start)
SECTIONS {
 . = 0x00400000; .text : { *(.text.start) *(.text*) }
 .rodata : { *(.rodata*) }
 .data : { *(.data*) }
 .MIPS.abiflags : { *(.MIPS.abiflags) }
 . = ALIGN(16); .bss : { *(.bss*) *(COMMON) }
 . = 0x00628030; .paint_cast : { *(.paint_cast) }
 . = 0x00628074; .native_paint : { *(.native_paint) }
 . = 0x00643ec4; .paint_draw : { *(.paint_draw) }
 . = 0x006448d4; .paint_vg : { *(.paint_vg) }
 . = 0x0064fcfc; .paint_transform : { *(.paint_transform) }
 . = 0x007fd000; .paint_got : { *(.paint_got) }
 /DISCARD/ : { *(.reginfo) *(.pdr) *(.comment) *(.gnu.attributes) *(.note*) }
}
