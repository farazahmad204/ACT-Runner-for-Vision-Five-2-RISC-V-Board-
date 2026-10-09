from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci.triage import context


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        damo = root / "external" / "damo-rv-priv-ats"
        (damo / "Hypervisor_CSR" / "tests").mkdir(parents=True)
        (damo / "Hypervisor_CSR" / "tests" / "test_hstatus.c").write_text(
            "\n".join(f"line {n}" for n in range(1, 241))
            + "\n    /* norm:H_vtvm_sinval */\n    TEST_ASSERT_EQ(\"cause=22\", cause, 22);\n"
        )
        (damo / "NORM").mkdir()
        (damo / "NORM" / "hypervisor_norm.md").write_text(
            "| Norm ID | 原文 | 中文说明 |\n|---|---|---|\n"
            "| `norm:H_vtvm_sinval` | When VTVM=1, SINVAL.VMA in VS-mode raises a virtual-instruction exception. | 中文 |\n"
        )
        act = root / "external" / "riscv-arch-test" / "config" / "cores" / "b"
        act.mkdir(parents=True)
        (act / "b.yaml").write_text(
            "implemented_extensions:\n  - { name: I, version: \"= 2.1\" }\n"
            "  - { name: S, version: \"= 1.12.0\" }  # DEVIATION: hardware is 1.11\n"
        )
        self.env = mock.patch.dict(os.environ, {"TRIAGE_SOURCE_ROOTS": str(root)})
        self.env.start()
        context._norm_table.cache_clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_assert_location_gives_source_and_its_requirement(self):
        label, snippet = context.test_source(
            None, None, "cause=22: got 0x2, expected 0x16 (tests/test_hstatus.c:242)", "Hypervisor_CSR")
        self.assertEqual(label, "test_hstatus.c:242")
        self.assertIn(">     TEST_ASSERT_EQ", snippet)
        self.assertEqual(context.norms(snippet),
                         ["norm:H_vtvm_sinval: When VTVM=1, SINVAL.VMA in VS-mode raises a "
                          "virtual-instruction exception."])

    def test_spec_passage_for_htinst(self):
        terms = context.query_terms({"extracted": {"mismatching_field": "MTINST/HTINST",
                                                   "failure": "Mismatch in htinst value"}})
        self.assertIn("htinst", terms)
        passages, tag = context.spec_passages(terms)
        self.assertTrue(tag.startswith("riscv-isa-release"))
        self.assertIn("htinst", passages[0].lower())

    def test_declared_isa_lists_extensions_and_deviations(self):
        text = context.declared_isa({"act_config": "config/cores/b/b.yaml"})
        self.assertIn("I 2.1, S 1.12.0", text)
        self.assertIn("DEVIATION: hardware is 1.11", text)
        self.assertEqual(context.declared_isa({}), "")


if __name__ == "__main__":
    unittest.main()
