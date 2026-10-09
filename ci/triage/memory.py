"""Triage memory: analyses already made, so a recurring failure costs no new AI call.

One SQLite file shared by every job on the agent (TRIAGE_MEMORY_DB, default
/data/ci/agent/triage-memory/triage.sqlite). A row is one analysis of one failure
signature (see agent.failure_signature) for one test on one board, from one source:

  person  a Verdict or root cause someone entered in the portal (most trusted)
  rule    a deterministic board-knowledge or value-diff explanation
  ai      an AI answer

Lookup prefers the same test, then the same signature in another test of the same board,
and within each, person > rule > ai.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_DB = "/data/ci/agent/triage-memory/triage.sqlite"
SOURCE_RANK = {"person": 0, "rule": 1, "ai": 2}

SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    board TEXT NOT NULL,
    signature TEXT NOT NULL,
    test TEXT NOT NULL,
    source TEXT NOT NULL,
    category TEXT,
    analysis TEXT NOT NULL,          -- JSON: failure_reason, root_cause, confidence, next_step, verdict
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    last_run TEXT,
    PRIMARY KEY (board, signature, test, source)
);
CREATE INDEX IF NOT EXISTS analyses_signature ON analyses (board, signature);
CREATE INDEX IF NOT EXISTS analyses_category ON analyses (board, category);
"""


class TriageMemory:
    def __init__(self, path: str | Path | None = None):
        target = Path(path or os.environ.get("TRIAGE_MEMORY_DB") or DEFAULT_DB)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(target)
            self.path = str(target)
        except (OSError, sqlite3.Error):
            # Not writable here (e.g. a developer machine): remember for this run only.
            self.conn = sqlite3.connect(":memory:")
            self.path = ":memory:"
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def lookup(self, board: str, signature: str, test: str) -> dict[str, Any] | None:
        """Best stored analysis: same test first, then the same signature in another test."""
        rows = self.conn.execute(
            "SELECT test, source, category, analysis, hits FROM analyses "
            "WHERE board = ? AND signature = ?",
            (board, signature),
        ).fetchall()
        if not rows:
            return None
        rows.sort(key=lambda r: (r[0] != test, SOURCE_RANK.get(r[1], 9), -r[4]))
        found_test = rows[0][0]
        # Merge that test's rows: a person's verdict or text overrides rule and AI fields.
        merged: dict[str, Any] = {}
        sources = []
        category = None
        for row_test, source, row_category, analysis, _hits in sorted(
            (r for r in rows if r[0] == found_test), key=lambda r: -SOURCE_RANK.get(r[1], 9)
        ):
            merged.update(json.loads(analysis))
            sources.append(source)
            category = category or row_category
        best = min(sources, key=lambda s: SOURCE_RANK.get(s, 9))
        self.conn.execute(
            "UPDATE analyses SET hits = hits + 1, last_seen = ? "
            "WHERE board = ? AND signature = ? AND test = ?",
            (time.time(), board, signature, found_test),
        )
        return {
            "source": best,
            "category": category,
            "match": "same_test" if found_test == test else "same_signature",
            "matched_test": found_test,
            **merged,
        }

    def related(self, board: str, category: str, exclude_signature: str, limit: int = 3) -> list[str]:
        """Short past findings for the same failure category: context for the AI, not an answer."""
        rows = self.conn.execute(
            "SELECT test, source, analysis FROM analyses WHERE board = ? AND category = ? "
            "AND signature != ? ORDER BY CASE source WHEN 'person' THEN 0 WHEN 'rule' THEN 1 "
            "ELSE 2 END, hits DESC, last_seen DESC LIMIT ?",
            (board, category, exclude_signature, limit),
        ).fetchall()
        out = []
        for test, source, analysis in rows:
            root = str(json.loads(analysis).get("root_cause", ""))[:220]
            if root:
                out.append(f"[{source}] {test}: {root}")
        return out

    def remember(self, board: str, signature: str, test: str, source: str, category: str,
                 analysis: dict[str, Any], run: str = "") -> None:
        now = time.time()
        keep = {k: analysis[k] for k in
                ("failure_reason", "root_cause", "confidence", "next_step", "verdict", "confirmed_by")
                if analysis.get(k)}
        self.conn.execute(
            "INSERT INTO analyses (board, signature, test, source, category, analysis, "
            "first_seen, last_seen, last_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (board, signature, test, source) DO UPDATE SET "
            "category = excluded.category, analysis = excluded.analysis, "
            "last_seen = excluded.last_seen, last_run = excluded.last_run",
            (board, signature, test, source, category, json.dumps(keep), now, now, run),
        )

    def stats(self) -> dict[str, int]:
        return {
            source: count
            for source, count in self.conn.execute(
                "SELECT source, COUNT(*) FROM analyses GROUP BY source"
            )
        }
