from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci.triage.ci_triage import AIConfig, run_triage
from ci.triage.investigate import Toolbox, investigate

ANSWER = {"classification": "Test issue", "failure_reason": "x", "root_cause": "y",
          "confidence": "medium", "next_step": "z", "spec_version": "", "spec_quote": ""}


def reply(action: str, argument: str = "", hypothesis: str = "h") -> str:
    return json.dumps({**ANSWER, "action": action, "argument": argument, "hypothesis": hypothesis})


def run_dir(root: Path) -> Path:
    folder = root / "per_case" / "Sm-00"
    folder.mkdir(parents=True)
    (folder / "test.S").write_text("start:\n  nop\ncheck_mstatus:\n  csrr a0, mstatus\n  j done\n")
    return root


class InvestigateTests(unittest.TestCase):
    def test_tool_result_goes_back_until_the_ai_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            toolbox = Toolbox(run_dir(Path(temporary)), "Sm-00", None, {})
            prompts = []
            replies = iter([reply("test_source", "check_mstatus", "reads mstatus"), reply("answer")])

            def ask(prompt):
                prompts.append(prompt)
                return next(replies), {"id": "r"}

            text, _raw, steps = investigate("evidence", ask, toolbox, 3)
        self.assertEqual(json.loads(text)["action"], "answer")
        self.assertEqual([s["tool"] for s in steps], ["test_source"])
        self.assertIn(">3: check_mstatus:", prompts[1])
        self.assertIn("hypothesis: reads mstatus", prompts[1])
        self.assertIn("(2 left)", prompts[1])

    def test_budget_forces_an_answer_and_repeats_are_not_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            toolbox = Toolbox(run_dir(Path(temporary)), "Sm-00", None, {})
            prompts = []

            def ask(prompt):
                prompts.append(prompt)
                return reply("test_source", "nop"), {}

            _text, _raw, steps = investigate("evidence", ask, toolbox, 2)
        self.assertEqual(len(prompts), 3)
        self.assertEqual(len(steps), 2)
        self.assertIn("already ran this", steps[1]["result"])
        self.assertIn("no tool steps are left", prompts[-1])

    def test_unknown_tools_and_plain_text_end_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            toolbox = Toolbox(run_dir(Path(temporary)), "Sm-00", None, {})
            self.assertIn("Unknown tool", toolbox.run("shell", "ls /"))
            self.assertIn("not in test.S", toolbox.run("test_source", "missing_label"))
            text, _raw, steps = investigate("evidence", lambda p: ("plain words", {}), toolbox, 3)
        self.assertEqual((text, steps), ("plain words", []))

    def test_steps_are_kept_in_the_report(self):
        replies = iter([reply("test_source", "check_mstatus", "reads mstatus"), reply("answer")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = run_dir(root / "run")
            (root / "state").mkdir()
            (run_root / "cases.json").write_text(json.dumps([{"test_name": "Sm-00", "status": "FAIL"}]))
            summary = run_triage(run_root, root / "state", "bpif3_k1", ai_enabled=True,
                                 ai_config=AIConfig(), ai_analyzer=lambda p, c, k: (next(replies), {}),
                                 memory_path=root / "memory.sqlite")
            report = summary["results"][0]["report"]
            cell = (run_root / "triage/per_case/Sm-00/ai_analysis.md").read_text()
        self.assertEqual(report["investigation"][0]["tool"], "test_source")
        self.assertIn("Step 1: test_source check_mstatus (testing: reads mstatus)", cell)
        self.assertIn("after 1 tool step(s)", summary["results"][0]["analysis_source"])


if __name__ == "__main__":
    unittest.main()
