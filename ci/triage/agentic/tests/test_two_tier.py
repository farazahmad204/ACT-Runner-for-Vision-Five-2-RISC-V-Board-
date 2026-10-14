from __future__ import annotations

import unittest

from ci.triage.agentic import ci_bridge, hardware, react

TIER1 = {"invariant": "norm:mstatus_mpp", "instruction": "ebreak", "pc": "0x90000138", "sail_step": "612",
         "divergence": "status word 0x11000673 vs 0x19000673", "subcomponent": "trap unit (inferred)"}


def final(kind, target="", klass="DUT_RTL_Bug"):
    return {"analysis_status": "COMPLETED", "failure_classification": klass, "symptom_isolation": "s",
            "identified_root_cause": "r", "reproduction_steps_or_fix": "f", "tier1_root_cause": TIER1,
            "tier2_remediation": {"fix_kind": kind, "target": target, "directive": "do it"}}


HARD = {"board_name": "vf2_jh7110", "core_type": "hard_macro"}
SOFT = {"board_name": "ibex_fpga", "core_type": "soft_core", "rtl_source": "https://github.com/lowRISC/ibex"}


class TwoTierTests(unittest.TestCase):
    def test_no_rtl_on_fixed_silicon(self):
        problems = react.validate_final(final("rtl", "rtl/ibex_cs_registers.sv"), profile=HARD)
        self.assertTrue(any("not possible on this core (core type hard_macro)" in p for p in problems))
        self.assertTrue(react.validate_final(final("rtl", "rtl/x.sv"), profile={}))  # unknown core: no RTL
        self.assertEqual(react.validate_final(final("erratum", "SiFive U74 errata"), profile=HARD), [])
        self.assertEqual(react.validate_final(final("firmware", "runner_machine_trap.c"), profile=HARD), [])

    def test_rtl_on_a_soft_core_names_the_module(self):
        self.assertEqual(react.validate_final(final("rtl", "rtl/ibex_cs_registers.sv"), profile=SOFT), [])
        self.assertIn("must name the HDL module file", react.validate_final(final("rtl", "the CSR logic"),
                                                                            profile=SOFT)[0])

    def test_tiers_are_required(self):
        payload = final("test", "x.S")
        del payload["tier1_root_cause"]
        self.assertIn("tier1_root_cause must have", react.validate_final(payload)[0])

    def test_profile_states_the_core_type(self):
        text = hardware.get_hardware_context_string(hardware.load_hardware_profile(
            "ci/triage/board_knowledge/vf2_jh7110.yaml"))
        self.assertIn("Core type: hard macro (fixed silicon)", text)
        self.assertIn("unknown: treat as fixed silicon", hardware.get_hardware_context_string({}))

    def test_report_carries_both_tiers(self):
        answer = ci_bridge.to_answer(final("firmware", "runner_machine_trap.c", "Test_Suite_Error"),
                                     {"steps": [], "status": "completed"})
        self.assertEqual(answer["tier1_root_cause"]["sail_step"], "612")
        self.assertEqual(answer["next_step"], "[firmware: runner_machine_trap.c] do it")


if __name__ == "__main__":
    unittest.main()
