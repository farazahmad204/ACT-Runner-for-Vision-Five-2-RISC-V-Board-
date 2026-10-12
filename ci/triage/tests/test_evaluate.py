from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci.triage import evaluate

NOTE = {"id": "ACT-9", "boards": ["b"], "tests": ["Known-*"], "auto": True, "verdict": "Test or ACT issue",
        "cause": "test-limitation", "state": "open", "resolution": "open",
        "_sections": {"what fails": "x", "what the maintainers concluded": "y", "how triage treats a match": "z"}}


def entry(test, verdict, klass, confidence="high", status="SUCCESS", quote="verified"):
    return {"test": test, "ai_status": status, "evidence": {"extracted": {}},
            "report": {"suggested_verdict": verdict, "classification": klass, "confidence": confidence,
                       "root_cause": "because", "spec": {"check": quote} if quote else {}}}


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.reports = [
            entry("Person-00", "Test or ACT issue", "Test issue"),
            entry("Other-00", "Known deviation", "Unsupported feature", "medium"),
            entry("Known-00", "Confirmed hardware bug", "DUT noncompliance", "high", quote="not_found"),
            entry("Unkeyed-00", "Known deviation", "Implementation-defined behavior", quote=""),
        ]
        self.feedback = [
            {"test": "Person-00", "run": "job#5", "verdict": "Test or ACT issue", "updated_by": "ana"},
            {"test": "Person-00", "run": "job#3", "verdict": "Waived", "updated_by": "bo"},
            {"test": "Other-00", "run": "job#3", "verdict": "Reference model issue", "updated_by": "bo"},
        ]

    def test_key_prefers_this_runs_person_then_other_runs_then_vault(self):
        key = evaluate.build_key(self.reports, "b", "job#5", self.feedback, [NOTE])
        self.assertEqual(key["Person-00"]["source"], "person")
        self.assertEqual(key["Other-00"], {"verdict": "Reference model issue", "source": "person-other",
                                           "by": "bo (job#3)"})
        self.assertEqual(key["Known-00"]["source"], "vault")
        self.assertEqual(key["Known-00"]["classification"], "Test issue")
        self.assertNotIn("Unkeyed-00", key)

    def test_scores_count_false_hardware_bugs_and_split_by_confidence(self):
        key = evaluate.build_key(self.reports, "b", "job#5", self.feedback, [NOTE])
        result = evaluate.score(self.reports, key)
        self.assertEqual(result["keyed"], 3)
        self.assertEqual(result["verdict_accuracy"], 33.3)
        self.assertEqual(result["accuracy"], 33.3)
        self.assertEqual(result["false_hardware_bugs"], 1)
        self.assertEqual(result["class_accuracy"], 0.0)
        self.assertEqual(result["by_confidence"]["high"], {"keyed": 2, "right": 1, "accuracy": 50.0})
        self.assertEqual(result["spec_quotes"], {"verified": 2, "not found": 1, "none": 1})
        self.assertEqual({row["test"] for row in result["disagreements"]}, {"Other-00", "Known-00"})
        text = evaluate.markdown(result, "job#5")
        self.assertIn("Hardware-bug false alarms: **1**", text)
        self.assertIn("| `Known-00` | Test or ACT issue / Test issue | Confirmed hardware bug / DUT noncompliance |", text)

    def test_reports_are_read_from_a_triage_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            (run / "triage" / "per_case" / "A-00").mkdir(parents=True)
            (run / "triage" / "per_case" / "A-00" / "report.json").write_text(json.dumps({"classification": "Test issue"}))
            (run / "triage" / "summary.json").write_text(json.dumps({"results": [
                {"case": "A-00", "report_dir": "triage/per_case/A-00", "ai_status": "SUCCESS"}]}))
            reports = evaluate.load_reports(run)
        self.assertEqual(reports[0]["report"]["classification"], "Test issue")


if __name__ == "__main__":
    unittest.main()
