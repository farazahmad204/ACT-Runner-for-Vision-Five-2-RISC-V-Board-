#!/usr/bin/env python3
"""Create a deterministic, offline RISC-V board-onboarding assessment."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import yaml

from validate_manifest import load_manifest, nested, validate_manifest
from validate_workspace_contract import load_contract, validate as validate_workspace


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def run_git(repo_root: Path, *args: str) -> tuple[int, str]:
    process = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process.returncode, process.stdout.strip()


def inspect_workspace(repo_root: Path) -> tuple[dict[str, Any], Check]:
    branch_rc, branch = run_git(repo_root, "branch", "--show-current")
    status_rc, status = run_git(repo_root, "status", "--short")
    if branch_rc != 0 or status_rc != 0:
        return {}, Check("workspace", "error", "Repository state could not be inspected.")

    changed = [line for line in status.splitlines() if line]
    state = {"branch": branch or "detached", "dirty_paths": len(changed)}
    if changed:
        return state, Check(
            "workspace",
            "warning",
            f"Branch {state['branch']} has {len(changed)} changed paths; preserve unrelated work.",
        )
    return state, Check("workspace", "pass", f"Branch {state['branch']} is clean.")


def inspect_contract(repo_root: Path, contract_path: Path) -> Check:
    """Implementation must start from the contract's stable runner base."""
    try:
        result = validate_workspace(repo_root, load_contract(contract_path), require_clean=False)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return Check("workspace_contract", "error", f"contract check failed: {exc}")
    if result["ok"]:
        return Check("workspace_contract", "pass",
                     f"based on {result['stable_branch']} {str(result['stable_sha'])[:12]}")
    return Check("workspace_contract", "blocked",
                 "not a valid implementation base (research only): " + "; ".join(result["errors"]))


def inspect_hypervisor_source(act_repo: Path | None) -> tuple[dict[str, Any], Check]:
    if act_repo is None:
        return {}, Check(
            "act_hypervisor_source",
            "blocked",
            "No local ACT Hypervisor checkout was supplied; use --act-repo after checking out the pinned branch.",
        )
    if not act_repo.is_dir():
        return {"path": str(act_repo)}, Check(
            "act_hypervisor_source", "blocked", f"ACT checkout does not exist: {act_repo}"
        )

    rc, revision = run_git(act_repo, "rev-parse", "HEAD")
    tsbi = act_repo / "tests" / "env" / "rvtest_trap_handler.h"
    suites = act_repo / "tests" / "priv"
    expected = ("H", "ExceptionsH", "ExceptionsHV", "InterruptsH", "PMPH", "SvH")
    present = [name for name in expected if (suites / name).is_dir()]
    tsbi_text = tsbi.read_text(encoding="utf-8", errors="replace") if tsbi.is_file() else ""
    has_tsbi = "TSBI_GOTO_VSMODE" in tsbi_text and "TSBI_GOTO_VUMODE" in tsbi_text
    state = {
        "path": str(act_repo.resolve()),
        "revision": revision if rc == 0 else None,
        "tsbi_mode_transitions": has_tsbi,
        "required_suites_present": present,
    }
    if has_tsbi and len(present) == len(expected):
        revision_label = revision[:12] if rc == 0 else "unknown revision"
        return state, Check(
            "act_hypervisor_source",
            "pass",
            f"ACT T-SBI and {len(present)} core Hypervisor suites are present at {revision_label}.",
        )
    missing = sorted(set(expected) - set(present))
    return state, Check(
        "act_hypervisor_source",
        "blocked",
        f"Checkout is incomplete for Hypervisor onboarding; T-SBI={has_tsbi}, missing suites={missing}.",
    )


def manifest_checks(data: dict) -> tuple[list[Check], dict[str, Any]]:
    errors, warnings, state, blockers, missing_offline = validate_manifest(data)
    checks: list[Check] = []
    if errors:
        checks.append(Check("manifest_schema", "error", "; ".join(errors)))
    else:
        checks.append(Check("manifest_schema", "pass", "Manifest schema and readiness gates are valid."))

    if missing_offline:
        checks.append(
            Check(
                "offline_inputs",
                "blocked",
                "Missing safe port inputs: " + ", ".join(missing_offline),
            )
        )
    else:
        checks.append(Check("offline_inputs", "pass", "All offline port inputs are recorded."))

    if nested(data, "board.physical_available"):
        checks.append(Check("hardware", "pending", "Hardware is available but has not been exercised by this tool."))
    else:
        checks.append(Check("hardware", "blocked", "Physical board and UART are unavailable."))

    facts = {
        "readiness_state": state,
        "validation_errors": errors,
        "validation_warnings": warnings,
        "manifest_blockers": blockers,
        "missing_offline_inputs": missing_offline,
    }
    return checks, facts


