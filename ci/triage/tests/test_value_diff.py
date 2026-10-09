#!/usr/bin/env python3
"""Tests for ci/triage/value_diff.py (ported from tools/act_agent).

Each case is a real RVCP block shape from VF2 weekly runs or Megrez; the negative cases guard
against closing a root cause the values do not prove.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ci.triage.value_diff import analyze, decode_word0

VF2_SAIL = {"base": {"medeleg": {"delegatable_bits": {"len": "xlen", "value": "0x0000_0000_0000_b15d"}}},
            "memory": {"physaddr_bits": 36, "regions": [
                {"base": {"value": "0x0"}, "size": {"value": "0x1000"}, "attributes": {"readable": False, "writable": False}},
                {"base": {"value": "0x80000000"}, "size": {"value": "0x80000000"},
                 "attributes": {"readable": True, "writable": True}}]}}


def csr_walk(info: str, insn: str, bad: str, exp: str) -> str:
    return (f'RVCP-SUMMARY: TEST FAILED\nRVCP: Test Info: "{info}"\nRVCP: Instruction: {insn}\n'
            f"RVCP: Register: x9\nRVCP: Bad Value:      {bad}\nRVCP: Expected Value: {exp}\n")


def trap_diag(field: str, exp: str, act: str, mode: str = "M-mode", xepc: str = "0x80001000",
              xcause: str | None = "0x5", xtval: str = "0x0", style: str = "X") -> str:
    out = (f'RVCP-SUMMARY: TEST FAILED\nRVCP: Test Info: "Mismatch in trap signature!"\n'
           f"RVCP: Trap handler mode: {mode}\nRVCP: Mismatching field: {field}\n"
           f"RVCP: Expected value: {exp}\nRVCP: Actual value:   {act}\nRVCP: {style}EPC:    {xepc}\n")
    if xcause is not None:
        out += f"RVCP: {style}CAUSE:  {xcause}\n"
    return out + f"RVCP: {style}TVAL:   {xtval}\nRVCP: {style}STATUS: 0x8000000a00007880\n"


def word0(mode: int, mpp: int = 0, xip: int = 0) -> str:
    return hex(mode | (8 << 2) | (xip << 12) | ((mpp << 11) << 13))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name)
        (self.cfg / "sail.json").write_text(json.dumps(VF2_SAIL))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def hyp(self, text: str, test: str = "t", board: dict | None = None, **kw) -> dict:
        return analyze(text, test, board or {"medeleg_mask": 0xB15D}, self.cfg, **kw).get("hypothesis") or {}


class PatternTests(Base):
    def test_megrez_mip_hypervisor_state(self) -> None:
        h = self.hyp(trap_diag("XIP (trap signature word 2, interrupt)", "0x800", "0xc40", xcause="0x800000000000000b"),
                     board={"replaced_firmware": "OpenSBI"})
        self.assertEqual(h["pattern"], "hypervisor_pending_state")
        self.assertIn("VSEIP", h["root_cause"])
        self.assertIn("OpenSBI", h["root_cause"])

    def test_pmpaddr_width_from_readback(self) -> None:
        h = self.hyp(csr_walk("cp: cp_pmp_grain", "0x3b0023f3", "0x00000003fffffc00", "0x0000000ffffffc00"))
        self.assertEqual(h["pattern"], "pmpaddr_width_or_grain")
        self.assertIn("36 physical-address bits", h["root_cause"])
        self.assertTrue(h["fixed_in_current_config"])                  # sail.json already says 36

    def test_sepc_sign_extension(self) -> None:
        h = self.hyp(csr_walk("cp: cp_scsrwalk; bin: sepc_set_bit_39", "0x141024f3", "0xffffff8000000000",
                              "0x0000008000000000"))
        self.assertEqual(h["pattern"], "address_sign_extension")

    def test_stval_canonicalized(self) -> None:
        h = self.hyp(trap_diag("XTVAL (trap signature word 3)", "0x8000000140009014", "0xffffff8140009014",
                               mode="S-mode", xcause="0xf"))
        self.assertEqual(h["pattern"], "address_canonicalized")

    def test_medeleg_mask_already_fixed(self) -> None:
        h = self.hyp(csr_walk("cp: cp_mcsr_access; bin: medeleg_csrrw1", "0x30249073", "0xb15c", "0xcb3fe"))
        self.assertEqual(h["pattern"], "writable_mask_mismatch")
        self.assertTrue(h["fixed_in_current_config"])

    def test_mcause_walk_truncation_not_priority(self) -> None:
        h = self.hyp(csr_walk("cp: cp_mcause_write_exception; bin: b_19", "0x34249073", "0x3", "0x13"))
        self.assertEqual(h["pattern"], "warl_field_truncation")

    def test_satp_ppn_width(self) -> None:
        h = self.hyp(csr_walk("Mismatch on setting satp.PPN", "0x18002773", "0x8000000000ffffff", "0x8000000003ffffff"))
        self.assertEqual(h["pattern"], "satp_ppn_width")
        self.assertIn("36 physical-address bits", h["root_cause"])

    def test_priority_needs_misaligned_inaccessible_address(self) -> None:
        good = self.hyp(trap_diag("XCAUSE (trap signature word 1)", "0x4", "0x5", xtval="0x1"))
        self.assertEqual(good["pattern"], "misaligned_vs_access_fault_priority")
        self.assertTrue(good["explains_observation"])
        ram = self.hyp(trap_diag("XCAUSE (trap signature word 1)", "0x7", "0x6", xtval="0x80010001"))
        self.assertEqual(ram["pattern"], "misaligned_vs_access_fault_unproven")
        self.assertFalse(ram["explains_observation"])


class Word0Tests(Base):
    def test_decode(self) -> None:
        d = decode_word0(0x0D040663)
        self.assertEqual((d["mode"], d["entry_size"], d["vector"]), (3, 8, 25))

    def test_non_delegatable_cause_fixed_in_config(self) -> None:
        h = self.hyp(trap_diag("Vector+Mode+Status word (trap signature word 0)", word0(1), word0(3, mpp=1),
                               xcause="0x5"))
        self.assertEqual(h["pattern"], "delegation_not_taken")
        self.assertTrue(h["fixed_in_current_config"])

    def test_trap_from_mmode(self) -> None:
        h = self.hyp(trap_diag("Vector+Mode+Status word (trap signature word 0)", word0(1), word0(3, mpp=3),
                               xcause="0x2"))
        self.assertEqual(h["pattern"], "trap_from_mmode")

    def test_stale_cause_with_zero_epc_is_not_used(self) -> None:
        h = self.hyp(trap_diag("Vector+Mode+Status word (trap signature word 0)", word0(1), word0(3, mpp=1),
                               xepc="0x0", xcause="0x4", style="M"))
        self.assertEqual(h["pattern"], "trap_signature_fields")
        self.assertFalse(h["explains_observation"])

    def test_zero_epc_is_genuine_for_fetch_faults(self) -> None:
        h = self.hyp(trap_diag("Vector+Mode+Status word (trap signature word 0)", word0(1), word0(3, mpp=0),
                               xepc="0x0", xcause="0x1"))
        self.assertNotEqual(h["pattern"], "trap_signature_fields")

    def test_missing_cause_never_defaults_to_zero(self) -> None:
        h = self.hyp(trap_diag("Vector+Mode+Status word (trap signature word 0)", word0(1), word0(3, mpp=1),
                               xcause=None))
        self.assertEqual(h["pattern"], "trap_signature_fields")

    def test_zero_readback_is_not_width_truncation(self) -> None:
        # Megrez InterruptsH_priority-00: sip read 0 where SSIP (2) was expected.
        h = self.hyp(csr_walk("test: 164; cg: InterruptsH_hs_cg; bin: ssi", "0x14402373",
                              "0x0000000000000000", "0x0000000000000002"))
        self.assertNotEqual(h.get("pattern"), "warl_field_truncation")

    def test_interrupt_pending_dropped(self) -> None:
        h = self.hyp(trap_diag("Vector+Mode+Status word (trap signature word 0)", word0(3, xip=1), word0(3),
                               xcause="0x8000000000000007"))
        self.assertEqual(h["pattern"], "interrupt_pending_dropped")


if __name__ == "__main__":
    unittest.main()
