#!/usr/bin/env python3
"""Turn damo-rv-priv-ats UART output into per-test-case results.

Each damo suite is one ELF that runs many test cases and prints, per case:

    [TEST] <name>
      ASSERT FAIL: <msg>: got 0x.., expected 0x.. (file:line)    (zero or more)
    [PASS] <name> | [FAIL] <name> | [SKIP] <name>: <reason> | [FATAL] <name>: <reason>

An unexpected trap prints "[ERROR] UNEXPECTED TRAP ..." plus mcause/mepc/mtval and
halts the suite, so the case that was running has no outcome line.

Reads the runner's summary.json (one result per suite ELF) and writes the portal
contract into the run root: cases.json, cases.csv and per_case/<name>/uart.log,
with one row per test case named "<Suite>-<case>".
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

START = re.compile(r"^\[TEST\] (.+?)\s*$")
STATUS = {"PASS": "PASS", "FAIL": "FAIL", "FATAL": "FAIL", "SKIP": "SKIPPED"}


def outcome(line: str, case: str) -> tuple[str, str] | None:
    """(status, reason) if line is "[TAG] <case>" or "[TAG] <case>: <reason>".

    Case names contain ": " themselves, so match the exact name, not a pattern.
    """
    for tag, status in STATUS.items():
        prefix = f"[{tag}] {case}"
        if line == prefix:
            return status, ""
        if line.startswith(prefix + ": "):
            return status, line[len(prefix) + 2 :]
    return None


def case_name(suite: str, case: str, used: set[str]) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.]+", "_", case).strip("_.")[:120] or "case"
    name = f"{suite}-{slug}"
    unique, counter = name, 2
    while unique in used:
        unique, counter = f"{name}_{counter}", counter + 1
    used.add(unique)
    return unique


def parse_suite(text: str) -> list[dict]:
    """Cases in order: {"case", "status", "reason", "log"}; status None if unfinished."""
    cases: list[dict] = []
    current: dict | None = None
    for raw in text.replace("\r", "").splitlines():
        line = raw.strip()
        start = START.match(line)
        if start:
            if current is not None:  # previous case never reported an outcome
                cases.append(current)
            current = {"case": start.group(1), "status": None, "reason": "", "log": [raw]}
            continue
        result = outcome(line, current["case"]) if current is not None else None
        if result is not None:
            current["log"].append(raw)
            current["status"], current["reason"] = result
            if current["status"] == "FAIL" and not current["reason"]:
                asserts = [entry.strip() for entry in current["log"] if "ASSERT FAIL" in entry]
                current["reason"] = "; ".join(asserts)[:500] or "test reported FAIL"
            cases.append(current)
            current = None
            continue
        if current is not None:
            current["log"].append(raw)
    if current is not None:
        cases.append(current)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))

    rows: list[dict] = []
    used: set[str] = set()
    for result in summary.get("results", []):
        suite = str(result.get("name", "suite"))
        suite_status = str(result.get("status", "ERROR")).upper()
        log_path = Path(str(result.get("uart_log", "")))
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        parsed = parse_suite(text)
        for item in parsed:
            status = item["status"]
            reason = item["reason"]
            if status is None:  # the suite stopped inside this case
                trap = [line.strip() for line in item["log"] if "[ERROR]" in line]
                status = "FAIL"
                reason = (
                    "; ".join(trap)[:500]
                    if trap
                    else f"no result: the suite ended with {suite_status} inside this test"
                )
            rows.append(
                {"name": case_name(suite, item["case"], used), "suite": suite, "status": status,
                 "reason": reason, "log": "\n".join(item["log"]) + "\n"}
            )
        if not parsed:  # crashed, timed out or never started before the first test
            rows.append(
                {"name": case_name(suite, "suite", used), "suite": suite,
                 "status": "PASS" if suite_status == "PASS" else "FAIL",
                 "reason": "" if suite_status == "PASS" else str(result.get("error") or f"suite {suite_status} before any test ran"),
                 "log": text}
            )

    cases = []
    for row in rows:
        log = Path("per_case") / row["name"] / "uart.log"
        (run_root / log).parent.mkdir(parents=True, exist_ok=True)
        (run_root / log).write_text(row["log"], encoding="utf-8")
        cases.append(
            {"test_name": row["name"], "suite": row["suite"], "status": row["status"],
             "root_cause": "" if row["status"] == "PASS" else row["reason"],
             "uart_log": str(log), "transport": "uart_stream"}
        )
    (run_root / "cases.json").write_text(json.dumps(cases, indent=2) + "\n", encoding="utf-8")
    with (run_root / "cases.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["test_name", "suite", "status", "root_cause", "uart_log"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(cases)
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["status"]] = counts.get(case["status"], 0) + 1
    print(f"damo cases: {len(cases)} from {len(summary.get('results', []))} suites {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
