from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci.triage.ci_triage import AIConfig, extract_log_evidence, run_triage
from ci.triage.publish_triage import build_payload


class CITriageTests(unittest.TestCase):
    def test_extracts_rvcp_trap_fields(self):
        evidence = extract_log_evidence(
            'RVCP: Test Info: "trap cause"\n'
            "RVCP: MCAUSE: 0x0000000000000006\n"
            "RVCP: MEPC: 0x0000000080000100\n"
            "RVCP: MTVAL: 0x0000000140001002\n"
        )
        self.assertEqual(evidence["mcause"], "0x0000000000000006")
        self.assertEqual(evidence["mepc"], "0x0000000080000100")
        self.assertEqual(evidence["mtval"], "0x0000000140001002")

    def test_deterministic_reports_are_created_without_ai(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "run"
            state_root = root / "state"
            log_dir = run_root / "per_case" / "ExceptionsSm-00"
            log_dir.mkdir(parents=True)
            state_root.mkdir()
            (log_dir / "uart.log").write_text(
                "RVCP: Expected cause: load access fault\n"
                "RVCP: Actual cause: load address misaligned\n"
            )
            (run_root / "cases.json").write_text(
                json.dumps(
                    [
                        {
                            "test_name": "ExceptionsSm-00",
                            "status": "FAIL",
                            "uart_log": "per_case/ExceptionsSm-00/uart.log",
                        },
                        {"test_name": "I-add-00", "status": "PASS"},
                    ]
                )
            )
            (state_root / "sail_reference_status.tsv").write_text(
                "test_name\tsail_status\thardware_elf\nExceptionsSm-00\tPASS\tyes\n"
            )

            summary = run_triage(run_root, state_root, "vf2_jh7110")

            self.assertEqual(summary["failed_cases"], 1)
            self.assertEqual(summary["results"][0]["category"], "trap_cause_mismatch")
            self.assertEqual(summary["results"][0]["ai_status"], "DISABLED")
            self.assertTrue(
                (run_root / "triage/per_case/ExceptionsSm-00/deterministic.md").is_file()
            )

    def test_ai_is_optional_bounded_and_advisory(self):
        calls = []

        def fake_ai(prompt, config):
            calls.append((prompt, config.model, config.provider))
            return "## Finding\nAdvisory only.", {"id": "resp_test", "model": config.model}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "run"
            state_root = root / "state"
            run_root.mkdir()
            state_root.mkdir()
            (run_root / "cases.json").write_text(
                json.dumps(
                    [
                        {"test_name": "fail-one", "status": "FAIL"},
                        {"test_name": "fail-two", "status": "FAIL"},
                    ]
                )
            )

            summary = run_triage(
                run_root,
                state_root,
                "bpif3_k1",
                ai_enabled=True,
                ai_config=AIConfig(model="test-model"),
                max_ai_failures=1,
                ai_analyzer=fake_ai,
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(summary["results"][0]["ai_status"], "SUCCESS")
            self.assertEqual(summary["results"][1]["ai_status"], "SKIPPED_LIMIT")
            serialized = (run_root / "triage/summary.json").read_text()
            self.assertEqual(summary["ai_provider"], "ollama")
            self.assertTrue((run_root / "triage/per_case/fail-one/ai_analysis.md").is_file())

    def test_local_runtime_failure_stops_repeated_connection_attempts(self):
        calls = []

        def unavailable_ai(prompt, config):
            calls.append((prompt, config.model))
            raise RuntimeError("local Ollama is unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "run"
            state_root = root / "state"
            run_root.mkdir()
            state_root.mkdir()
            (run_root / "cases.json").write_text(
                json.dumps(
                    [
                        {"test_name": "fail-one", "status": "FAIL"},
                        {"test_name": "fail-two", "status": "FAIL"},
                    ]
                )
            )

            summary = run_triage(
                run_root,
                state_root,
                "vf2_jh7110",
                ai_enabled=True,
                ai_config=AIConfig(model="test-model"),
                ai_analyzer=unavailable_ai,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["results"][0]["ai_status"], "ERROR")
        self.assertEqual(summary["results"][1]["ai_status"], "SKIPPED_UNAVAILABLE")

    def test_portal_payload_targets_existing_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            case_root = run_root / "triage" / "per_case" / "case-a"
            case_root.mkdir(parents=True)
            (run_root / "triage" / "summary.json").write_text(
                json.dumps(
                    {
                        "ai_model": "test-model",
                        "results": [
                            {
                                "case": "case-a",
                                "report_dir": "triage/per_case/case-a",
                                "ai_status": "DISABLED",
                            }
                        ],
                    }
                )
            )
            (case_root / "evidence.json").write_text(
                json.dumps(
                    {
                        "deterministic_category": "insufficient_evidence",
                        "deterministic_owner": "Unassigned",
                        "deterministic_explanation": "Need a trap tuple.",
                        "extracted": {},
                    }
                )
            )
            payload = build_payload(run_root, "vf2-uart-weekly", 7)

        self.assertEqual(payload["job_name"], "vf2-uart-weekly")
        self.assertEqual(payload["build_number"], 7)
        self.assertEqual(payload["results"][0]["name"], "case-a")


if __name__ == "__main__":
    unittest.main()
