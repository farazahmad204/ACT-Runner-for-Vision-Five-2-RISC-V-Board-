from __future__ import annotations

import copy
import unittest

from ci.triage.agentic import report_schema as rs

GOOD = {
    "triage_status": "SUCCESS", "target_board": "vf2_jh7110", "failure_classification": "UDB_Config_Mismatch",
    "tier_1_root_cause": {"failing_pc": "0x90000138", "failing_instruction": "ebreak",
                          "architectural_symptom": "status word 0x11000673 vs 0x19000673 (mstatus.MPV)",
                          "divergence_cycle": 612},
    "tier_2_board_specific_remediation": {"remediation_type": "CONFIG_REMAP",
                                          "target_repository_or_file": "config/cores/sifive_u74/sail.json",
                                          "actionable_fix_directive": "1. Set ... 2. Regenerate ..."},
}
HARD = {"board_name": "vf2_jh7110", "core_type": "hard_macro"}
SOFT = {"board_name": "ibex_fpga", "core_type": "soft_core", "rtl_source": "https://github.com/lowRISC/ibex"}


def edit(**changes):
    report = copy.deepcopy(GOOD)
    for path, value in changes.items():
        node = report
        keys = path.split("__")
        for key in keys[:-1]:
            node = node[key]
        if value is KeyError:
            del node[keys[-1]]
        else:
            node[keys[-1]] = value
    return report


class StrictSchemaTests(unittest.TestCase):
    def test_valid_report_parses_to_typed_model(self):
        report = rs.parse_report(GOOD, HARD)
        self.assertEqual(report.tier_1_root_cause.divergence_cycle, 612)
        self.assertEqual(report.tier_2_board_specific_remediation.remediation_type, "CONFIG_REMAP")
        self.assertEqual(rs.check_report(edit(tier_1_root_cause__divergence_cycle="not traceable"), HARD), [])

    def test_type_enum_required_and_extra_keys(self):
        cases = {
            "triage_status must be one of": edit(triage_status="DONE"),
            "failure_classification must be one of": edit(failure_classification="Inconclusive"),
            "tier_1_root_cause.failing_pc 'pc 0x10' does not match": edit(tier_1_root_cause__failing_pc="pc 0x10"),
            "divergence_cycle must be string or integer": edit(tier_1_root_cause__divergence_cycle=1.5),
            "tier_2_board_specific_remediation.remediation_type must be one of":
                edit(tier_2_board_specific_remediation__remediation_type="PATCH"),
            "actionable_fix_directive must not be empty":
                edit(tier_2_board_specific_remediation__actionable_fix_directive="  "),
            "tier_1_root_cause.failing_instruction is missing": edit(tier_1_root_cause__failing_instruction=KeyError),
            "report.notes is not allowed": {**GOOD, "notes": "x"},
            "tier_1_root_cause.extra is not allowed": edit(tier_1_root_cause__extra="x"),
            "report must be object": "not json",
        }
        for expected, payload in cases.items():
            problems = rs.check_report(payload, HARD)
            self.assertTrue(any(expected in p for p in problems), (expected, problems))
        with self.assertRaises(rs.SchemaError):
            rs.parse_report(edit(triage_status="DONE"))

    def test_board_rules(self):
        self.assertIn("target_board must be 'vf2_jh7110'", rs.check_report(edit(target_board="bpif3_k1"), HARD)[0])
        rtl = edit(failure_classification="DUT_RTL_Bug", tier_2_board_specific_remediation__remediation_type="RTL_PATCH",
                   tier_2_board_specific_remediation__target_repository_or_file="rtl/ibex_cs_registers.sv")
        self.assertIn("RTL_PATCH is not possible on this core (core type hard_macro)", rs.check_report(rtl, HARD)[0])
        self.assertEqual(rs.check_report({**rtl, "target_board": "ibex_fpga"}, SOFT), [])
        no_file = edit(target_board="ibex_fpga", tier_2_board_specific_remediation__remediation_type="RTL_PATCH",
                       tier_2_board_specific_remediation__target_repository_or_file="the CSR unit")
        self.assertIn("must name the HDL module source file", rs.check_report(no_file, SOFT)[0])

    def test_status_rules(self):
        self.assertIn("Unclassified is only for FAILED_TO_ISOLATE",
                      rs.check_report(edit(failure_classification="Unclassified"), HARD)[0])
        self.assertIn("a SUCCESS must give tier_1_root_cause.failing_pc",
                      rs.check_report(edit(tier_1_root_cause__failing_pc=""), HARD)[0])
        unresolved = rs.unresolved_report("vf2_jh7110", "No valid report.", ["finding"])
        self.assertEqual(rs.check_report(unresolved, HARD), [])

    def test_sanity_applies_to_isolated_reports(self):
        sanity = [{"check": "a_extension_missing", "message": "m", "allowed": ["Test_Suite_Error"]}]
        self.assertIn("contradicts the architectural sanity check", rs.check_report(GOOD, HARD, sanity)[0])
        self.assertEqual(rs.check_report(edit(failure_classification="Test_Suite_Error"), HARD, sanity), [])


class BackendSchemaTests(unittest.TestCase):
    def test_openai_strict_schema_keeps_structure_drops_unsupported_keywords(self):
        from ci.triage.agentic import react
        strict = react.openai_strict(rs.TRIAGE_REPORT_SCHEMA)
        self.assertNotIn("minLength", str(strict))
        self.assertEqual(strict["required"], rs.TRIAGE_REPORT_SCHEMA["required"])
        self.assertFalse(strict["properties"]["tier_1_root_cause"]["additionalProperties"])
        self.assertEqual(strict["properties"]["tier_1_root_cause"]["properties"]["divergence_cycle"]["type"],
                         ["string", "integer"])


if __name__ == "__main__":
    unittest.main()
