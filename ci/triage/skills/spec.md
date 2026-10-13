---
name: spec
title: Specification research
summary: The rule in the spec version this board implements (priv 1.11/1.12, H 0.6/1.0, unpriv) and where current text differs.
tools: [spec]
---
Find the rule that decides who is right, in the version the board implements.

- Search with the precise subject: CSR or field name, instruction, exception name, "priority",
  "delegat", "WARL", "reserved".
- Decide: does the rule mandate one outcome, allow several (implementation-defined, WARL), or
  not cover the case? Quote the sentence that says so, from the version the board implements.
- If the current ratified text differs, the test may expect newer behavior than the board
  claims: that is an unsupported feature or a test/config issue, not a DUT bug.
- Report: version id, the exact quote, and what it requires here.