def fmt_scalar(value: Any) -> str:
    if value is None:
        return "UNRESOLVED"
    if isinstance(value, int):
        return hex(value)
    return str(value)


def write_draft_scaffold(data: dict, scaffold: Path) -> list[str]:
    board_id = str(nested(data, "board.id"))
    board_dir = scaffold / "cert_harness" / "boards"
    adapter_dir = scaffold / "cert_harness" / "board_adapters"
    board_dir.mkdir(parents=True, exist_ok=True)
    adapter_dir.mkdir(parents=True, exist_ok=True)
    reserved_regions = nested(data, "memory.reserved_regions") or []
    payload_base = nested(data, "memory.payload_base")
    staging = nested(data, "memory.staging_address")
    # Prefer an explicit flag; otherwise the reserved region that starts
    # between the payload window and the staging buffer (a hole the runner
    # must not load into). Never fall back to an arbitrary region.
    regions = [r for r in reserved_regions if isinstance(r, dict) and isinstance(r.get("start"), int)]
    excluded_region = next((r for r in regions if r.get("exclude_from_payload")), None)
    if excluded_region is None and isinstance(payload_base, int) and isinstance(staging, int):
        excluded_region = next((r for r in regions if payload_base <= r["start"] < staging), None)
    excluded_region = excluded_region or {}
    excluded_start = excluded_region.get("start")
    excluded_limit = (
        excluded_start + excluded_region.get("bytes", 0)
        if isinstance(excluded_start, int)
        else None
    )

    env_path = board_dir / f"{board_id}.env.draft"
    env_path.write_text(
        "\n".join(
            [
                "# DRAFT ONLY: unresolved values intentionally prevent a production build.",
                f"BOARD_NAME={board_id}",
                f'BOARD_DESCRIPTION="{nested(data, "board.vendor")} {nested(data, "board.name")} / {nested(data, "board.soc")}"',
                f"BOARD_ADAPTER={board_id}",
                "BOARD_PAYLOAD_TRANSPORT=uart_stream",
                "BOARD_PLATFORM_ID=UNRESOLVED",
                "BOARD_LINKER_SCRIPT=UNRESOLVED",
                f"BOARD_FW_LOAD_ADDR={fmt_scalar(nested(data, 'memory.firmware_load_address'))}",
                f"BOARD_FW_STACK_BYTES={fmt_scalar(nested(data, 'memory.stack_bytes'))}",
                f"BOARD_RAM_BASE={fmt_scalar(nested(data, 'memory.dram_base'))}",
                f"BOARD_RAM_LIMIT={fmt_scalar(nested(data, 'memory.usable_limit'))}",
                f"BOARD_PAYLOAD_BASE={fmt_scalar(nested(data, 'memory.payload_base'))}",
                f"BOARD_PAYLOAD_LIMIT={fmt_scalar(nested(data, 'memory.payload_limit'))}",
                f"BOARD_DDR_EXCLUDE_BASE={fmt_scalar(excluded_start)}",
                f"BOARD_DDR_EXCLUDE_LIMIT={fmt_scalar(excluded_limit)}",
                f"BOARD_UART_BASE={fmt_scalar(nested(data, 'uart.base'))}",
                f"BOARD_UART_SIZE={fmt_scalar(nested(data, 'uart.size'))}",
                f"BOARD_UART_REG_IO_WIDTH={fmt_scalar(nested(data, 'uart.register_width'))}",
                f"BOARD_RUNNER_HART_ID={fmt_scalar(nested(data, 'cpu.runner_hart_id'))}",
                f"BOARD_MONITOR_HART_ID={fmt_scalar(nested(data, 'cpu.monitor_hart_id'))}",
                f"BOARD_CLINT_MTIME_ADDR={fmt_scalar(nested(data, 'timer.mtime_address'))}",
                f"BOARD_EXT_PACK_ADDR={fmt_scalar(nested(data, 'memory.staging_address'))}",
                f"BOARD_EXT_PACK_MAX_BYTES={fmt_scalar(nested(data, 'memory.staging_max_bytes'))}",
                "DEFAULT_SERIAL_DEV=UNRESOLVED",
                "TOOLCHAIN_TRIPLE=riscv64-unknown-elf",
                "",
            ]
        ),
        encoding="utf-8",
    )

    adapter_path = adapter_dir / f"{board_id}.sh.draft"
    adapter_path.write_text(
        """#!/usr/bin/env bash

# DRAFT ONLY: hardware mutation is blocked until the manifest is complete.
board_flash_image() {
  echo "BLOCKED: image format, target device, and recovery procedure are unresolved" >&2
  return 2
}

board_write_pack() {
  echo "BLOCKED: this board uses UART ELF transport" >&2
  return 2
}

board_capture_uart() {
  echo "BLOCKED: serial device and bidirectional UART are unvalidated" >&2
  return 2
}
""",
        encoding="utf-8",
    )
    return [str(env_path), str(adapter_path)]


