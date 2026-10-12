from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci.triage import casefile
from ci.triage.collect_case_files import collect

TEST_S = """\
##### START_TEST_CONFIG #####
# REQUIRED_EXTENSIONS: ['I', 'S']
##### END_TEST_CONFIG #####
#define BOOT_TO_SMODE
RVTEST_BEGIN
Prev_cg_cp_x_bin_a:
  nop
  csrw sie, x14 # sie = 0
  RVTEST_SET_SSW_INT_S # SET SSI
Intr_cg_cp_priority_bin_b:
  csrr x13, sip
  LI(x14, 0x2222)
"""
ENV_UTILS = """\
#define RVTEST_SET_SSW_INT_S \\
  jal rvtest_set_ssw_int_su     /* Trigger supervisor software interrupt */
"""
ENV_SETUP = """\
    rvtest_set_ssw_int_su:
      #ifdef RVMODEL_SET_SSW_INT
        RVMODEL_SET_SSW_INT(a0, a1)
      #endif
      ret
    rvtest_other:
      ret
"""
BOARD = """\
#define SIG_ADDRESS  (0x0c000000 + 0x4)
#define RVMODEL_SET_SSW_INT(_R1, _R2)        \\
  li _R2, SIG_ADDRESS;    \\
  sw _R1, 0(_R2)
#define RVMODEL_UNRELATED 1
"""
SAIL = """{
  // comment lines are allowed in sail.json
  "base": {"xlen": 64, "stvec": {"vectored": {"base_alignment": 7}}},
  "memory": {"misaligned": {"exceptions": {"amo": {"None": null}}}},
  "platform": {"clint": {"base": 33554432}},
  "extensions": {"S": {"supported": true}, "V": {"supported": false}}
}"""


class CaseFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name) / "run"
        (self.run / "per_case" / "InterruptsS-00").mkdir(parents=True)
        (self.run / "per_case" / "InterruptsS-00" / "test.S").write_text(TEST_S)
        (self.run / "act" / "env").mkdir(parents=True)
        (self.run / "act" / "env" / "utils.h").write_text(ENV_UTILS)
        (self.run / "act" / "env" / "rvtest_setup.h").write_text(ENV_SETUP)
        (self.run / "act" / "config").mkdir(parents=True)
        (self.run / "act" / "config" / "rvmodel_macros.h").write_text(BOARD)
        (self.run / "act" / "config" / "sail.json").write_text(SAIL)
        casefile._definitions.cache_clear()
        casefile._routines.cache_clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_bin_label_from_test_info(self):
        self.assertEqual(casefile.bin_label("test: 37; cg: Sstvecd_cg; cp: cp_stvec_mode; bin: stvec_set_bit_38"),
                         "Sstvecd_cg_cp_stvec_mode_stvec_set_bit_38")
        self.assertIsNone(casefile.bin_label("Mismatch in trap signature!"))

    def test_intent_is_the_checks_setup_back_to_the_previous_label(self):
        test = casefile.intent(self.run / "per_case" / "InterruptsS-00" / "test.S", "Intr_cg_cp_priority_bin_b", None)
        self.assertIn(">", test["code"].splitlines()[test["code"].count("\n") - 2])
        self.assertIn("csrw sie", test["code"])
        self.assertNotIn("Prev_cg_cp_x_bin_a", test["code"])
        self.assertIn("RVTEST_SET_SSW_INT_S", test["macros_used"])
        self.assertIn("# REQUIRED_EXTENSIONS: ['I', 'S']", test["header"])

    def test_board_macros_are_followed_through_jal_routines_with_their_constants(self):
        found = "\n".join(casefile.board_macros(self.run, ["RVTEST_SET_SSW_INT_S"]))
        self.assertIn("#define RVMODEL_SET_SSW_INT", found)
        self.assertIn("#define SIG_ADDRESS", found)
        self.assertNotIn("RVMODEL_UNRELATED", found)

    def test_sail_settings_follow_the_failure_not_generic_words(self):
        rows = casefile.sail_settings(self.run, ["stvec", "extensions", "count"], 6, ["I", "S"])
        self.assertIn("base.stvec.vectored.base_alignment = 7", rows)
        self.assertIn('memory.misaligned.exceptions.amo.None = null', rows)
        self.assertIn("extensions.S.supported = true", rows)
        self.assertNotIn("extensions.V.supported = false", rows)

    def test_build_puts_it_together(self):
        cf = casefile.build(self.run, "InterruptsS-00", {"test_info": "cg: Intr_cg; cp: cp_priority; bin: bin_b"},
                            None, ["sip"])
        self.assertIn("intent", cf)
        self.assertTrue(any("RVMODEL_SET_SSW_INT" in m for m in cf["board_macros"]))
        self.assertTrue(any(row.startswith("platform.clint") for row in cf["sail_settings"]))


