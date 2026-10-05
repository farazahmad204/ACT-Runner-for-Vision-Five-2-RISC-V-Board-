# Minimal RISC-V UART ELF Runner

This branch contains one purpose-built runner: receive one ELF over UART and
execute it in M-mode. The same source builds for:

- StarFive VisionFive 2 (`vf2_jh7110`)
- Banana Pi BPI-F3 (`bpif3_k1`)
- Milk-V Megrez / ESWIN EIC7700X (`milkv_megrez_eic7700x`); see
  `cert_harness/boards/milkv_megrez_eic7700x/README.md`

It intentionally excludes SD payload loading, embedded packs, S/U-mode runner
profiles, Riescue profiles, generated tests, and historical debug artifacts.
The uploaded ELF may implement its own privilege transitions and page tables;
the runner itself always enters the ELF in M-mode.

## Build

```bash
PATH=/home/lpt-10xe/riscv64/bin:$PATH \
  bash cert_harness/tools/build_runner.sh --board vf2_jh7110

PATH=/home/lpt-10xe/riscv64/bin:$PATH \
  bash cert_harness/tools/build_runner.sh --board bpif3_k1

MEGREZ_NSIGN_ROOT=/path/to/rockos-opensbi \
  PATH=/home/lpt-10xe/riscv64/bin:$PATH \
  bash cert_harness/tools/build_runner.sh --board milkv_megrez_eic7700x
```

Artifacts are written to `cert_harness/build/<board>/UART_M_MODE/`.
`manifest.json` records the full Git revision, advertised build ID, board,
toolchain, execution mode, and transport.

## Send one ELF

```bash
python3 cert_harness/uart_stream/send_elf.py test.elf \
  --serial-dev /dev/ttyUSB0 \
  --expect-board vf2_jh7110 \
  --expect-runner-build <build-id> \
  --log uart.log
```

For automated power cycling and multiple independent boots, use
`cert_harness/uart_stream/run_elf_batch.py`.

## Revision management

`ci/runner_inventory.json` is the reviewed record of the runner currently
installed on each physical board. Jenkins uses it when
`INSTALLED_RUNNER_BUILD` is blank. Updating firmware on a board requires a PR
that updates its `installed_build_id` and full `source_revision` together.

All Jenkins UART pipelines fetch `vf2-uart-stream-jenkins` by default and
support an exact `RUNNER_REVISION_OVERRIDE` for reproducible reruns.

Run `python3 ci/check_minimal_uart_repo.py` before review. It prevents legacy
profiles, SD workflows, or unrelated Jenkins pipelines from returning to this
branch accidentally.