def status_label(checks: list[Check]) -> str:
    statuses = {check.status for check in checks}
    if "error" in statuses:
        return "invalid"
    if "blocked" in statuses:
        return "blocked"
    if "warning" in statuses or "pending" in statuses:
        return "pending"
    return "ready"


def write_report(
    report_path: Path,
    data: dict,
    checks: list[Check],
    workspace: dict[str, Any],
    hypervisor: dict[str, Any],
    scaffold_files: list[str],
) -> None:
    board = f"{nested(data, 'board.vendor')} {nested(data, 'board.name')}"
    lines = [
        f"# {board} onboarding report",
        "",
        f"**Overall status:** `{status_label(checks)}`  ",
        f"**Declared readiness:** `{nested(data, 'readiness.state')}`",
        "",
        "## Stage gates",
        "",
        "| Stage | Status | Evidence |",
        "|---|---|---|",
    ]
    for check in checks:
        lines.append(f"| {check.name} | `{check.status}` | {check.detail} |")

    lines.extend(["", "## Runner contract", ""])
    lines.extend(
        [
            "- Start the ELF at its entry point in M-mode.",
            "- Keep the runner responsible for UART loading, range checks, timeout, `tohost`, and result reporting.",
        ]
    )

    lines.extend(["", "## Workspace", "", f"```json\n{json.dumps(workspace, indent=2)}\n```"])
    if hypervisor:
        lines.extend(["", "## Optional ACT Hypervisor source", "", f"```json\n{json.dumps(hypervisor, indent=2)}\n```"])
    if scaffold_files:
        lines.extend(["", "## Draft scaffold", ""])
        lines.extend(f"- `{path}`" for path in scaffold_files)

    lines.extend(["", "## Next actions", ""])
    for blocker in nested(data, "readiness.blockers") or []:
        lines.append(f"- {blocker}")
    readiness_state = nested(data, "readiness.state")
    if readiness_state == "researching":
        lines.extend(
            [
                "- Create the offline port once the boot, runner-execution, and DDR-placement contracts are recorded.",
                "- Build the runner and inspect its linker map before any boot-media operation.",
            ]
        )
    elif readiness_state == "offline_prepared":
        lines.extend(
            [
                "- Preserve the offline build, linker map, packaging hashes, and recovery image.",
                "- When hardware is available, use the documented recovery path for the first non-destructive boot and capture UART evidence.",
            ]
        )
    lines.append(
        "- Require human approval before flashing, power control, pushing code, or creating a pull request."
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    default_root = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=default_root)
    parser.add_argument("--act-repo", type=Path)
    parser.add_argument("--contract", type=Path,
                        default=Path(__file__).resolve().parent.parent / "assets" / "system-contract.yaml")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument(
        "--draft-scaffold",
        action="store_true",
        help="Write non-buildable draft board files inside the run directory.",
    )
    args = parser.parse_args()

    try:
        data = load_manifest(args.manifest)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    args.run_dir.mkdir(parents=True, exist_ok=True)
    checks, facts = manifest_checks(data)
    workspace, workspace_check = inspect_workspace(args.repo_root)
    hypervisor: dict[str, Any] = {}
    checks = [workspace_check, inspect_contract(args.repo_root, args.contract), *checks]
    if args.act_repo is not None:
        hypervisor, hypervisor_check = inspect_hypervisor_source(args.act_repo)
        checks.append(hypervisor_check)

    scaffold_files: list[str] = []
    if args.draft_scaffold:
        scaffold_files = write_draft_scaffold(data, args.run_dir / "scaffold")
        checks.append(
            Check(
                "draft_scaffold",
                "pass",
                "Non-buildable drafts were isolated in the run directory; no production board files changed.",
            )
        )

    state = {
        "schema_version": 1,
        "board_id": nested(data, "board.id"),
        "overall_status": status_label(checks),
        "workspace": workspace,
        "manifest": facts,
        "hypervisor_source": hypervisor,
        "checks": [asdict(check) for check in checks],
        "scaffold_files": scaffold_files,
    }
    state_path = args.run_dir / "state.json"
    report_path = args.run_dir / "report.md"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    write_report(report_path, data, checks, workspace, hypervisor, scaffold_files)

    print(f"board={state['board_id']} status={state['overall_status']}")
    print(f"state={state_path}")
    print(f"report={report_path}")
    return 1 if state["overall_status"] == "invalid" else 0


if __name__ == "__main__":
    raise SystemExit(main())
