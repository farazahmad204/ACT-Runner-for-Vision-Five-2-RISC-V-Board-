---
name: act_framework
title: ACT framework
summary: How the test checks - test header, coverpoint/bin, RVTEST/RVMODEL macros, the board's rvmodel_macros.h, normative rules.
tools: [test_source]
---
Decide whether the failure comes from how the test or its board macros are written.

- Read the test header (required extensions, privilege modes): does the board implement them?
- Read the check: what the signature or assertion compares, and which macros produce the value.
- Board macros (RVMODEL_* in the board's rvmodel_macros.h) raise interrupts, set timers or halt;
  a macro that does not match the hardware (wrong address, missing clear) is a test-setup issue.
- The normative rule ids the test names say what requirement it claims to check.
- Report: what the test checks, and any assumption it makes that this board does not meet.
