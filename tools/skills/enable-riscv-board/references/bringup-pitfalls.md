# Bring-up pitfalls (learned on Milk-V Megrez)

Each item cost hours on a real board. Check them before concluding that the
runner or a test is broken.

## Host serial path

- **Onboard USB-UART bridges.** Many boards wire an onboard CH340/FTDI to the
  console UART behind a USB-C "debug" port. While that port is powered, the
  bridge drives the SoC RX line and an external USB-TTL adapter cannot
  transmit (output still reads fine). Symptom: runner prints `READY`, then
  `rx_timeout phase=header`. Check the schematic (fact `uart.host_bridge`) and
  use either the onboard port or the external adapter with USB-C unplugged.
- **Prove each direction separately.** Adapter self-loopback (TX↔RX), then a
  stock Linux login prompt answering Enter, then the runner. Do not change
  runner code until the bench passes these.
- Prefer `/dev/serial/by-path/...` when two identical bridges are attached:
  CH340 `by-id` names are generic and `ttyUSBn` numbers change on replug.

## UART and timers

- **Who initialized the UART?** If the runner replaces the stage that set up
  the console (usually OpenSBI's 8250 driver), the runner must do the 8250
  init itself (`BOARD_UART_8250_INIT_ENABLE=1`). `collect_board_facts.py`
  infers this from the OpenSBI banner.
- **mtime rate scales every timeout.** Timeouts are in mtime ticks. At 1 MHz
  the default 200 M-tick RX timeout is about 200 s, so a host that waits 10 s
  never sees the runner's error line. Record `timer.mtime_hz`.
- Register width: DTBs can disagree (Megrez U-Boot says 2, Linux says 4); both
  worked for TX. Prefer the width proven by the most exercised software.

## Boot and install

- **Recovery modes only boot from RAM.** They need their USB link (often
  USB-C) and never change the boot flash. A permanent install needs a tool
  that writes flash, typically the vendor U-Boot (`es_burn`, `sf`, …).
- **Vendor U-Boot can be sensitive to the console port state.** Megrez U-Boot
  failed to start when the host held the onboard port open during boot. Watch
  the boot on another receiver and open the console at the autoboot countdown
  (`megrez_uboot_session.py --watch-dev`).
- Rehearse the install and the restore (load to RAM + CRC check) before any
  flash write; keep the exact restore image (match the U-Boot banner build).
- Keep dependencies (packaging trees, vendor blobs) outside `/tmp`; a reboot
  wipes them.

## ACT and reference model

- **Spec versions gate test generation.** UDB's ratified H 1.0 requires
  S/Sm ≥ 1.12. A priv-1.11 core with (draft) H needs an explicit, documented
  deviation for UDB while `sail.json` keeps the real version.
- **Leftover H state.** The runner replaces OpenSBI and does not initialize H
  CSRs; non-zero `hvip` shows up as VSEIP/VSTIP in `mip` and fails interrupt
  tests. Read it with the CSR probe; clear H state in the runner if set.
- **WARL legalization differs from Sail.** Example: P550 sign-extends
  `stvec.BASE` above bit 50; Sail keeps all 64 bits. Classify as a
  model/config difference, not a hardware bug.
- Sail rejects delegatable bits it models itself (VS bits in `mideleg` with H
  enabled). Validate `sail.json` with `sail_riscv_sim --validate-config`.
- Board linker scripts must place ACT ELFs inside the runner payload window,
  not at a default `0x80000000`.
