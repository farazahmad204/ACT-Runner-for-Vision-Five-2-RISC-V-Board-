from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ci.triage.agentic import ci_bridge, tools

UDB = """kind: architecture configuration
name: vendor-x1
description: Vendor X1 board with an Ibex core.
implemented_extensions:
  - { name: I, version: "= 2.1" }
  - { name: M, version: "= 2.0" }
  - { name: C, version: "= 2.0" }
  - { name: U, version: "= 1.0.0" }
params:
  MXLEN: 32
"""


class BridgeTests(unittest.TestCase):
    def test_profile_from_the_runs_act_config_for_an_unknown_board(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "act" / "config").mkdir(parents=True)
            (run / "act" / "config" / "x1.yaml").write_text(UDB)
            profile = ci_bridge.board_profile(run, "vendor_x1")
        self.assertEqual((profile["board_name"], profile["xlen"], profile["implemented_isa"]),
                         ("vendor_x1", 32, "RV32IMC"))
        self.assertEqual(profile["mmu_type"], "None")

    def test_known_board_profile_comes_from_its_board_knowledge(self):
        profile = ci_bridge.board_profile(Path("/nonexistent"), "milkv_megrez_eic7700x")
        self.assertEqual(profile["core_name"], "SiFive P550")
        self.assertEqual(profile["spec_versions"]["hypervisor"], "h-0.6")
        self.assertEqual(profile["priv_version"], "1.11")  # measured, not the ACT config's declared 1.12
        self.assertTrue(profile["facts"])

    def test_answer_mapping(self):
        state = {"steps": [{"step": 1, "tool": "tool_parse_rvcp_log", "ok": True, "thought": "t", "arguments": {}}],
                 "sanity": [], "findings": [{"text": "f"}], "status": "completed"}
        final = {"failure_classification": "DUT_RTL_Bug", "symptom_isolation": "s", "identified_root_cause": "r",
                 "reproduction_steps_or_fix": "n", "analysis_status": "COMPLETED"}
        answer = ci_bridge.to_answer(final, state)
        self.assertEqual((answer["classification"], answer["confidence"]), ("DUT noncompliance", "low"))
        self.assertIn("without a specification search", answer["checked"])
        state["steps"].append({"step": 2, "tool": "tool_search_spec", "ok": True, "thought": "", "arguments": {}})
        self.assertEqual(ci_bridge.to_answer(final, state)["confidence"], "medium")
        self.assertEqual(ci_bridge.to_answer({**final, "failure_classification": "UDB_Config_Mismatch"},
                                             state)["classification"], "Reference-model mismatch")

    def test_sail_trace_is_generated_on_demand_and_the_pc_mapped(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["TRIAGE_TOOL_ROOTS"] = d
            trace = Path(d) / "traces" / "x.sig.trace"

            def generate(path):
                path.parent.mkdir(parents=True)
                path.write_text("[0] [M]: 0x0000000080001000 (0x00000013) addi x0, x0, 0x0   start+0\n")
                return True

            tools.SAIL_HOOKS.clear()
            tools.SAIL_HOOKS.update(generate=generate, map_pc=lambda pc: (0x80001000, "start+0x0"))
            try:
                result = tools.dispatch("tool_query_sail_trace", {"trace_path": str(trace), "target_pc": "0x90000000"})
                tools.SAIL_HOOKS["generate"] = lambda path: False
                missing = tools.dispatch("tool_query_sail_trace", {"trace_path": str(Path(d) / "none.sig.trace"),
                                                                    "target_pc": "0x0"})
            finally:
                tools.SAIL_HOOKS.clear()
                os.environ.pop("TRIAGE_TOOL_ROOTS")
        self.assertTrue(result["ok"], result)
        self.assertIn("0x90000000 (board ELF) -> start+0x0", result["mapped_from"])
        self.assertIn("no Sail reference run", missing["error"])


if __name__ == "__main__":
    unittest.main()
