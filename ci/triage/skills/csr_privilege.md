---
name: csr_privilege
title: CSR and privilege
summary: CSR values, writable bits, delegation (medeleg/mideleg/hedeleg), trap routing, mstatus/PMP state.
tools: [sail_config]
---
Decide whether the difference is explained by CSR state, writable bits or delegation.

- For a CSR value mismatch: which fields differ (value diff), and whether the board's measured
  writable mask or an absent CSR explains it.
- For a trap that landed in the wrong mode: is the cause delegatable on this board (measured
  medeleg/mideleg/hedeleg), and did the test delegate it?
- Compare the board's sail.json settings for these CSRs with the board facts: a reference
  configuration that differs from measured hardware is a reference-model mismatch, not a DUT bug.
- Report: the field-level difference and the board fact or setting that explains it, or that none does.
