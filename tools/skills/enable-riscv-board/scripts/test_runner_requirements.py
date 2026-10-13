#!/usr/bin/env python3
"""Tests for assets/runner-requirements.yaml and resolve_requirements.py."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REPO_ROOT = SCRIPT_DIR.parents[3]
CATALOG = SKILL_DIR / "assets" / "runner-requirements.yaml"
KINDS = {"fact", "choice", "config", "bench"}
STEP_KINDS = {"extract", "manifest", "document", "web", "probe"}
NEEDS = {"offline_build", "hardware_smoke", "install", "act", "ci"}


def build_script_text() -> str:
    """build_runner.sh from this checkout, else from the contract's stable ref."""
    local = REPO_ROOT / "cert_harness" / "tools" / "build_runner.sh"
    if local.is_file():
        return local.read_text(encoding="utf-8")
    contract = yaml.safe_load((SKILL_DIR / "assets" / "system-contract.yaml").read_text())
    ref = contract["repositories"]["runner"]["stable_remote_ref"]
    result = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{ref}:cert_harness/tools/build_runner.sh"],
                            capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        cls.reqs = cls.catalog["requirements"]

    def test_entries_are_well_formed(self) -> None:
        ids = [r["id"] for r in self.reqs]
        self.assertEqual(len(ids), len(set(ids)), "duplicate requirement IDs")
        for r in self.reqs:
            for key in ("id", "group", "kind", "consumer", "needed_for", "description", "resolve", "verify"):
                self.assertIn(key, r, f"{r.get('id')}: missing {key}")
            self.assertIn(r["kind"], KINDS, r["id"])
            self.assertIn(r["group"], self.catalog["groups"], r["id"])
            self.assertTrue(set(r["needed_for"]) <= NEEDS, r["id"])
            for step in r["resolve"]:
                if step == "user":
                    continue
                self.assertIsInstance(step, dict, r["id"])
                self.assertTrue(set(step) <= STEP_KINDS, f"{r['id']}: {step}")
            if r["kind"] == "choice":
                self.assertTrue(r.get("rules") or r.get("default") is not None, f"{r['id']}: choice needs rules")
            if r["kind"] == "fact" and r.get("default") is not None:
                self.assertIn(r["group"], ("uart", "platform", "harts"), f"{r['id']}: facts rarely allow defaults")

    def test_every_build_input_is_covered(self) -> None:
        text = build_script_text()
        if not text:
            self.skipTest("build_runner.sh not available in this checkout or the stable ref")
        loop = re.search(r"^for var in(.*?)^do", text, re.S | re.M)
        inputs = set(re.findall(r"BOARD_[A-Z0-9_]+", loop.group(1))) if loop else set()
        inputs |= set(re.findall(r"defsym=(BOARD_[A-Z0-9_]+)", text))
        inputs |= set(re.findall(r"\$\{?(BOARD_[A-Z0-9_]+)", text))
        consumers = " ".join(r["consumer"] for r in self.reqs)
        missing = sorted(v for v in inputs if v not in consumers)
        self.assertEqual(missing, [], "build_runner.sh inputs missing from runner-requirements.yaml")


class ResolverTests(unittest.TestCase):
    def run_resolver(self, **files) -> tuple[int, dict, str]:
        with tempfile.TemporaryDirectory() as d:
            args = [sys.executable, str(SCRIPT_DIR / "resolve_requirements.py"), "--board-id", "t", "--out", d]
            for name, data in files.items():
                path = Path(d) / f"{name}.yaml"
                path.write_text(yaml.safe_dump(data))
                args += [f"--{name}", str(path)]
            r = subprocess.run(args, capture_output=True, text=True)
            cov = yaml.safe_load((Path(d) / "coverage.yaml").read_text())
            questions = (Path(d) / "questions.md").read_text()
            return r.returncode, cov, questions

    def test_conflicting_numbers_are_reported(self) -> None:
        facts = {"facts": {"uart.register_width": {"value": 4, "source": "dtb", "evidence_status": "confirmed"}}}
        manifest = {"uart": {"register_width": 2}}
        rc, cov, questions = self.run_resolver(facts=facts, manifest=manifest)
        rec = cov["requirements"]["uart.register_width"]
        self.assertEqual(rec["status"], "conflict")
        self.assertEqual(rec["conflicts"][0]["value"], 2)
        self.assertIn("Resolve conflicting values first", questions)

    def test_nothing_supplied_leaves_facts_open_and_choices_for_the_agent(self) -> None:
        rc, cov, questions = self.run_resolver(user={})
        reqs = cov["requirements"]
        self.assertEqual(reqs["uart.base"]["status"], "open")
        self.assertEqual(reqs["memory.payload_window"]["status"], "choice_open")
        self.assertEqual(reqs["uart.baud"]["status"], "default")
        self.assertIn("Search the documents the user supplied", questions)
        self.assertIn("Agent decisions", questions)

    def test_probe_facts_resolve_act_items(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            probe = Path(d) / "probe.json"
            probe.write_text('{"facts": {"act.medeleg_writable": "0xf0b7ff", "act.time_csr": false}}')
            args = [sys.executable, str(SCRIPT_DIR / "resolve_requirements.py"), "--board-id", "t",
                    "--probe", str(probe), "--out", d]
            subprocess.run(args, capture_output=True, text=True)
            reqs = yaml.safe_load((Path(d) / "coverage.yaml").read_text())["requirements"]
            self.assertEqual((reqs["act.medeleg_writable"]["via"], reqs["act.medeleg_writable"]["value"]),
                             ("probe", "0xf0b7ff"))
            self.assertEqual(reqs["act.time_csr"]["evidence_status"], "confirmed")

    def test_memory_rule_violation_fails(self) -> None:
        manifest = {"memory": {"dram_base": 0x80000000, "usable_limit": 0xC8000000,
                               "payload_base": 0x90000000, "payload_limit": 0xC4000000,
                               "staging_address": 0xC0000000, "staging_max_bytes": 0x08000000}}
        rc, cov, _ = self.run_resolver(manifest=manifest)
        self.assertEqual(rc, 1)
        self.assertTrue(any("overlaps" in p for p in cov["rule_violations"]), cov["rule_violations"])


if __name__ == "__main__":
    unittest.main()
