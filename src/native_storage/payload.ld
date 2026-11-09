OUTPUT_ARCH(mips)
ENTRY(pa_worker_play)
SECTIONS {
 . = 0x00900000;
 .text : { *(.text*) }
 .rodata : { *(.rodata*) }
 .data : { *(.data*) }
 . = ALIGN(16); __art_file_end = .;
 .bss (NOLOAD) : { *(.bss*) *(COMMON) }
 __art_mem_end = .;
 /DISCARD/ : { *(.MIPS.abiflags) *(.reginfo) *(.pdr) *(.gnu.attributes) *(.comment) *(.note*) }
}
