# Board specs

Each board is described in one file, `<board>.yaml`. Two deterministic
generators read it, so every board fact is written once:

| Generator | Reads | Writes |
|---|---|---|
| `scripts/gen_board_files.py <spec> --out-root <runner checkout>` | `test_hart`, `memory`, `platform`, `runner`, `boot` | `cert_harness/boards/<id>.env`, the board `link.ld` (when a firmware window is set), the FIT source or packaging adapter, and `runner_report.yaml` |
| `scripts/gen_act_config.py <spec> --out-dir <ACT>/config/cores/<vendor>/<dut> --act-root <ACT>` | `dut`, `isa`, `priv_version`, `hypervisor`, `test_hart`, `memory`, `platform`, `csr`, `params`, `sail`, `evidence` | ACT DUT folder (see `assets/act/README.md`) |

## Sections

| Section | Fields | Used by |
|---|---|---|
| `test_hart` | Hart that runs the tests (`BOARD_RUNNER_HART_ID`). | both |
| `memory` | `base`, `size`: the runner payload window and the ACT link window. | both |
| `platform` | `clint.base`, `plic.{base,sources,test_hart_contexts.{m,s}}`, `uart.{base,reg_shift,access_bytes,plic_source}`, `cpu_hz_max`, `mtime_hz`, `cache_block_bytes`, `ids`, `access_fault_address` | both |
| `runner` | `board_id`, `description`, `platform_id`, `monitor_hart`, `load_address`, `firmware_window_limit`, `ram`, `excluded`, `staging`, `tohost_fallback`, `uart.{size,access_bytes,init_8250,input_clock_hz,baud,rx_diag}`, `external_irq_cleanup`, `watchdog`, `k1_hart_wakeup`, `serial_dev`, `toolchain_dir`, and optionally `payload_window` (overrides `memory`) | `gen_board_files.py` |
| `boot` | `pattern`, which is `fit` (U-Boot FIT: VF2, BPI-F3), `eswin_nsign` (Megrez SPI chain) or `custom` (skeleton adapter). Also `image`, `slot`, and pattern options. | `gen_board_files.py` |
| `dut`, `isa`, `csr`, ... | ACT-only facts | `gen_act_config.py` |

Everything the runner build needs must be in the spec; neither generator
relies on runner defaults. Each value is evidence-backed. The
`runner-requirements.yaml` catalog and `resolve_requirements.py` say where
each value comes from. Record the sources in `evidence:`.

## Exit codes

Both generators write their files even when they exit 2. For
`gen_board_files.py`, exit 2 means a layout problem or a manual step remains.
Manual steps include a new board's platform-name registration in
`runner_shared.h` and the packaging function of a `custom` adapter. For
`gen_act_config.py`, exit 2 means a required input is missing.

## Regression evidence (2026-10-03)

Runner branch `megrez-uart-runner` `e77fbc5`, built with
`build_runner.sh --no-package` and `RUNNER_BUILD_ID_OVERRIDE`:
- **VF2, BPI-F3 and Megrez: `firmware.bin` from the generated presets is
  byte-identical to the build from the production presets.**
- The Megrez adapter, `vf2_act4.its` and `bpif3_opensbi.its` generate
  identically.
- The generator flags that VF2 and BPI-F3 set no payload window. The runner
  image and staging buffer then sit inside the window that ELF segments may use.
