#!/usr/bin/env python3
"""Keep what the triage agent needs to understand each failed test, next to the run.

    collect_case_files.py --run-root RUN --state-root STATE --board BOARD [--store-dir DIR]

For every failed case (not PASS/SKIPPED) it copies into RUN/per_case/<case>/:
  test.elf   the self-checking ELF that ran on the board (disassembly, addr2line)
  test.S     the generated test source (what the test does around the failing check)
  test.sig.elf  the signature ELF ACT built for Sail (sail_trace.py runs it to show what the
             reference model did at the failing instruction)
Once per run it copies into RUN/act/:
  config/    the board's ACT config directory (rvmodel_macros.h, sail.json, UDB yaml, link.ld)
  env/       ACT's tests/env headers (trap handler, signature and failure macros)
  revision.txt  the ACT commit and the config path
With --store-dir the same files are mirrored into the retained artifact store so a later
re-triage (riscv-uart-triage) sees exactly what this build ran. Best effort: missing files are
reported and skipped; it never fails the build.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "board_knowledge"
NOT_FAILURES = {"PASS", "SKIPPED", "SKIP"}


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "case"


def failed_cases(run_root: Path) -> list[dict]:
    cases = json.loads((run_root / "cases.json").read_text(encoding="utf-8"))
    return [c for c in cases if str(c.get("status", "")).upper() not in NOT_FAILURES]


DAMO_SOURCE = {".c", ".h", ".S", ".ld"}


def collect_damo(run_root: Path, state_root: Path, damo_root: Path, suites: set[str]) -> int:
    """damo-rv-priv-ats runs one ELF per suite and reports failed assertions by file:line, so
    keep each failing suite's ELF and sources, the shared code and the requirement tables."""
    out = run_root / "damo"
    kept = 0
    for suite in sorted(suites):
        source = damo_root / suite
        if not source.is_dir():
            continue
        for path in source.rglob("*"):
            if path.is_file() and path.suffix in DAMO_SOURCE and "build" not in path.parts:
                copy(path, out / suite / path.relative_to(source))
        elf = state_root / "elfs" / f"{suite}.elf"
        if elf.is_file():
            copy(elf, out / suite / "suite.elf")
        kept += 1
    for shared in ("common", "NORM"):
        for path in (damo_root / shared).rglob("*") if (damo_root / shared).is_dir() else ():
            if path.is_file() and (path.suffix in DAMO_SOURCE or shared == "NORM"):
                copy(path, out / shared / path.relative_to(damo_root / shared))
    revision = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(damo_root), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    if kept:
        (out / "revision.txt").write_text(f"damo_commit={revision}\n", encoding="utf-8")
    return kept


def index_files(roots: list[Path], wanted: set[str]) -> dict[str, dict[str, Path]]:
    """{case: {"elf": path, "S": path}} for the wanted case names, newest file wins."""
    found: dict[str, dict[str, Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            name = path.name
            for suffix, kind in ((".sig.elf", "sig"), (".elf", "elf"), (".S", "S")):
                if name.endswith(suffix) and name[: -len(suffix)] in wanted:
                    slot = found.setdefault(name[: -len(suffix)], {})
                    if kind not in slot or path.stat().st_mtime > slot[kind].stat().st_mtime:
                        slot[kind] = path
                    break
    return found


def act_config_dir(board: str, act_root: Path) -> Path | None:
    path = KNOWLEDGE_DIR / f"{board}.yaml"
    if not path.is_file():
        return None
    relative = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("act_config")
    if not relative:
        return None
    directory = (act_root / relative).parent
    return directory if directory.is_dir() else None


def copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def collect(run_root: Path, state_root: Path, board: str, act_root: Path,
            search: list[Path], damo_root: Path | None = None) -> dict[str, int]:
    failed = failed_cases(run_root)
    suites = {str(c["suite"]) for c in failed if c.get("suite")}
    cases = [str(c.get("test_name") or c.get("name")) for c in failed if not c.get("suite")]
    counts = {"failed": len(failed), "elf": 0, "sig": 0, "S": 0, "suites": 0}
    if suites and damo_root:
        counts["suites"] = collect_damo(run_root, state_root, damo_root, suites)
    found = index_files(search, set(cases)) if cases else {}
    for case in cases:
        target = run_root / "per_case" / safe_name(case)
        for kind, name in (("elf", "test.elf"), ("sig", "test.sig.elf"), ("S", "test.S")):
            source = found.get(case, {}).get(kind)
            if source:
                copy(source, target / name)
                counts[kind] += 1
    if not cases:  # damo only: no ACT config or env headers involved
        return counts
    act_out = run_root / "act"
    config = act_config_dir(board, act_root)
    if config:
        for path in config.iterdir():
            if path.is_file() and path.suffix in {".h", ".json", ".yaml", ".yml", ".ld", ".md"}:
                copy(path, act_out / "config" / path.name)
    plans = act_root / "testplans"
    if plans.is_dir():  # what each coverpoint checks (spec_alignment reads it)
        for path in plans.rglob("*"):
            if path.is_file() and path.suffix in {".yaml", ".csv", ".json"}:
                copy(path, act_out / "testplans" / path.relative_to(plans))
    env = act_root / "tests" / "env"
    if env.is_dir():
        for path in env.glob("*.h"):
            copy(path, act_out / "env" / path.name)
    revision = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(act_root), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    sail = ""
    act_config_py = act_root / "framework" / "src" / "act" / "config.py"
    if act_config_py.is_file():
        found = re.search(r'^REQUIRED_SAIL_VERSION = "([^"]+)"', act_config_py.read_text(encoding="utf-8"), re.M)
        sail = found.group(1) if found else ""
    if config or env.is_dir():
        act_out.mkdir(parents=True, exist_ok=True)
        (act_out / "revision.txt").write_text(
            f"act_commit={revision}\nconfig_dir={config.relative_to(act_root) if config else ''}\n"
            f"sail_version={sail}\n", encoding="utf-8")
    return counts


def mirror(run_root: Path, store: Path) -> None:
    for path in (list((run_root / "per_case").glob("*/test.*")) + list((run_root / "act").rglob("*"))
                 + list((run_root / "damo").rglob("*"))):
        if path.is_file():
            copy(path, store / path.relative_to(run_root))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--act-root", type=Path, default=Path("external/riscv-arch-test"))
    parser.add_argument("--damo-root", type=Path, default=Path("external/damo-rv-priv-ats"))
    parser.add_argument("--search", type=Path, action="append", default=[],
                        help="extra directories holding generated .S and .elf files")
    parser.add_argument("--store-dir", type=Path, help="retained artifact directory of this build")
    args = parser.parse_args()
    if not (args.run_root / "cases.json").is_file():
        print("Case files: no cases.json, skipped")
        return 0
    search = [args.state_root / "generated_tests", *args.act_root.glob("work-*"), *args.search]
    try:
        counts = collect(args.run_root, args.state_root, args.board, args.act_root, search,
                         args.damo_root)
        if args.store_dir:
            mirror(args.run_root, args.store_dir)
    except Exception as exc:  # noqa: BLE001 - advisory; never fail the build
        print(f"Case files: not collected ({exc})", file=sys.stderr)
        return 0
    print(f"Case files: {counts['elf']} ELF, {counts['sig']} signature ELF and {counts['S']} source file(s)"
          + (f" and {counts['suites']} damo suite(s)" if counts["suites"] else "")
          + f" kept for {counts['failed']} failed case(s)" + (f"; mirrored to {args.store_dir}" if args.store_dir else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
