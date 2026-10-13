/* CSR probe layout. scripts/csr_probe.py passes --defsym=PROBE_BASE=<payload base>. */
OUTPUT_ARCH("riscv")
ENTRY(_start)
SECTIONS
{
  . = DEFINED(PROBE_BASE) ? PROBE_BASE : 0x80000000;
  .text.init : { *(.text.init) }
  .text : { *(.text .text.*) }
  . = ALIGN(0x1000);
  .data : { *(.data .data.*) }
  .tohost : ALIGN(0x100) { *(.tohost) }
  .bss : { *(.bss .bss.*) }
  _end = .;
}
