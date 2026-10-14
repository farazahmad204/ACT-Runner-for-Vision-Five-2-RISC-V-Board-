#!/usr/bin/env python3
"""Map a MEPC/PC value to the closest lines in an objdump."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


LINE_RE = re.compile(r"^\s*([0-9a-fA-F]+):\s+([0-9a-fA-F]+)\s+(.*)$")
LABEL_RE = re.compile(r"^([0-9a-fA-F]+)\s+<([^>]+)>:")


def parse_objdump(path: Path) -> list[dict[str, Any]]:
    return parse_objdump_text(path.read_text(errors="replace"))


def parse_objdump_text(text: str) -> list[dict[str, Any]]:
    """Label and instruction rows of `objdump -d` output."""
    rows: list[dict[str, Any]] = []
    current_label = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        label = LABEL_RE.match(line)
        if label:
            current_label = {"addr": int(label.group(1), 16), "name": label.group(2), "line": lineno}
            rows.append({"type": "label", "line": lineno, "addr": current_label["addr"], "text": line, "label": current_label["name"]})
            continue
        match = LINE_RE.match(line)
        if match:
            rows.append(
                {
                    "type": "insn",
                    "line": lineno,
                    "addr": int(match.group(1), 16),
                    "encoding": match.group(2),
                    "asm": match.group(3).strip(),
                    "text": line,
                    "label": current_label["name"] if current_label else None,
                }
            )
    return rows


def lookup(path: Path, pc: int, context: int) -> dict[str, Any]:
    rows = parse_objdump(path)
    insns = [row for row in rows if row["type"] == "insn"]
    prior = [row for row in insns if row["addr"] <= pc]
    hit = prior[-1] if prior else None
    if hit is None and insns:
        hit = insns[0]

    if hit is None:
        return {"objdump": str(path), "pc": hex(pc), "error": "no instructions found"}

    idx = rows.index(hit)
    start = max(0, idx - context)
    end = min(len(rows), idx + context + 1)
    return {
        "objdump": str(path),
        "pc": f"0x{pc:016x}",
        "matched_addr": f"0x{hit['addr']:016x}",
        "matched_line": hit["line"],
        "matched_label": hit.get("label"),
        "matched_encoding": hit.get("encoding"),
        "matched_asm": hit.get("asm"),
        "exact": hit["addr"] == pc,
        "context": [
            {
                "line": row["line"],
                "addr": f"0x{row['addr']:016x}" if "addr" in row else None,
                "text": row["text"],
                "is_match": row is hit,
            }
            for row in rows[start:end]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("objdump", type=Path)
    parser.add_argument("pc", help="PC/MEPC value, hex or decimal")
    parser.add_argument("--context", type=int, default=8)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    pc = int(args.pc, 0)
    print(json.dumps(lookup(args.objdump, pc, args.context), indent=2 if args.pretty else None, sort_keys=True))


if __name__ == "__main__":
    main()
