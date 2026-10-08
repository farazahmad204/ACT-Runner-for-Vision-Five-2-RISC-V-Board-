from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci.triage.ci_triage import (
    AIConfig,
    call_codex,
    classify,
    format_ai_report,
    extract_log_evidence,
    read_bounded_text,
    run_triage,
)
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

    def test_trap_signature_diagnostics_explain_the_failure(self):
        evidence = extract_log_evidence(
            'RVCP: Failure: "Mismatch in trap signature! Trap was being handled in M-Mode."\n'
            "RVCP: Trap handler mode: M-mode\n"
            "RVCP: Mismatching field: Vector+Mode+Status word (trap signature word 0)\n"
            "RVCP: Expected value: 0x0000000019000673\n"
            "RVCP: Actual value:   0x0000000011000673\n"
            "RVCP: XEPC:    0x0000000090000138\n"
            "RVCP: XCAUSE:  0x0000000000000003\n"
            "RVCP: HINT: Vector+Mode word mismatch may indicate: trap handled in wrong\n"
            "RVCP:       privilege mode (check medeleg/mideleg).\n"
        )
        self.assertEqual(evidence["xcause"], "0x0000000000000003")
        self.assertEqual(evidence["hint"], (
            "Vector+Mode word mismatch may indicate: trap handled in wrong "
            "privilege mode (check medeleg/mideleg)."
        ))
        category, _owner, explanation = classify({"status": "FAIL"}, evidence)
        self.assertEqual(category, "architectural_value_mismatch")
        self.assertIn("Mismatching field: Vector+Mode+Status word", explanation)
        self.assertIn("xcause=0x0000000000000003", explanation)
        self.assertIn("Hint: Vector+Mode word mismatch", explanation)

    def test_register_check_failure(self):
        evidence = extract_log_evidence(
            'RVCP: Test Info: "test: 13; cg: HSm_mcsr_cg; bin: hedeleg_csrrw1"\n'
            "RVCP: Instruction: 0x60231073\n"
            "RVCP: Approximate address (failure may be slightly after this): 0x900002b8\n"
            "RVCP: Register: x14\n"
            "RVCP: Bad Value:      0x000000000000b1fe\n"
            "RVCP: Expected Value: 0x00000000000cb1fe\n"
        )
        category, _owner, explanation = classify({"status": "FAIL"}, evidence)
        self.assertEqual(category, "register_value_mismatch")
        self.assertTrue(explanation.startswith(
            "x14 = 0x000000000000b1fe, expected 0x00000000000cb1fe after instruction "
            "0x60231073 near 0x900002b8."
        ))

    def test_signature_dump_does_not_hide_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "uart.log"
            log.write_text(
                "RVCP: XCAUSE:  0x0000000000000003\n"
                + "[SIGQ] 0x0000000090028890 : 0xdeadbeefdeadbeef\n" * 20000
                + "[UART_STREAM] DONE name=x status=FAIL\n"
            )
            text = read_bounded_text(log)
        self.assertNotIn("[SIGQ]", text)
        self.assertEqual(extract_log_evidence(text)["xcause"], "0x0000000000000003")

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

        def fake_ai(prompt, config, api_key):
            calls.append((prompt, config.model, api_key))
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
                api_key="secret-test-key",
                max_ai_failures=1,
                ai_analyzer=fake_ai,
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(summary["results"][0]["ai_status"], "SUCCESS")
            self.assertEqual(summary["results"][1]["ai_status"], "SKIPPED_LIMIT")
            serialized = (run_root / "triage/summary.json").read_text()
            self.assertNotIn("secret-test-key", serialized)
            self.assertTrue((run_root / "triage/per_case/fail-one/ai_analysis.md").is_file())

    def test_ai_answer_becomes_labelled_lines(self):
        report = format_ai_report(json.dumps({
            "failure_reason": "hedeleg bit 18 read back 0.",
            "root_cause": "H draft 0.6 vs H 1.0.",
            "confidence": "high",
            "next_step": "Mark as a known deviation.",
        }))
        self.assertEqual(report, (
            "Failure reason: hedeleg bit 18 read back 0.\n"
            "Root cause: H draft 0.6 vs H 1.0.\n"
            "Confidence: high\n"
            "Next step: Mark as a known deviation."
        ))
        self.assertEqual(format_ai_report("plain text"), "plain text")

    def test_codex_runs_read_only_with_the_answer_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "argv.json"
            fake = Path(temporary) / "codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "args = sys.argv[1:]\n"
                f"open({str(log)!r}, 'w').write(json.dumps({{'args': args, 'stdin': sys.stdin.read()}}))\n"
                "out = args[args.index('-o') + 1]\n"
                "open(out, 'w').write(json.dumps({'failure_reason': 'x', 'root_cause': 'y',"
                " 'confidence': 'low', 'next_step': 'z'}))\n"
            )
            fake.chmod(0o755)
            text, meta = call_codex("evidence prompt", AIConfig(provider="codex", codex_bin=str(fake)))
            seen = json.loads(log.read_text())
        self.assertEqual(json.loads(text)["root_cause"], "y")
        self.assertEqual(meta["provider"], "codex")
        args = seen["args"]
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        for flag in ("--ephemeral", "--ignore-user-config", "--output-schema"):
            self.assertIn(flag, args)
        disabled = {args[i + 1] for i, arg in enumerate(args) if arg == "--disable"}
        self.assertTrue({"shell_tool", "unified_exec", "code_mode_host"} <= disabled)
        self.assertNotIn("-m", args)  # blank model: Codex's default
        self.assertEqual(seen["stdin"], "evidence prompt")

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
