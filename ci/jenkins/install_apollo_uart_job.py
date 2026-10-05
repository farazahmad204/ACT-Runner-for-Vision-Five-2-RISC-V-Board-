#!/usr/bin/env python3
"""Create or update a VF2 or BPI-F3 UART job on Apollo Jenkins."""

from __future__ import annotations

import argparse
import base64
import getpass
import html
import http.cookiejar
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


JOB_CONFIGS = {
    "vf2-uart-sanity": (
        "Jenkinsfile.uart-sanity",
        "ci/jenkins/job-config-uart-sanity.xml",
    ),
    "vf2-uart-weekly": (
        "Jenkinsfile.uart-weekly",
        "ci/jenkins/job-config-uart-weekly.xml",
    ),
    "bpif3-uart-sanity": (
        "Jenkinsfile.bpif3-uart-sanity",
        "ci/jenkins/job-config-bpif3-uart-sanity.xml",
    ),
    "bpif3-uart-weekly": (
        "Jenkinsfile.bpif3-uart-weekly",
        "ci/jenkins/job-config-bpif3-uart-weekly.xml",
    ),
    "megrez-uart-sanity": (
        "Jenkinsfile.megrez-uart-sanity",
        "ci/jenkins/job-config-megrez-uart-sanity.xml",
    ),
    "riscv-uart-single-elf": (
        "Jenkinsfile.uart-single-elf",
        "ci/jenkins/job-config-uart-single-elf.xml",
    ),
    "riscv-uart-triage": (
        "Jenkinsfile.uart-triage",
        "ci/jenkins/job-config-uart-triage.xml",
    ),
}


def request(
    opener: urllib.request.OpenerDirector,
    url: str,
    authorization: str,
    context: ssl.SSLContext,
    *,
    data: bytes | None = None,
    content_type: str | None = None,
    crumb: tuple[str, str] | None = None,
) -> tuple[int, bytes]:
    headers = {"Authorization": authorization}
    if content_type:
        headers["Content-Type"] = content_type
    if crumb:
        headers[crumb[0]] = crumb[1]
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )
    try:
        with opener.open(req, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return exc.code, exc.read()
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Jenkins returned HTTP {exc.code}: {detail[:500]}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://192.168.100.150/")
    parser.add_argument("--user", required=True)
    parser.add_argument("--job", choices=sorted(JOB_CONFIGS), default="vf2-uart-sanity")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--ca-file", type=Path, default=Path("/home/lpt-10xe/jenkins-agent/jenkins-internal-ca.crt")
    )
    args = parser.parse_args()

    token = getpass.getpass("Apollo Jenkins API token: ").strip()
    if not token:
        raise ValueError("An API token is required")
    encoded = base64.b64encode(f"{args.user}:{token}".encode()).decode("ascii")
    authorization = f"Basic {encoded}"
    context = ssl.create_default_context(cafile=str(args.ca_file.resolve()))
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    base = args.url.rstrip("/")

    _, who_raw = request(opener, f"{base}/whoAmI/api/json", authorization, context)
    who = json.loads(who_raw)
    if not who.get("authenticated"):
        raise RuntimeError("Apollo did not authenticate the supplied Jenkins account")

    _, crumb_raw = request(opener, f"{base}/crumbIssuer/api/json", authorization, context)
    crumb_payload = json.loads(crumb_raw)
    crumb = (str(crumb_payload["crumbRequestField"]), str(crumb_payload["crumb"]))

    repo_root = args.repo_root.resolve()
    pipeline_path, template_path = JOB_CONFIGS[args.job]
    pipeline = (repo_root / pipeline_path).read_text(encoding="utf-8")
    template = (repo_root / template_path).read_text(encoding="utf-8")
    rendered = template.replace("__JENKINSFILE__", html.escape(pipeline))
    ET.fromstring(rendered)
    config = rendered.encode("utf-8")

    quoted_job = urllib.parse.quote(args.job, safe="")
    status, _ = request(
        opener, f"{base}/job/{quoted_job}/api/json", authorization, context
    )
    if status == 404:
        endpoint = f"{base}/createItem?name={urllib.parse.quote(args.job, safe='')}"
        action = "created"
    else:
        endpoint = f"{base}/job/{quoted_job}/config.xml"
        action = "updated"
    request(
        opener,
        endpoint,
        authorization,
        context,
        data=config,
        content_type="application/xml",
        crumb=crumb,
    )

    _, job_raw = request(
        opener, f"{base}/job/{quoted_job}/api/json", authorization, context
    )
    job = json.loads(job_raw)
    print(f"Apollo Jenkins job {action}: {job.get('url', base + '/job/' + quoted_job + '/')}")
    print("Agent label: riscv-hw-agent")
    print("Portal credential: riscv-portal-ingest-token")
    if args.job in {"vf2-uart-weekly", "bpif3-uart-weekly", "riscv-uart-triage"}:
        print("Optional AI credential: openai-api-key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
