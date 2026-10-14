#!/usr/bin/env python3
"""Tests for riscv_state_decoder.py (register fields and probe rows)."""

from __future__ import annotations

import unittest

from ci.triage.agentic import decoder as d


def log(words: list[int]) -> str:
    return "".join(f"[SIGQ] 0x{0x80001000 + 8 * i:016x} : 0x{w:016x}\n" for i, w in enumerate(words))


class RegisterTests(unittest.TestCase):
    def test_mstatus_rv64_and_rv32(self):
        f = d.decode_register("mstatus", "0x8000000a00006080", "RV64GC")["fields"]
        self.assertEqual((f["MPIE"], f["FS"], f["SD"], f["UXL"]), (1, "3 (Dirty)", 1, "2 (XLEN 64)"))
        f32 = d.decode_register("mstatus", "0x80001888", "RV32IMAC")["fields"]
        self.assertEqual((f32["MPP"], f32["SD"], f32["MIE"]), ("3 (M)", 1, 1))
        self.assertNotIn("UXL", f32)

    def test_sstatus_shows_only_supervisor_fields(self):
        f = d.decode_register("sstatus", "0x1800", "RV64")["fields"]
        self.assertNotIn("MPP", f)

    def test_cause_tval_and_canonical(self):
        self.assertEqual(d.decode_register("mcause", "0x8000000000000007", "RV64")["fields"]["name"],
                         "MTI (machine timer)")
        self.assertEqual(d.decode_register("scause", "0xd", "RV64")["fields"]["name"], "load page fault")
        tval = d.decode_register("stval", "0x4000000001", "RV64")
        self.assertFalse(tval["fields"]["canonical_Sv39"])
        self.assertTrue(tval["fields"]["canonical_Sv48"])
        self.assertIn("not a canonical Sv39", tval["notes"][0])

    def test_delegation_interrupts_and_translation(self):
        self.assertEqual(sorted(d.decode_register("medeleg", "0xb1ff", "RV64")["fields"]["delegated"]),
                         [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 13, 15])
        self.assertEqual(list(d.decode_register("sie", "0x222", "RV64")["fields"]["bits"]), [1, 5, 9])
        satp = d.decode_register("satp", hex(8 << 60 | 5 << 44 | 0x80123), "RV64")["fields"]
        self.assertEqual((satp["MODE"], satp["ASID"], satp["root_table"]), ("Sv39", 5, "0x80123000"))
        self.assertEqual(d.decode_register("hgatp", hex(8 << 60), "RV64")["fields"]["MODE"], "Sv39x4")
        pte = d.decode_register("pte", hex(0x80200 << 10 | 0b01000111), "RV64")
        self.assertIn("stores fault unless", " ".join(pte["notes"]))
        self.assertIn("W=1 with R=0", d.decode_register("pte", "0x5", "RV64")["notes"][0])

    def test_misa_tvec_unknown(self):
        self.assertEqual(d.decode_register("misa", hex(2 << 62 | 0x14112D), "RV64")["fields"]["extensions"],
                         "ACDFIMSU")
        self.assertEqual(d.decode_register("mtvec", "0x80000101", "RV64")["fields"]["MODE"], "Vectored")
        self.assertEqual(d.decode_register("mscratch", "0x5", "RV64")["fields"]["set_bits"], [0, 2])


class ProbeTests(unittest.TestCase):
    def test_marker_detection_and_cause_rows(self):
        probe = d.PROBES["sv39_amo_priority"]
        rows = [probe.marker] + [c << 56 | 15 << 48 | 0x40 + c for c in range(1, 8)]
        result = d.decode_probe(log(rows))
        self.assertEqual(result["probe"], "sv39_amo_priority")
        self.assertEqual(result["rows"][0]["cause_name"], "store/AMO page fault")
        self.assertEqual(result["missing"], [8])

    def test_readback_rows_are_decoded(self):
        probe = d.PROBES["medeleg_bits"]
        result = d.decode_probe(log([probe.marker, 2 << 56 | 0x8 << 32 | 0x8, probe.end]))
        self.assertEqual(result["rows"][0]["readback_decoded"], {3: "breakpoint"})
        self.assertIn("final marker seen", result["notes"])

    def test_no_marker(self):
        self.assertIn("error", d.decode_probe(log([1, 2, 3])))


if __name__ == "__main__":
    unittest.main()
