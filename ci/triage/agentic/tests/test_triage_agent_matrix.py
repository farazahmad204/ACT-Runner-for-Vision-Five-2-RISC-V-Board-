#!/usr/bin/env python3
"""Multi-board matrix for the triage agent: one failure, two boards, two kinds of fix.

The failure: `mul a2,a0,a1` at 0x80000008 raises an illegal-instruction trap (mcause 2) on a
core whose profile implements M - a genuine hardware bug on both boards. The log and the test
source are the same; the ELF is built for each board's XLEN from the same source.

  Scenario A  lowRISC Ibex, RV32, open RTL on an FPGA (soft core)
              -> DUT_RTL_Bug with an RTL_PATCH naming an HDL source file
  Scenario B  VisionFive 2, SiFive U74, RV64, fixed silicon (hard macro)
              -> DUT_RTL_Bug with a FIRMWARE_WORKAROUND (e.g. trap-and-emulate mul), no HDL change

Two ways to run it:

  python3 -m pytest ci/triage/agentic/tests/test_triage_agent_matrix.py   (or run the file)
      Deterministic, no model: each board gets the right context; a model that follows the
      profile produces the expected fix; a model that always proposes RTL is accepted on the
      FPGA and rejected and corrected on the U74.

  python3 ci/triage/agentic/tests/test_triage_agent_matrix.py --live [--backend codex] [--runs 3]
      Benchmark of the real agent: runs both scenarios N times and scores classification,
      remediation type, HDL leakage on the hard macro, schema validity and steps. Exit 1 when
      the accuracy is below --min-accuracy.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from ci.triage.agentic import react, report_schema, triage_suite  # noqa: E402

REPO = Path(__file__).resolve().parents[4]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "matrix"
SCENARIOS = {
    "A_fpga_soft_core": {"elf": "rv32.elf", "configs": [FIXTURES / "ibex_fpga.yaml", FIXTURES / "ibex_udb.yaml"],
                         "board": "ibex_arty_a7", "expect_class": "DUT_RTL_Bug", "expect_fix": {"RTL_PATCH"}},
    "B_vf2_hard_macro": {"elf": "rv64.elf", "configs": [REPO / "ci/triage/board_knowledge/vf2_jh7110.yaml"],
                         "board": "vf2_jh7110", "expect_class": "DUT_RTL_Bug",
                         "expect_fix": {"FIRMWARE_WORKAROUND"}},
}
HDL = re.compile(r"\.(sv|svh|v|vh|vhd|vhdl|scala)\b|verilog|chisel|\bRTL\b|always_ff|module\s+\w+", re.I)


def assemble(scenario: str, out_dir: Path) -> dict[str, Any]:
    """The coordinator's context for a scenario (profile, intent, invariants, inputs)."""
    spec = SCENARIOS[scenario]
    argv = ["--log", str(FIXTURES / "uart.log"), "--elf", str(FIXTURES / spec["elf"]),
            "--test-source", str(FIXTURES / "test.S"), "--out-dir", str(out_dir)]
    for config in spec["configs"]:
        argv += ["--board-config", str(config)]
    parser = argparse.ArgumentParser()
    for flag in ("--log", "--elf", "--sig-elf", "--sail-config", "--test-source", "--case", "--out-dir"):
        parser.add_argument(flag)
    parser.add_argument("--board-config", action="append", default=[])
    parser.add_argument("--sail-version", default="")
    args = parser.parse_args(argv)
    return triage_suite.assemble(triage_suite.ingest(args), args)


def investigate(scenario: str, out_dir: Path, backend=None, backend_name: str = "codex", model: str = "",
                max_steps: int = 8, codex_bin: str = "codex") -> tuple[dict[str, Any], dict[str, Any]]:
    context = assemble(scenario, out_dir)
    report = react.run_react(context["case"], context["inputs"], out_dir, backend_name, model, max_steps,
                             backend=backend, hardware=context["hardware"], profile=context["profile"],
                             codex_bin=codex_bin, alignment=context["alignment"])
    state = json.loads((out_dir / "agent_state.json").read_text())
    return report, state


# ---------- scripted models (no LLM) ----------

def report_for(board: str, fix: str, target: str, directive: str) -> dict[str, Any]:
    return {"triage_status": "SUCCESS", "target_board": board, "failure_classification": "DUT_RTL_Bug",
            "tier_1_root_cause": {"failing_pc": "0x80000008", "failing_instruction": "mul a2,a0,a1",
                                  "architectural_symptom": "mul raised illegal instruction (mcause 2) although "
                                                           "the core implements M", "divergence_cycle": "not traceable"},
            "tier_2_board_specific_remediation": {"remediation_type": fix, "target_repository_or_file": target,
                                                  "actionable_fix_directive": directive}}


