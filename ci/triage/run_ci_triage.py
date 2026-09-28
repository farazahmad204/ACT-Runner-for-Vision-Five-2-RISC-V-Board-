#!/usr/bin/env python3
"""Run deterministic and optional AI triage for a weekly UART result directory."""

from __future__ import annotations

import argparse
from pathlib import Path

from ci_triage import AIConfig, run_triage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument(
        "--local-ai", action="store_true", help="enable advisory local Ollama analysis"
    )
    parser.add_argument("--provider", choices=("ollama",), default="ollama")
    parser.add_argument("--model", default="qwen2.5:1.5b")
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:11434/api/generate",
    )
    parser.add_argument(
        "--max-ai-failures",
        type=int,
        default=0,
        help="maximum AI-analyzed failures; 0 analyzes every failure",
    )
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if args.max_ai_failures < 0:
        parser.error("--max-ai-failures must be non-negative")

    summary = run_triage(
        args.run_root,
        args.state_root,
        args.board,
        ai_enabled=args.local_ai,
        ai_config=AIConfig(
            model=args.model,
            provider=args.provider,
            endpoint=args.endpoint,
            timeout_seconds=args.timeout,
        ),
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
