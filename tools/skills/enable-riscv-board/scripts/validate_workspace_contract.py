#!/usr/bin/env python3
"""Validate that board implementation uses the canonical stable runner base."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any

import yaml


def git(repo: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    output = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()
    return result.returncode, output


def normalize_git_url(value: str) -> str:
    url = value.strip()
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url[len("git@github.com:") :]
    if url.endswith("/"):
        url = url[:-1]
    if url.endswith(".git"):
        url = url[:-4]
    return url.lower()


def load_contract(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("system contract must be a schema_version 1 mapping")
    return data


def validate(
    repo: Path,
    contract: dict[str, Any],
    require_clean: bool,
    stable_commit: str | None = None,
) -> dict[str, Any]:
    runner = contract["repositories"]["runner"]
    errors: list[str] = []
    warnings: list[str] = []

    origin_rc, origin = git(repo, "remote", "get-url", "origin")
    accepted = {normalize_git_url(url) for url in runner["accepted_origins"]}
    if origin_rc != 0:
        errors.append("runner checkout has no readable origin remote")
    elif normalize_git_url(origin) not in accepted:
        errors.append(f"origin {origin!r} is not an accepted runner repository")

    head_rc, head = git(repo, "rev-parse", "HEAD")
    branch_rc, branch = git(repo, "branch", "--show-current")
    stable_ref = str(runner["stable_remote_ref"])
    stable_rc, stable_sha = git(repo, "rev-parse", stable_commit or stable_ref)
    if stable_rc != 0:
        errors.append(f"stable runner ref is unavailable: {stable_ref}")
    elif head_rc != 0:
        errors.append("runner HEAD cannot be resolved")
    else:
        ancestor_rc, _ = git(repo, "merge-base", "--is-ancestor", stable_sha, "HEAD")
        if ancestor_rc != 0:
            errors.append(
                f"HEAD {head[:12]} is not based on recorded stable commit {stable_sha[:12]}"
            )

    required_paths = {
        "build_entrypoint": runner["build_entrypoint"],
        "profile_file": runner["profile_file"],
    }
    for label, relative in required_paths.items():
        if not (repo / relative).is_file():
            errors.append(f"declared {label} does not exist on this base: {relative}")

    status_rc, status = git(repo, "status", "--short")
    dirty_paths = len([line for line in status.splitlines() if line]) if status_rc == 0 else None
    if require_clean and dirty_paths:
        errors.append(f"implementation workspace is not clean ({dirty_paths} changed paths)")
    elif dirty_paths:
        warnings.append(f"workspace has {dirty_paths} changed paths")

    return {
        "ok": not errors,
        "repository": str(repo),
        "origin": origin if origin_rc == 0 else None,
        "branch": branch if branch_rc == 0 and branch else "detached",
        "head": head if head_rc == 0 else None,
        "stable_branch": runner["stable_branch"],
        "stable_ref": stable_ref,
        "stable_sha": stable_sha if stable_rc == 0 else None,
        "production_profile": runner["production_profile"],
        "build_entrypoint": runner["build_entrypoint"],
        "dirty_paths": dirty_paths,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    default_contract = script_dir.parent / "assets" / "system-contract.yaml"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--contract", type=Path, default=default_contract)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    args = parser.parse_args()

    try:
        contract = load_contract(args.contract.resolve())
        result = validate(args.repo_root.resolve(), contract, args.require_clean)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"workspace_contract=ERROR detail={exc}")
        return 2

    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        status = "PASS" if result["ok"] else "BLOCKED"
        print(f"workspace_contract={status}")
        print(f"origin={result['origin']}")
        print(f"branch={result['branch']} head={(result['head'] or 'unknown')[:12]}")
        print(
            f"stable={result['stable_branch']} "
            f"sha={(result['stable_sha'] or 'unknown')[:12]}"
        )
        print(f"profile={result['production_profile']}")
        print(f"build_entrypoint={result['build_entrypoint']}")
        for warning in result["warnings"]:
            print(f"WARNING: {warning}")
        for error in result["errors"]:
            print(f"ERROR: {error}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
