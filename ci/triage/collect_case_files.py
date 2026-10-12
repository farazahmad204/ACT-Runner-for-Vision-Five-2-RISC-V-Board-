#!/usr/bin/env python3
"""Keep what the triage agent needs to understand each failed test, next to the run.

    collect_case_files.py --run-root RUN --state-root STATE --board BOARD [--store-dir DIR]

For every failed case (not PASS/SKIPPED) it copies into RUN/per_case/<case>/:
  test.elf   the self-checking ELF that ran on the board (disassembly, addr2line)
  test.S     the generated test source (what the test does around the failing check)
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


def failed_cases(run_root: Path) -> list[str]:
    cases = json.loads((run_root / "cases.json").read_text(encoding="utf-8"))
    return [str(c.get("test_name") or c.get("name")) for c in cases
            if str(c.get("status", "")).upper() not in NOT_FAILURES]


def index_files(roots: list[Path], wanted: set[str]) -> dict[str, dict[str, Path]]:
    """{case: {"elf": path, "S": path}} for the wanted case names, newest file wins."""
    found: dict[str, dict[str, Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            name = path.name
            if name.endswith(".sig.elf"):
                continue
            for suffix, kind in ((".elf", "elf"), (".S", "S")):
                if name.endswith(suffix) and name[: -len(suffix)] in wanted:
                    slot = found.setdefault(name[: -len(suffix)], {})
                    if kind not in slot or path.stat().st_mtime > slot[kind].stat().st_mtime:
                        slot[kind] = path
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
            search: list[Path]) -> dict[str, int]:
    cases = failed_cases(run_root)
    found = index_files(search, set(cases))
    counts = {"failed": len(cases), "elf": 0, "S": 0}
    for case in cases:
        target = run_root / "per_case" / safe_name(case)
        for kind, name in (("elf", "test.elf"), ("S", "test.S")):
            source = found.get(case, {}).get(kind)
            if source:
                copy(source, target / name)
                counts[kind] += 1
    act_out = run_root / "act"
    config = act_config_dir(board, act_root)
    if config:
        for path in config.iterdir():
            if path.is_file() and path.suffix in {".h", ".json", ".yaml", ".yml", ".ld", ".md"}:
                copy(path, act_out / "config" / path.name)
    env = act_root / "tests" / "env"
    if env.is_dir():
        for path in env.glob("*.h"):
            copy(path, act_out / "env" / path.name)
    revision = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(act_root), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    if config or env.is_dir():
        act_out.mkdir(parents=True, exist_ok=True)
        (act_out / "revision.txt").write_text(
            f"act_commit={revision}\nconfig_dir={config.relative_to(act_root) if config else ''}\n",
            encoding="utf-8")
    return counts


def mirror(run_root: Path, store: Path) -> None:
    for path in list((run_root / "per_case").glob("*/test.*")) + list((run_root / "act").rglob("*")):
        if path.is_file():
            copy(path, store / path.relative_to(run_root))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--act-root", type=Path, default=Path("external/riscv-arch-test"))
    parser.add_argument("--search", type=Path, action="append", default=[],
                        help="extra directories holding generated .S and .elf files")
    parser.add_argument("--store-dir", type=Path, help="retained artifact directory of this build")
    args = parser.parse_args()
    if not (args.run_root / "cases.json").is_file():
        print("Case files: no cases.json, skipped")
        return 0
    search = [args.state_root / "generated_tests", *args.act_root.glob("work-*"), *args.search]
    try:
        counts = collect(args.run_root, args.state_root, args.board, args.act_root, search)
        if args.store_dir:
            mirror(args.run_root, args.store_dir)
    except Exception as exc:  # noqa: BLE001 - advisory; never fail the build
        print(f"Case files: not collected ({exc})", file=sys.stderr)
        return 0
    print(f"Case files: {counts['elf']} ELF and {counts['S']} source file(s) kept for "
          f"{counts['failed']} failed case(s)" + (f"; mirrored to {args.store_dir}" if args.store_dir else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
