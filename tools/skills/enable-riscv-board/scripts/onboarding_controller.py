#!/usr/bin/env python3
"""Durable, resumable controller for AI-assisted RISC-V board onboarding."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterator

import yaml

from validate_manifest import load_manifest, nested, validate_manifest
from validate_research_report import validate as validate_research
from validate_workspace_contract import load_contract, validate as validate_workspace


SCHEMA_VERSION = 1
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
REQUIRED_RESEARCH_ROLES = {"boot_image", "runner_contract", "ddr_placement"}
TERMINAL_STATES = {"offline_complete", "ci_operational"}
RETRYABLE_BLOCKED_FROM = {
    "requested",
    "contract_validated",
    "researching",
    "evidence_validated",
    "workspace_ready",
    "implemented",
    "offline_validated",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 60,
    log_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
        env=env,
    )
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(result.stdout, encoding="utf-8")
    return result


def git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return run_command(["git", *args], cwd=repo, timeout=timeout)


@contextmanager
def controller_lock(run_dir: Path) -> Iterator[None]:
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / ".controller.lock"
    with lock_path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another controller owns {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_state(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "state.json"
    if not path.is_file():
        raise ValueError(f"state does not exist: {path}; run init first")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported onboarding state schema")
    return data


def save_state(run_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_json(run_dir / "state.json", state)


def add_history(state: dict[str, Any], action: str, result: str, detail: str) -> None:
    state.setdefault("history", []).append(
        {
            "sequence": len(state.get("history", [])) + 1,
            "timestamp": utc_now(),
            "state": state["current_state"],
            "action": action,
            "result": result,
            "detail": detail,
        }
    )


def set_transition(
    state: dict[str, Any],
    next_state: str,
    status: str,
    action: str,
    detail: str,
    *,
    next_action: str | None = None,
) -> None:
    previous = state["current_state"]
    add_history(state, action, status, detail)
    state["current_state"] = next_state
    state["status"] = status
    state["next_action"] = next_action
    state["last_transition"] = {"from": previous, "to": next_state}


def block(state: dict[str, Any], action: str, detail: str) -> None:
    state["resume_state"] = state["current_state"]
    set_transition(
        state,
        "blocked",
        "blocked",
        action,
        detail,
        next_action="Resolve the recorded blocker, then run retry and resume.",
    )


def write_task(run_dir: Path, name: str, title: str, lines: list[str]) -> Path:
    path = run_dir / "tasks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("\n".join([f"# {title}", "", *lines, ""]), encoding="utf-8")
    return path


def validate_reports(research_dir: Path, board_id: str) -> tuple[list[str], list[Path]]:
    reports = sorted(research_dir.glob("*.yaml")) if research_dir.is_dir() else []
    if not reports:
        return [f"no research reports found under {research_dir}"], []
    errors: list[str] = []
    roles: set[str] = set()
    accepted: list[Path] = []
    for report in reports:
        try:
            data = yaml.safe_load(report.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{report}: {exc}")
            continue
        report_errors = validate_research(data)
        errors.extend(f"{report}: {item}" for item in report_errors)
        if isinstance(data, dict):
            role = data.get("agent_role")
            if role in REQUIRED_RESEARCH_ROLES:
                roles.add(str(role))
            if data.get("board_id") != board_id:
                errors.append(f"{report}: board_id does not match {board_id}")
        if not report_errors:
            accepted.append(report)
    missing = REQUIRED_RESEARCH_ROLES - roles
    if missing:
        errors.append(f"missing research roles: {sorted(missing)}")
    return errors, accepted


def verify_sha256_file(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"missing checksum file: {path}"]
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        parts = raw.split(maxsplit=1)
        if len(parts) != 2:
            errors.append(f"{path}:{number}: invalid checksum record")
            continue
        expected, relative = parts
        relative = relative.lstrip("* ")
        artifact = path.parent / relative
        if not artifact.is_file():
            errors.append(f"checksum target missing: {artifact}")
        elif sha256_file(artifact) != expected.lower():
            errors.append(f"checksum mismatch: {artifact}")
    return errors


def changed_paths(repo: Path) -> list[str]:
    result = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if result.returncode != 0:
        raise RuntimeError(f"git status failed: {result.stdout[-500:]}")
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return paths


def path_matches(path: str, exact: list[str], prefixes: list[str]) -> bool:
    return path in exact or any(path.startswith(prefix) for prefix in prefixes)


def verify_workspace_identity(state: dict[str, Any], repo: Path) -> str | None:
    """Return a blocker when a resumed run moved to another branch or base."""

    identity = state.get("workspace_identity")
    if not identity:
        return "workspace identity was not recorded during contract validation"
    branch_result = git(repo, "branch", "--show-current")
    if branch_result.returncode != 0:
        return f"cannot resolve current feature branch: {branch_result.stdout[-500:]}"
    branch = branch_result.stdout.strip() or "detached"
    if branch != identity["branch"]:
        return f"feature branch changed: expected {identity['branch']!r}, got {branch!r}"
    ancestor = git(repo, "merge-base", "--is-ancestor", identity["stable_sha"], "HEAD")
    if ancestor.returncode != 0:
        return "HEAD is no longer based on the stable commit recorded at initialization"
    return None


def advance(state: dict[str, Any], run_dir: Path, execute_build: bool) -> bool:
    """Advance one transition. Return True when another transition may run."""

    repo = Path(state["repo_root"])
    manifest_path = Path(state["manifest"])
    contract_path = Path(state["contract"])
    research_dir = Path(state["research_dir"])
    board_id = state["board_id"]
    current = state["current_state"]
    contract = load_contract(contract_path)
    runner_contract = contract["repositories"]["runner"]

    if current != "requested":
        identity_error = verify_workspace_identity(state, repo)
        if identity_error:
            block(state, "verify_workspace_identity", identity_error)
            return False

    if current == "requested":
        result = validate_workspace(repo, contract, require_clean=True)
        state["workspace_contract"] = result
        if not result["ok"]:
            block(state, "validate_workspace_contract", "; ".join(result["errors"]))
            return False
        if result["branch"] == "detached":
            block(state, "validate_workspace_contract", "onboarding requires a named feature branch")
            return False
        state["workspace_identity"] = {
            "branch": result["branch"],
            "initial_head": result["head"],
            "stable_sha": result["stable_sha"],
            "origin": result["origin"],
        }
        set_transition(
            state,
            "contract_validated",
            "active",
            "validate_workspace_contract",
            f"canonical runner base accepted at {result['head'][:12]}",
        )
        return True

    if current in {"contract_validated", "researching"}:
        try:
            manifest = load_manifest(manifest_path)
        except ValueError as exc:
            block(state, "validate_evidence", str(exc))
            return False
        manifest_errors, warnings, readiness, blockers, missing = validate_manifest(manifest)
        report_errors, reports = validate_reports(research_dir, board_id)
        state["manifest_validation"] = {
            "readiness": readiness,
            "warnings": warnings,
            "blockers": blockers,
            "missing_offline_inputs": missing,
            "reports": [str(path) for path in reports],
        }
        report_files_present = research_dir.is_dir() and any(research_dir.glob("*.yaml"))
        state["manifest_validation"]["report_errors"] = report_errors
        if manifest_errors or (report_errors and report_files_present):
            block(
                state,
                "validate_evidence",
                "; ".join([*manifest_errors, *report_errors]),
            )
            return False
        if missing or not reports:
            task = write_task(
                run_dir,
                "research.md",
                f"Research required for {board_id}",
                [
                    f"Manifest: `{manifest_path}`",
                    f"Research output directory: `{research_dir}`",
                    "Produce one validated report for each role: boot_image, runner_contract and ddr_placement.",
                    "Update the manifest only from validated primary-source evidence.",
                    f"Missing offline inputs: `{', '.join(missing) or 'research reports'}`",
                ],
            )
            set_transition(
                state,
                "researching",
                "waiting_agent",
                "request_research",
                f"research evidence is incomplete; task={task}",
                next_action="Run the restricted research agents, update the manifest, then resume.",
            )
            return False
        set_transition(
            state,
            "evidence_validated",
            "active",
            "validate_evidence",
            f"manifest and {len(reports)} research reports passed",
        )
        return True

    if current == "evidence_validated":
        workspace = validate_workspace(
            repo,
            contract,
            require_clean=False,
            stable_commit=state["workspace_identity"]["stable_sha"],
        )
        state["workspace_contract"] = workspace
        if not workspace["ok"]:
            block(state, "revalidate_workspace", "; ".join(workspace["errors"]))
            return False
        task = write_task(
            run_dir,
            "implementation.md",
            f"Implement {board_id}",
            [
                f"Repository: `{repo}`",
                f"Stable base: `{workspace['stable_ref']}` at `{workspace['stable_sha']}`",
                f"Production profile: `{workspace['production_profile']}`",
                f"Build entry point: `{workspace['build_entrypoint']}`",
                "Add only the board environment, linker, adapter and evidence-backed generic changes.",
                "Do not add vendor DDR initialization or OpenSBI services without hardware evidence.",
            ],
        )
        set_transition(
            state,
            "workspace_ready",
            "waiting_agent",
            "request_implementation",
            f"implementation workspace accepted; task={task}",
            next_action="Run the implementation agent in this workspace, then resume.",
        )
        return False

    if current == "workspace_ready":
        required = [
            runner_contract["board_environment_pattern"].format(board=board_id),
            runner_contract["board_linker_pattern"].format(board=board_id),
            runner_contract["board_adapter_pattern"].format(board=board_id),
        ]
        missing_files = [relative for relative in required if not (repo / relative).is_file()]
        if missing_files:
            state["status"] = "waiting_agent"
            state["next_action"] = "Implementation files are still missing: " + ", ".join(missing_files)
            add_history(state, "inspect_implementation", "waiting_agent", state["next_action"])
            return False
        set_transition(
            state,
            "implemented",
            "active",
            "inspect_implementation",
            "board environment, linker and adapter are present",
        )
        return True

    if current == "implemented":
        if not execute_build:
            state["status"] = "waiting_local_execution"
            state["next_action"] = "Resume with --execute-build to run the deterministic build gate."
            add_history(state, "build_gate", "waiting_local_execution", state["next_action"])
            return False
        build_log = run_dir / "build" / "build.log"
        build_script = repo / runner_contract["build_entrypoint"]
        command = ["bash", str(build_script), "--board", board_id]
        try:
            result = run_command(
                command,
                cwd=repo,
                timeout=int(state["build_timeout_seconds"]),
                log_path=build_log,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired:
            block(state, "build", f"build timed out; log={build_log}")
            return False
        if result.returncode != 0:
            block(state, "build", f"build failed rc={result.returncode}; log={build_log}")
            return False
        artifact_dir = (
            repo
            / "cert_harness"
            / "build"
            / board_id
            / str(runner_contract["production_profile"])
        )
        checksum_errors = verify_sha256_file(artifact_dir / "sha256sums.txt")
        if checksum_errors:
            block(state, "verify_build", "; ".join(checksum_errors))
            return False
        manifest_output = artifact_dir / "manifest.json"
        if not manifest_output.is_file():
            block(state, "verify_build", f"build manifest missing: {manifest_output}")
            return False
        state["build"] = {
            "command": command,
            "log": str(build_log),
            "artifact_dir": str(artifact_dir),
            "manifest": str(manifest_output),
            "boot_image_sha256": sha256_file(artifact_dir / "boot_image.bin")
            if (artifact_dir / "boot_image.bin").is_file()
            else None,
        }
        set_transition(
            state,
            "offline_validated",
            "active",
            "build_and_verify",
            f"build and artifact checks passed; artifacts={artifact_dir}",
        )
        return True

    if current == "offline_validated":
        review_dir = run_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_policy = contract.get("review", {})
        allowed_paths = list(
            review_policy.get(
                "allowed_paths",
                [
                    "Makefile.act",
                    "act_core.c",
                    "runner_shared.h",
                    "runner_uart_stream.c",
                    "cert_harness/tools/build_runner.sh",
                ],
            )
        )
        allowed_prefixes = list(
            review_policy.get(
                "allowed_path_prefixes",
                ["cert_harness/boards/", "cert_harness/board_adapters/"],
            )
        )
        ignored_paths = list(
            review_policy.get(
                "ignored_generated_paths",
                ["firmware.elf", "firmware.bin", "firmware.dis", "firmware.map"],
            )
        )
        ignored_prefixes = list(
            review_policy.get(
                "ignored_generated_prefixes",
                ["cert_harness/build/", "out/"],
            )
        )
        workspace_paths = changed_paths(repo)
        unexpected = [
            path
            for path in workspace_paths
            if not path_matches(path, allowed_paths, allowed_prefixes)
            and not path_matches(path, ignored_paths, ignored_prefixes)
        ]
        if unexpected:
            block(
                state,
                "prepare_review",
                "unexpected changed paths are outside the review contract: "
                + ", ".join(unexpected),
            )
            return False
        stage_paths = [
            path
            for path in workspace_paths
            if path_matches(path, allowed_paths, allowed_prefixes)
        ]
        if not stage_paths:
            block(state, "prepare_review", "no reviewable implementation changes found")
            return False
        add_result = git(repo, "add", "-A", "--", *sorted(set(stage_paths)), timeout=120)
        if add_result.returncode != 0:
            block(state, "prepare_review", f"git add failed: {add_result.stdout[-500:]}")
            return False
        stable_ref = str(runner_contract["stable_remote_ref"])
        patch_result = git(repo, "diff", "--cached", "--binary", "--no-color", stable_ref, timeout=120)
        if patch_result.returncode != 0:
            block(state, "prepare_review", f"git diff failed: {patch_result.stdout[-500:]}")
            return False
        patch_path = review_dir / f"{board_id}.patch"
        patch_path.write_text(patch_result.stdout, encoding="utf-8")
        stat_result = git(repo, "diff", "--cached", "--stat", stable_ref)
        (review_dir / "diffstat.txt").write_text(stat_result.stdout, encoding="utf-8")
        status_result = git(repo, "status", "--short")
        (review_dir / "workspace-status.txt").write_text(status_result.stdout, encoding="utf-8")
        if patch_path.stat().st_size == 0:
            block(state, "prepare_review", "review patch is empty")
            return False
        state["review"] = {
            "patch": str(patch_path),
            "patch_sha256": sha256_file(patch_path),
            "diffstat": str(review_dir / "diffstat.txt"),
            "staged_paths": sorted(set(stage_paths)),
            "ignored_generated_paths": sorted(
                path
                for path in workspace_paths
                if path_matches(path, ignored_paths, ignored_prefixes)
            ),
        }
        set_transition(
            state,
            "review_ready",
            "active",
            "prepare_review",
            f"review patch created: {patch_path}",
        )
        return True

    if current in {"review_ready", "awaiting_external_approval"}:
        gate = "push_or_open_pull_request"
        approval = state.get("approvals", {}).get(gate)
        expected = state.get("review", {}).get("patch_sha256")
        if not approval or approval.get("artifact_sha256") != expected:
            set_transition(
                state,
                "awaiting_external_approval",
                "waiting_approval",
                "approval_gate",
                f"approval required for {gate}; patch_sha256={expected}",
                next_action="Review the patch and record a scoped approval; no push is performed automatically.",
            )
            return False
        if datetime.fromisoformat(approval["expires_at"]) <= datetime.now(timezone.utc):
            block(state, "approval_gate", f"approval expired for {gate}")
            return False
        set_transition(
            state,
            "offline_complete",
            "complete",
            "approval_gate",
            f"offline onboarding approved by {approval['actor']}; external push remains a separate action",
            next_action="Push/open the reviewed PR, then wait for physical hardware smoke validation.",
        )
        return False

    if current in TERMINAL_STATES:
        state["status"] = "complete"
        return False

    block(state, "advance", f"unsupported state: {current}")
    return False


def initialize(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    manifest_path = args.manifest.resolve()
    repo = args.repo_root.resolve()
    contract_path = args.contract.resolve()
    manifest = load_manifest(manifest_path)
    board_id = str(nested(manifest, "board.id") or "")
    if not SAFE_ID.fullmatch(board_id):
        raise ValueError(f"unsafe or missing board.id: {board_id!r}")
    run_id = args.run_id or run_dir.name
    if not SAFE_ID.fullmatch(run_id):
        raise ValueError(f"unsafe run ID: {run_id!r}")
    with controller_lock(run_dir):
        state_path = run_dir / "state.json"
        if state_path.exists():
            existing = load_state(run_dir)
            if existing["board_id"] != board_id:
                raise ValueError("existing state belongs to a different board")
            print(json.dumps(existing, indent=2, sort_keys=True))
            return 0
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "board_id": board_id,
            "repo_root": str(repo),
            "manifest": str(manifest_path),
            "research_dir": str(args.research_dir.resolve()),
            "contract": str(contract_path),
            "current_state": "requested",
            "status": "active",
            "next_action": "Run resume.",
            "build_timeout_seconds": args.build_timeout,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "history": [],
            "approvals": {},
        }
        add_history(state, "initialize", "active", "durable onboarding state created")
        save_state(run_dir, state)
        print(json.dumps(state, indent=2, sort_keys=True))
    return 0


def resume(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    with controller_lock(run_dir):
        state = load_state(run_dir)
        if state["current_state"] == "blocked":
            print("Controller is blocked; resolve the issue and run retry before resume.", file=sys.stderr)
            return 2
        if state["status"].startswith("waiting"):
            state["status"] = "active"
        transitions = 0
        while transitions < args.max_transitions:
            keep_going = advance(state, run_dir, args.execute_build)
            transitions += 1
            save_state(run_dir, state)
            if not keep_going:
                break
        print(json.dumps(state, indent=2, sort_keys=True))
        return 2 if state["status"] == "blocked" else 0


def approve(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    with controller_lock(run_dir):
        state = load_state(run_dir)
        if args.gate != "push_or_open_pull_request":
            raise ValueError("this controller version accepts only the push_or_open_pull_request gate")
        if args.expires_hours <= 0:
            raise ValueError("approval expiry must be a positive number of hours")
        expected = state.get("review", {}).get("patch_sha256")
        if not expected or args.artifact_sha256 != expected:
            raise ValueError("approval artifact SHA-256 does not match the review patch")
        expires = datetime.now(timezone.utc) + timedelta(hours=args.expires_hours)
        state.setdefault("approvals", {})[args.gate] = {
            "actor": args.actor,
            "artifact_sha256": args.artifact_sha256,
            "approved_at": utc_now(),
            "expires_at": expires.replace(microsecond=0).isoformat(),
        }
        add_history(state, "approve", "approved", f"gate={args.gate} actor={args.actor}")
        save_state(run_dir, state)
        print(json.dumps(state["approvals"][args.gate], indent=2, sort_keys=True))
    return 0


def retry(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    with controller_lock(run_dir):
        state = load_state(run_dir)
        if state["current_state"] != "blocked":
            raise ValueError("retry is valid only for blocked state")
        resume_state = state.get("resume_state")
        if resume_state not in RETRYABLE_BLOCKED_FROM:
            raise ValueError(f"blocked state is not retryable: {resume_state}")
        set_transition(
            state,
            resume_state,
            "active",
            "retry",
            f"operator requested retry from {resume_state}",
            next_action="Run resume.",
        )
        save_state(run_dir, state)
        print(json.dumps(state, indent=2, sort_keys=True))
    return 0


def status(args: argparse.Namespace) -> int:
    with controller_lock(args.run_dir.resolve()):
        print(json.dumps(load_state(args.run_dir.resolve()), indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent
    default_contract = script_dir.parent / "assets" / "system-contract.yaml"
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--repo-root", type=Path, required=True)
    init_parser.add_argument("--manifest", type=Path, required=True)
    init_parser.add_argument("--research-dir", type=Path, required=True)
    init_parser.add_argument("--run-dir", type=Path, required=True)
    init_parser.add_argument("--run-id")
    init_parser.add_argument("--contract", type=Path, default=default_contract)
    init_parser.add_argument("--build-timeout", type=int, default=1800)
    init_parser.set_defaults(handler=initialize)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--run-dir", type=Path, required=True)
    resume_parser.add_argument("--max-transitions", type=int, default=20)
    resume_parser.add_argument("--execute-build", action="store_true")
    resume_parser.set_defaults(handler=resume)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--run-dir", type=Path, required=True)
    status_parser.set_defaults(handler=status)

    approve_parser = subparsers.add_parser("approve")
    approve_parser.add_argument("--run-dir", type=Path, required=True)
    approve_parser.add_argument("--gate", required=True)
    approve_parser.add_argument("--actor", required=True)
    approve_parser.add_argument("--artifact-sha256", required=True)
    approve_parser.add_argument("--expires-hours", type=int, default=8)
    approve_parser.set_defaults(handler=approve)

    retry_parser = subparsers.add_parser("retry")
    retry_parser.add_argument("--run-dir", type=Path, required=True)
    retry_parser.set_defaults(handler=retry)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"onboarding_controller=ERROR detail={exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
