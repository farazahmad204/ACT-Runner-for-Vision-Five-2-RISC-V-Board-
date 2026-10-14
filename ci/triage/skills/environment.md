---
name: environment
title: Context checks
summary: Whether the evidence belongs together - kept ELF vs the ELF the board received, board and runner build, ACT revision, Sail version, spec versions.
tools: []
---
Checked by code before the investigation. Read these first.

- A MISMATCH on the ELF means the disassembly and the Sail comparison may describe a different
  build: do not base a root cause on them, and do not claim a hardware bug.
- UNCHECKED items are gaps, not failures: say what the conclusion cannot rely on.
- Judge the board against the spec versions listed here, not the current ratified text.
