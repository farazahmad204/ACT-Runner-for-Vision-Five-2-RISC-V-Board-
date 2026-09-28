#!/usr/bin/env python3
"""Run deterministic and optional AI triage for a weekly UART result directory."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ci_triage import AIConfig, run_triage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--ai", action="store_true", help="enable advisory AI analysis")
    parser.add_argument("--model", default=os.environ.get("AI_TRIAGE_MODEL", "gpt-5"))
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("OPENAI_RESPONSES_ENDPOINT", "https://api.openai.com/v1/responses"),
    )
    parser.add_argument(
        "--max-ai-failures",
        type=int,
        default=0,
        help="maximum AI-analyzed failures; 0 analyzes every failure",
    )
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if args.max_ai_failures < 0:
        parser.error("--max-ai-failures must be non-negative")

    summary = run_triage(
        args.run_root,
        args.state_root,
        args.board,
        ai_enabled=args.ai,
        ai_config=AIConfig(model=args.model, endpoint=args.endpoint, timeout_seconds=args.timeout),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        max_ai_failures=args.max_ai_failures,
    )
    print(
        "ACT CI triage: "
        f"failures={summary['failed_cases']} ai_enabled={summary['ai_enabled']} "
        f"output={args.run_root.resolve() / 'triage'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
