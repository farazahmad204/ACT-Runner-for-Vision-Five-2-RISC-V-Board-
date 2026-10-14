#!/usr/bin/env python3
"""Tests for the ReAct loop in act_triage_agent.py, with a scripted model."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ci.triage.agentic import react as agent

LOG = """[CASE] START name=Sm-00
RVCP: Failure: "Mismatch in trap signature!"
RVCP: Mismatching field: xcause
RVCP: Expected value: 0x4
RVCP: Actual value: 0x5
RVCP: MEPC: 0x80000a3c
RVCP: MCAUSE: 0x5
RVCP-SUMMARY: TEST FAILED - Test File "x.S"
"""
FINAL = {"analysis_status": "COMPLETED", "failure_classification": "Test_Suite_Error",
         "symptom_isolation": "lw at 0x80000a3c: expected cause 4, got 5 (steps 1-2)",
         "identified_root_cause": "The test assumes misaligned before access fault; the spec allows either.",
         "reproduction_steps_or_fix": "Relax the test's expected cause.", "tier1_root_cause": {"invariant": "", "instruction": "", "pc": "", "sail_step": "", "divergence": "", "subcomponent": ""},
         "tier2_remediation": {"fix_kind": "test", "target": "x.S", "directive": "relax the check"}}


class Scripted:
    """A model that plays a fixed list of turns and records what it was told."""

    def __init__(self, turns):
        self.turns, self.observations, self.forced = list(turns), [], []

    def start(self, system, user):
        self.system, self.user = system, user

    def next(self, force_final):
        self.forced.append(force_final)
        return self.turns.pop(0) if self.turns else agent.Turn("nothing more", [])

    def observe(self, call, text):
        self.observations.append((call.name, text))


def turn(thought, name, arguments):
    return agent.Turn(thought, [agent.ToolCall(name, arguments)])


class ReactLoopTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        os.environ["TRIAGE_TOOL_ROOTS"] = str(self.root)
        (self.root / "events.log").write_text(LOG)
        self.inputs = {"log_path": str(self.root / "events.log"), "isa": "RV64GC"}

    def tearDown(self):
        os.environ.pop("TRIAGE_TOOL_ROOTS", None)
        self.dir.cleanup()

    def test_reason_act_observe_until_a_valid_final(self):
        model = Scripted([
            turn("Start from the log.", "tool_parse_rvcp_log", {"log_path": self.inputs["log_path"]}),
            turn("Decode the cause.", "tool_decode_hardware_state", {"reg_name": "mcause", "hex_val": "0x5"}),
            turn("Remember it.", "record_finding", {"text": "board raised load access fault (cause 5)"}),
            turn("Again?", "tool_parse_rvcp_log", {"log_path": self.inputs["log_path"]}),
            turn("Finish, wrongly.", "submit_analysis", {**FINAL, "failure_classification": "Maybe"}),
            turn("Finish.", "submit_analysis", FINAL),
        ])
        out = self.root / "out"
        final = agent.run_react("Sm-00", self.inputs, out, backend=model)
        self.assertEqual(final, FINAL)
        state = json.loads((out / "agent_state.json").read_text())
        self.assertEqual(state["status"], "completed")
        self.assertEqual([s["tool"] for s in state["steps"]],
                         ["tool_parse_rvcp_log", "tool_decode_hardware_state", "record_finding",
                          "tool_parse_rvcp_log", "submit_analysis", "submit_analysis"])
        self.assertTrue(state["steps"][3]["repeated"])
        self.assertIn("Already called", model.observations[3][1])
        self.assertIn("failure_classification must be one of", model.observations[4][1])
        self.assertIn("load access fault", model.observations[1][1])
        self.assertIn("Relevant rules", model.observations[0][1])  # knowledge arrives with the parsed failure
        self.assertTrue(state["knowledge"][0]["block"].startswith("Relevant rules"))
        self.assertEqual(state["findings"][0]["text"], "board raised load access fault (cause 5)")
        self.assertEqual(json.loads((out / "agent_final.json").read_text()), FINAL)
        self.assertIn("Never decode CSR", agent.SYSTEM_PROMPT)

    def test_budget_forces_the_final_and_falls_back_to_inconclusive(self):
        calls = [turn(f"look {i}", "tool_decode_hardware_state",
                      {"reg_name": "mcause", "hex_val": hex(i + 1)}) for i in range(10)]
        model = Scripted(calls)
        final = agent.run_react("Sm-00", self.inputs, self.root / "out2", backend=model, max_steps=3)
        self.assertTrue(model.forced[3])  # the 4th turn is told to submit
        self.assertEqual(final["failure_classification"], "Inconclusive")
        self.assertEqual(agent.validate_final(final), [])
        state = json.loads((self.root / "out2" / "agent_state.json").read_text())
        self.assertEqual(state["status"], "budget_exhausted")

    def test_repeated_calls_force_the_final(self):
        same = turn("again", "tool_parse_rvcp_log", {"log_path": self.inputs["log_path"]})
        model = Scripted([same] * 6)
        agent.run_react("Sm-00", self.inputs, self.root / "out3", backend=model, max_steps=12)
        self.assertIn(True, model.forced)  # three repeats: told to submit before the budget

    def test_codex_backend_drives_one_tool_per_turn(self):
        fake = self.root / "codex"
        fake.write_text("#!/usr/bin/env python3\nimport json, sys\na = sys.argv[1:]\n"
                        f"open({str(self.root / 'argv.json')!r}, 'w').write(json.dumps({{'a': a, 'in': sys.stdin.read()}}))\n"
                        f"open(a[a.index('-o') + 1], 'w').write(json.dumps({{'thought': 't', 'tool': 'submit_analysis', "
                        f"'arguments_json': json.dumps({FINAL!r})}}))\n")
        fake.chmod(0o755)
        state = agent.CaseState(self.root / "s.json", "Sm-00")
        backend = agent.CodexBackend("", state, str(fake))
        backend.start("SYSTEM", "USER")
        result = backend.next(force_final=True)
        seen = json.loads((self.root / "argv.json").read_text())
        self.assertEqual(result.calls[0].name, "submit_analysis")
        self.assertEqual(result.calls[0].arguments, FINAL)
        self.assertEqual(seen["a"][seen["a"].index("--sandbox") + 1], "read-only")
        self.assertIn("no steps left", seen["in"])
        self.assertIn("tool_query_sail_trace", seen["in"])


if __name__ == "__main__":
    unittest.main()
