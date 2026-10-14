# VF2 / U74 Platform Knowledge

Platform: VisionFive2 / JH7110 / SiFive U74.

## Delegation

<!-- tags: delegation, ecall, protection -->

Observed `medeleg` WARL mask:

```text
0x000000000000b15d
```

Delegatable exception causes:

```text
0  Instruction address misaligned
2  Illegal instruction
3  Breakpoint
4  Load address misaligned
6  Store/AMO address misaligned
8  ECALL from U-mode
12 Instruction page fault
13 Load page fault
15 Store/AMO page fault
```

Not delegatable:

```text
1  Instruction access fault
5  Load access fault
7  Store/AMO access fault
9  ECALL from S-mode
11 ECALL from M-mode
```

Rules:

- M-mode exceptions are never delegated.
- If ACT/Sail expects S-mode handling for a non-delegatable cause, do not call a
  DUT bug. Report VF2/U74 delegation limitation and profile/ACT expectation.

## Known Priority Behavior

For address `0x1` with `lh`:

- address is misaligned
- address is inaccessible
- VF2/U74 reports Load access fault: `mcause=5`, `mtval=0x1`

Use this as a known platform behavior when comparing against Sail configs that
prioritize alignment first.

## Known ACT Issue

`ExceptionsSm-00` / `cp_misaligned_priority_load`:

- uses `RVMODEL_ACCESS_FAULT_ADDRESS=0x0`
- can create a double-fault priority dependency when the effective address is
  both misaligned and access-faulting
- do not classify as DUT bug without checking Sail config, exact faulting
  instruction, and first-trap provenance

## Runner/Environment Notes

- FP CSR tests require valid FS state before reading `fflags`, `frm`, or `fcsr`.
- C-extension trap handling must skip/return using actual instruction length.
- External interrupt sources must be masked/cleared unless the ACT case is
  intentionally testing them.
