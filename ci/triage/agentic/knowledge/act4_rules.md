# ACT 4 Rules

Use these rules when interpreting ACT/RVCP reports and signatures.

- ACT signature mismatch may occur after the original fault.
- Do not use signature helper PC as original fault PC unless provenance confirms
  it.
- RVCP `MEPC` or approximate address is medium-confidence PC evidence.
- `[TRAP] mcause/mepc/mtval/mstatus` is highest-confidence PC/cause evidence.
- FAILQ `saved_mepc/saved_mcause/saved_mtval/saved_mstatus` is useful but must
  be checked against first-trap logs.
- FAILQ `failing_addr_or_pc` is low-confidence and may point at ACT failure
  helper/signature code rather than the original faulting instruction.
- If selected PC, ACT trap report, Sail trace, and RVCP `mcause/mtval` do not
  describe one event, run the existing-artifact provenance sweep before
  stopping: all `[TRAP]` entries, FAILQ metadata, selected PC source,
  selected-instruction/cause compatibility, exact-PC Sail context, exact
  `mtval` Sail search, ACT trap report, ACT results/signature, and objdump
  context.
- Request new first-trap/failure-scratch instrumentation only after the
  existing-artifact provenance sweep cannot prove whether `mcause/mtval` are
  first-trap, stale, or later-trap fields.
- Always identify the ACT test/bin intent before assigning root cause.
- For trap-priority tests, distinguish "expected fault class" from the specific
  priority rule the test is trying to validate.
- Do not generate a root-cause CSV as the first output. Use per-case
  `report_compact.md`, `report.md`, and `report.json`.
