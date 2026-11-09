/* payload.lds -- B1 fixed-address layout for the injected AF payload.
 * Single contiguous image at the reserved vaddr 0xe10000 (G1: >= load_end
 * 0xe03b38, non-overlap). Non-PIC: all code/data use absolute addressing, so
 * the link vaddr MUST equal the final load vaddr. No dynamic section, no GOT.
 * .bss is NOBITS: it adds to memsz only; the appended file image is text+
 * rodata+data, and the PT_LOAD memsz covers .bss (calloc-backed state is heap,
 * so .bss is expected to be tiny/zero -- verified in the build log). */
ENTRY(xm5_peq_open)
SECTIONS
{
  . = 0xe10000;
  .text : {
    *(.text.xm5_peq_open)        /* entry first, so its vaddr == section base */
    *(.text .text.* .stub .gnu.linkonce.t.*)
  }
  . = ALIGN(16);
  .rodata : { *(.rodata .rodata.* .gnu.linkonce.r.*) }
  . = ALIGN(16);
  .data : { *(.data .data.* .gnu.linkonce.d.*) }
  . = ALIGN(16);
  .bss (NOLOAD) : { *(.bss .bss.* .sbss .sbss.* COMMON) }
  _payload_end = .;
  /* Discard everything we must not emit into a flat non-PIC blob. */
  /DISCARD/ : {
    *(.MIPS.abiflags) *(.reginfo) *(.gnu.attributes)
    *(.pdr) *(.comment) *(.note.* ) *(.eh_frame) *(.gcc_except_table)
    *(.rel.* ) *(.rela.*)
  }
}
