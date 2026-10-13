# Runner architecture and file ownership

## Reusable runtime

Normally reuse these files without board forks:

- `main_act.c`: runner entry and payload transport selection.
- `runner_uart_stream.c`: READY/header/payload/CRC/DONE protocol.
- `act_core.c`: ELF parsing, range validation, loading, and shared execution support.
- `act_runtime.c`: ACT payload execution.
- `runner_machine_trap.c`: machine trap capture.
- `runner_monitor.c`: completion monitoring and reporting.
- `runner_shared.h`: shared contracts and board-supplied compile-time values.
- `cert_harness/uart_stream/send_elf.py`: single-ELF host transport.
- `cert_harness/uart_stream/run_elf_batch.py`: reset/power-cycle batch orchestration.

Change shared runtime code only when a documented platform requirement cannot be expressed through the board preset, linker script, adapter, or a narrowly isolated backend.

## Board-owned files

Create or select:

```text
cert_harness/boards/<board>.env
cert_harness/boards/<board>/link.ld
cert_harness/board_adapters/<board>.sh
```

The board preset owns ISA identity, hart IDs, DRAM bounds, firmware placement, stack size, staging buffer, UART/timer MMIO, watchdog, and selected transport.

The linker script owns actual placement and reserved-memory exclusions. The board adapter owns image packaging, installation/loading, UART capture, and power/reset actions.

## Boot boundary

SD is not part of the UART protocol. A board may start the runner from SD/eMMC, SPI flash, USB recovery, TFTP/U-Boot, JTAG RAM loading, or a vendor tool. Adapt only the packaging and boot adapter where possible. After the runner emits `READY`, reuse the common host protocol.

VF2 and BPI-F3 establish the default architecture: the same
`firmware_act.bin` is packaged for different load addresses and boot slots.
Earlier ROM/SPL/vendor firmware owns any required SoC and DRAM initialization;
the runner does not install OpenSBI or duplicate vendor DDR training. A FIT
node or partition named `opensbi` describes the boot-chain slot, not the actual
contents of the common runner.

For a new board, first retain the known-good early boot components and replace
or load only the final M-mode payload. Do not add DDR initialization, OpenSBI,
SBI services, or Linux handoff code unless a recoverable hardware experiment
shows that the common runner cannot start without one specific service. Treat
vendor OpenSBI source as evidence about the handoff and hardware state, not as
code that must automatically be imported.

## Memory boundary

Derive these from primary evidence before linking or loading:

- Usable DRAM base and limit.
- Bootloader, firmware, secure, device, DMA, framebuffer, and reserved regions.
- Runner code/data/stack range.
- UART receive staging range and maximum ELF size.
- Permitted payload segment ranges and entry points.

These are physical-address contracts, not device-tree dependencies. Prefer a
small documented DRAM window over waiting for the complete installed-memory
topology. The UART staging buffer is a board-policy selection inside another
documented safe interval; it is not a value supplied to the runner by Linux.

The ELF loader must reject segments outside usable RAM or overlapping the runner, staging buffer, or reserved regions.

## CI boundary

Reuse result conversion and portal publishing. A new board adds only board identity, hardware resource/lock, serial/power configuration, sanity selection, weekly selection, and portal metadata.
