from __future__ import annotations

import unittest

from ci.triage.agentic import issue_memory_vault as v


class IssueMemoryTests(unittest.TestCase):
    def test_records_follow_the_ingestion_schema(self):
        issues = v.load_issues()
        self.assertGreater(len(issues), 600)
        for issue in issues[:50]:
            self.assertIsInstance(issue.issue_number, int)
            self.assertIn(issue.status, ("OPEN", "CLOSED"))
            self.assertTrue(issue.title and issue.symptom_pattern and issue.root_cause_or_workaround)
            self.assertIsInstance(issue.tags, list)
        self.assertTrue(all(i.issue_number < 100000 for i in issues))  # only ACT issues, no board notes

    def test_curated_note_wins(self):
        issue = next(i for i in v.load_issues() if i.issue_number == 1172)
        self.assertTrue(issue.curated)
        self.assertEqual(issue.cause, "framework-bug")
        self.assertIn("fixed in: ACT PR #1271", issue.root_cause_or_workaround)

    def test_tags(self):
        tags = v.tags_of("hlv.w raised a load guest-page fault in Sv39x4; Sail sets htinst, H extension", ["H_trap-00"])
        for tag in ("Sv39x4", "Sail", "hlv.w", "htinst", "H", "H_trap-00"):
            self.assertIn(tag, tags)

    def test_query_ranks_the_known_issue_first_and_drops_noise(self):
        top = v.tool_query_historical_issues("c.ebreak mcause access fault", ["Zca"], test="ExceptionsZc-00")[0]
        self.assertEqual((top["issue_number"], top["same_test"]), (1172, True))
        self.assertEqual(v.tool_query_historical_issues("hello world printf", []), [])
        self.assertLessEqual(len(v.tool_query_historical_issues("mcause access fault misaligned", [])), 3)


if __name__ == "__main__":
    unittest.main()
