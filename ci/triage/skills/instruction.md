---
name: instruction
title: Instruction debugging
summary: The failing instruction in the ELF that ran - disassembly, operands, source line, test code around it.
tools: [disassemble, test_source]
---
Find the exact instruction the failure points at and what the test meant to check with it.

- Read the instruction at the failing PC (marked <==) and its operands: which CSR, register,
  address or immediate it uses, and the privilege mode it runs in.
- Read the test code around the failing check: the setup just before it (CSR writes, PMP or
  page-table setup, delegation) and what value or trap the test expects.
- If the PC is in a trap handler or macro rather than the test body, disassemble the test's
  own instruction (its label or address) with `disassemble`.
- Report: instruction, operands, mode, and the expectation the test encodes.
