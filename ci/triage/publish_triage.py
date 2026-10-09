#!/usr/bin/env python3
"""Publish advisory triage for an existing portal run, then learn from people's edits.

After the triage is published, the portal's person-edited Verdicts and root causes for the
same board are pulled back into the triage memory, so the next run trusts them over the AI.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from .memory import TriageMemory
except ImportError:  # run as a script from ci/triage
    from memory import TriageMemory  # type: ignore[no-redef]


def build_payload(run_root: Path, job_name: str, build_number: int) -> dict:
    triage_root = run_root.resolve() / "triage"
    summary = json.loads((triage_root / "summary.json").read_text(encoding="utf-8"))
    results = []
    for item in summary.get("results", []):
        case_root = run_root / str(item["report_dir"])
        evidence = json.loads((case_root / "evidence.json").read_text(encoding="utf-8"))
        ai_path = case_root / "ai_analysis.md"
        results.append(
            {
                "name": item["case"],
                "triage_category": evidence.get("deterministic_category", ""),
                "triage_owner": evidence.get("deterministic_owner", ""),
                "triage_explanation": evidence.get("deterministic_explanation", ""),
                "triage_evidence": {
                    k: v for k, v in evidence.get("extracted", {}).items() if k != "evidence_lines"
                },
                "triage_signature": item.get("signature", ""),
                "analysis_source": item.get("analysis_source", ""),
                "verdict": item.get("verdict", ""),
                "ai_status": item.get("ai_status", ""),
                "ai_model": summary.get("ai_model", ""),
                "ai_analysis": (
                    ai_path.read_text(encoding="utf-8") if ai_path.is_file() else ""
                ),
            }
        )
    return {"job_name": job_name, "build_number": build_number, "results": results}


def learn_from_portal(base_url: str, token: str, context, job_name: str, board: str,
                      memory_path: str | None) -> int:
    """Store person-edited Verdicts/root causes for this job's board in the triage memory."""
    query = urllib.parse.urlencode({"job_name": job_name})
    request = urllib.request.Request(
        base_url.rstrip("/") + f"/api/v1/triage/feedback/?{query}",
        headers={"X-Portal-Token": token},
    )
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        items = json.loads(response.read().decode("utf-8")).get("feedback", [])
    memory = TriageMemory(memory_path)
    learned = 0
    for item in items:
        if not item.get("signature") or not (item.get("verdict") or item.get("root_cause")):
            continue
        memory.remember(
            board, item["signature"], item["test"], "person", item.get("category", ""),
            {"verdict": item.get("verdict", ""), "root_cause": item.get("root_cause", ""),
             "confidence": "high", "confirmed_by": item.get("updated_by", "")},
            str(item.get("run", "")),
        )
        learned += 1
    memory.close()
    return learned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--build-number", type=int, required=True)
    parser.add_argument("--portal-url", default="https://apollo/portal/")
    parser.add_argument("--token", default=os.environ.get("PORTAL_INGEST_TOKEN", ""))
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--memory", default=os.environ.get("TRIAGE_MEMORY_DB", ""),
                        help="triage memory to update with person-edited analyses")
    args = parser.parse_args()
    if not args.token:
        parser.error("portal token is required via --token or PORTAL_INGEST_TOKEN")

    payload = build_payload(args.run_root, args.job_name, args.build_number)
    request = urllib.request.Request(
        args.portal_url.rstrip("/") + "/api/v1/triage/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Portal-Token": args.token},
        method="POST",
    )
    context = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file else None
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        result = json.loads(response.read().decode("utf-8"))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    try:
        board = json.loads((args.run_root / "triage" / "summary.json").read_text())["board"]
        learned = learn_from_portal(args.portal_url, args.token, context, args.job_name, board,
                                    args.memory or None)
        print(f"Triage memory: learned {learned} person-confirmed analyses from the portal")
    except Exception as exc:  # learning is best effort; the triage is already published
        print(f"Triage memory: could not read portal feedback ({exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
