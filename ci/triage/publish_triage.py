#!/usr/bin/env python3
"""Publish advisory triage for an existing portal run."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import urllib.request
from pathlib import Path


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
                "triage_evidence": evidence.get("extracted", {}),
                "ai_status": item.get("ai_status", ""),
                "ai_model": summary.get("ai_model", ""),
                "ai_analysis": (
                    ai_path.read_text(encoding="utf-8") if ai_path.is_file() else ""
                ),
            }
        )
    return {"job_name": job_name, "build_number": build_number, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--build-number", type=int, required=True)
    parser.add_argument("--portal-url", default="https://apollo/portal/")
    parser.add_argument("--token", default=os.environ.get("PORTAL_INGEST_TOKEN", ""))
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--output", type=Path)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
