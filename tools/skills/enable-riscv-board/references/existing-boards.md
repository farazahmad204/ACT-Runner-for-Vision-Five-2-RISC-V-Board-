# Existing board implementations

Treat board presets, adapters, linker inputs, build manifests, and UART logs as
the executable source of truth. Run `system_inventory.py` before relying on this
overview.

## Shared implementation

Both boards reuse the common M-mode runner, UART framing, ELF loader, trap
capture, result monitor, host sender, batch runner, JUnit conversion, and portal
publication. Board differences belong in:

```text
cert_harness/boards/<board>.env
cert_harness/board_adapters/<board>.sh
board-selected linker input
board boot/FIT source
ACT target configuration
Jenkins parameters and hardware lock
```

Both images contain the same `firmware_act.bin`. Neither runner performs vendor
DDR initialization or installs OpenSBI. On BPI-F3, `bpif3_opensbi.its` and the
`opensbi` GPT partition are packaging/boot-slot names; the slot contains the
minimal ACT runner. Use this replacement model as the default for new boards.

Use `cert_harness/tools/build_profile.sh` to combine a board preset, execution
profile, and transport. Its build manifest records the exact board values,
profile, transport, Git revision, toolchain, sizes, and hashes.

## VisionFive 2 / JH7110

- Board ID: `vf2_jh7110`; platform ID `1`.
- Runner/monitor harts: 1/2.
- Runner link address: `0x40000000`.
- UART: `0x10000000`, 32-bit register I/O.
- UART staging: `0x88000000`, maximum 128 MiB.
- Packaging: `vf2_act4.its` produces the padded `uboot_part2_new.bin`/`boot_image.bin` image.
- Adapter: `cert_harness/board_adapters/vf2_jh7110.sh` delegates flashing to `vf2_act_flash.sh`.
- Jenkins lock: `vf2-hardware`; portal slug: `visionfive2`.

## Banana Pi BPI-F3 / SpacemiT K1

- Board ID: `bpif3_k1`; platform ID `2`.
- Runner/monitor harts: 0/1.
- Runner link address: `0x04000000`.
- UART: `0xD4017000`.
- UART staging: `0x0C200000`, maximum 128 MiB.
- K1 secondary-hart wakeup is board-enabled.
- Packaging: `bpif3_opensbi.its` produces `bpif3_opensbi.itb`/`boot_image.bin`.
- The adapter writes the image to GPT partition 3 (`opensbi`) on the selected SD device.
- Jenkins lock: `bpif3-hardware`; portal slug: `bananapi-f3`.

## Milk-V Megrez / ESWIN EIC7700X (SiFive P550)

- Board ID: `milkv_megrez_eic7700x`; platform ID `3`.
- Runner/monitor harts: 1/2. Runner window `0x80000000-0x80080000`.
- UART0 `0x50900000`, 16-bit access, runner-owned 8250 init.
- Payload window `0x90000000-0xB0000000`; excluded DDR `0xB0000000-0xC0000000`;
  UART staging `0xC0000000`, 128 MiB.
- Packaging: ESWIN `nsign` keeps `sys_init.bin` + `ddr_fw.bin` and replaces
  `fw_payload.bin`; installed in SPI flash with the official U-Boot (`es_burn`).
- Console for tests: external USB-TTL adapter with USB-C unplugged, or the
  onboard USB-C CH340 (never both: the onboard bridge blocks external TX).
- State: `hardware_smoke_passed`; no remote power backend yet.

## UART runner build

The stable branch builds one profile (`UART_M_MODE`) for every board:

```bash
bash cert_harness/tools/build_runner.sh --board <vf2_jh7110|bpif3_k1|milkv_megrez_eic7700x>
```

Never assume this local output is installed. Compare its manifest build ID with
the target's `[UART_STREAM] READY ... board=... runner_build=...` line.

## Adding a board

Copy structure, not values. Create a manifest first, then a board preset, linker
layout, adapter, image recipe, smoke tests, Jenkins lock/job parameters, and
portal metadata. Shared runner changes require evidence that the difference
cannot be isolated behind the board layer.

Preserve a proven ROM/SPL/sys-init/DDR chain and replace only its final M-mode
payload when possible. Do not pre-emptively port vendor DDR or OpenSBI code into
the runner. If hardware fails before `READY`, capture the failure and identify
the exact missing initialization before proposing a narrowly isolated backend.
