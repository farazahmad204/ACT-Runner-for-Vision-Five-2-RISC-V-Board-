"""The triage agent's long-term knowledge: an Obsidian vault of Markdown notes.

  obsidian-vault/issues/          one note per reported ACT/Sail issue (sync_issue_notes.py)
  obsidian-vault/verifications/   one note per Verification Agent experiment

Each note has YAML frontmatter saying when it applies:

  boards:  [vf2_jh7110]        board ids; empty = any board
  tests:   ["Sstvecd*"]        case-name globs (case-insensitive); empty = any test
  signals: ["mcause=0x1"]      lowercase substrings that must all appear in the evidence
  auto:    true                a match explains the failure without AI
  verdict, cause, resolution, state, url

A note with neither tests nor signals (it would match everything) or with `curated: false`
(a stub from sync_issue_notes.py) never matches. A matching
`auto` note that names the board and the test explains the failure (0 tokens); any other
match is given to the AI as context. The vault lives in the runner repository so people can
edit it in Obsidian and review changes like code; TRIAGE_VAULT points elsewhere.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_VAULT = Path(__file__).resolve().parents[2] / "obsidian-vault"
NOTE_DIRS = ("issues", "verifications")


def vault_root() -> Path:
    return Path(os.environ.get("TRIAGE_VAULT") or DEFAULT_VAULT)


def parse_note(text: str) -> tuple[dict[str, Any], dict[str, str]]:
    """(frontmatter, {section heading: text}) of one note."""
    meta: dict[str, Any] = {}
    body = text
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if match:
        meta = yaml.safe_load(match.group(1)) or {}
        body = match.group(2)
    sections: dict[str, str] = {}
    heading = "summary"
    for line in body.splitlines():
        if line.startswith("# "):
            continue
        found = re.match(r"^##\s+(.*)$", line)
        if found:
            heading = found.group(1).strip().lower()
            continue
        sections[heading] = (sections.get(heading, "") + "\n" + line).strip()
    return meta, sections


@lru_cache(maxsize=4)
def _load(root: str) -> tuple[dict[str, Any], ...]:
    notes = []
    for folder in NOTE_DIRS:
        for path in sorted((Path(root) / folder).glob("*.md")):
            try:
                meta, sections = parse_note(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(meta, dict):
                continue
            meta.setdefault("id", path.stem)
            meta["_sections"] = sections
            meta["_path"] = f"{folder}/{path.name}"
            notes.append(meta)
    return tuple(notes)


def load_notes(root: Path | None = None) -> list[dict[str, Any]]:
    return list(_load(str(root or vault_root())))


def evidence_text(evidence: dict[str, Any]) -> str:
    """What signals are matched against: the extracted failure fields, not the whole UART log
    (its signature dumps contain almost any value and CSR name)."""
    ex = {k: v for k, v in evidence.get("extracted", {}).items()
          if k not in {"trap_records", "evidence_lines"}}
    vd = evidence.get("value_diff") or {}
    return " ".join([json.dumps(ex, default=str), json.dumps(vd, default=str),
                     str(evidence.get("deterministic_explanation", ""))]).lower()


def match(notes: list[dict[str, Any]], board: str, test: str, text: str) -> list[dict[str, Any]]:
    """Notes that apply to this failure, most specific first."""
    found = []
    for note in notes:
        boards = [str(b) for b in note.get("boards") or []]
        tests = [str(t) for t in note.get("tests") or []]
        signals = [str(s).lower() for s in note.get("signals") or []]
        if (not tests and not signals) or note.get("curated") is False:
            continue
        if boards and board not in boards:
            continue
        if tests and not any(fnmatch.fnmatch(test.lower(), t.lower()) for t in tests):
            continue
        if any(s not in text for s in signals):
            continue
        score = 3 * bool(tests) + 2 * bool(boards) + len(signals)
        found.append((score, note))
    found.sort(key=lambda item: -item[0])
    return [note for _score, note in found]


def resolves(note: dict[str, Any], board: str) -> bool:
    """A note may explain a failure without AI only when it names the board and the test."""
    return bool(note.get("auto")) and board in (note.get("boards") or []) and bool(note.get("tests"))


def link(note: dict[str, Any]) -> str:
    return f"[[{note['id']}]]" + (f" {note['url']}" if note.get("url") else "")


def analysis(note: dict[str, Any]) -> dict[str, Any]:
    s = note["_sections"]
    status = note.get("state") or "unknown"
    return {
        "category": f"known_issue:{note.get('cause', 'unknown')}",
        "failure_reason": (s.get("what fails") or s.get("summary") or note.get("title", ""))[:400],
        "root_cause": (f"Known issue {link(note)} ({status}, {note.get('resolution', 'unresolved')}): "
                       + (s.get("what the maintainers concluded") or s.get("summary", ""))[:500]),
        "confidence": "high",
        "next_step": (s.get("how triage treats a match") or "See the linked issue.")[:300],
        "verdict": str(note.get("verdict") or "Needs investigation"),
        "known_issue": note["id"],
    }


def context_lines(notes: list[dict[str, Any]], limit: int = 2) -> list[str]:
    """Short summaries of matching notes for the AI question."""
    lines = []
    for note in notes[:limit]:
        s = note["_sections"]
        lines.append(f"{note['id']} ({note.get('state', '?')}, {note.get('resolution', '?')}, "
                     f"cause {note.get('cause', '?')}): {s.get('summary', '')[:260]} "
                     f"Maintainers: {s.get('what the maintainers concluded', '')[:260]}")
    return lines
