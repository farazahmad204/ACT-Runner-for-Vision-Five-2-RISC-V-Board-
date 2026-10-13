---
name: reference
title: Reference comparison
summary: What the Sail reference model did at the same instruction with the board's configuration.
tools: [reference]
---
Compare the board with Sail at the same instruction, and check the comparison is fair.

- Read what Sail executed at the mapped instruction: mode, registers/CSRs written, trap taken.
- Check the setup matches before interpreting a difference: same privilege mode, same CSR and
  PMP/page-table writes earlier in the run (the setup lines).
- If Sail never executed that instruction, or the code differs between the two builds (board
  RVMODEL macros), say so: then Sail gives no verdict for this instruction.
- To look at another address (for example the test's own instruction before a trap handler),
  run `reference` with that board address.
- Report: board behavior, Sail behavior, and whether the setups match.
