#!/usr/bin/env python3
"""Consolidate validated research reports without silently resolving conflicts."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import yaml

from validate_research_report import validate


def consolidate(paths: list[Path]) -> dict:
    reports = []
    ids: dict[str, str] = {}
    duplicates = []
    statuses: Counter[str] = Counter()
    blockers: list[dict] = []
    conflicts: list[dict] = []
    recommendations: list[dict] = []
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        errors = validate(data)
        if errors:
            raise ValueError(f"{path} is invalid: {'; '.join(errors)}")
        role = str(data["agent_role"])
        reports.append({"path": str(path), "agent_role": role, "board_id": str(data["board_id"]),
                        "evidence_count": len(data["evidence"])})
        for record in data["evidence"]:
            record_id = str(record["id"])
            if record_id in ids:
                duplicates.append({"id": record_id, "first_role": ids[record_id], "second_role": role})
            else:
                ids[record_id] = role
            statuses[str(record["evidence_status"])] += 1
        blockers.extend({"agent_role": role, "text": item} for item in data["blockers"])
        conflicts.extend({"agent_role": role, "text": item} for item in data["conflicts"])
        recommendations.extend({"agent_role": role, "text": item} for item in data["recommendations"])
    return {
        "schema_version": 1,
        "reports": reports,
        "evidence_status_counts": dict(sorted(statuses.items())),
        "duplicate_evidence_ids": duplicates,
        "blockers": blockers,
        "conflicts": conflicts,
        "recommendations": recommendations,
    }


def markdown(data: dict) -> str:
    boards = sorted({r["board_id"] for r in data["reports"]})
    lines = [f"# {', '.join(boards) or 'Board'} consolidated research", "", "## Inputs", ""]
    for report in data["reports"]:
        lines.append(f"- `{report['agent_role']}`: `{report['path']}` ({report['evidence_count']} records)")
    lines.extend(["", "## Evidence status", ""])
    for status, count in data["evidence_status_counts"].items():
        lines.append(f"- `{status}`: {count}")
    for heading, key in (("Duplicate IDs", "duplicate_evidence_ids"), ("Blockers", "blockers"), ("Conflicts", "conflicts"), ("Recommendations", "recommendations")):
        lines.extend(["", f"## {heading}", ""])
        items = data[key]
        if not items:
            lines.append("- None")
        else:
            for item in items:
                if key == "duplicate_evidence_ids":
                    lines.append(f"- `{item['id']}`: {item['first_role']} / {item['second_role']}")
                else:
                    lines.append(f"- **{item['agent_role']}** — {item['text']}")
    lines.extend([
        "",
        "## Integration rule",
        "",
        "This report aggregates evidence; it does not promote inferred values or resolve conflicts. Update the board manifest only after primary-agent review.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = consolidate(args.reports)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(markdown(data), encoding="utf-8")
    print(f"reports={len(data['reports'])} evidence={sum(data['evidence_status_counts'].values())} conflicts={len(data['conflicts'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
