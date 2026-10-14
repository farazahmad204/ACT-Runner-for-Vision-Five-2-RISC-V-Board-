#!/usr/bin/env python3
"""Run deterministic triage and the ReAct triage agent (ci/triage/agentic) for a UART result directory."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# A package import: the ReAct agent (ci/triage/agentic) uses relative imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ci.triage.ci_triage import AIConfig, run_triage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--blind", action="store_true",
                        help="no vault notes and no memory: for scoring with evaluate.py")
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--ai", action="store_true", help="enable advisory AI analysis")
    parser.add_argument(
        "--provider",
        choices=["anthropic", "codex", "openai"],
        default=os.environ.get("AI_TRIAGE_PROVIDER") or "codex",
        help="codex: the Codex CLI logged in with a ChatGPT account; openai / anthropic: API key and SDK",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("AI_TRIAGE_MODEL", ""),
        help="model name; blank uses the provider's default",
    )
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument(
        "--max-ai-failures",
        type=int,
        default=0,
        help="maximum AI-analyzed failures; 0 analyzes every failure",
    )
    parser.add_argument("--max-steps", type=int, default=int(os.environ.get("AI_TRIAGE_MAX_STEPS") or "12"),
                        help="tool calls the ReAct agent may make per failure signature")
    parser.add_argument(
        "--memory",
        default=os.environ.get("TRIAGE_MEMORY_DB", ""),
        help="triage memory SQLite file (default /data/ci/agent/triage-memory/triage.sqlite)",
    )
    parser.add_argument(
        "--refresh-memory",
        action="store_true",
        help="ignore stored analyses and analyze every failure again (results are still stored)",
    )
    args = parser.parse_args()
    if args.max_ai_failures < 0:
        parser.error("--max-ai-failures must be non-negative")

    summary = run_triage(
        args.run_root,
        args.state_root,
        args.board,
        ai_enabled=args.ai,
        ai_config=AIConfig(
            model=args.model,
            provider=args.provider,
            codex_bin=args.codex_bin,
            max_steps=args.max_steps,
        ),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        max_ai_failures=args.max_ai_failures,
        memory_path=args.memory or None,
        refresh_memory=args.refresh_memory,
        blind=args.blind,
    )
    print(
        "ACT CI triage: "
        f"failures={summary['failed_cases']} signatures={summary['signatures']} "
        f"vault={summary['resolved']['VAULT']} memory={summary['resolved']['MEMORY']} rules={summary['resolved']['RULE']} "
        f"ai_calls={summary['ai_calls']} ai_enabled={summary['ai_enabled']} "
        f"output={args.run_root.resolve() / 'triage'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
