#!/usr/bin/env python3
"""Keep the vault's issue notes in step with the team's issue sheet and GitHub.

    python3 ci/triage/tools/sync_issue_notes.py [--sheet-csv FILE] [--dry-run]

Reads every github.com/riscv/<repo>/issues/<n> URL from the sheet (default: the shared
"Reported ACT/Sail Issues" sheet's CSV export), asks the GitHub API for each issue's state,
and then:
  - updates `state` (and `updated`) in an existing note's frontmatter, keeping everything else;
  - creates a stub note for an issue that has none: `curated: false`, `auto: false`, the issue
    text as summary, and no tests/signals, so it never matches until someone curates it.
Uses the logged-in `gh` CLI when present; otherwise the API, with GITHUB_TOKEN (optional, sent
only to api.github.com) to raise the rate limit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vault import parse_note, vault_root  # noqa: E402

SHEET = ("https://docs.google.com/spreadsheets/d/1nHuRoDclKPpNVozJC5cAejAWQ_Emo8BD9yTtYsMKVOw"
         "/export?format=csv")
URL = re.compile(r"https://github\.com/riscv/(riscv-arch-test|sail-riscv)/issues/(\d+)")
PREFIX = {"riscv-arch-test": "ACT", "sail-riscv": "SAIL"}


def fetch(url: str, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def issue(repo: str, number: int) -> dict:
    if shutil.which("gh") and not os.environ.get("GITHUB_TOKEN"):  # logged-in gh: no rate limit
        done = subprocess.run(["gh", "api", f"repos/riscv/{repo}/issues/{number}"],
                              capture_output=True, text=True, timeout=60)
        if done.returncode == 0:
            return json.loads(done.stdout)
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return json.loads(fetch(f"https://api.github.com/repos/riscv/{repo}/issues/{number}", headers))


def write_note(path: Path, meta: dict, body: str) -> None:
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
                    + "---\n" + body, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--sheet-csv", type=Path, help="local CSV instead of the shared sheet")
    parser.add_argument("--vault", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    text = args.sheet_csv.read_text(encoding="utf-8") if args.sheet_csv else fetch(SHEET).decode()
    wanted = sorted({(m.group(1), int(m.group(2))) for m in URL.finditer(text)})
    folder = (args.vault or vault_root()) / "issues"
    folder.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    changed = skipped = 0
    for repo, number in wanted:
        note_id = f"{PREFIX[repo]}-{number}"
        path = folder / f"{note_id}.md"
        try:
            data = issue(repo, number)
        except (OSError, ValueError) as exc:  # rate limit, network: keep the note as it is
            print(f"{note_id}: skipped ({exc})")
            skipped += 1
            continue
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
            meta, _sections = parse_note(raw)
            if meta.get("state") == data["state"]:
                continue
            print(f"{note_id}: state {meta.get('state')} -> {data['state']}")
            meta.update(state=data["state"], updated=today)
            body = re.sub(r"^---\n.*?\n---\n?", "", raw, count=1, flags=re.S)
        else:
            print(f"{note_id}: new stub note (needs curating)")
            meta = {"id": note_id, "title": data["title"], "url": data["html_url"], "repo": repo,
                    "number": number, "state": data["state"], "resolution": "unknown",
                    "fixed_in": "", "cause": "unknown", "boards": [], "tests": [], "signals": [],
                    "auto": False, "verdict": "Needs investigation",
                    "reported_by": data["user"]["login"], "curated": False, "updated": today}
            summary = re.sub(r"\s+", " ", data.get("body") or "")[:1200]
            body = (f"\n# {note_id}: {data['title']}\n\n{summary}\n\n## What fails\n\nTODO\n\n"
                    "## What the maintainers concluded\n\nTODO: read the comments.\n\n"
                    "## How triage treats a match\n\nTODO: set boards, tests, signals, verdict.\n")
        changed += 1
        if not args.dry_run:
            write_note(path, meta, body)
    print(f"{len(wanted)} issues in the sheet, {changed} note(s) {'would change' if args.dry_run else 'changed'}, "
          f"{skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
