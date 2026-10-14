#!/usr/bin/env python3
"""Tests for triage_tools.py: argument safety, schemas and the tools on small files."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ci.triage.agentic import tools as t

LOG = """[CASE] START name=Sm-00
RVCP: Failure: "Mismatch in trap signature!"
RVCP: Mismatching field: xcause
RVCP: Expected value: 0x4
RVCP: Actual value: 0x5
RVCP: MEPC: 0x80000a3c
RVCP: MCAUSE: 0x5
RVCP: MTVAL: 0x1
RVCP-SUMMARY: TEST FAILED - Test File "x.S"
"""
TRACE = """mem[X,0x0000000080000000] -> 0x0013
[0] [M]: 0x0000000080000000 (0x00000013) addi x0, x0, 0x0              start+0

[1] [M]: 0x0000000080000004 (0x00102503) lw x10, 1(x0)                    start+4
trap load access fault
CSR mcause (0x342) <- 0x0000000000000005

[2] [M]: 0x0000000080000100 (0x00000013) addi x0, x0, 0x0              handler+0

"""


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        os.environ["TRIAGE_TOOL_ROOTS"] = str(self.root)
        (self.root / "uart.log").write_text(LOG)
        (self.root / "x.sig.trace").write_text(TRACE)
        (self.root / "x.sig.log").write_text(f"using {self.root / 'x.sig.trace'} for trace output.\n")

    def tearDown(self):
        os.environ.pop("TRIAGE_TOOL_ROOTS", None)
        self.dir.cleanup()

    def test_footprint(self):
        r = t.dispatch("tool_parse_rvcp_log", {"log_path": str(self.root / "uart.log")})
        self.assertTrue(r["ok"])
        self.assertEqual((r["failing_pc"], r["failing_pc_source"]), ("0x80000a3c", "mepc"))
        self.assertEqual(r["mismatch"], {"kind": "signature", "register": "xcause", "expected": "0x4", "actual": "0x5"})
        self.assertEqual(r["trap"]["cause_name"], "load access fault")

    def test_sail_trace_through_the_log_pointer(self):
        r = t.dispatch("tool_query_sail_trace", json.dumps({"trace_path": str(self.root / "x.sig.log"),
                                                             "target_pc": "0x80000004", "window": 1}))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["at"]["asm"], "lw x10, 1(x0)")
        self.assertEqual(r["at"]["symbol"], "start+4")
        self.assertIn("CSR mcause (0x342) <- 0x0000000000000005", r["at"]["effects"])
        self.assertEqual((r["before"][0]["pc"], r["after"][0]["pc"]), ("0x80000000", "0x80000100"))
        missing = t.dispatch("tool_query_sail_trace", {"trace_path": str(self.root / "x.sig.trace"),
                                                       "target_pc": "0x90000000"})
        self.assertIn("never executed", missing["error"])

    def test_decode_follows_the_board_xlen(self):
        rv64 = {"board_name": "vf2", "xlen": 64}
        rv32 = {"board_name": "ibex", "xlen": 32}
        r = t.dispatch("tool_decode_hardware_state", {"reg_name": "mcause", "hex_val": "0xd"}, rv64)
        self.assertEqual(r["fields"]["name"], "load page fault")
        self.assertEqual(t.dispatch("tool_decode_hardware_state", {"reg_name": "mcause",
                                                                   "hex_val": "0x80000007"}, rv32)["fields"]["name"],
                         "MTI (machine timer)")
        sd32 = t.dispatch("tool_decode_hardware_state", {"reg_name": "mstatus", "hex_val": "0x80006000"}, rv32)
        self.assertEqual((sd32["xlen"], sd32["fields"]["SD"]), (32, 1))
        self.assertNotIn("UXL", sd32["fields"])
        sd64 = t.dispatch("tool_decode_hardware_state", {"reg_name": "mstatus", "hex_val": "0x80006000"}, rv64)
        self.assertEqual(sd64["fields"]["SD"], 0)  # bit 31 is not SD on RV64
        wide = t.dispatch("tool_decode_hardware_state", {"reg_name": "mstatus", "hex_val": "0x8000000000006000"},
                          rv32)
        self.assertIn("wider than XLEN 32", wide["error"])
        self.assertIn("no hardware profile", t.dispatch("tool_decode_hardware_state",
                                                        {"reg_name": "mcause", "hex_val": "0x1"})["error"])

    def test_disassembly_choices(self):
        self.assertEqual(t.disassembler_flags({"priv_version": "1.11"}, "rv64i2p1"), ["-M", "priv-spec=1.11"])
        self.assertEqual(t.disassembler_flags({"priv_version": "1.12.0"}, None), ["-M", "priv-spec=1.12,max"])
        self.assertEqual(t.arch_extensions("rv32i2p1_m2p0_c2p0_zicsr2p0_zba1p0"), {"I", "M", "C", "Zicsr", "Zba"})
        elf = t.arch_extensions("rv32i2p1_m2p0_a2p1_c2p0_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0")
        self.assertEqual(t.beyond_board(elf, {"I", "M", "C", "Zicsr", "U"}), ["A"])
        self.assertEqual(t.beyond_board(elf, {"I", "M", "A", "C"}), [])
        os.environ["TRIAGE_RISCV32_UNKNOWN_ELF_OBJDUMP"] = "/opt/fake/riscv32-objdump"
        try:
            self.assertEqual(t.objdump_for(32), ("/opt/fake/riscv32-objdump", ""))
        finally:
            del os.environ["TRIAGE_RISCV32_UNKNOWN_ELF_OBJDUMP"]
        self.assertIn("is not an ELF", t.dispatch("tool_get_disassembly", {
            "elf_path": str(self.root / "uart.log"), "target_pc": "0x80000000"}, {"xlen": 64})["error"])
        self.assertIn("does not fit a 32-bit", t.dispatch("tool_get_disassembly", {
            "elf_path": str(self.root / "uart.log"), "target_pc": "0x180000000"}, {"xlen": 32})["error"])

    def test_type_safety(self):
        bad = [
            ("tool_decode_hardware_state", {"reg_name": "mcause", "hex_val": "13"}),
            ("tool_decode_hardware_state", {"reg_name": "mcause", "hex_val": "0x" + "f" * 17}),
            ("tool_decode_hardware_state", {"reg_name": "mcause", "hex_val": "0xd", "isa": "RV64"}),
            ("tool_get_disassembly", {"elf_path": str(self.root / "uart.log"), "target_pc": "0x0", "window": 0}),
            ("tool_get_disassembly", {"elf_path": str(self.root / "uart.log"), "target_pc": "0x0", "window": True}),
            ("tool_parse_rvcp_log", {"log_path": str(self.root / "uart.log"), "extra": 1}),
            ("tool_parse_rvcp_log", {}),
            ("tool_parse_rvcp_log", {"log_path": "/etc/passwd"}),
            ("no_such_tool", {}),
        ]
        for name, args in bad:
            result = t.dispatch(name, args, {"xlen": 64})
            self.assertFalse(result["ok"], (name, args))
            self.assertTrue(result["error"])

    def test_schema_exports(self):
        names = [s["name"] for s in t.TOOL_SCHEMAS]
        self.assertEqual(names, list(t.FUNCTIONS))
        strict = t.openai_tools()
        for tool in strict:
            params = tool["function"]["parameters"]
            self.assertEqual(set(params["required"]), set(params["properties"]))
            self.assertFalse(params["additionalProperties"])
        window = strict[1]["function"]["parameters"]["properties"]["window"]
        self.assertEqual(window["type"], ["integer", "null"])
        self.assertEqual(t.anthropic_tools()[0]["input_schema"]["required"], ["log_path"])


if __name__ == "__main__":
    unittest.main()
