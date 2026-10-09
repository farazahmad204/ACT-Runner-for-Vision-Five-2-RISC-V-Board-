#!/usr/bin/env python3
"""Build the triage agent's index of riscv-arch-test issues (open and closed).

    python3 ci/triage/tools/build_issue_index.py --cache DIR          # scrape, then index
    python3 ci/triage/tools/build_issue_index.py --cache DIR --offline

Reads the github.com issue pages (the anonymous REST API allows only 60 calls an hour),
saving each issue as JSON under --cache so a rerun fetches only new issues; --refresh-open
re-fetches open issues to pick up new comments and closures. Then writes
ci/triage/knowledge/act_issues.json.gz: per issue its state, labels, linked PRs, the test
names it mentions and a condensed text (opening report plus the last comments, where the
maintainers usually give the conclusion). context.related_issues() ranks it per failure.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = "riscv/riscv-arch-test"
OUT = Path(__file__).resolve().parents[1] / "knowledge" / "act_issues.json.gz"
EMBED = re.compile(r'<script type="application/json" data-target="react-app.embeddedData">(.*?)</script>', re.S)
TEST_NAME = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*-\d{2})\b")


def get(url: str, tries: int = 6) -> str:
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ci-triage)"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - network: back off and retry
            time.sleep((30 if "429" in str(exc) else 5) * (attempt + 1))
    raise RuntimeError(f"cannot fetch {url}")


def listing() -> dict[int, str]:
    numbers: dict[int, str] = {}
    for state in ("open", "closed"):
        page = 1
        while True:
            html = get(f"https://github.com/{REPO}/issues?q=is%3Aissue+state%3A{state}&page={page}")
            for number in re.findall(rf'/{REPO}/issues/(\d+)"', html):
                numbers.setdefault(int(number), state)
            info = re.search(r'"pageInfo":\{"currentPage":\d+,"totalPages":(\d+)', html)
            if not info or page >= int(info.group(1)):
                break
            page += 1
            time.sleep(1.5)
    return numbers


def parse(html: str, number: int) -> dict | None:
    for match in EMBED.finditer(html):
        data = json.loads(match.group(1))
        issue = ((((data.get("payload") or {}).get("issueViewerRoute") or {}).get("data") or {})
                 .get("repository") or {}).get("issue")
        if not issue:
            continue
        events, seen = [], set()
        # Long threads show their first and last events; together they hold the conclusion.
        for key in ("timelineItems", "backTimelineItems"):
            for edge in (issue.get(key) or {}).get("edges", []):
                node = edge.get("node") or {}
                ident = (node.get("__typename"), node.get("createdAt"), (node.get("body") or "")[:40])
                if ident not in seen:
                    seen.add(ident)
                    events.append(node)
        events.sort(key=lambda n: n.get("createdAt") or "")
        labels = [(edge.get("node") or {}).get("name") for edge in
                  ((issue.get("labels") or {}).get("edges") or []) if isinstance(edge, dict)]
        return {
            "number": number, "title": issue.get("title") or "", "state": issue.get("state"),
            "state_reason": issue.get("stateReason"),
            "author": (issue.get("author") or {}).get("login"),
            "created": issue.get("createdAt"), "closed": issue.get("closedAt"),
            "labels": [label for label in labels if label], "body": issue.get("body") or "",
            "comments": [{"author": (n.get("author") or {}).get("login"), "at": n.get("createdAt"),
                          "body": n.get("body") or ""}
                         for n in events if n.get("__typename") == "IssueComment"],
            "pull_refs": sorted({int(p) for p in re.findall(r"/pull/(\d+)", json.dumps(events))}),
        }
    return None


def scrape(cache: Path, refresh_open: bool) -> None:
    (cache / "issues").mkdir(parents=True, exist_ok=True)
    numbers = listing()
    (cache / "numbers.json").write_text(json.dumps(numbers))
    for number, state in sorted(numbers.items()):
        path = cache / "issues" / f"{number}.json"
        if path.is_file():
            saved = json.loads(path.read_text())
            if saved.get("state", "").lower() == state and not (refresh_open and state == "open"):
                continue
        data = parse(get(f"https://github.com/{REPO}/issues/{number}"), number)
        if data:
            path.write_text(json.dumps(data))
            print(f"#{number} {data['state']} {data['title'][:70]}", flush=True)
        time.sleep(1.2)


def clean(text: str) -> str:
    text = re.sub(r"```.*?```", " [code] ", text, flags=re.S)           # logs and dumps
    text = re.sub(r"!?\[[^\]]*\]\(https?://[^)]*\)", " ", text)         # attachments, images
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def condensed(issue: dict) -> str:
    parts = [clean(issue["body"])[:1400]]
    for comment in issue["comments"][-4:]:
        parts.append(f"[{comment['author']}] {clean(comment['body'])[:450]}")
    return " | ".join(p for p in parts if p)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="index the cache without fetching")
    parser.add_argument("--refresh-open", action="store_true")
    args = parser.parse_args()
    if not args.offline:
        scrape(args.cache, args.refresh_open)
    entries = []
    for path in sorted((args.cache / "issues").glob("*.json"), key=lambda p: int(p.stem)):
        issue = json.loads(path.read_text())
        everything = " ".join([issue["title"], issue["body"]] + [c["body"] for c in issue["comments"]])
        entries.append({
            "n": issue["number"], "title": issue["title"], "state": (issue["state"] or "").lower(),
            "reason": (issue.get("state_reason") or "").lower(), "labels": issue["labels"],
            "created": (issue.get("created") or "")[:10], "closed": (issue.get("closed") or "")[:10],
            "prs": issue["pull_refs"][:8], "tests": sorted(set(TEST_NAME.findall(everything)))[:20],
            "text": condensed(issue),
        })
    payload = {"source": f"https://github.com/{REPO}/issues", "built": datetime.now(timezone.utc)
               .strftime("%Y-%m-%d"), "issues": entries}
    with gzip.open(OUT, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
    print(f"{len(entries)} issues -> {OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
