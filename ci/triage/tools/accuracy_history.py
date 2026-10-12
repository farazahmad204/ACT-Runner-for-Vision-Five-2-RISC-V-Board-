#!/usr/bin/env python3
"""Show how the triage agent's accuracy changes over time.

    python3 ci/triage/tools/accuracy_history.py [/data/ci/agent/triage-evals/history.jsonl]

Each line of the history is one evaluation (riscv-uart-triage with EVALUATE=true); this prints
them oldest first so a change to the agent or its knowledge shows up as a step in the numbers.
"""

import json
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/data/ci/agent/triage-evals/history.jsonl")
    if not path.is_file():
        print(f"No evaluations yet ({path})")
        return 0
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"{'When (UTC)':<17} {'Run':<30} {'Keyed':>5} {'Root cause':>10} {'Verdict':>8} {'False HW bugs':>13}")
    for row in rows:
        def pct(value):
            return "-" if value is None else f"{value}%"
        print(f"{row.get('evaluated_at', '')[:16]:<17} {row.get('run', ''):<30} {row.get('keyed', 0):>5} "
              f"{pct(row.get('accuracy')):>10} {pct(row.get('verdict_accuracy')):>8} "
              f"{row.get('false_hardware_bugs', 0):>13}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
