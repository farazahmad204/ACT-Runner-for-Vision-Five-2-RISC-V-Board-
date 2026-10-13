from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci.triage import skills
from ci.triage.agent import check_answer
from ci.triage.ci_triage import AIConfig, run_triage
from ci.triage.investigate import Toolbox, investigate_skills

ANSWER = {"classification": "Test issue", "failure_reason": "x", "root_cause": "y", "confidence": "medium",
          "next_step": "z", "spec_version": "", "spec_quote": ""}


def reply(action: str, argument: str = "", ids: list[str] = (), **over) -> str:
    return json.dumps({**ANSWER, "action": action, "argument": argument, "hypothesis": "h",
                       "evidence_ids": list(ids), **over})


def context(category="trap_cause_mismatch", case="ExceptionsSm-00", **ex) -> skills.SkillContext:
    evidence = {"case": case, "deterministic_category": category, "deterministic_explanation": "Expected 4, got 5",
                "extracted": {"expected_cause": "0x4", "actual_cause": "0x5", "trap_mtval": "0x1", **ex}}
    case_file = {"instruction": {"window": ["0x80000010: lw a0,1(zero)   <=="], "source_line": 42},
                 "intent": {"code": "> lw a0, 1(x0)", "header": ["REQUIRED_EXTENSIONS: ['I']"]},
                 "reference": {"executed": True, "label": "cp+0x4", "mode": "M", "at": "lw a0,1(zero)",
                               "result": ["trap: load address misaligned"], "window": []}}
    grounding = {"spec_versions": "This board implements: priv: 1.11 [priv-1.11]\n"
                                  "[priv-1.11] Exception priority:\nmisaligned before access fault",
                 "known_issues": ["ACT-9 (closed) misaligned priority"], "issues": []}
    return skills.SkillContext(evidence, case_file, grounding, {"facts": ["Misaligned loads trap (measured)."]})


class PlanAndDecodeTests(unittest.TestCase):
    def test_every_skill_file_loads(self):
        names = set(skills.catalog())
        self.assertEqual(names, {"log", "instruction", "csr_privilege", "memory_mmu", "reference", "spec",
                                 "act_framework", "history"})
        for meta in skills.catalog().values():
            self.assertTrue(meta["summary"] and meta["body"])

    def test_plan_follows_the_failure(self):
        self.assertEqual(skills.plan(context().evidence)[0], "memory_mmu")  # load access fault
        self.assertEqual(skills.plan({"deterministic_category": "architectural_value_mismatch",
                                      "case": "Zicsr-00", "extracted": {}})[0], "csr_privilege")
        self.assertEqual(skills.plan({"deterministic_category": "trap_cause_mismatch",
                                      "case": "InterruptsS-00", "extracted": {"actual_cause": "0x2"}})[0],
                         "act_framework")

    def test_mmu_decoders(self):
        self.assertIn("MODE=Sv39, ASID=0x5, root PPN=0x80123",
                      skills.decode_atp("satp", (8 << 60) | (5 << 44) | 0x80123))
        pte = skills.decode_pte("pte", (0x80200 << 10) | 0b01000111)  # V R W, A=1, D=0
        self.assertIn("flags VRW---A-", pte)
        self.assertIn("D=0: store faults", pte)
        self.assertIn("W without R is reserved", skills.decode_pte("pte", 0b101))
        items = skills.mmu_items({"actual_cause": "0xd", "evidence_lines": ["satp=0x8000000000080000"]})
        self.assertTrue(any("Sv39" in i for i in items))
        self.assertTrue(any("load page fault" in i for i in items))


class SkillLoopTests(unittest.TestCase):
    def toolbox(self, root: Path) -> Toolbox:
        return Toolbox(root, "ExceptionsSm-00", None, {})

    def test_card_and_first_planned_skill_are_loaded_then_the_model_loads_more(self):
        prompts = []
        replies = iter([reply("skill", "spec"), reply("answer", ids=["log.3", "spec.2"])])

        def ask(prompt):
            prompts.append(prompt)
            return next(replies), {}

        with tempfile.TemporaryDirectory() as d:
            text, _raw, steps, found = investigate_skills("HEADER", context(), ask, self.toolbox(Path(d)), 3,
                                                          "RULES")
        self.assertEqual(steps[0]["argument"], "log, instruction, memory_mmu")
        self.assertIn("## Skill: Log debugging (log)", prompts[0])
        self.assertNotIn("## Skill: Specification research", prompts[0])  # on demand only
        self.assertIn("Suggested next, in order: reference", prompts[0])
        self.assertIn("[spec.2] [priv-1.11] Exception priority", prompts[1])
        self.assertEqual(json.loads(text)["evidence_ids"], ["log.3", "spec.2"])
        self.assertIn("Trap cause: expected 0x4, observed 0x5", found.values())

    def test_an_answer_without_evidence_is_sent_back_once(self):
        prompts = []
        replies = iter([reply("answer"), reply("answer")])

        def ask(prompt):
            prompts.append(prompt)
            return next(replies), {}

        with tempfile.TemporaryDirectory() as d:
            _text, _raw, steps, _found = investigate_skills("H", context(), ask, self.toolbox(Path(d)), 3, "R")
        self.assertEqual(len(prompts), 2)
        self.assertEqual(steps[-1]["tool"], "challenge")
        self.assertIn("not accepted because it cites no evidence id", prompts[1])

    def test_check_answer_holds_citations_to_the_investigation(self):
        rep = {"extracted": {"expected_cause": "0x4", "actual_cause": "0x5", "instruction": "0x00102503"}}
        answer = check_answer({**ANSWER, "confidence": "high", "evidence_ids": ["log.1", "spec.9"]}, rep,
                              evidence_ids={"log.1", "spec.1"})
        self.assertEqual(answer["evidence_ids"], ["log.1"])
        self.assertEqual(answer["confidence"], "medium")
        self.assertIn("did not produce: spec.9", answer["checked"])
        bug = check_answer({**ANSWER, "classification": "DUT noncompliance", "confidence": "high",
                            "evidence_ids": ["log.1"]}, rep, evidence_ids={"log.1"})
        self.assertEqual(bug["classification"], "Inconclusive")
        self.assertIn("cited spec evidence", bug["checked"])

    def test_skill_mode_end_to_end_keeps_the_cited_evidence(self):
        replies = iter([reply("answer", ids=["log.1"])])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "run").mkdir()
            (root / "state").mkdir()
            (root / "run" / "cases.json").write_text(json.dumps([{"test_name": "Sm-00", "status": "FAIL",
                                                                  "root_cause": "boom"}]))
            summary = run_triage(root / "run", root / "state", "bpif3_k1", ai_enabled=True,
                                 ai_config=AIConfig(mode="skills"),
                                 ai_analyzer=lambda p, c, k: (next(replies), {}),
                                 memory_path=root / "memory.sqlite")
        report = summary["results"][0]["report"]
        self.assertEqual(report["evidence"][0]["id"], "log.1")
        self.assertEqual(report["investigation"][0]["tool"], "skill")


if __name__ == "__main__":
    unittest.main()
