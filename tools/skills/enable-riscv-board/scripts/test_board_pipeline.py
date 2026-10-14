#!/usr/bin/env python3
"""Tests for decide_choices.py and board_pipeline.py (offline, no model, no runner build)."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import unittest

import yaml

from decide_choices import decide
import board_pipeline as bp

SKILL = Path(__file__).resolve().parent.parent


def rec(value, status="resolved"):
    return {"status": status, "value": value}


def coverage(**over):
    req = {
        "harts.ids": rec([0, 1, 2, 3]), "harts.entering": rec("harts 1 and 2 run the runner"),
        "harts.runner": rec(None, "choice_open"), "harts.monitor": rec(None, "choice_open"),
        "boot.load_address": rec("0x80000000"),
        "boot.firmware_window": rec([{"start": "0x80000000", "end": "0x8007ffff"}]),
        "memory.dram_window": rec([{"base": "0x80000000", "size": "0x400000000"}]),
        "memory.reserved_regions": rec([{"name": "lpcpu", "base": "0xdfff0000", "end": "0xdfffffff"}]),
        "memory.payload_window": rec(None, "choice_open"), "memory.staging_buffer": rec(None, "choice_open"),
        "memory.ram_window": rec(None, "choice_open"), "memory.excluded_range": rec(None, "choice_open"),
        "memory.fixed_tohost": rec(None, "choice_open"),
    }
    req.update(over)
    return {"requirements": req}


class DecideTests(unittest.TestCase):
    def test_megrez_like_layout(self) -> None:
        d, notes, blockers = decide(coverage())
        self.assertEqual(blockers, [])
        self.assertEqual((d["harts.runner"], d["harts.monitor"]), (1, 2))
        self.assertEqual(d["memory.payload_window"], {"base": 0x90000000, "limit": 0xB0000000})
        self.assertEqual(d["memory.staging_buffer"], {"base": 0xB0000000, "bytes": 0x8000000})
        self.assertEqual(d["memory.excluded_range"], {"base": 0, "limit": 0})
        self.assertEqual(d["memory.ram_window"], {"base": 0x80000000, "limit": 0xB8000000})
        self.assertEqual(d["memory.fixed_tohost"], 0x90008000)
        self.assertIn("confirm by smoke test", notes[-1])

    def test_known_secure_region_becomes_excluded(self) -> None:
        regions = [{"name": "secure", "base": "0xb0000000", "size": "0x10000000"},
                   {"name": "lpcpu", "base": "0xdfff0000", "end": "0xdfffffff"}]
        d, _, _ = decide(coverage(**{"memory.reserved_regions": rec(regions)}))
        self.assertEqual(d["memory.excluded_range"], {"base": 0xB0000000, "limit": 0xC0000000})
        self.assertEqual(d["memory.staging_buffer"]["base"], 0xC0000000)    # Megrez's reviewed layout
        self.assertEqual(d["memory.ram_window"]["limit"], 0xC8000000)

    def test_resolved_choices_are_kept_and_one_hart_blocks(self) -> None:
        d, _, blockers = decide(coverage(**{"harts.entering": rec("hart 0"),
                                            "memory.staging_buffer": rec({"base": 0xC0000000, "bytes": 1 << 27})}))
        self.assertNotIn("memory.staging_buffer", d)
        self.assertTrue(any("two harts" in b for b in blockers))


class RequestTests(unittest.TestCase):
    def test_request_validation(self) -> None:
        self.assertEqual(bp.validate_request({"board_id": "acme_x1", "isa": "rv64gc", "docs_quality": "vague",
                                              "documents": ["/tmp/a.pdf", {"url": "https://192.168.100.150/f.pdf",
                                                                           "sha256": "0" * 64}]}), [])
        errors = bp.validate_request({"board_id": "Bad-Id", "isa": "x86", "docs_quality": "great",
                                      "documents": [{"url": "ftp://x"}]})
        self.assertEqual(len(errors), 4)

    def test_document_host_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            req = Path(d) / "r.yaml"
            req.write_text(yaml.safe_dump({"board_id": "acme_x1", "documents": [
                {"url": "https://evil.example/trm.pdf", "sha256": "0" * 64}]}))
            args = argparse.Namespace(allow_model_calls=False, udb_validate=False, from_step=None, until=None,
                                      force=False)
            with self.assertRaises(SystemExit) as cm:
                bp.Pipeline(req, Path(d) / "w", args)
            self.assertIn("not allowed", str(cm.exception))


class PipelineTests(unittest.TestCase):
    def test_megrez_request_until_spec(self) -> None:
        example = SKILL / "assets" / "board_request.example.yaml"
        req = yaml.safe_load(example.read_text())
        paths = [*req["documents"], req["dtb"], *req["boot_logs"].values(), req["user_answers"]]
        if not all((example.parent / p).exists() for p in paths):
            self.skipTest("Megrez evidence files are not on this machine")
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(allow_model_calls=False, udb_validate=False, from_step=None, until="spec",
                                      force=False)
            rc = bp.Pipeline(example, Path(d), args).run()
            self.assertEqual(rc, 0)
            state = yaml.safe_load((Path(d) / "state.json").read_text())
            self.assertEqual(state["steps"]["fill_gaps"]["live"], False)       # never calls a model by default
            self.assertEqual(state["steps"]["spec"]["status"], "ok")
            spec = yaml.safe_load((Path(d) / "spec.yaml").read_text())
            self.assertEqual(spec["runner"]["uart"]["access_bytes"], 2)        # reviewed value kept over DTB's 4
            self.assertEqual(spec["runner"]["uart"]["size"], 0x10000)          # difference listed, spec kept
            self.assertIn("runner.uart.size", (Path(d) / "spec_review.md").read_text())
            rc2 = bp.Pipeline(example, Path(d), args).run()                    # unchanged inputs: steps skipped
            self.assertEqual(rc2, 0)
            args.from_step, args.until = "report", None
            bp.Pipeline(example, Path(d), args).run()
            result = yaml.safe_load((Path(d) / "result.json").read_text())
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(set(result["steps"]), set(bp.STEPS))
            self.assertIn("needs_person", result)
            reqs = {r["id"]: r for r in result["requirements"]}
            self.assertEqual(reqs["boot.medium"]["status"], "resolved")
            self.assertIn("uart.base", reqs)

    def test_offline_decisions_after_decide(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            request = work / "request.yaml"
            request.write_text(yaml.safe_dump({"board_id": "vendor_x1"}))
            (work / "facts.yaml").write_text(yaml.safe_dump({"facts": {"interrupt.contexts": {"value": [
                {"context": 0, "hart": 0, "mode": "M (unused by Linux)"}, {"context": 1, "hart": 0, "mode": "S"},
                {"context": 2, "hart": 1, "mode": "M (unused by Linux)"}, {"context": 3, "hart": 1, "mode": "S"}]}}}))
            decided = work / "coverage_decided.yaml"
            decided.write_text(yaml.safe_dump({"requirements": {
                "harts.runner": {"status": "decided", "value": 1},
                "interrupt.runner_m_context": {"status": "open"}, "interrupt.runner_s_context": {"status": "open"},
                "boot.image_format": {"status": "open"}, "bench.console_path": {"status": "open"}}}))
            args = argparse.Namespace(allow_model_calls=False, udb_validate=False, from_step=None, until=None,
                                      force=False)
            notes = bp.Pipeline(request, work, args).offline_decisions(decided)
            reqs = yaml.safe_load(decided.read_text())["requirements"]
        self.assertEqual((reqs["interrupt.runner_m_context"]["value"], reqs["interrupt.runner_s_context"]["value"]),
                         (2, 3))
        self.assertEqual(reqs["boot.image_format"]["status"], "decided")
        self.assertEqual(reqs["bench.console_path"]["value"], bp.Pipeline.SERIAL_PLACEHOLDER)
        self.assertEqual(len(notes), 3)

    def test_found_dtb_and_boot_logs_feed_extract(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            (work / "docs").mkdir()
            (work / "docs" / "02_log.txt").write_text("OpenSBI v1.4\nU-Boot 2024.01\n")
            (work / "docs" / "03_board.dtb").write_bytes(b"\xd0\x0d\xfe\xed")
            (work / "docs" / "sources.yaml").write_text(yaml.safe_dump({"documents": [
                {"kind": "boot_log", "status": "saved", "file": "02_log.txt"},
                {"kind": "device_tree", "status": "saved", "file": "03_board.dtb"}]}))
            request = work / "request.yaml"
            request.write_text(yaml.safe_dump({"board_id": "vendor_x1"}))
            args = argparse.Namespace(allow_model_calls=False, udb_validate=False, from_step=None,
                                      until="find_docs", force=False)
            pipeline = bp.Pipeline(request, work, args)
        self.assertEqual(pipeline.req["boot_logs"], {"opensbi": str(work / "docs" / "02_log.txt"),
                                                     "uboot": str(work / "docs" / "02_log.txt")})
        self.assertEqual(pipeline.req["dtb"], str(work / "docs" / "03_board.dtb"))
        self.assertNotIn(str(work / "docs" / "03_board.dtb"), pipeline.req["documents"])  # binary: not text

    def test_found_documents_join_the_request_and_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            (work / "docs").mkdir()
            (work / "docs" / "01_TRM.pdf").write_bytes(b"%PDF")
            (work / "docs" / "sources.yaml").write_text(yaml.safe_dump({"documents": [
                {"title": "TRM", "url": "https://v.example/trm.pdf", "kind": "soc_trm", "status": "saved",
                 "file": "01_TRM.pdf"},
                {"title": "Gone", "url": "https://v.example/x.pdf", "kind": "datasheet", "status": "rejected"}]}))
            request = work / "request.yaml"
            request.write_text(yaml.safe_dump({"board_id": "vendor_x1", "board_name": "Vendor X1", "soc": "X1",
                                               "find_docs": True, "model": {"backend": "codex"}}))
            args = argparse.Namespace(allow_model_calls=False, udb_validate=False, from_step=None,
                                      until="find_docs", force=False)
            pipeline = bp.Pipeline(request, work / "w", args)
            self.assertEqual(pipeline.req["documents"], [])  # sources.yaml is under <work>/docs
            pipeline = bp.Pipeline(request, work, args)
            self.assertEqual(pipeline.req["documents"], [str(work / "docs" / "01_TRM.pdf")])
            pipeline.run()
            self.assertEqual(pipeline.state["steps"]["find_docs"]["status"], "skipped")  # no model calls
            pipeline.write_result("researching", None, [])
            result = yaml.safe_load((work / "result.json").read_text())
            self.assertEqual([d["status"] for d in result["documents_found"]], ["saved", "rejected"])
            self.assertEqual(len(result["requirements"]),
                             len(yaml.safe_load(bp.CATALOG.read_text())["requirements"]))
            self.assertEqual(bp.validate_request({"board_id": "ab", "model": {"backend": "gpt"}}),
                             ["model.backend: claude-cli, codex or api"])


if __name__ == "__main__":
    unittest.main()
