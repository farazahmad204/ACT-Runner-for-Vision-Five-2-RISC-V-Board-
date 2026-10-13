#!/usr/bin/env python3
"""Tests for gen_act_config.py (ACT config generation from a board spec)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

import yaml

import gen_act_config as gen

SKILL_DIR = Path(__file__).resolve().parent.parent
SPECS = SKILL_DIR / "assets" / "board_specs"
LIBRARY = yaml.safe_load((SKILL_DIR / "assets" / "act" / "udb_library.yaml").read_text())

# Extension list of the reviewed hand-made Megrez config (ACT milkv-megrez-config 073917f).
MEGREZ_HAND_EXTS = {"I", "M", "Zmmul", "A", "F", "D", "C", "U", "S", "Sm", "Zicsr", "Zifencei", "Zihpm", "Zicntr",
                    "Zaamo", "Zalrsc", "Zca", "Zcd", "Zba", "Zbb", "Sscofpmf", "Sscounterenw", "Sstvecd", "Ssu64xl",
                    "Sv39", "Sv48", "Svbare"}


def spec(name: str) -> dict:
    return yaml.safe_load((SPECS / f"{name}.yaml").read_text())


def run(s: dict, act_root: Path | None = None) -> tuple[int, Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    try:
        rc = gen.generate(s, LIBRARY, Path(tmp.name), act_root)
    except Exception:
        tmp.cleanup()
        raise
    return rc, Path(tmp.name), tmp


class IsaTests(unittest.TestCase):
    def test_g_and_c_expand(self) -> None:
        xlen, exts, nonstd = gen.parse_isa("rv64gc_zba", LIBRARY)
        self.assertEqual(xlen, 64)
        self.assertTrue({"I", "M", "A", "F", "D", "C", "Zca", "Zcd", "Zicsr", "Zifencei", "Zaamo", "Zalrsc",
                         "Zmmul", "Zba"} <= exts)
        self.assertEqual(nonstd, [])

    def test_unknown_extension_and_nonstandard(self) -> None:
        with self.assertRaises(gen.SpecError):
            gen.parse_isa("rv64imac_zfoo", LIBRARY)
        _, _, nonstd = gen.parse_isa("rv64imac_xtheadba", LIBRARY)
        self.assertEqual(nonstd, ["xtheadba"])

    def test_version_requirements(self) -> None:
        self.assertFalse(gen.version_ok("1.11.0", ">= 1.12.0"))
        self.assertTrue(gen.version_ok("1.0.0", "~> 1.0"))
        self.assertFalse(gen.version_ok("2.0.0", "~> 1.0"))


class GenerateTests(unittest.TestCase):
    def test_megrez_matches_reviewed_config(self) -> None:
        rc, out, tmp = run(spec("milkv_megrez"))
        with tmp:
            self.assertEqual(rc, 0)
            udb = yaml.safe_load((out / "milkv-megrez-p550.yaml").read_text())
            self.assertEqual({e["name"] for e in udb["implemented_extensions"]}, MEGREZ_HAND_EXTS)
            p = udb["params"]
            self.assertEqual(p["PHYS_ADDR_WIDTH"], 41)
            self.assertEqual(p["PMP_GRANULARITY"], 12)
            self.assertEqual(p["HPM_COUNTER_EN"][3:8], [True, True, True, True, False])
            self.assertNotIn("CONFIG_PTR_ADDRESS", p)          # Sm >= 1.12 only
            self.assertNotIn("HPM_COUNTER7_WIDTH", p)          # counter 7 not implemented
            sail = json.loads((out / "sail.json").read_text())
            self.assertTrue(sail["extensions"]["H"]["supported"])   # draft H kept for misa
            self.assertEqual(sail["base"]["medeleg"]["delegatable_bits"]["value"], "0x0000_0000_00f0_b7ff")
            self.assertEqual(sail["memory"]["pmp"]["grain"], 10)
            self.assertEqual(sail["memory"]["regions"][-1]["base"]["value"], "0x90000000")
            m = (out / "rvmodel_macros.h").read_text()
            for line in ("BOARD_PLIC_EN_M_UART  0x0c00210c", "BOARD_PLIC_EN_WORDS   17",
                         "BOARD_PLIC_CLAIM_S    0x0c203004", "BOARD_UART_BIT        0x10",
                         "RVMODEL_MTIMECMP_ADDRESS  0x02004008", "RVMODEL_MAX_CYCLES_PER_TIMER_TICK 2048"):
                self.assertIn(line, m)
            self.assertNotIn("{{", m)
            report = yaml.safe_load((out / "generation_report.yaml").read_text())
            self.assertEqual(report["hypervisor"], "excluded")
            self.assertIn("RAM_ORIGIN = 0x90000000;", (out / "link.ld").read_text())

    def test_vf2_uart_byte_access_and_contexts(self) -> None:
        rc, out, tmp = run(spec("visionfive2"))
        with tmp:
            self.assertEqual(rc, 0)
            m = (out / "rvmodel_macros.h").read_text()
            self.assertIn("lbu _R3, 0(_R2)", m)
            self.assertIn("BOARD_PLIC_EN_M_BASE  0x0c002080", m)
            self.assertIn("BOARD_PLIC_EN_WORDS   5", m)
            self.assertIn("RVMODEL_MAX_CYCLES_PER_TIMER_TICK 512", m)
            sail = json.loads((out / "sail.json").read_text())
            self.assertFalse(sail["extensions"]["H"]["supported"])
            self.assertEqual(sail["memory"]["asidlen"], 0)
            udb = yaml.safe_load((out / "visionfive2-rv64gc.yaml").read_text())
            self.assertEqual(udb["params"]["COUNTINHIBIT_EN"][3:5], [False, False])

    def test_hypervisor_as_ratified_declares_priv_112(self) -> None:
        s = spec("milkv_megrez")
        s["hypervisor"].update(test_as_ratified=True, geilen=0)
        rc, out, tmp = run(s)
        with tmp:
            udb = yaml.safe_load((out / "milkv-megrez-p550.yaml").read_text())
            vers = {e["name"]: e["version"] for e in udb["implemented_extensions"]}
            self.assertIn("H", vers)
            self.assertEqual(vers["Sm"], "= 1.12.0")
            sail = json.loads((out / "sail.json").read_text())
            self.assertEqual(sail["base"]["privileged_isa_version"], "Privileged_ISA_1_11")
            self.assertEqual(sail["extensions"]["H"]["geilen"], 0)
            report = yaml.safe_load((out / "generation_report.yaml").read_text())
            self.assertEqual(report["hypervisor"], "tested_as_ratified")
            self.assertEqual(len(report["deviations"]), 2)

    def test_missing_input_returns_2(self) -> None:
        s = spec("milkv_megrez")
        del s["csr"]["medeleg_writable"]
        rc, out, tmp = run(s)
        with tmp:
            self.assertEqual(rc, 2)
            self.assertIn("Missing inputs", (out / "README.md").read_text())

    def test_unknown_param_override_is_rejected(self) -> None:
        s = spec("milkv_megrez")
        s["params"]["NOT_A_PARAM"] = 1
        with self.assertRaises(gen.SpecError):
            run(s)

    def test_window_overlapping_sail_io_is_rejected(self) -> None:
        s = spec("milkv_megrez")
        s["memory"] = {"base": 0x08000000, "size": 0x10000000}
        with self.assertRaises(gen.SpecError):
            run(s)


class SuiteTests(unittest.TestCase):
    def test_suite_split(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "testplans" / "priv").mkdir(parents=True)
            for n in ("I", "Zba", "Zbs", "Vx", "ZfaD"):
                (root / "testplans" / f"{n}.csv").write_text("")
            gen_dir = root / "generators" / "testgen" / "src" / "testgen" / "priv" / "extensions"
            gen_dir.mkdir(parents=True)
            for n in ("Sm", "InterruptsSm", "ExceptionsHSm", "ZicntrCommon", "SmV"):
                (gen_dir / f"{n}.py").write_text("")
            (root / "tests" / "priv" / "ExceptionsSvSm").mkdir(parents=True)
            (root / "tests" / "priv" / "PMPSm").mkdir()
            exts = {"I", "Zba", "Sm", "S", "U", "H", "Sv39"}
            plan = gen.plan_suites(root, exts, has_pmp=True, h_status="tested")
            s = plan["suites"]
            self.assertEqual(s["unpriv"], ["I", "Zba"])
            self.assertEqual(sorted(s["priv"]), ["ExceptionsSvSm", "InterruptsSm", "PMPSm", "Sm"])
            self.assertEqual(s["hypervisor"], ["ExceptionsHSm"])
            self.assertEqual(s["vector"], [])
            skipped = " ".join(plan["skipped"]["not implemented"])
            self.assertIn("Zbs", skipped)
            self.assertIn("SmV", skipped)


if __name__ == "__main__":
    unittest.main()