RTL = ("RTL_PATCH", "rtl/ibex_decoder.sv", "1. In ibex_decoder.sv decode OPCODE_OP funct7=0000001 as MULT when "
       "RV32M is enabled. 2. Rebuild the bitstream and rerun MExt_mul-00.")
FIRMWARE = ("FIRMWARE_WORKAROUND", "runner_machine_trap.c",
            "1. In the M-mode trap handler, emulate mul/mulh*/div* when mcause=2 and mtval decodes as an M "
            "instruction. 2. File a SiFive erratum with the reproducer. 3. Rerun MExt_mul-00.")


class Scripted:
    """Reads the log and the instruction, then answers with policy(first message, rejections)."""

    def __init__(self, policy: Callable[[str, list[str]], tuple[str, str, str]]):
        self.policy, self.user, self.turn, self.rejections, self.observations = policy, "", 0, [], []

    def start(self, system: str, user: str) -> None:
        self.system, self.user = system, user
        self.inputs = json.loads(user.split("Inputs for the tools:\n", 1)[1].split("\nYou have")[0])
        self.board = self.inputs.get("board")

    def next(self, force_final: bool) -> react.Turn:
        self.turn += 1
        if self.turn == 1:
            return react.Turn("Read the failure.", [react.ToolCall("tool_parse_rvcp_log",
                                                                   {"log_path": self.inputs["log_path"]})])
        if self.turn == 2:
            return react.Turn("See the instruction.", [react.ToolCall(
                "tool_get_disassembly", {"elf_path": self.inputs["elf_path"], "target_pc": "0x80000008",
                                         "window": 2})])
        fix = self.policy(self.user, self.rejections)
        return react.Turn("Conclude.", [react.ToolCall("submit_analysis", report_for(self.board, *fix))])

    def observe(self, call: react.ToolCall, text: str) -> None:
        self.observations.append((call.name, text))
        if call.name == "submit_analysis" and text.startswith("Rejected"):
            self.rejections.append(text)


def follows_profile(first_message: str, rejections: list[str]) -> tuple[str, str, str]:
    return RTL if "Core type: soft core" in first_message else FIRMWARE


def always_rtl_first(first_message: str, rejections: list[str]) -> tuple[str, str, str]:
    return FIRMWARE if rejections else RTL


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_each_board_gets_its_own_context(self):
        a = assemble("A_fpga_soft_core", self.root / "a")
        b = assemble("B_vf2_hard_macro", self.root / "b")
        self.assertEqual((a["profile"]["xlen"], a["profile"]["core_type"]), (32, "soft_core"))
        self.assertEqual((b["profile"]["xlen"], b["profile"]["core_type"]), (64, "hard_macro"))
        self.assertIn("Core type: soft core (open RTL, https://github.com/lowRISC/ibex)", a["hardware"])
        self.assertIn("Core type: hard macro (fixed silicon)", b["hardware"])
        self.assertIn("never RTL", b["hardware"])
        for context in (a, b):  # the same failure and test intent on both
            self.assertEqual(context["pc"], "0x80000008")
            self.assertEqual(context["label"], "MExt_cg_cp_mul_basic_b0")
            self.assertIn('"coverpoint": "cp_mul_basic"', context["alignment"])

    def test_a_soft_core_gets_an_rtl_patch(self):
        report, state = investigate("A_fpga_soft_core", self.root / "a", backend=Scripted(follows_profile))
        self.assertEqual(report["failure_classification"], "DUT_RTL_Bug")
        fix = report["tier_2_board_specific_remediation"]
        self.assertEqual(fix["remediation_type"], "RTL_PATCH")
        self.assertRegex(fix["target_repository_or_file"], r"\.(sv|v|scala)$")
        self.assertEqual(state["status"], "completed")
        self.assertIn("mul", state["steps"][1]["observation"])  # the real disassembly of the fixture

    def test_b_hard_macro_gets_a_firmware_workaround(self):
        report, _state = investigate("B_vf2_hard_macro", self.root / "b", backend=Scripted(follows_profile))
        self.assertEqual(report["failure_classification"], "DUT_RTL_Bug")
        fix = report["tier_2_board_specific_remediation"]
        self.assertEqual(fix["remediation_type"], "FIRMWARE_WORKAROUND")
        self.assertIsNone(HDL.search(fix["target_repository_or_file"] + " " + fix["actionable_fix_directive"]))

    def test_an_rtl_patch_is_refused_on_silicon_and_corrected(self):
        model = Scripted(always_rtl_first)
        report, state = investigate("B_vf2_hard_macro", self.root / "b", backend=model)
        self.assertEqual(len(model.rejections), 1)
        self.assertIn("RTL_PATCH is not possible on this core (core type hard_macro)", model.rejections[0])
        self.assertEqual(report["tier_2_board_specific_remediation"]["remediation_type"], "FIRMWARE_WORKAROUND")
        model = Scripted(always_rtl_first)
        report, _state = investigate("A_fpga_soft_core", self.root / "a", backend=model)
        self.assertEqual((model.rejections, report["tier_2_board_specific_remediation"]["remediation_type"]),
                         ([], "RTL_PATCH"))

    def test_the_written_report_is_the_strict_schema(self):
        investigate("B_vf2_hard_macro", self.root / "b", backend=Scripted(follows_profile))
        written = json.loads((self.root / "b" / "triage_report.json").read_text())
        report_schema.parse_report(written, {"board_name": "vf2_jh7110", "core_type": "hard_macro"})


