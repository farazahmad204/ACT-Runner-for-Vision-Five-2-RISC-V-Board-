---
name: memory_mmu
title: Memory and MMU
summary: satp/hgatp/vsatp modes, page-table entries (V R W X U G A D), page and access faults, PMP and address ranges.
tools: [sail_config]
---
Decide whether a page fault, access fault or translated value is explained by the translation
or protection setup.

- Decode satp/hgatp/vsatp (mode, ASID/VMID, root PPN) and any PTE values in the evidence:
  V/R/W/X/U/G/A/D bits and the PPN. Reserved or inconsistent bits change which fault is legal.
- Page fault vs access fault vs guest-page fault: which one the PTE bits, PMP and the address
  imply, and which one was observed.
- A/D-bit handling (hardware update vs fault) and Svade/Svadu settings are implementation
  choices: check the board's sail.json and facts before calling a difference a bug.
- Address width: a VA/GPA wider than the mode allows must fault; check phys/virt address bits.
- Report: the decoded fields and the fault the setup implies, compared with the observed one.
