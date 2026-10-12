---
id: VER-20261012-vf2-ssw-int-macro
title: VF2 RVMODEL_SET_SSW_INT writes a PLIC register instead of raising SSIP
board: vf2_jh7110
runner_build: ''
date: '2026-10-12'
question: Why is sip/mip.SSIP missing in the VF2 interrupt priority tests?
outcome: confirmed
boards:
- vf2_jh7110
tests:
- InterruptsS*
signals:
- '"field": "ssip"'
auto: true
cause: act-config
verdict: Test or ACT issue
evidence:
- riscv-uart-triage re-triage of vf2-uart-weekly #6 (2026-10-12), per_case evidence.json
- Arshia2564/riscv-arch-test sifive_u74 @ 9a1336ba2 config/cores/sifive_u74/rvmodel_macros.h lines 183, 443-451
related:
- VER-20261009-vf2-sail-json-h-asid
curated: true
updated: '2026-10-12'
---

# VER-20261012-vf2-ssw-int-macro

The VF2 ACT config raises the supervisor software interrupt by storing to `SIG_ADDRESS`
(0x0c000004), the template's address for Sail's simple interrupt generator. On the JH7110 that
address is the PLIC priority register of source 1, so the store never sets SSIP.

## Experiment

Read the VF2 `rvmodel_macros.h` that the pipeline uses (ACT `sifive_u74` @ 9a1336ba2) and compared it
with the values the failing interrupt tests read back.

## Result

- `RVMODEL_SET_SSW_INT` / `RVMODEL_CLR_SSW_INT` store `(1<<31)|(1<<1)` / `(1<<1)` to `SIG_ADDRESS`
  = 0x0c000004 (comment in the file: "Sail's simple interrupt generator remains in use for the
  software-interrupt macros").
- Re-triage of vf2-uart-weekly #6: every InterruptsS/InterruptsSm priority test that failed reads
  sip/mip with only SSIP (bit 1) missing, for example sip 0x200 instead of 0x202, mip 0x800
  instead of 0x802, mip 0xaa8 instead of 0xaaa.
- The Megrez config had the same template macro and the same symptom (megrez-uart-weekly #5,
  InterruptsHSm_*); its fix is stashed in the ACT worktree `act-megrez-hyp`
  ("megrez SSW macro fix (not pushed) 2026-10-08").

## Conclusion

An ACT config error, not hardware: the U74 has no supervisor software-interrupt controller, so
the macros must not be defined. ACT then sets and clears mip.SSIP itself (tests/env/check_defines.h).
Fix: remove both macros from the VF2 `rvmodel_macros.h`, as in the Megrez fix, and rerun the
Interrupts suites. Triage marks failures whose value diff shows SSIP missing as 'Test or ACT
issue'. Interrupt failures without a value diff (e.g. signature word mismatches in the trigger
tests) are not covered by this note.