# ---------- live benchmark ----------

def score(scenario: str, report: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    spec = SCENARIOS[scenario]
    fix = report.get("tier_2_board_specific_remediation") or {}
    text = f"{fix.get('target_repository_or_file', '')} {fix.get('actionable_fix_directive', '')}"
    hard = "hard_macro" in scenario
    return {
        "scenario": scenario, "status": report.get("triage_status"),
        "class_ok": report.get("failure_classification") == spec["expect_class"],
        "fix_ok": fix.get("remediation_type") in spec["expect_fix"],
        "hdl_leak": bool(hard and HDL.search(text)),
        "schema_ok": not report_schema.check_report(report, {"board_name": spec["board"]}),
        "steps": len(state.get("steps") or []), "rejected": sum(1 for s in state.get("steps") or []
                                                               if str(s.get("observation", "")).startswith("Rejected")),
        "classification": report.get("failure_classification"), "remediation": fix.get("remediation_type"),
    }


def benchmark(backend: str, model: str, runs: int, codex_bin: str, max_steps: int, keep: Path | None) -> list[dict]:
    rows = []
    for scenario in SCENARIOS:
        for run in range(runs):
            out = (keep or Path(tempfile.mkdtemp(prefix="matrix-"))) / f"{scenario}-{run + 1}"
            started = time.time()
            try:
                report, state = investigate(scenario, out, backend_name=backend, model=model, max_steps=max_steps,
                                            codex_bin=codex_bin)
                row = score(scenario, report, state)
            except Exception as exc:  # a run that cannot complete counts as wrong
                row = {"scenario": scenario, "status": f"ERROR {type(exc).__name__}: {str(exc)[:80]}",
                       "class_ok": False, "fix_ok": False, "hdl_leak": False, "schema_ok": False, "steps": 0,
                       "rejected": 0, "classification": None, "remediation": None}
            row["seconds"] = round(time.time() - started)
            rows.append(row)
            print(json.dumps(row), flush=True)
    return rows


def summary(rows: list[dict]) -> tuple[str, float]:
    lines = ["| Scenario | Runs | Class ok | Fix ok | HDL on silicon | Schema ok | Avg steps | Rejections |",
             "|---|---|---|---|---|---|---|---|"]
    for scenario in SCENARIOS:
        mine = [r for r in rows if r["scenario"] == scenario]
        n = len(mine) or 1
        lines.append(f"| {scenario} | {len(mine)} | {sum(r['class_ok'] for r in mine)}/{len(mine)} | "
                     f"{sum(r['fix_ok'] for r in mine)}/{len(mine)} | {sum(r['hdl_leak'] for r in mine)} | "
                     f"{sum(r['schema_ok'] for r in mine)}/{len(mine)} | {sum(r['steps'] for r in mine) / n:.1f} | "
                     f"{sum(r['rejected'] for r in mine)} |")
    correct = sum(r["class_ok"] and r["fix_ok"] and not r["hdl_leak"] and r["schema_ok"] for r in rows)
    accuracy = correct / len(rows) if rows else 0.0
    lines.append(f"\nCross-platform accuracy: {correct}/{len(rows)} = {accuracy:.0%}")
    return "\n".join(lines), accuracy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="benchmark the real agent instead of the unit tests")
    parser.add_argument("--backend", choices=["codex", "openai", "anthropic"], default="codex")
    parser.add_argument("--model", default="")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--runs", type=int, default=3, help="runs per scenario")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--min-accuracy", type=float, default=0.8)
    parser.add_argument("--keep", type=Path, help="keep each run's agent_state.json and report here")
    args, rest = parser.parse_known_args()
    if not args.live:
        return 0 if unittest.main(argv=[sys.argv[0], *rest], exit=False).result.wasSuccessful() else 1
    rows = benchmark(args.backend, args.model, args.runs, args.codex_bin, args.max_steps, args.keep)
    table, accuracy = summary(rows)
    print("\n" + table)
    return 0 if accuracy >= args.min_accuracy else 1


if __name__ == "__main__":
    sys.exit(main())
