#!/usr/bin/env python3
"""Tests for just-in-time knowledge retrieval (find_artifacts.inject_curated_knowledge)."""

from __future__ import annotations

import re
import unittest

from ci.triage.agentic import knowledge as f

VF2 = {"board_name": "VisionFive2", "core_name": "SiFive U74", "soc": "JH7110", "implemented_isa": "RV64GC"}
IBEX = {"board_name": "ibex-fpga", "core_name": "lowRISC Ibex", "implemented_isa": "RV32IMC"}


class KnowledgeTests(unittest.TestCase):
    def test_rules_are_whole_and_tagged(self):
        rules = f.knowledge_rules()
        sections = {r["section"]: r for r in rules if r["section"]}
        self.assertIn("lh", sections["Known Priority Behavior"]["text"])  # the section stays one rule
        self.assertEqual(sections["Delegation"]["topics"], {"delegation", "ecall", "protection"})  # pinned
        self.assertTrue(all(r["text"] and not r["text"].startswith(";") for r in rules))

    def test_failure_topics(self):
        self.assertEqual(f.failure_topics("mcause=0x800000000000000b"), {"interrupt"})
        self.assertIn("translation", f.failure_topics("load page fault mcause=0xd"))
        self.assertIn("translation", f.failure_topics("SvSm-00"))
        self.assertTrue({"atomic", "priority", "protection"} <= f.failure_topics("lr.w misaligned mcause=0x5"))
        self.assertIn("compressed", f.failure_topics("c.lw at 0x80000010"))
        self.assertEqual(f.failure_topics("nothing relevant"), set())

    def test_selection_budget_and_exclusion(self):
        block = f.inject_curated_knowledge("RV64GC", "access fault vs misaligned mcause=0x5", budget=1300, profile=VF2)
        self.assertLessEqual(len(block), 1300)
        self.assertIn("Access Fault vs Misaligned Priority", block)
        self.assertNotIn("FP CSR", block)
        first = [line.split("] ", 1)[1] for line in block.splitlines() if line.startswith("- [")]
        again = f.inject_curated_knowledge("RV64GC", "access fault vs misaligned mcause=0x5", exclude=set(first),
                                           profile=VF2)
        self.assertFalse(set(first) & {l.split("] ", 1)[1] for l in again.splitlines() if l.startswith("- [")})
        self.assertEqual(f.inject_curated_knowledge("RV64GC", "nothing relevant"), "")

    def test_missing_extension_is_flagged(self):
        self.assertIn("involves A, which the ISA RV64IMC does not list",
                      f.inject_curated_knowledge("RV64IMC", "amoadd.w access fault mcause=0x7"))
        self.assertNotIn("does not list", f.inject_curated_knowledge("RV64GC", "amoadd.w access fault mcause=0x7"))


class PlatformIsolationTests(unittest.TestCase):
    def test_only_self_declared_platform_files(self):
        self.assertEqual(set(f.platform_signatures()), {"vf2_u74_platform.md"})  # act4/rva23 are not boards
        self.assertIn("u74", f.platform_signatures()["vf2_u74_platform.md"])
        self.assertNotIn("sifive", f.platform_signatures()["vf2_u74_platform.md"])  # a vendor, not a core

    def test_matching_board_gets_its_notes(self):
        rules, info = f.filter_knowledge(VF2)
        self.assertEqual((info["mode"], info["platforms"]), ("platform", ["vf2_u74_platform.md"]))
        self.assertIn("vf2_u74_platform.md", info["files"])
        self.assertTrue(any("VF2/U74" in r["text"] for r in rules))

    def test_other_boards_get_the_baselines_without_vf2_rules(self):
        for profile in (IBEX, {"board_name": "Milk-V Megrez", "core_name": "SiFive P550"}, None):
            rules, info = f.filter_knowledge(profile)
            self.assertEqual(info["mode"], "baseline")
            self.assertEqual(info["files"], ["act4_rules.md", "riscv_privileged_rules.md"])
            self.assertFalse(any(re.search(r"vf2|u74|visionfive", r["text"], re.I) for r in rules))
            self.assertGreaterEqual(info["excluded_platform_rules"], 1)
        block, info = f.curated_knowledge_for(IBEX, "Trap handled in M-Mode mcause=0x9 delegation")
        self.assertIn("riscv_privileged_rules", block)
        self.assertNotIn("VF2", block)


if __name__ == "__main__":
    unittest.main()
