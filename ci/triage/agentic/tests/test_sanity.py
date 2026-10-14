#!/usr/bin/env python3
"""Tests for the architectural sanity checks, errata and payload assembly (Phase 7)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ci.triage.agentic import react as agent
from ci.triage.agentic.tests.test_react import Scripted, turn

IBEX = {"board_name": "ibex", "core_name": "lowRISC Ibex", "xlen": 32, "implemented_isa": "RV32IMC_Zicsr",
        "extensions": ["I", "M", "C", "Zicsr", "U"], "privilege_modes": ["M", "U"], "mmu_type": "None"}
VF2 = {"board_name": "VisionFive2", "core_name": "SiFive U74", "xlen": 64, "implemented_isa": "RV64GC",
       "extensions": ["I", "M", "A", "F", "D", "C", "Zicsr", "Zifencei", "S", "U", "Sv39"],
       "privilege_modes": ["M", "S", "U"], "mmu_type": "Sv39"}
DUT_BUG = {"analysis_status": "COMPLETED", "failure_classification": "DUT_RTL_Bug",
           "symptom_isolation": "amoadd.w trapped", "identified_root_cause": "the core rejects amoadd.w",
           "reproduction_steps_or_fix": "file a bug"}


def ok(**fields):
    return {"ok": True, **fields}


class SanityTests(unittest.TestCase):
    def checks(self, profile, tool, result):
        return [c["check"] for c in agent.architectural_sanity(profile, tool, result)]

    def test_translation_on_a_core_without_mmu(self):
        reported = ok(trap={"mcause": "0xd"}, case="SvSm-00", failure="x")
        self.assertEqual(self.checks(IBEX, "tool_parse_rvcp_log", reported), ["translation_reported_without_mmu"])
        expected = ok(trap={"mcause": "0x2"}, case="SvSm-00", failure="expected load page fault")
        self.assertEqual(self.checks(IBEX, "tool_parse_rvcp_log", expected), ["translation_expected_without_mmu"])
        self.assertEqual(self.checks(VF2, "tool_parse_rvcp_log", reported), [])  # VF2 has Sv39

    def test_extension_missing_for_the_failing_instruction(self):
        amo = ok(instruction={"asm": "amoadd.w\ta1,a0,(a2)", "encoding": "00a6252f"}, notes=[])
        self.assertEqual(self.checks(IBEX, "tool_get_disassembly", amo), ["a_extension_missing"])
        self.assertEqual(self.checks(VF2, "tool_get_disassembly", amo), [])
        compressed = ok(instruction={"asm": "addi\ta0,a0,1", "encoding": "0505"}, notes=[])
        no_c = {**IBEX, "extensions": ["I", "M", "Zicsr", "U"]}
        self.assertEqual(self.checks(no_c, "tool_get_disassembly", compressed), ["c_extension_missing"])
        self.assertEqual(self.checks(IBEX, "tool_get_disassembly", compressed), [])
        fp = ok(instruction={"asm": "fld\tfa0,0(a0)", "encoding": "00053507"}, notes=[])
        self.assertEqual(self.checks(IBEX, "tool_get_disassembly", fp), ["f_extension_missing"])
        xlen = ok(instruction={"asm": "nop", "encoding": "00000013"},
                  notes=["MISMATCH: t.elf is a 64-bit ELF but the board is 32-bit"])
        self.assertEqual(self.checks(IBEX, "tool_get_disassembly", xlen), ["xlen_mismatch"])

    def test_a_contradicting_dut_bug_is_rejected(self):
        sanity = agent.architectural_sanity(IBEX, "tool_get_disassembly",
                                            ok(instruction={"asm": "amoadd.w a1,a0,(a2)", "encoding": "00a6252f"}))
        problems = agent.validate_final(DUT_BUG, sanity)
        self.assertIn("contradicts the architectural sanity check a_extension_missing", problems[0])
        self.assertEqual(agent.validate_final({**DUT_BUG, "failure_classification": "Test_Suite_Error"}, sanity), [])

    def test_errata_and_payload_order(self):
        errata = agent.platform_errata(VF2)
        self.assertIn("vf2_u74_platform.md", errata)
        self.assertIn("Known Priority Behavior", errata)
        self.assertEqual(agent.platform_errata(IBEX), "")
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "events.log"
            log.write_text("boot noise\nRVCP: Failure: \"x\"\nmore noise\n[TRAP] mcause=0x2 mepc=0x1 mtval=0x0 mstatus=0x0\n")
            text = agent.build_user_payload("Sm-00", {"log_path": str(log)}, 8, "Target hardware: VF2", errata)
        order = [text.index(k) for k in ("TARGET HARDWARE PROFILE", "Board platform notes",
                                         "RUNTIME FAILURE LOG", "Failed case: Sm-00")]
        self.assertEqual(order, sorted(order))
        self.assertIn('RVCP: Failure: "x"', text)
        self.assertNotIn("boot noise", text)


class LoopTests(unittest.TestCase):
    def test_the_loop_enforces_sanity(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            os.environ["TRIAGE_TOOL_ROOTS"] = d
            (root / "events.log").write_text('RVCP: Failure: "page fault expected"\nRVCP: MCAUSE: 0xd\n')
            model = Scripted([
                turn("Read it.", "tool_parse_rvcp_log", {"log_path": str(root / "events.log")}),
                turn("Hardware bug.", "submit_analysis", {**DUT_BUG, "identified_root_cause": "page fault"}),
                turn("Then config.", "submit_analysis", {**DUT_BUG, "failure_classification": "Test_Suite_Error"}),
            ])
            final = agent.run_react("SvSm-00", {"log_path": str(root / "events.log"), "isa": "RV32IMC"},
                                    root / "out", backend=model, profile=IBEX, hardware="Target hardware: ibex")
            os.environ.pop("TRIAGE_TOOL_ROOTS")
            state = json.loads((root / "out" / "agent_state.json").read_text())
        self.assertEqual(final["failure_classification"], "Test_Suite_Error")
        self.assertIn("ARCHITECTURAL SANITY (translation_reported_without_mmu)", model.observations[0][1])
        self.assertIn("contradicts the architectural sanity check", model.observations[1][1])
        self.assertEqual(state["sanity"][0]["check"], "translation_reported_without_mmu")
        self.assertTrue(model.user.startswith("TARGET HARDWARE PROFILE"))


if __name__ == "__main__":
    unittest.main()
