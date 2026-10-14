# Sail Configuration Rules

Use these rules only after hardware evidence and ACT intent are known.

- `memory.misaligned.exceptions.load_store = AlignmentException` means Sail
  prioritizes load/store address-misaligned before memory protection.
- `memory.misaligned.exceptions.load_store = None` allows
  translation/protection/PMP/PMA checks to happen before a misaligned exception.
- If Sail chooses misaligned first and VF2/U74 chooses access fault first for an
  address that is both misaligned and inaccessible, classify as Sail config,
  ACT expectation, profile, or spec-allowed implementation difference until the
  spec proves otherwise.
- Compare the exact report-local Sail trace, trap report, and generated results
  for the same ELF. Do not compare against artifacts from another run.
- For halfword/unaligned PCs, prefer the Sail trace instruction line over
  nearest objdump context.
- If Sail trace has no selected-PC trap match, do not claim Sail expected that
  selected instruction to trap.
- Do not call a pure Sail issue unless the Sail model/config is proven wrong for
  the target profile.
