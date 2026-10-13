#!/usr/bin/env python3
"""Tell the portal what a Run ELF build is doing, before or without a published result.

    report_submission_status.py --submission-id UUID --status RUNNING|ERROR|ABORTED \
        --build-url URL --portal-url URL [--ca-file FILE] [--reason TEXT]

The job calls it when it starts (RUNNING, with its Jenkins build URL, so the portal links the
submission to the build at once) and, if it ends without publishing a result, with the final
status. Published results update the submission themselves. Advisory: a failure here is
reported and ignored.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--status", required=True, choices=["RUNNING", "ERROR", "ABORTED"])
    parser.add_argument("--build-url", default="")
    parser.add_argument("--portal-url", required=True)
    parser.add_argument("--ca-file", default=None)
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    token = os.environ.get("PORTAL_INGEST_TOKEN", "")
    if not args.submission_id or not token:
        print("Submission status not reported: no submission id or portal token", file=sys.stderr)
        return 0
    body = json.dumps({"status": args.status, "build_url": args.build_url,
                       "reason": args.reason[:500]}).encode()
    request = urllib.request.Request(
        args.portal_url.rstrip("/") + f"/api/v1/elf/{args.submission_id}/status/",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Portal-Token": token},
    )
    context = ssl.create_default_context(cafile=args.ca_file) if args.ca_file else None
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            print(f"Submission {args.submission_id}: {args.status} reported ({response.status})")
    except (urllib.error.URLError, OSError) as exc:
        print(f"Submission status not reported: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
