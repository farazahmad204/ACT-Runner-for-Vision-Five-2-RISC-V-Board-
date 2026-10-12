---
id: VER-20261009-vf2-sail-json-h-asid
title: VF2 sail.json enables H and a 16-bit ASID the U74 does not have
board: vf2_jh7110
runner_build: ''
date: '2026-10-09'
question: Why do Sm_mcsr_access/walk and the satp_access tests fail on VF2?
outcome: confirmed
boards:
- vf2_jh7110
tests:
- Sm_mcsr_access*
- Sm_mcsr_walk*
signals:
- '"extensions": ["h"]'
auto: true
cause: act-config
verdict: Test or ACT issue
evidence:
- 'vf2-uart-weekly #6 per_case evidence.json'
- Arshia2564/riscv-arch-test sifive_u74 @ 9a1336ba2 config/cores/sifive_u74/sail.json
related:
- SAIL-1846
curated: true
updated: '2026-10-09'
---

# VER-20261009-vf2-sail-json-h-asid

The VF2 reference config (ACT branch `sifive_u74` @ 9a1336ba2,
`config/cores/sifive_u74/sail.json`) declares `"H": {"supported": true}` and `"asidlen": 16`.
The SiFive U74 implements neither the hypervisor extension nor ASIDs (ASIDLEN=0, SAIL-1846).

## Experiment

Compared the value diff of every VF2 weekly #6 failure with the sail.json in the ACT branch the
pipeline uses.

## Result

- Sm_mcsr_access-00 (`mstatus_csrrw1`): expected 0x800000c0007e79aa, read 0x80000000007e79aa;
  the missing bits are mstatus.GVA (38) and MPV (39), both defined only with H.
- Sm_mcsr_walk-00 (`mstatus_set_bit_38`): expected bit 38 (GVA) set, read 0.
- Sv_sv39_satp_access_Smode-00 / SvSm_sv39_satp_access_Mmode-00: expected satp bit 44 (ASID) set,
  read 0.

## Conclusion

Sail predicts H and ASID bits the U74 does not implement, so these failures are an ACT config
error, not hardware. Fix: in the VF2 sail.json set `H.supported` false and `asidlen` 0, and drop
H from the UDB yaml if it is declared there; then regenerate. Until then triage marks the mstatus
GVA/MPV failures 'Test or ACT issue' (this note) and the satp ASID failures via SAIL-1846.
