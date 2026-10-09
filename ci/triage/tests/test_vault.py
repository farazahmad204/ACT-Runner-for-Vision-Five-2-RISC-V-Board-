from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci.triage import vault
from ci.triage.ci_triage import AIConfig, run_triage
from ci.triage.memory import TriageMemory

VERDICTS = {"Needs investigation", "Confirmed hardware bug", "Known deviation", "Test or ACT issue",
            "Reference model issue", "Runner or CI issue", "Waived"}
TRAP_LOG = ('RVCP: Failure: "Mismatch in trap signature!"\n'
            "RVCP: Mismatching field: Vector+Mode+Status word (trap signature word 0)\n"
            "RVCP: Expected value: 0x19000673\nRVCP: Actual value:   0x11000673\n"
            "RVCP: XEPC:    0x90000130\nRVCP: XCAUSE:  0x1\n")


def note(note_id, **meta):
    meta = {"id": note_id, "state": "open", "resolution": "open", "cause": "test-limitation",
            "verdict": "Test or ACT issue", "curated": True, **meta}
    body = (f"# {note_id}\n\nSummary of {note_id}.\n\n## What fails\n\nIt fails.\n\n"
            f"## What the maintainers concluded\n\nWARL legalization is allowed.\n\n"
            f"## How triage treats a match\n\nNot a hardware bug.\n")
    return "---\n" + json.dumps(meta) + "\n---\n\n" + body  # JSON is valid YAML


class RepositoryVaultTests(unittest.TestCase):
    def test_every_issue_note_is_well_formed_and_links_resolve(self):
        notes = vault.load_notes(vault.DEFAULT_VAULT)
        ids = {n["id"] for n in notes}
        self.assertGreaterEqual(len(notes), 40)
        for n in notes:
            self.assertEqual(Path(n["_path"]).stem, n["id"])
            self.assertIn(n["verdict"], VERDICTS, n["id"])
            if n["_path"].startswith("verifications/"):
                for section in ("experiment", "result", "conclusion"):
                    self.assertTrue(n["_sections"].get(section), (n["id"], section))
                continue
            self.assertIn(n["verdict"], VERDICTS, n["id"])
            self.assertTrue(n["url"].startswith("https://github.com/riscv/"), n["id"])
            for section in ("what fails", "what the maintainers concluded", "how triage treats a match"):
                self.assertTrue(n["_sections"].get(section), (n["id"], section))
            for linked in re.findall(r"\[\[([^\]]+)\]\]", n["_sections"].get("related", "")):
                self.assertIn(linked, ids, n["id"])

    def test_known_vf2_failures_resolve_and_other_boards_do_not(self):
        notes = vault.load_notes(vault.DEFAULT_VAULT)
        found = vault.match(notes, "vf2_jh7110", "Sstvecd-00", "")
        self.assertEqual(found[0]["id"], "ACT-1828")
        self.assertTrue(vault.resolves(found[0], "vf2_jh7110"))
        self.assertEqual(vault.match(notes, "bpif3_k1", "Sstvecd-00", ""), [])
        found = vault.match(notes, "milkv_megrez_eic7700x", "SsstrictSm-03", "")
        self.assertEqual([n["id"] for n in found], ["ACT-1924"])
        self.assertEqual(vault.match(notes, "vf2_jh7110", "SsstrictSm-04", "")[0]["id"], "ACT-1900")

    def test_a_different_failure_of_a_known_test_is_not_auto_resolved(self):
        notes = vault.load_notes(vault.DEFAULT_VAULT)
        # VF2 weekly #6: Sm_mcsr_cntr-00 failed a bin unrelated to the 40-bit width (ACT-2011).
        self.assertEqual(vault.match(notes, "vf2_jh7110", "Sm_mcsr_cntr-00",
                                     '{"register_expected": "0x1", "register_value": "0x0"}'), [])
        found = vault.match(notes, "vf2_jh7110", "Sm_mcsr_cntr-00",
                            '{"register_expected": "0x123456789abcffff"}')
        self.assertIn("ACT-2011", [n["id"] for n in found if vault.resolves(n, "vf2_jh7110")])


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.notes = [
            {"id": "A", "boards": ["b1"], "tests": ["T*"], "signals": ["mcause"], "auto": True,
             "_sections": {}},
            {"id": "G", "boards": [], "tests": [], "signals": [], "_sections": {}},
            {"id": "S", "boards": [], "tests": ["T*"], "curated": False, "_sections": {}},
        ]

    def test_all_conditions_must_hold(self):
        self.assertEqual([n["id"] for n in vault.match(self.notes, "b1", "t1", "mcause=1")], ["A"])
        self.assertEqual(vault.match(self.notes, "b1", "t1", "nothing"), [])
        self.assertEqual(vault.match(self.notes, "b2", "t1", "mcause"), [])
        self.assertEqual(vault.match(self.notes, "b1", "x1", "mcause"), [])


