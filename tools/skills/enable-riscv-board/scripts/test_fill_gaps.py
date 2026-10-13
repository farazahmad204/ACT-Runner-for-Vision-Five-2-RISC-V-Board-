#!/usr/bin/env python3
"""Tests for fill_gaps.py (retrieval, answer verification, reports) with a fake model."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import unittest

import yaml

import fill_gaps as fg
from validate_research_report import validate

DOC = """EIC9999 Technical Reference Manual
Chapter 4 Memory map
UART0 base address 0x5090_0000, size 64 KiB. UART1 base address 0x5091_0000.
DDR memory port 0 starts at 0x8000_0000.

Chapter 7 Clocks
The UART reference clock uart_sclk runs at 200 MHz.

Chapter 9 Interrupts
Interrupt ID 100 lsp_uart0_intr High level
Interrupt ID 101 lsp_uart1_intr High level
""" + "\n".join(f"Filler paragraph {i} about the GPU, video codec and NPU blocks." for i in range(200))


def tool_response(findings: list[dict], tokens: int = 1000) -> dict:
    return {"content": [{"type": "tool_use", "name": "report_findings", "input": {"findings": findings}}],
            "usage": {"input_tokens": tokens, "output_tokens": 100}}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.doc = self.dir / "trm.txt"
        self.doc.write_text(DOC)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def args(self, ids: list[str], **kw) -> argparse.Namespace:
        base = dict(coverage=None, ids=ids, docs=[self.doc], out_dir=self.dir / "out", board_id="eic9999_test",
                    cache_dir=self.dir / "cache", model="fake-small", escalate_model=None, top_k=3, batch=6,
                    budget_tokens=150000, dry_run=False, show_prompts=False, eval=None)
        base.update(kw)
        return argparse.Namespace(**base)


class RetrievalTests(Base):
    def test_known_answers_are_retrieved(self) -> None:
        answers = self.dir / "answers.yaml"
        answers.write_text(yaml.safe_dump({"uart.base": 0x50900000, "uart.plic_source": 100}))
        result = fg.run(self.args(["uart.base", "uart.plic_source"], dry_run=True, eval=answers))
        self.assertEqual(result["retrieval"], {"uart.base": True, "uart.plic_source": True})
        self.assertEqual(len(result["calls"]), 1)            # batched into one call
        self.assertIn("estimated_input", result["tokens"])

    def test_numbers_parse_units_and_separators(self) -> None:
        self.assertIn(0x50900000, fg.numbers_in("base address 0x5090_0000"))
        self.assertIn(200000000, fg.numbers_in("runs at 200 MHz"))
        self.assertIn(65536, fg.numbers_in("size 64 KiB"))


class VerifyTests(Base):
    def calls_with(self, findings: list[dict]):
        seen = []

        def fake(model, system, user, max_tokens):
            seen.append(user)
            cids = [line[1:line.index("]")] for line in user.splitlines() if line.startswith("[")]
            for f in findings:
                if f.get("excerpt_id") == "FIRST":
                    f["excerpt_id"] = cids[0]
            return tool_response(findings)
        return fake, seen

    def run_one(self, ids: list[str], findings: list[dict]) -> dict:
        fake, _ = self.calls_with(findings)
        return fg.run(self.args(ids), call=fake)

    def test_verbatim_quote_with_value_is_partial(self) -> None:
        r = self.run_one(["uart.base"], [{"id": "uart.base", "found": True, "value": "0x50900000",
                                          "quote": "UART0 base address 0x5090_0000", "excerpt_id": "FIRST",
                                          "confidence": "high"}])
        self.assertEqual(r["items"]["uart.base"]["status"], "partial")

    def test_fabricated_quote_is_rejected(self) -> None:
        r = self.run_one(["uart.base"], [{"id": "uart.base", "found": True, "value": "0x10000000",
                                          "quote": "UART0 base address 0x1000_0000", "excerpt_id": "FIRST"}])
        self.assertEqual(r["items"]["uart.base"]["status"], "rejected")

    def test_value_not_in_quote_needs_derivation(self) -> None:
        f = {"id": "uart.base", "found": True, "value": "0x50900004", "quote": "UART0 base address 0x5090_0000",
             "excerpt_id": "FIRST"}
        self.assertEqual(self.run_one(["uart.base"], [dict(f)])["items"]["uart.base"]["status"], "rejected")
        f["derivation"] = "base + 4"                       # not checkable arithmetic
        self.assertEqual(self.run_one(["uart.base"], [dict(f)])["items"]["uart.base"]["status"], "rejected")
        f["derivation"] = "0x5090_0000 + 4"
        self.assertEqual(self.run_one(["uart.base"], [dict(f)])["items"]["uart.base"]["status"], "inferred")
        f["derivation"] = "0x5090_0000 + 8"                # arithmetic does not give the value
        self.assertEqual(self.run_one(["uart.base"], [f])["items"]["uart.base"]["status"], "rejected")

    def test_range_with_mistyped_number_is_rejected(self) -> None:
        f = {"id": "memory.dram_window", "found": True, "value": "0x80000000-0x7fffffff",
             "quote": "DDR memory port 0 starts at 0x8000_0000", "excerpt_id": "FIRST", "derivation": "end of range"}
        r = self.run_one(["memory.dram_window"], [f])
        self.assertEqual(r["items"]["memory.dram_window"]["status"], "rejected")

    def test_quote_must_mention_the_quantity(self) -> None:
        r = self.run_one(["act.mtvec_vectored_alignment"], [{
            "id": "act.mtvec_vectored_alignment", "found": True, "value": 4, "excerpt_id": "FIRST",
            "quote": "UART0 base address 0x5090_0000, size 64 KiB"}])
        self.assertEqual(r["items"]["act.mtvec_vectored_alignment"]["status"], "rejected")

    def test_bitmask_is_computed_from_quoted_rows(self) -> None:
        doc = "Machine Exception Delegation Register (medeleg)\n0 RW Delegate Instruction Address Misaligned\n" \
              "1 RW Delegate Instruction Access Fault\n2 WARL Reserved\n3 RW Delegate Breakpoint\n"
        self.doc.write_text(doc + DOC)
        good = {"id": "act.medeleg_writable", "found": True, "value": None, "bits": [0, 1, 3], "excerpt_id": "FIRST",
                "quote": "0 RW Delegate Instruction Address Misaligned | 1 RW Delegate Instruction Access Fault | "
                         "3 RW Delegate Breakpoint"}
        r = self.run_one(["act.medeleg_writable"], [dict(good)])
        self.assertEqual((r["items"]["act.medeleg_writable"]["status"], r["items"]["act.medeleg_writable"]["value"]),
                         ("inferred", "0xb"))
        r = self.run_one(["act.medeleg_writable"], [dict(good, bits=[0, 1, 3, 7])])
        self.assertEqual(r["items"]["act.medeleg_writable"]["status"], "rejected")

    def test_unknown_excerpt_and_not_found(self) -> None:
        r = self.run_one(["uart.base", "uart.plic_source"], [
            {"id": "uart.base", "found": True, "value": "0x50900000", "quote": "UART0 base address 0x5090_0000",
             "excerpt_id": "made-up"},
            {"id": "uart.plic_source", "found": False, "missing": "interrupt table"}])
        self.assertEqual(r["items"]["uart.base"]["status"], "rejected")
        self.assertEqual(r["items"]["uart.plic_source"]["status"], "not_found")

    def test_budget_stops_calls(self) -> None:
        fake, seen = self.calls_with([])
        r = fg.run(self.args(["uart.base"], budget_tokens=10), call=fake)
        self.assertEqual(seen, [])
        self.assertEqual(r["items"]["uart.base"]["status"], "skipped")

    def test_escalation_retries_only_unanswered(self) -> None:
        models = []

        def fake(model, system, user, max_tokens):
            models.append(model)
            cid = next(line[1:line.index("]")] for line in user.splitlines() if line.startswith("["))
            if model == "fake-small":
                return tool_response([{"id": "uart.plic_source", "found": False, "missing": "?"}])
            return tool_response([{"id": "uart.plic_source", "found": True, "value": 100,
                                   "quote": "Interrupt ID 100 lsp_uart0_intr", "excerpt_id": cid}])
        r = fg.run(self.args(["uart.plic_source"], escalate_model="fake-big", top_k=8), call=fake)
        self.assertEqual(models, ["fake-small", "fake-big"])
        self.assertEqual(r["items"]["uart.plic_source"]["status"], "partial")

    def test_reports_validate_and_feed_the_resolver(self) -> None:
        r = self.run_one(["uart.base"], [{"id": "uart.base", "found": True, "value": "0x50900000",
                                          "quote": "UART0 base address 0x5090_0000", "excerpt_id": "FIRST"}])
        fg.write_outputs(r, self.dir / "out", yaml.safe_load(fg.CATALOG.read_text()))
        report = self.dir / "out" / "fill_gaps_runner_contract.yaml"
        self.assertEqual(validate(yaml.safe_load(report.read_text())), [])
        from resolve_requirements import load_research
        research = load_research(self.dir / "out")
        self.assertEqual(research["uart.base"]["value"], "0x50900000")
        self.assertEqual(research["uart.base"]["evidence_status"], "partial")
        self.assertNotIn("ANTHROPIC", (self.dir / "out" / "fill_gaps.json").read_text())


if __name__ == "__main__":
    unittest.main()
