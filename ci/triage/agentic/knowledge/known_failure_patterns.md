# Known Failure Patterns

Use these patterns to avoid rediscovering known issues.

## Provenance Mismatch

- Symptom: selected PC maps to an instruction that cannot produce the reported
  `mcause/mtval`.
- Action: stop root-cause assignment. Prove whether `mcause/mtval` are
  first-trap, stale, later-trap, or failure-helper fields.
- Owner until proven: diagnostic/provenance blocker, with hardware/Sail/ACT
  ownership deferred.

## Access Fault vs Misaligned Priority

- Symptom: ACT/Sail expects load/store access fault but hardware reports
  load/store address misaligned, or the reverse.
- Action: prove exact faulting instruction, address, alignment, PMP/PMA/page
  state, Sail misaligned config, and spec rule.
- Owner until proven: Sail config, ACT expectation, platform/EEI, or
  spec-allowed implementation difference; not DUT bug by default.

## Wrong Trap Mode

- Symptom: ACT expected S/U trap signature but RVCP reports M-mode handling.
- Action: check actual execution mode, `medeleg`, `mideleg`, trap origin mode,
  and whether the cause is delegatable on VF2/U74.
- Owner: runner/profile/ACT expectation unless lower-privilege setup was proven
  correct and hardware violated a required routing rule.

## FP CSR Illegal Trap

- Symptom: F CSR selected instruction, expected illegal instruction, but RVCP
  reports unrelated memory fault.
- Action: check `mstatus.FS`, FP CSR access path, first-trap capture, and later
  failure-scratch overwrite.
- Owner: firmware/ACT environment or provenance blocker until tuple coherence is
  proven.

## Unexpected Machine External Interrupt

- Symptom: ACT expected timer/S/U interrupt but RVCP reports
  `mcause=0x800000000000000b`.
- Action: check PLIC/external interrupt masking at runner entry and before each
  payload.
- Owner: board/runner environment until masking is proven correct.
