---
board: milkv_megrez_eic7700x
core: SiFive P550 (EIC7700X), privileged 1.11, hypervisor draft 0.6
---

# Milk-V Megrez (milkv_megrez_eic7700x)

Measured facts live in `ci/triage/board_knowledge/milkv_megrez_eic7700x.yaml`
(absent CSRs, writable masks).

Reported issues that apply because the P550 is a priv-1.11 core like the U74:
[[ACT-1651]] / [[ACT-1899]] xenvcfg, [[ACT-1900]] mconfigptr, [[ACT-1924]] time CSR,
[[ACT-1875]] reserved PTE bits, [[ACT-2010]] / [[SAIL-1862]] reserved mcause codes.

H tests: the core implements H draft 0.6 while ACT generates ratified H 1.0 tests; see
the ACT branch `megrez-hypervisor` README and our report [[ACT-2717]]. Since 2026-10-12 the
config's sail.json declares priv 1.12 like its UDB yaml (David's fix), so all 121 H tests pass on
Sail and run on the board; tests that touch 1.12-only CSRs (menvcfg, senvcfg, henvcfg,
mconfigptr) fail there with an illegal-instruction trap: a known deviation. xtinst checks are relaxed until
sail-riscv#1982 ([[ACT-2670]]).
