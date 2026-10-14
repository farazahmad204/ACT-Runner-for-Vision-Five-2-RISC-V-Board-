#!/usr/bin/env python3
"""Tests for hardware_registry.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci.triage.agentic import hardware as hr

UDB = """kind: architecture configuration
name: ibex-fpga
description: lowRISC Ibex on an Arty A7 FPGA.
implemented_extensions:
  - { name: I, version: "= 2.1" }
  - { name: M, version: "= 2.0" }
  - { name: C, version: "= 2.0" }
  - { name: Zicsr, version: "= 2.0" }
  - { name: U, version: "= 1.0.0" }
  - { name: Sm, version: "= 1.12.0" }
params:
  MXLEN: 32
"""


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.write_text(text)
        return str(path)

    def test_isa_strings(self):
        self.assertEqual(hr.parse_isa("rv64gc_zba")[1][:5], ["I", "M", "A", "F", "D"])
        self.assertEqual(hr.isa_string(64, ["I", "M", "A", "F", "D", "C", "Zicsr", "Zifencei", "Zba"]),
                         "RV64GC_Zba")
        self.assertEqual(hr.isa_string(32, ["I", "M", "C", "Zicsr"]), "RV32IMC_Zicsr")

    def test_rv32_bare_metal_core_from_udb(self):
        p = hr.load_hardware_profile(self.write("ibex.yaml", UDB))
        self.assertEqual((p["board_name"], p["core_name"], p["xlen"]), ("ibex-fpga", "lowRISC Ibex", 32))
        self.assertEqual(p["implemented_isa"], "RV32IMC_Zicsr")
        self.assertEqual(p["privilege_modes"], ["M", "U"])
        self.assertEqual(p["mmu_type"], "None")  # no S-mode: no translation
        self.assertEqual(p["missing"], [])
        text = hr.get_hardware_context_string(p)
        self.assertIn("XLEN: 32; ISA: RV32IMC_Zicsr", text)
        self.assertIn("Address translation: None", text)

    def test_test_config_follows_udb_config(self):
        self.write("ibex.yaml", UDB)
        p = hr.load_hardware_profile(self.write("test_config.yaml", "name: x\nudb_config: ibex.yaml\n"))
        self.assertEqual(p["xlen"], 32)

    def test_markdown_is_completed_from_the_same_core(self):
        p = hr.load_hardware_profile(str(Path(__file__).parents[1] / "knowledge" / "vf2_u74_platform.md"))
        self.assertEqual((p["board_name"], p["soc"], p["core_name"]), ("VisionFive2", "JH7110", "SiFive U74"))
        if (hr.STRUCTURED_DIRS[0] / "visionfive2.yaml").is_file():
            self.assertEqual((p["xlen"], p["mmu_type"]), (64, "Sv39"))
            self.assertIn("board_specs/visionfive2.yaml", p["sources"]["xlen"])

    def test_nothing_is_guessed(self):
        p = hr.load_hardware_profile(self.write("x_platform.md", "# Board X\n\nPlatform: Board X / Chip Y.\n"))
        self.assertIsNone(p["xlen"])
        self.assertIn("xlen", p["missing"])
        self.assertIn("Unknown (no source states it; do not assume)", hr.get_hardware_context_string(p))

    def test_unknown_format(self):
        with self.assertRaises(ValueError):
            hr.load_hardware_profile(self.write("other.yaml", "foo: 1\n"))


if __name__ == "__main__":
    unittest.main()
