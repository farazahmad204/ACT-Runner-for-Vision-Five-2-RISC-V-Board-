from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci.triage.agentic import report_schema, triage_suite

LOG = """[CASE] START name=Sm-00
RVCP: Failure: "Mismatch in trap signature!"
RVCP: Mismatching field: Vector+Mode+Status word (trap signature word 0)
RVCP: Expected value: 0x0000000019000673
RVCP: Actual value:   0x0000000011000673
RVCP: XEPC:    0x0000000090000138
RVCP: XCAUSE:  0x0000000000000003
"""
VF2 = "ci/triage/board_knowledge/vf2_jh7110.yaml"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = triage_suite.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "uart.log").write_text(LOG)
        (self.root / "test.elf").write_bytes(b"\x7fELF" + b"\x02" + b"\x00" * 59)
        self.base = ["--log", str(self.root / "uart.log"), "--elf", str(self.root / "test.elf"),
                     "--out-dir", str(self.root / "out")]

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_or_bad_inputs_fail_with_diagnostics(self):
        cases = [
            (["--log", str(self.root / "nope.log"), "--elf", str(self.root / "test.elf"), "--out-dir", "o"],
             "--log (UART/RVCP failure log): ", "does not exist"),
            (["--log", str(self.root / "uart.log"), "--elf", str(self.root / "uart.log"), "--out-dir", "o"],
             "--elf", "is not an ELF file"),
            (self.base, "--board-config", "give at least one board description"),
            (self.base + ["--board-config", str(self.root / "none.yaml")], "--board-config #1", "does not exist"),
            (self.base + ["--board-config", VF2, "--sig-elf", str(self.root / "test.elf")],
             "--sig-elf and --sail-config go together", ""),
        ]
        for argv, first, second in cases:
            code, _out, err = run(*argv)
            self.assertEqual(code, 2, err)
            self.assertIn(first, err)
            self.assertIn(second, err)

    def test_dry_run_assembles_the_context(self):
        code, out, err = run(*self.base, "--board-config", VF2, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("TARGET HARDWARE PROFILE"))
        for part in ("core SiFive U74", "[TEST INTENT]", "[SPECIFICATION INVARIANTS]", "RVCP: XEPC:",
                     '"board": "vf2_jh7110"'):
            self.assertIn(part, out)

    def test_report_routing_and_markdown(self):
        report = {"triage_status": "SUCCESS", "target_board": "vf2_jh7110", "failure_classification": "Test_Suite_Error",
                  "tier_1_root_cause": {"failing_pc": "0x90000138", "failing_instruction": "ebreak",
                                        "architectural_symptom": "MPV not set", "divergence_cycle": 612},
                  "tier_2_board_specific_remediation": {"remediation_type": "CONFIG_REMAP",
                                                        "target_repository_or_file": "sail.json",
                                                        "actionable_fix_directive": "1. Do this."}}

        def fake_run(case, inputs, out_dir, *args, **kw):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "agent_state.json").write_text(json.dumps({"case": case, "steps": [{}] * 5,
                                                                  "status": "completed"}))
            (out_dir / "triage_report.json").write_text(json.dumps(report))
            return report

        with mock.patch.object(triage_suite.react, "run_react", fake_run), \
                mock.patch.object(triage_suite.shutil, "which", lambda name: "/bin/codex"):
            code, out, err = run(*self.base, "--board-config", VF2)
        self.assertEqual(code, 0, err)
        self.assertIn("**SUCCESS**: `Test_Suite_Error`", out)
        self.assertIn("CONFIG_REMAP in `sail.json`", out)
        self.assertEqual(json.loads((self.root / "out" / "triage_report.json").read_text()), report)
        with mock.patch.object(triage_suite.react, "run_react",
                               lambda *a, **k: report_schema.unresolved_report("vf2_jh7110", "no answer")), \
                mock.patch.object(triage_suite.shutil, "which", lambda name: "/bin/codex"):
            (self.root / "out").mkdir(exist_ok=True)
            (self.root / "out" / "agent_state.json").write_text("{}")
            code, _out, _err = run(*self.base, "--board-config", VF2)
        self.assertEqual(code, 1)  # FAILED_TO_ISOLATE

    def test_agent_failure_is_reported(self):
        def broken(*a, **k):
            raise RuntimeError("codex failed (rc=1)")
        with mock.patch.object(triage_suite.react, "run_react", broken), \
                mock.patch.object(triage_suite.shutil, "which", lambda name: "/bin/codex"):
            code, _out, err = run(*self.base, "--board-config", VF2)
        self.assertEqual(code, 3)
        self.assertIn("the agent could not run: RuntimeError: codex failed", err)


if __name__ == "__main__":
    unittest.main()
