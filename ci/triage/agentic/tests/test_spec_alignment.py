from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ci.triage.agentic import react, spec_alignment as sa
from ci.triage.agentic.tests.test_react import Scripted, turn

SOURCE = """///////////////////////////////////////////
// ExceptionsSm/ExceptionsSm-00.S
///////////////////////////////////////////

##### START_TEST_CONFIG #####
# REQUIRED_EXTENSIONS: ['I', 'Sm']
# # # no param constraints
# MARCH: rv${XLEN}i_zicsr
##### END_TEST_CONFIG #####

#define BOOT_TO_MMODE

// cp_mprv_load, cp_misaligned_priority_load
ExceptionsSm_cg_cp_mprv_load_s:
  lw a0, 0(a1)
ExceptionsSm_cg_cp_misaligned_priority_load_lh:
  lh a0, 1(zero)
"""
PLAN = """suite: ExceptionsSm
coverpoints:
  - name: cp_mprv_load
    goal: MPRV loads use MPP's privilege
    expectation: translated with MPP
    bins: "2"
    rules:
      - "[norm:mstatus_mprv_ldst_op]"
  - name: cp_misaligned_priority_load
    goal: misaligned vs access-fault priority on loads
    expectation: either cause, per the EEI
    bins: "4"
"""


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "act" / "testplans" / "priv").mkdir(parents=True)
        (self.root / "act" / "testplans" / "priv" / "ExceptionsSm.yaml").write_text(PLAN)
        (self.root / "act" / "testplans" / "I.csv").write_text(
            "Instruction,Type,RV32,RV64,cp_asm_count,cp_rs1,cp_imm_edges\naddi,I,x,x,x,x,x\n")
        self.source = self.root / "test.S"
        self.source.write_text(SOURCE)
        sa._priv_index.cache_clear()

    def tearDown(self):
        self.dir.cleanup()

    def test_header_and_failing_check(self):
        intent = sa.extract_test_intent(str(self.source), "ExceptionsSm_cg_cp_misaligned_priority_load_lh+0x4",
                                        self.root)
        self.assertEqual((intent["suite"], intent["boot_mode"], intent["march"]),
                         ("ExceptionsSm", "BOOT_TO_MMODE", "rv${XLEN}i_zicsr"))
        self.assertEqual(intent["required_extensions"], ["I", "Sm"])
        self.assertEqual(intent["failing_check"], {"label": "ExceptionsSm_cg_cp_misaligned_priority_load_lh",
                                                   "coverpoint": "cp_misaligned_priority_load", "bin": "lh"})
        self.assertEqual(intent["testplan"][0]["goal"], "misaligned vs access-fault priority on loads")
        self.assertEqual(intent["coverpoints_in_test"], ["cp_mprv_load", "cp_misaligned_priority_load"])

    def test_rule_ids_bring_the_normative_text(self):
        intent = sa.extract_test_intent(str(self.source), "ExceptionsSm_cg_cp_mprv_load_s", self.root)
        self.assertEqual(intent["rule_ids"], ["mstatus_mprv_ldst_op"])
        text = sa.fetch_isa_invariants({"case": "ExceptionsSm-00", "trap": {"mcause": "0x5"},
                                        "failure": "load access fault"}, None, intent["rule_ids"])
        self.assertIn("[norm:mstatus_mprv_ldst_op]", text)
        self.assertIn("riscv_privileged_rules", text)
        self.assertNotIn("VF2", text)  # no profile: baselines only

    def test_unprivileged_plan_and_missing_plan(self):
        source = self.root / "addi.S"
        source.write_text("// I/I-addi-00.S\n")
        intent = sa.extract_test_intent(str(source), None, self.root)
        self.assertEqual(intent["testplan"][0]["coverpoints"], ["cp_asm_count", "cp_rs1", "cp_imm_edges"])
        self.assertIn("not kept", sa.extract_test_intent(str(self.root / "none.S"))["note"])

    def test_sections_have_explicit_boundaries(self):
        text = sa.render_sections({"suite": "X"}, "rule")
        for marker in ("[TEST INTENT]", "[/TEST INTENT]", "[SPECIFICATION INVARIANTS]", "[/SPECIFICATION INVARIANTS]"):
            self.assertIn(marker, text)
        payload = react.build_user_payload("X", {}, 4, "Target hardware: x", alignment=text)
        self.assertLess(payload.index("TARGET HARDWARE PROFILE"), payload.index("[TEST INTENT]"))

    def test_the_agent_gets_the_intent_of_the_check_it_finds(self):
        os.environ["TRIAGE_TOOL_ROOTS"] = str(self.root)
        os.environ["TRIAGE_ACT_ROOT"] = str(self.root / "act")
        try:
            model = Scripted([turn("look", "tool_get_disassembly", {"elf_path": str(self.source), "target_pc": "0x0"})])
            original = react.triage_tools.dispatch
            react.triage_tools.dispatch = lambda name, args, profile=None: {
                "ok": True, "symbol": "ExceptionsSm_cg_cp_misaligned_priority_load_lh",
                "instruction": {"asm": "lh a0,1(zero)", "encoding": "00101503"}, "notes": []}
            react.run_react("ExceptionsSm-00", {"test_source_path": str(self.source), "isa": "RV64GC"},
                            self.root / "out", backend=model, max_steps=2)
        finally:
            react.triage_tools.dispatch = original
            os.environ.pop("TRIAGE_TOOL_ROOTS")
            os.environ.pop("TRIAGE_ACT_ROOT")
        self.assertIn("[TEST INTENT]", model.observations[0][1])
        self.assertIn("misaligned vs access-fault priority on loads", model.observations[0][1])


if __name__ == "__main__":
    unittest.main()
