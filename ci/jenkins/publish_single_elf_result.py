#!/usr/bin/env python3
"""Publish one portal-uploaded UART ELF result to the CI portal."""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import ssl
import urllib.parse
import urllib.request
from pathlib import Path


BOARDS = {
    "visionfive2": ("VisionFive 2", "SiFive U74"),
    "bananapi-f3": ("Banana Pi BPI-F3", "SpacemiT K1/X60"),
    "milkv-megrez": ("Milk-V Megrez", "ESWIN EIC7700X/SiFive P550"),
}


def uart_content(path: Path) -> bytes:
    content = path.read_bytes()
    limit = 2 * 1024 * 1024
    if len(content) > limit:
        marker = b"[portal upload truncated to final 2 MiB]\n"
        content = marker + content[-(limit - len(marker)) :]
    return content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--board", choices=sorted(BOARDS), required=True)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--elf-name", required=True)
    parser.add_argument("--elf-sha256", required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--build-number", type=int, required=True)
    parser.add_argument("--build-url", required=True)
    parser.add_argument("--portal-url", required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--git-revision", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-post", action="store_true", help="Write payload without posting it.")
    args = parser.parse_args()

    summary_path = args.run_root / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {}
    result = summary.get("results", [{}])[0] if summary.get("results") else {}
    hardware_status = result.get("status", "FAIL")
    if hardware_status not in {"PASS", "FAIL"}:
        hardware_status = "FAIL"
    uart_path = Path(result.get("uart_log", "")) if result.get("uart_log") else None
    item = {
        "name": args.elf_name,
        "category": "Single ELF",
        "extension": "Uploaded",
        "sail_status": "SKIPPED",
        "spike_status": "SKIPPED",
        "hardware_status": hardware_status,
        "duration_seconds": result.get("elapsed_seconds"),
        "failure_reason": "" if hardware_status == "PASS" else "Target or UART transport failed",
    }
    if uart_path and uart_path.is_file():
        item["uart_log_gzip_b64"] = base64.b64encode(
            gzip.compress(uart_content(uart_path))
        ).decode("ascii")

    board_name, core_profile = BOARDS[args.board]
    payload = {
        "board": {"slug": args.board, "name": board_name, "core_profile": core_profile},
        "job": {
            "name": f"{args.job_name}-{args.board}",
            "jenkins_url": args.build_url,
        },
        "build_number": args.build_number,
        "status": hardware_status,
        "expected_cases": 1,
        "completed_cases": 1,
        "passed_cases": 1 if hardware_status == "PASS" else 0,
        "failed_cases": 0 if hardware_status == "PASS" else 1,
        "git_revision": args.git_revision,
        "parameters": {"TARGET_BOARD": args.board, "ELF_NAME": args.elf_name},
        "metadata": {
            "submission_id": args.submission_id,
            "elf_sha256": args.elf_sha256,
            "transport": "uart-stream",
        },
        "results": [item],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    if args.no_post:
        print(json.dumps({"output": str(args.output), "posted": False}))
        return 0

    token = os.environ.get("PORTAL_INGEST_TOKEN", "")
    if not token:
        raise SystemExit("PORTAL_INGEST_TOKEN is required")
    endpoint = urllib.parse.urljoin(args.portal_url.rstrip("/") + "/", "api/v1/runs/")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Portal-Token": token},
        method="POST",
    )
    context = ssl.create_default_context(cafile=str(args.ca_file))
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        print(response.read().decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