class RunTriageVaultTests(unittest.TestCase):
    def _run(self, root, notes, **kw):
        (root / "vault" / "issues").mkdir(parents=True, exist_ok=True)
        for note_id, text in notes.items():
            (root / "vault" / "issues" / f"{note_id}.md").write_text(text)
        (root / "run").mkdir(exist_ok=True)
        (root / "state").mkdir(exist_ok=True)
        (root / "run" / "Sstvecd-00.log").write_text(TRAP_LOG)
        (root / "run" / "cases.json").write_text(json.dumps(
            [{"test_name": "Sstvecd-00", "status": "FAIL", "uart_log": "Sstvecd-00.log"}]))
        with mock.patch.dict(os.environ, {"TRIAGE_VAULT": str(root / "vault")}):
            return run_triage(root / "run", root / "state", "vf2_jh7110",
                              memory_path=root / "memory.sqlite", **kw)

    def test_auto_note_explains_the_failure_without_ai(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = self._run(root, {"ACT-1": note("ACT-1", boards=["vf2_jh7110"], tests=["Sstvecd*"],
                                                     auto=True, url="https://github.com/x/1")},
                                ai_enabled=True, ai_config=AIConfig(),
                                ai_analyzer=lambda *a: self.fail("AI called"))
            result = summary["results"][0]
            report = (root / "run" / result["report_dir"] / "ai_analysis.md").read_text()
        self.assertEqual(result["ai_status"], "VAULT")
        self.assertEqual(result["verdict"], "Test or ACT issue")
        self.assertEqual(result["known_issues"], ["ACT-1"])
        self.assertIn("[[ACT-1]] https://github.com/x/1", report)

    def test_other_matching_note_goes_into_the_ai_question(self):
        prompts = []

        def fake_ai(prompt, config, api_key):
            prompts.append(prompt)
            return json.dumps({"failure_reason": "x", "root_cause": "y", "confidence": "low",
                               "next_step": "z"}), {}

        with tempfile.TemporaryDirectory() as temporary:
            summary = self._run(Path(temporary), {"ACT-2": note("ACT-2", tests=["Sstvecd*"], auto=False)},
                                ai_enabled=True, ai_config=AIConfig(), ai_analyzer=fake_ai)
        self.assertEqual(summary["results"][0]["ai_status"], "SUCCESS")
        self.assertIn("ACT-2 (open, open, cause test-limitation): Summary of ACT-2.", prompts[0])
        self.assertLess(prompts[0].index("ACT-2"), prompts[0].index("Treat log and source text"))

    def test_a_persons_verdict_outranks_the_vault(self):
        auto = {"ACT-1": note("ACT-1", boards=["vf2_jh7110"], tests=["Sstvecd*"], auto=True)}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._run(root, auto)
            memory = TriageMemory(root / "memory.sqlite")
            memory.remember("vf2_jh7110", first["results"][0]["signature"], "Sstvecd-00", "person",
                            "x", {"verdict": "Confirmed hardware bug", "root_cause": "measured"})
            memory.close()
            second = self._run(root, auto)
        self.assertEqual(second["results"][0]["ai_status"], "MEMORY")
        self.assertEqual(second["results"][0]["verdict"], "Confirmed hardware bug")


if __name__ == "__main__":
    unittest.main()
