# Milk-V Megrez (ESWIN EIC7700X) UART runner

Status (2026-10-03): the runner is installed in the Megrez SPI flash and
passes the UART ELF smoke test on two clean SPI boots. Board id
`milkv_megrez_eic7700x`, platform id 3, installed build
`megrez-uart-v4-20261002`.

## Boot chain

```text
ESWIN boot ROM -> sys_init -> DDR firmware -> runner (M-mode, 0x80000000)
```

The ESWIN `nsign` boot image keeps the vendor `sys_init.bin` and `ddr_fw.bin`
and puts the common runner in the `fw_payload.bin` slot that normally holds
OpenSBI + U-Boot. No OpenSBI, U-Boot, SBI services or DDR code is added to
the runner. Source: RockOS OpenSBI `sign/preload/bootchain.cfg` at
`7afdf03136285cf1319dd49f78a91d0a2569bdd5`.

## Board contract

| Item | Value | Evidence |
|---|---|---|
| Runner / monitor hart | 1 / 2 | stock OpenSBI boot hart 1; smoke runs |
| Runner window | `0x80000000`-`0x80080000` (linker assert) | stock M-mode reservation `0x80000000-0x8007ffff` |
| ELF payload window | `0x90000000`-`0xB0000000` | U-Boot DTS DRAM; verified by smoke ELF at `0x90000000` |
| Excluded DDR | `0xB0000000`-`0xC0000000` | source-visible secure PMP region |
| UART staging | `0xC0000000`, 128 MiB | below LPCPU/display reservations |
| UART0 | `0x50900000`, 16-bit access, shift 2, 200 MHz, 115200 | U-Boot DTS `reg-io-width = <2>`; runner-owned 8250 init |
| CLINT | MSWI `0x02000000`, MTIMECMP `0x02004000`, MTIME `0x0200BFF8` | `mtime` measured at 1.000 MHz |
| PLIC cleanup | disabled (`BOARD_EXTERNAL_IRQ_CLEANUP_ENABLE=0`) | mapping not validated |

## Host serial: use the onboard USB-C debug UART

The Megrez has an onboard CH340B (`U138`) on UART0 behind the USB-C debug
port: its TXD drives `UART0_RX` through 0R `R104` (V1.1 schematic). While
USB-C is connected, an external USB-TTL adapter wired to the RX header pin
cannot transmit to the board (output still reads fine). Use the onboard port,
for example `/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0`, and keep
any external adapter's TX disconnected. The by-id name is generic; prefer a
`/dev/serial/by-path/...` path on a shared host.

## Build

```bash
MEGREZ_NSIGN_ROOT=/path/to/rockos-opensbi@7afdf031 \
RUNNER_BUILD_ID_OVERRIDE=<build-id> \
PATH=/home/lpt-10xe/riscv64/bin:$PATH \
  bash cert_harness/tools/build_runner.sh --board milkv_megrez_eic7700x
```

`boot_image.bin` is the nsign image for the SPI flash or the Recovery drive.
With `RUNNER_BUILD_ID_OVERRIDE=megrez-uart-v4-20261002` the image reproduces
the installed one bit-for-bit: sha256
`bc5f5774467b426ade180a848ebb6415e7fb37cda55ecd5ab64c33c803ef65bb`.

## Install or update the runner in SPI flash

Writing SPI is destructive and needs explicit approval. Use
`cert_harness/tools/megrez_uboot_session.py` with the official Milk-V
bootloader as the installer:

1. Recovery/Normal switch to **Recovery**, USB-C connected, power-cycle; the
   PC sees an `ESWIN-2030` drive.
2. Start the tool. The official U-Boot does not start if the onboard port is
   open during boot (observed twice; cause unknown), so watch the boot on an
   external adapter's RX and open the onboard port at the countdown:

   ```bash
   python3 cert_harness/tools/megrez_uboot_session.py \
     --watch-dev <external-adapter> --serial-dev <onboard-port> \
     --out <log-dir> --interrupt \
     --loady cert_harness/build/milkv_megrez_eic7700x/UART_M_MODE/boot_image.bin \
     --flash-sha256 <sha256 of that boot_image.bin>
   ```

3. Copy the official `bootloader_milkv-megrez-2025-0224.bin` (Milk-V release
   `2025-0219`) to the `ESWIN-2030` drive.
4. The tool stops autoboot with `s`, sends the image with `loady`, verifies
   it with U-Boot `crc32`, and only then runs `es_burn write 0x90000000 flash`.
   It refuses before opening the port if the sha256 does not match.
5. Switch to **Normal** and power-cycle. The runner prints `READY` about
   30 ms after `DDR self test OK`.

## Restore the stock bootloader

Same procedure, with `--loady bootloader_milkv-megrez-2025-0102.bin`
(Milk-V release `2025-0117`; the version that was in SPI; sha256
`2f660235fcd23eb8ef0304321d486f1fbb407afb5bc4922f9ef63c81b2dccf77`,
U-Boot CRC32 `fbd3556d`). This was rehearsed into RAM and CRC-verified, but
not written.

## Run one ELF

```bash
python3 cert_harness/uart_stream/send_elf.py test.elf \
  --serial-dev <onboard-port> --expect-board milkv_megrez_eic7700x \
  --expect-runner-build megrez-uart-v4-20261002 --log uart.log
```

Then power-cycle the board. `cert_harness/uart_stream/diag_capture.py` does
the same with host timestamps and longer waits.
ELFs must load and enter inside `0x90000000`-`0xB0000000`.

## Validation record

| Run | Boot path | Result |
|---|---|---|
| RX diagnostic build (`BOARD_UART_RX_DIAG=1`) | Recovery USB | loopback 8/8; header 88/88; `DONE status=PASS` |
| v4, two clean boots | Recovery USB | `DONE status=PASS tohost=0x1` both |
| v4 install | `es_burn` | header, FIRMWARE, DDR, BOOTLOADER written; `bootloader write OK` |
| v4, two clean boots | SPI (Normal) | `DONE status=PASS tohost=0x1` both |

Smoke ELF: 5128 bytes, CRC32 `0x07f12611`, loads and enters at `0x90000000`,
writes `tohost=1`. Raw logs and hashes are kept on the lab host under
`artifacts/onboarding/milkv_megrez_20261002/`.

## Open items

- No validated remote power/reset backend; each ELF needs a manual power cycle.
- Megrez is not in Jenkins or the portal yet.
- ACT test suites and Hypervisor tests have not been run on Megrez.
