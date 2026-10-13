---
name: log
title: Log debugging
summary: What failed according to the UART log - expected vs observed, trap cause, PC, check name.
tools: []
---
Read the failure exactly as the board reported it before forming any opinion.

- Name the check that failed (coverpoint / bin / assertion) and the expected and observed values.
- For a trap: the cause number and its name, the PC (mepc/xepc), tval, and which mode handled it.
  An unexpected trap means the test never reached its check: the trap is the failure.
- Note what the log does not show (no trap record, no values): later skills must not assume it.
- Questions to leave for other skills: which instruction was at the PC (instruction), what Sail
  did there (reference), which rule decides it (spec).