class CollectTests(unittest.TestCase):
    def test_failed_cases_get_their_elf_and_source_and_the_board_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run, state, act = root / "run", root / "state", root / "act"
            run.mkdir()
            (run / "cases.json").write_text(json.dumps([
                {"test_name": "A-00", "status": "FAIL"}, {"test_name": "B-00", "status": "PASS"}]))
            (state / "generated_tests" / "priv" / "A").mkdir(parents=True)
            (state / "generated_tests" / "priv" / "A" / "A-00.S").write_text("src")
            work = act / "work-x" / "cfg"
            (work / "elfs" / "A").mkdir(parents=True)
            (work / "elfs" / "A" / "A-00.elf").write_text("elf")
            (work / "build" / "A").mkdir(parents=True)
            (work / "build" / "A" / "A-00.sig.elf").write_text("sig")
            (work / "elfs" / "B").mkdir(parents=True)
            (work / "elfs" / "B" / "B-00.elf").write_text("elf")
            (act / "tests" / "env").mkdir(parents=True)
            (act / "tests" / "env" / "utils.h").write_text("#define X 1")
            (act / "config" / "cores" / "sifive_u74").mkdir(parents=True)
            (act / "config" / "cores" / "sifive_u74" / "sail.json").write_text("{}")
            (act / "config" / "cores" / "sifive_u74" / "visionfive2-rv64gc.yaml").write_text("x: 1")
            counts = collect(run, state, "vf2_jh7110", act, [state / "generated_tests", *act.glob("work-*")])
            self.assertEqual(counts, {"failed": 1, "elf": 1, "S": 1, "suites": 0})
            self.assertEqual((run / "per_case" / "A-00" / "test.elf").read_text(), "elf")
            self.assertFalse((run / "per_case" / "B-00").exists())
            self.assertTrue((run / "act" / "config" / "sail.json").is_file())
            self.assertTrue((run / "act" / "env" / "utils.h").is_file())


if __name__ == "__main__":
    unittest.main()


class DamoCaseFileTests(unittest.TestCase):
    def test_damo_case_shows_the_c_source_at_the_assertion_and_its_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            tests = run / "damo" / "Hypervisor_CSR" / "tests"
            tests.mkdir(parents=True)
            (tests / "test_csr.c").write_text("\n".join(
                [f"// line {n}" for n in range(1, 30)]
                + ["    /* Per norm:H_scsrs_nomatch, senvcfg keeps its usual accessibility. */",
                   '    TEST_ASSERT("VS-mode senvcfg read (no trap per norm:H_scsrs_nomatch)", ok);']))
            (run / "damo" / "NORM").mkdir(parents=True)
            (run / "damo" / "NORM" / "hypervisor_norm.md").write_text(
                "| `norm:H_scsrs_nomatch` | Some standard supervisor CSRs have no matching VS CSR. |\n")
            casefile._damo_norms.cache_clear()
            found = casefile.build(run, "Hypervisor_CSR-VCSR_16", {
                "damo_assert": "VS-mode senvcfg read (no trap per norm:H_scsrs_nomatch) (tests/test_csr.c:31)"},
                None, [], "Hypervisor_CSR")
        self.assertEqual(found["intent"]["file"], "tests/test_csr.c")
        self.assertIn(">     TEST_ASSERT(\"VS-mode senvcfg read", found["intent"]["code"])
        self.assertEqual(found["norms"],
                         ["norm:H_scsrs_nomatch: Some standard supervisor CSRs have no matching VS CSR."])


class DamoCollectTests(unittest.TestCase):
    def test_failing_suites_keep_their_elf_sources_and_requirement_tables(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run, state, damo = root / "run", root / "state", root / "damo"
            run.mkdir()
            (run / "cases.json").write_text(json.dumps([
                {"test_name": "Hypervisor_CSR-A", "suite": "Hypervisor_CSR", "status": "FAIL"},
                {"test_name": "Sha-B", "suite": "Sha", "status": "PASS"}]))
            (damo / "Hypervisor_CSR" / "tests").mkdir(parents=True)
            (damo / "Hypervisor_CSR" / "tests" / "t.c").write_text("x")
            (damo / "Hypervisor_CSR" / "main.o").write_text("object")
            (damo / "Sha").mkdir()
            (damo / "Sha" / "main.c").write_text("x")
            (damo / "NORM").mkdir()
            (damo / "NORM" / "h.md").write_text("table")
            (state / "elfs").mkdir(parents=True)
            (state / "elfs" / "Hypervisor_CSR.elf").write_text("elf")
            counts = collect(run, state, "milkv_megrez_eic7700x", root / "act", [], damo)
            self.assertEqual(counts["suites"], 1)
            self.assertTrue((run / "damo" / "Hypervisor_CSR" / "tests" / "t.c").is_file())
            self.assertTrue((run / "damo" / "Hypervisor_CSR" / "suite.elf").is_file())
            self.assertFalse((run / "damo" / "Hypervisor_CSR" / "main.o").exists())
            self.assertFalse((run / "damo" / "Sha").exists())
            self.assertTrue((run / "damo" / "NORM" / "h.md").is_file())
