# Minimal board discovery for the UART runner

The first objective is only to build and boot the common M-mode runner and run
one ELF over UART. Do not expand discovery into a full SoC or Linux audit.

The exact allowed agent roles and fact IDs are defined in
[research-scope.md](research-scope.md). Search engines and AI models may locate
sources, but the underlying document or versioned source file is the evidence.

## Three required outputs

### 1. Boot and image contract

Record the boot medium and chain, final M-mode payload slot, image/container
format, load/install method and recovery method. Prefer SD when the board offers
a compatible path. Retain proven early firmware that initializes DRAM; replace
or load only the final runner payload.

### 2. Runner execution contract

Record the linker load/entry address, M-mode handoff, runner and monitor hart
IDs, UART base/register width/spacing needed for bidirectional access, and the
`mtime` address used by receive timeouts. Do not inventory unused interrupts,
DMA engines, display, storage or networking peripherals.

### 3. DDR placement contract

Record a conservative physical range for each of:

- runner code/data/BSS/stacks;
- ACT ELF segments and entry;
- UART receive staging buffer.

Prove that the ranges do not overlap each other or a documented reserved/MMIO
area. The full DRAM size is unnecessary when all three ranges fit inside a
documented safe subset.

## Evidence order

Use sources in this order and retain URLs, revisions, and captured logs:

1. Vendor board/SoC memory map and boot/recovery documentation.
2. Released image recipe, boot-chain configuration and bootloader source.
3. Known-good UART boot log and recoverable hardware probe.
4. BSP device tree only for a missing address or overlap question.
5. Community material only as a corroborated lead.

Generate a linker script after the selected runner, ELF and staging ranges are
non-overlapping. Inspect the final map and program headers. Investigate a
reserved region only when it intersects one of these selected ranges.

The minimal M-mode runner does not require a Linux DTB. Use a compiled or live
device tree only as one possible evidence source. A primary vendor memory map,
SoC TRM, boot-chain layout, or bootloader source is sufficient to select a
conservative low-DRAM operating window. The project may intentionally set
`BOARD_RAM_LIMIT` below the physical end of DRAM; discovering the full installed
capacity is unnecessary when the runner, payload and staging buffer stay inside
the documented window.

## Minimum peripheral contract

Confirm UART base, register spacing/width and TX/RX operation. Record clock,
reset and divisor details only if the runner cannot preserve the boot firmware's
UART setup. Confirm the `mtime` address because the current receiver uses it for
timeouts. Do not research PLIC/APLIC/IMSIC, watchdog or other peripherals unless
the basic runner actually requires them.

## Boot classification

Identify the privilege level and exact handoff point. Common cases include:

- runner replaces OpenSBI and enters in M-mode;
- vendor SPL/FIT loads an M-mode payload;
- ROM/recovery tool loads a raw payload;
- JTAG initializes DRAM and starts the runner;
- U-Boot loads an S-mode payload, which is insufficient for the current M-mode
  runner unless an earlier M-mode component deliberately provides a handoff.

Document normal boot, development/recovery boot, packaging command, install
command, and known-good recovery procedure separately.

Separate the *initialization owner* from the *runner payload*. A vendor
ROM/SPL/sys-init/DDR component may prepare hardware before loading the common
runner. A component, FIT node, or partition named `opensbi` can be only the slot
being replaced and is not proof that the runner needs an OpenSBI implementation.

Default to retaining the proven early boot chain and substituting only the
final M-mode payload. Use OpenSBI and BSP sources to learn the entry contract,
hart state, MMIO setup, and reservations. Do not copy DDR training or SBI code
unless a recoverable hardware run produces evidence that a specific operation
is missing; record that observation as a blocker before changing the runner.

## Initial-port sequence

1. Validate the evidence manifest.
2. Produce non-buildable drafts while mandatory values are unknown.
3. Create board preset, linker script, UART/startup backend and adapter.
4. Build with `build_profile.sh`.
5. Inspect ELF headers, linker map, disassembly, sizes and SHA-256 manifest.
6. Package without touching hardware.
7. With approval, validate recovery, install, capture two clean boots and
   transfer one minimal ELF.
8. Stop and report the basic runner result. Add architecture suites,
   Hypervisor, Jenkins and portal work only as separately requested phases.
