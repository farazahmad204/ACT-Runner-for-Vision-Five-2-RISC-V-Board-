#!/usr/bin/env python3
"""Historical memory of riscv-arch-test issues for the triage agent.

Sources, merged into one structured record per issue (HistoricalIssue):
  - knowledge/act_issues.json.gz: every open and closed riscv-arch-test issue (scraped by
    tools/build_issue_index.py): title, state, close reason, linked PRs, named tests, and the
    opening report plus the final comment;
  - the team's Obsidian vault (vault.py): curated notes for issues the team reported, with the
    confirmed cause (framework-bug, simulator-bug, ...), resolution, fix and summary. A curated
    note wins over the scraped text for the same issue.

symptom_pattern is the opening report (or the curated summary); root_cause_or_workaround is
the resolution: the curated cause and fix, else the close reason, linked PRs and the final
comment, or "open" with the latest comment. Tags are derived from the text: extensions (Sv39,
Zaamo, H...), tools (Sail, Spike, UDB, testgen), instructions (amoswap, hlv, wfi...), CSRs, and
named tests.

tool_query_historical_issues(failure_keywords, hardware_tags) returns the 2-3 most relevant
issues. There is no embedding model on the CI agent, so ranking is lexical: TF-IDF over the
issue text, plus tag overlap, the same test, curation, and a lower weight for issues from
before ACT 4 (2025).

    python3 -m ci.triage.agentic.issue_memory_vault query "hlv guest page fault htinst" --tag H --tag Sv39
    python3 -m ci.triage.agentic.issue_memory_vault export issue_memory.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from ci.triage import vault  # noqa: E402

INDEX = Path(__file__).resolve().parents[1] / "knowledge" / "act_issues.json.gz"
REPO_URL = "https://github.com/riscv/riscv-arch-test/issues/"
EXTENSION = re.compile(r"\b(Sv\d+(?:x4)?|Svbare|Svade|Svadu|Svnapot|Svpbmt|Svinval|Sm|Ss[a-z]+|Sh[a-z]+|Sm[a-z]+|"
                       r"Z[a-z]{2,}[a-z0-9]*|H(?=[\s,:;)\-]|$)|RV(?:32|64)[A-Z]+)\b")
TOOL = re.compile(r"\b(sail|spike|udb|testgen|riscof|rvmodel|cocotb|qemu|isac|ctg)\b", re.I)
INSTRUCTION = re.compile(r"\b(amo[a-z]+(?:\.[wd])?|lr\.[wd]|sc\.[wd]|hlvx?\.[a-z]+|hsv\.[a-z]+|hfence\.[a-z]+|"
                         r"sfence\.vma|fence\.i|ecall|ebreak|wfi|mret|sret|csrr[sciw]i?|c\.[a-z]+)\b", re.I)
CSR = re.compile(r"\b(m|s|h|vs)(status|cause|tval|epc|tvec|ie|ip|deleg|ideleg|edeleg|atp|envcfg|counteren|"
                 r"tinst|tval2|scratch|vip|geie|geip|timedelta)\b", re.I)
WORD = re.compile(r"[a-z][a-z0-9_.]{2,}")
STOP = set("the and for with that this from are was were has have not but you can our its into when then than "
           "which what will would should could does did any all one two also there their test tests issue issues "
           "file files run running using use used get got see seems like just only some more most".split())


@dataclass
class HistoricalIssue:
    issue_number: int
    status: str  # "OPEN" | "CLOSED"
    title: str
    tags: list[str]
    symptom_pattern: str
    root_cause_or_workaround: str
    url: str = ""
    cause: str = ""  # curated: framework-bug, simulator-bug, test-limitation, ... ("" if not curated)
    curated: bool = False
    tests: list[str] = field(default_factory=list)
    created: str = ""


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(str(value).replace("'", '"'))
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def tags_of(text: str, tests: list[str] = ()) -> list[str]:
    found = {m.group(1) for m in EXTENSION.finditer(text)}
    found |= {m.group(1).capitalize() if m.group(1).lower() in ("sail", "spike") else m.group(1).upper()
              if m.group(1).lower() == "udb" else m.group(1).lower() for m in TOOL.finditer(text)}
    found |= {m.group(1).lower() for m in INSTRUCTION.finditer(text)}
    found |= {m.group(0).lower() for m in CSR.finditer(text)}
    found |= set(tests)
    return sorted(found)


def _excerpt(text: str, limit: int, tail: bool = False) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return ("… " + text[-limit:]) if tail else (text[:limit].rsplit(" ", 1)[0] + " …")


def from_scraped(item: dict[str, Any]) -> HistoricalIssue:
    text = str(item.get("text") or "")
    open_ = str(item.get("state")) == "open"
    prs = _list(item.get("prs"))
    tests = _list(item.get("tests"))
    if open_:
        resolution = "OPEN: no fix merged. Latest discussion: " + _excerpt(text, 300, tail=True)
    else:
        resolution = (f"CLOSED ({item.get('reason') or 'completed'})"
                      + (f"; PRs {', '.join('#' + p for p in prs[:4])}" if prs else "")
                      + ". Final discussion: " + _excerpt(text, 300, tail=True))
    number = int(item["n"])
    return HistoricalIssue(issue_number=number, status="OPEN" if open_ else "CLOSED", title=str(item.get("title")),
                           tags=tags_of(f"{item.get('title')} {text}", tests), symptom_pattern=_excerpt(text, 400),
                           root_cause_or_workaround=resolution, url=REPO_URL + str(number), tests=tests,
                           created=str(item.get("created") or ""))


def apply_note(issue: HistoricalIssue | None, note: dict[str, Any]) -> HistoricalIssue:
    """A curated vault note: its summary is the symptom, its cause/resolution/fix the root cause."""
    sections = note.get("_sections") or {}
    if isinstance(sections, str):
        sections = {}
    number = int(note.get("number") or re.sub(r"\D", "", str(note.get("id"))) or 0)
    summary = " ".join(str(sections.get("summary") or note.get("title") or "").split())
    fix = "; ".join(str(x) for x in (note.get("cause") and f"cause: {note['cause']}",
                                      note.get("resolution") and f"resolution: {note['resolution']}",
                                      note.get("fixed_in") and f"fixed in: {note['fixed_in']}",
                                      sections.get("workaround") and f"workaround: {sections['workaround']}")
                    if x)
    tests = _list(note.get("tests"))
    base = issue or HistoricalIssue(number, "OPEN", str(note.get("title") or ""), [], "", "",
                                    url=str(note.get("url") or REPO_URL + str(number)))
    state = str(note.get("state") or base.status).upper()
    return HistoricalIssue(
        issue_number=number, status="OPEN" if state.startswith("OPEN") else "CLOSED",
        title=str(note.get("title") or base.title),
        tags=sorted(set(base.tags) | set(tags_of(f"{note.get('title')} {summary}", tests)) | set(_list(note.get("signals")))),
        symptom_pattern=summary or base.symptom_pattern,
        root_cause_or_workaround=fix or base.root_cause_or_workaround,
        url=str(note.get("url") or base.url), cause=str(note.get("cause") or ""), curated=True,
        tests=sorted(set(base.tests) | set(tests)), created=base.created)


@lru_cache(maxsize=1)
def load_issues() -> tuple[HistoricalIssue, ...]:
    """Every issue, scraped and curated, one record each."""
    issues: dict[int, HistoricalIssue] = {}
    if INDEX.is_file():
        for item in json.load(gzip.open(INDEX)).get("issues", []):
            try:
                issue = from_scraped(item)
            except (KeyError, ValueError):
                continue
            issues[issue.issue_number] = issue
    for note in vault.load_notes():
        # Only riscv-arch-test issues (ACT-<number>); the vault also holds board and Sail notes.
        found = re.fullmatch(r"ACT-(\d+)", str(note.get("id") or ""))
        if not found or str(note.get("curated")) == "False":
            continue
        number = int(found.group(1))
        issues[number] = apply_note(issues.get(number), note)
    return tuple(sorted(issues.values(), key=lambda i: -i.issue_number))


def _words(issue: HistoricalIssue) -> Counter:
    text = f"{issue.title} {issue.title} {issue.symptom_pattern} {issue.root_cause_or_workaround} {' '.join(issue.tags)}"
    return Counter(w for w in WORD.findall(text.lower()) if w not in STOP)


@lru_cache(maxsize=1)
def _index() -> tuple[list[Counter], dict[str, float]]:
    docs = [_words(i) for i in load_issues()]
    df = Counter(w for d in docs for w in d)
    n = max(len(docs), 1)
    return docs, {w: math.log(n / (1 + c)) + 1 for w, c in df.items()}


def tool_query_historical_issues(failure_keywords: str, hardware_tags: list[str] = (), limit: int = 3,
                                 test: str = "") -> list[dict[str, Any]]:
    """The issues most like this failure: TF-IDF of the keywords over the issue text, plus
    matching tags (extensions, tools, instructions, CSRs), the same test, curated notes; issues
    from before ACT 4 count half. Weak matches are left out."""
    issues, (docs, idf) = load_issues(), _index()
    words = [w for w in WORD.findall(failure_keywords.lower()) if w not in STOP]
    wanted = {t.lower() for t in hardware_tags or []}
    scored = []
    for issue, doc in zip(issues, docs):
        hits = [w for w in dict.fromkeys(words) if doc.get(w)]
        rare = [w for w in hits if idf.get(w, 0) > 3.0]  # words most issues do not use
        score = sum((1 + math.log(doc[w])) * idf.get(w, 1.0) for w in hits)
        tag_hits = sorted({t for t in issue.tags if t.lower() in wanted})
        score += 3.0 * len(tag_hits)
        same_test = bool(test) and any(test.lower() == t.lower() or test.lower().startswith(t.lower().rstrip("*"))
                                       for t in issue.tests)
        score += 12.0 if same_test else 0.0
        score += 2.0 if issue.curated else 0.0
        if issue.created and issue.created < "2025-01-01":
            score *= 0.5
        if score >= 8.0 and (len(rare) >= 2 or (rare and tag_hits) or same_test):
            scored.append((score, issue, hits, tag_hits, same_test))
    scored.sort(key=lambda row: -row[0])
    out = []
    for score, issue, hits, tag_hits, same_test in scored[:limit]:
        record = asdict(issue)
        record.update(score=round(score, 1), matched_terms=hits[:8], matched_tags=tag_hits, same_test=same_test)
        out.append(record)
    return out


def export(path: Path) -> int:
    """Write the ingestion format (a JSON list of HistoricalIssue) for inspection or reuse."""
    rows = [asdict(i) for i in load_issues()]
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=1)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    query = sub.add_parser("query")
    query.add_argument("keywords")
    query.add_argument("--tag", action="append", default=[])
    query.add_argument("--test", default="")
    out = sub.add_parser("export")
    out.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "query":
        print(json.dumps(tool_query_historical_issues(args.keywords, args.tag, test=args.test), indent=1))
    else:
        print(f"{export(args.path)} issues written to {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
