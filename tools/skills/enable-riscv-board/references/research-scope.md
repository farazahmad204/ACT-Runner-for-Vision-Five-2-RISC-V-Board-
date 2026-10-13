# Restricted board-research scope

The initial AI-assisted port answers one question: **can the existing common
M-mode UART runner be built, packaged and safely placed for this board?**

Research agents are read-only. They search documentation and source material;
they do not edit runner code, invent a platform design, flash hardware, change
CI, or recommend unrelated features.

## Allowed roles and outputs

### `boot_image`

Return only:

- `identity.board_soc`: board name, SoC and revision when documented.
- `identity.isa`: compile-relevant ISA string or documented CPU ISA.
- `boot.medium`: boot medium used for the proposed runner image.
- `boot.chain`: stages before the common runner and which stage initializes DDR.
- `boot.payload_slot`: final M-mode payload component that the runner replaces.
- `boot.image_format`: required container, signature or header format.
- `boot.packaging`: reproducible packaging tool/command and required inputs.
- `boot.install`: non-destructive load/install method, if documented.
- `boot.recovery`: documented recovery path.

### `runner_contract`

Return only:

- `runner.load_address`: physical load address.
- `runner.entry_address`: first instruction address.
- `runner.privilege`: privilege mode at entry; it must be M-mode for this runner.
- `runner.hart_entry`: which harts enter the payload, or `unknown`.
- `runner.runner_hart`: proposed fixed runner hart, supported by the entry facts.
- `runner.monitor_hart`: proposed fixed monitor hart, supported by the entry facts.
- `uart.base`: debug UART MMIO base.
- `uart.register_width`: MMIO access width.
- `uart.register_spacing`: register shift/stride.
- `uart.registers`: offsets and semantics needed for TX, RX and line status.
- `uart.clock`: input clock only when documented or required for initialization.
- `uart.initialization_owner`: earlier stage that configures UART, or `unknown`.
- `timer.mswi_base`: ACLINT/CLINT software-interrupt base used by the runner.
- `timer.mtimecmp_base`: ACLINT/CLINT machine-timer compare base.
- `timer.mtime_address`: memory-mapped `mtime` used by receive timeouts.
- `interrupt.external_cleanup`: whether the runner may safely use its external
  interrupt/PLIC cleanup path; include the controller and UART source only when
  documented, otherwise require that optional path to be disabled.

### `ddr_placement`

Return only:

- `memory.dram_base`: documented DRAM base.
- `memory.documented_window`: a documented DRAM interval large enough for the port.
- `memory.runner_range`: proposed runner/code/data/BSS/stack interval.
- `memory.payload_range`: proposed ELF segment and entry interval.
- `memory.staging_range`: proposed UART receive buffer interval and maximum size.
- `memory.intersecting_reservations`: only reserved, secure or MMIO ranges that
  intersect or bound the three proposed intervals.
- `memory.overlap_result`: explicit overlap/non-overlap result.

### Recommended facts (any role listed)

Accepted by the validator but not required. Each was a real bring-up blocker:

- `boot_image`: `identity.priv_version`, `identity.h_version`,
  `boot.host_console`, `boot.recovery_requires`.
- `runner_contract`: `timer.mtime_hz`, `uart.host_bridge`,
  `interrupt.plic_base`, `interrupt.uart_source`, `interrupt.runner_contexts`,
  `cpu.phys_addr_bits`, `cpu.asid_bits`.
- `ddr_placement`: `memory.firmware_window`.

### Requirement catalog and web search

`assets/runner-requirements.yaml` is the canonical list of what the runner
needs; `resolve_requirements.py` turns it into `questions.md`. Agents resolve
those questions in catalog order: user input, extraction, user documents,
then official vendor web sources (cited with URL and revision), then hardware
probes. Reports may also use catalog IDs owned by their role: boot and
identity groups -> `boot_image`; harts, uart, timer, interrupt, platform ->
`runner_contract`; memory -> `ddr_placement`. Bench items are user answers.

### Start from extracted facts

Before assigning research, run `scripts/collect_board_facts.py` on the vendor
DTB and stock boot logs. Give agents only the IDs it leaves unknown.

## Forbidden initial topics

Do not research or report Jenkins, the portal, Linux services, full device-tree
topology, Hypervisor tests, OpenSBI services, performance, networking, display,
storage drivers, interrupt-controller topology, or complete peripheral
inventories. Do not design vendor DDR initialization. A DTS may be consulted
only when it is the best available source for one allowed address or overlap.

## Evidence rules

Use evidence in this order:

1. Vendor board manual, SoC TRM, boot and recovery documentation.
2. Versioned vendor BSP, bootloader source, image recipe or configuration.
3. Captured known-good boot log.
4. Community source only as a lead or clearly marked inference.

Each fact must include its exact source URL or repository path, revision when
applicable, and one of `confirmed`, `partial`, `inferred`, or `unknown`.
Search performed by an AI model is discovery, not evidence: cite the underlying
document, never the model response or search-result snippet. Conflicting values
remain a conflict until resolved. Missing values remain `unknown`.

## Required agent response

Write the YAML report using `assets/research-report.template.yaml` and validate:

```bash
python3 tools/skills/enable-riscv-board/scripts/validate_research_report.py \
  artifacts/onboarding/<board>/research/<role>.yaml
```

Only validated reports may update the board manifest.
