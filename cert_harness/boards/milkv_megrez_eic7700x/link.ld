/* Milk-V Megrez: final ESWIN boot-chain payload at 0x80000000. */
OUTPUT_ARCH(riscv)
ENTRY(_start)

SECTIONS
{
  . = DEFINED(BOARD_FW_LOAD_ADDR) ? BOARD_FW_LOAD_ADDR : 0x80000000;

  __text_start = .;
  .text : ALIGN(16) { *(.text .text.*) }
  __text_end = .;

  __rodata_start = .;
  .rodata : ALIGN(16) { *(.rodata .rodata.*) *(.srodata .srodata.*) }
  __rodata_end = .;

  .act_elf : ALIGN(16) {
    _act_elf_start = .;
    KEEP(*(.act_elf))
    KEEP(*(.act_elf.*))
    _act_elf_end = .;
  }

  .act_pack : ALIGN(16) {
    _act_pack_start = .;
    KEEP(*(.act_pack))
    KEEP(*(.act_pack.*))
    _act_pack_end = .;
  }

  __data_start = .;
  .data : ALIGN(16) { *(.data .data.*) *(.sdata .sdata.*) }
  __data_end = .;

  __bss_start = .;
  .bss : ALIGN(16) {
    *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON)
    __bss_end = .;
  }

  .stack (NOLOAD) : ALIGN(16) {
    __stack_bottom = .;
    . = . + (DEFINED(BOARD_FW_STACK_BYTES) ? BOARD_FW_STACK_BYTES : 0x4000);
    __stack_top = .;
  }

  /* Stock OpenSBI/Linux report 0x80000000-0x8007ffff as M-mode reserved. */
  ASSERT(__stack_top <= 0x80080000,
         "Megrez runner exceeds observed 512 KiB M-mode firmware window")

  /DISCARD/ : { *(.comment*) *(.note*) *(.eh_frame*) }
}
