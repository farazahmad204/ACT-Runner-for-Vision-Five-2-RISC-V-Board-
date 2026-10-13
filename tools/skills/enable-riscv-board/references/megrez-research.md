# Milk-V Megrez research status

## Confirmed from official public sources

- Board: Milk-V Megrez.
- SoC: ESWIN EIC7700X.
- CPU: four SiFive P550 cores at up to 1.8 GHz.
- Published ISA: RV64GBCH, including the Hypervisor extension.
- Memory options: 8, 16, or 32 GB LPDDR5.
- Storage options include SPI flash, eMMC, microSD, SATA, and M.2 SATA.
- Debug interfaces include UART and JTAG.
- Milk-V publishes RockOS/Debian resources, bootloader binaries, and board schematics.
- RockOS is described as supporting virtualization; this is not by itself proof that the bare-metal Hypervisor test suite matches the runner contract.
- The RockOS U-Boot Megrez device tree selects UART0 as the console and describes DRAM starting at `0x80000000`.
- The RockOS EIC770x SoC device tree describes UART0 at `0x50900000`, input clock 200 MHz, register shift 2, and a `reg-io-width` value of 2. RockOS OpenSBI parses those properties and performs 16-bit accesses plus a complete 8250 initialization. Because the runner replaces that OpenSBI stage, it must perform the same minimal initialization itself.
- The official ACT `hypervisor` branch contains generated Hypervisor suites and an ELF-resident T-SBI that performs HS/VS/VU mode transitions.
- A captured Megrez boot log reports four platform harts, root-domain harts
  `0-3`, and boot hart 2 for that boot. RockOS OpenSBI `fw_base.S` uses a
  multi-hart cold-boot lottery: all participating harts enter the M-mode
  firmware entry, one wins cold boot and the others take the wait/warm path.
  The runner uses hart 1 (runner) and hart 2 (monitor), validated on
  hardware (2026-10-02/03); the lottery winner does not define those roles.

## Boot-chain findings

The normal Megrez boot flow is:

```text
ESWIN ROM/SCPU
  -> signed SPI boot chain
     -> sys_init.bin at 0x59000000
     -> DDR firmware at 0x59000000
     -> fw_payload.bin at 0x80000000
        -> OpenSBI in M-mode
           -> U-Boot in S-mode
              -> RockOS from SD/eMMC/NVMe/SATA/USB
```

- RockOS builds `fw_payload.bin` by combining OpenSBI, the Megrez U-Boot binary, and U-Boot DTB.
- `bootchain.cfg` loads and enters that component at `0x80000000`.
- OpenSBI independently sets `FW_TEXT_START=0x80000000` and embeds U-Boot at offset `0x200000`, matching U-Boot's `0x80200000` text/load address.
- Therefore, a bare-metal M-mode runner cannot be loaded as an ordinary file by the normal S-mode U-Boot. The integration must change the final boot-chain component or otherwise introduce an earlier M-mode handoff.
- The RockOS `nsign` workflow is the source-backed packaging mechanism. Replacing its payload with the runner is a design inference and still needs an offline build plus hardware validation.
- `bootchain.cfg` proves the final component's address. The existing VF2 and BPI-F3 ports demonstrate the intended runner boundary: vendor early firmware initializes the SoC/DRAM, then the common `firmware_act.bin` replaces the normal final OpenSBI/U-Boot payload.
- RockOS OpenSBI performs additional EIC7700X services and tuning, but the minimal runner does not require SBI domains or a Linux handoff. Do not copy those functions pre-emptively; investigate them only if the recovery-mode runner fails.
- The official recovery process can start a temporary bootloader over USB-C and restore SPI. Writing a custom image to SPI remains destructive and requires explicit human approval.

## Memory findings

- DRAM begins at `0x80000000`.
- The source DTS declares a 16 GiB span, while U-Boot can detect larger installed memory. That distinction does not block the minimal runner because its intentionally conservative physical-address window remains within the documented low DRAM range.
- U-Boot deliberately relocates below the 4 GiB boundary for 32-bit DMA compatibility. This is a U-Boot policy, not the physical end of DRAM.
- The Megrez DTS marks `0xDFFF0000-0xDFFFFFFF` for the low-power CPU and `0xE0000000-0xE0FFFFFF` for display as `no-map`.
- The enabled die-0 PMP node selects flat mode in the published source and describes `0xB0000000-0xBFFFFFFF` as secure memory. Confirm this in the compiled Megrez DTB and on hardware before treating it as final.
- Normal U-Boot uses addresses starting at `0x80200000`, `0x84000000`, `0x88000000`, `0x90000000`, and `0x98300000`. These are not permanent reservations after the runner replaces U-Boot, but they must not be reused by an image that keeps the normal OpenSBI/U-Boot chain.

### Conservative offline runner partition

For the custom boot-chain design, the following is a reasonable layout to validate rather than an approved layout:

| Purpose | Candidate range | Reason |
|---|---:|---|
| Minimal runner | starts at `0x80000000` | Replaces the final signed component after vendor sys-init and DDR firmware. |
| ACT ELF execution window | `0x90000000-0xAFFFFFFF` | Avoids runner and the source-visible PMP region; ACT linker configuration must move away from `0x80000000`. |
| UART receive staging | `0xC0000000-0xC7FFFFFF` | 128 MiB window above the PMP region and below LPCPU/display reservations. |

Use these values for the offline port, build the runner and inspect its linker
map. Confirm UART and memory access from M-mode before promoting the port to CI.
No DTB is required by the runner.

## Current state (2026-10-03)

- `hardware_smoke_passed`: v4 runner (`megrez-uart-v4-20261002`) installed in
  SPI flash; 2 clean SPI boots return `DONE status=PASS`.
- Runner/monitor harts 1/2 (the stock OpenSBI boot hart was 1).
- Runner window capped at `0x80080000` (stock M-mode reservation).
- Console: onboard USB-C CH340 drives UART0 RX; use either it or an external
  adapter with USB-C unplugged (see `bringup-pitfalls.md`).
- ACT priv smoke: 3/5 PASS; failures explained (non-zero `hvip` suspected;
  `stvec` sign-extension WARL). CSR probe pending.
- Evidence: `artifacts/onboarding/milkv_megrez_20261002/` and `..._20261003/`,
  manifest `assets/manifests/milkv_megrez.yaml`, runner branch
  `megrez-uart-runner`.

Open: remote power backend, CSR probe results, H CSR reset in the runner,
Jenkins/portal integration.
