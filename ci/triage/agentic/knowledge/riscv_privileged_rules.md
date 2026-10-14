# RISC-V Privileged Rules

Use these rules before assigning ownership for trap, delegation, and privilege
failures.

- M-mode traps are never delegated.
- `medeleg` and `mideleg` only affect traps taken from lower privilege modes.
- Supported `medeleg`/`mideleg` bits are WARL and must be discovered by
  write/readback on the target.
- If a trap originates in M-mode, an S-mode expected trap signature is a
  test-environment/profile mismatch unless the runner should have entered S/U.
- ECALL from M-mode is never delegatable.
- ECALL from S-mode is not delegatable on VF2/U74 based on observed WARL
  behavior.
- Instruction/load/store access-fault delegation is platform WARL-dependent;
  check the target delegation table before blaming trap routing.
- Misaligned versus access/page/access-fault priority can be
  implementation-defined or EEI-dependent. Do not call a DUT bug until the spec
  requires one priority for the exact instruction and address state.
- Illegal instruction must report illegal-instruction cause when the instruction
  is fetched and decoded as illegal.
- `mtval` provenance matters. Confirm `mtval` belongs to the same trap as
  `mcause` and `mepc` before using it as the fault address.
- For compressed instructions, trap return/skip must use the actual instruction
  length, not a fixed 4 bytes.
