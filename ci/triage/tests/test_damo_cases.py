from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "damo_cases", Path(__file__).resolve().parents[2] / "jenkins" / "damo_cases.py"
)
damo_cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(damo_cases)

LOG = """[TEST] VCSR-01: V=1 read sstatus accesses vsstatus
[PASS] VCSR-01: V=1 read sstatus accesses vsstatus

[TEST] HSTAT-09: VTVM=1 VS SINVAL.VMA triggers cause=22
  ASSERT FAIL: cause=22 (virtual-instruction): got 0x2, expected 0x16 (tests/test_hstatus.c:242)
[FAIL] HSTAT-09: VTVM=1 VS SINVAL.VMA triggers cause=22

[TEST] HENV-02: FIOM=1 modifies FENCE behavior
[SKIP] HENV-02: FIOM=1 modifies FENCE behavior: FIOM not implemented in menvcfg

[TEST] HENV-01: henvcfg basic read/write
  [ERROR] UNEXPECTED TRAP in M-mode !!!
  [ERROR] mcause  = 0x2
"""


class DamoCasesTests(unittest.TestCase):
    def test_names_with_colons_outcomes_and_unfinished_case(self):
        cases = damo_cases.parse_suite(LOG)
        self.assertEqual(
            [(c["case"], c["status"]) for c in cases],
            [
                ("VCSR-01: V=1 read sstatus accesses vsstatus", "PASS"),
                ("HSTAT-09: VTVM=1 VS SINVAL.VMA triggers cause=22", "FAIL"),
                ("HENV-02: FIOM=1 modifies FENCE behavior", "SKIPPED"),
                ("HENV-01: henvcfg basic read/write", None),
            ],
        )
        self.assertIn("got 0x2, expected 0x16", cases[1]["reason"])
        self.assertEqual(cases[2]["reason"], "FIOM not implemented in menvcfg")

    def test_run_root_gets_per_case_rows_and_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "csr.log").write_text(LOG)
            (root / "summary.json").write_text(json.dumps({"results": [
                {"name": "Hypervisor_CSR", "status": "FAIL", "uart_log": str(root / "csr.log")},
                {"name": "Sha", "status": "TIMEOUT", "uart_log": "", "error": "no READY"},
            ]}))
            import sys
            argv = sys.argv
            sys.argv = ["damo_cases.py", "--run-root", str(root)]
            try:
                damo_cases.main()
            finally:
                sys.argv = argv
            cases = {c["test_name"]: c for c in json.loads((root / "cases.json").read_text())}
        self.assertEqual(cases["Hypervisor_CSR-VCSR_01_V_1_read_sstatus_accesses_vsstatus"]["status"], "PASS")
        self.assertEqual(cases["Hypervisor_CSR-HENV_01_henvcfg_basic_read_write"]["status"], "FAIL")
        self.assertIn("UNEXPECTED TRAP", cases["Hypervisor_CSR-HENV_01_henvcfg_basic_read_write"]["root_cause"])
        self.assertEqual(cases["Sha-suite"]["root_cause"], "no READY")
        self.assertEqual(len(cases), 5)


if __name__ == "__main__":
    unittest.main()
