#!/usr/bin/env python3
"""Create a read-only inventory of the runner, boards, and UART CI pipelines."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import yaml

from validate_workspace_contract import load_contract


DEFAULT_CONTRACT = Path(__file__).resolve().parent.parent / "assets" / "system-contract.yaml"
UART_PIPELINES = {
    "vf2-uart-sanity": "Jenkinsfile.uart-sanity",
    "vf2-uart-weekly": "Jenkinsfile.uart-weekly",
    "bpif3-uart-sanity": "Jenkinsfile.bpif3-uart-sanity",
    "bpif3-uart-weekly": "Jenkinsfile.bpif3-uart-weekly",
    "riscv-uart-single-elf": "Jenkinsfile.uart-single-elf",
}
RUNTIME_FILES = (
    "main_act.c",
    "runner_uart_stream.c",
    "act_core.c",
    "act_runtime.c",
    "runner_machine_trap.c",
    "runner_monitor.c",
    "runner_shared.h",
    "cert_harness/uart_stream/send_elf.py",
    "cert_harness/uart_stream/run_elf_batch.py",
)


def contract_string(contract: dict[str, Any], path: str) -> str:
    value: Any = contract
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"system contract is missing {path}")
        value = value[part]
    # YAML turns unquoted 0.14 into a float and 0.10 into 0.1; insist on text.
    if not isinstance(value, str) or not value:
        raise ValueError(f"system contract {path} must be a non-empty string (quote version numbers)")
    return value


def load_policy(contract_path: Path) -> dict[str, str]:
    """Read the stable branches and toolchain policy from the system contract."""

    contract = load_contract(contract_path)
    portal_url = contract_string(contract, "repositories.portal.canonical_url")
    return {
        "contract": str(contract_path),
        "stable_runner_branch": contract_string(contract, "repositories.runner.stable_branch"),
        "stable_act_branch": contract_string(contract, "repositories.act.stable_branch"),
        "required_sail_version": contract_string(contract, "toolchain.required_sail_version"),
        "portal_repository": portal_url.removesuffix(".git"),
    }


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            values[key] = value.strip().strip('"').strip("'")
    return values


def groovy_default(text: str, name: str) -> str | None:
    pattern = re.compile(
        rf"(?:string|password)\s*\(\s*name:\s*['\"]{re.escape(name)}['\"]"
        rf"\s*,\s*defaultValue:\s*['\"]([^'\"]*)['\"]",
        re.MULTILINE,
    )
    match = pattern.search(text)
    return match.group(1) if match else None


def groovy_environment(text: str, name: str) -> str | None:
    match = re.search(
        rf"^\s*{re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE
    )
    return match.group(1) if match else None


def inspect_pipeline(repo: Path, job: str, relative: str) -> dict:
    path = repo / relative
    if not path.is_file():
        return {"job": job, "file": relative, "exists": False}
    text = path.read_text(encoding="utf-8")
    labels = sorted(set(re.findall(r"\blabel\s+['\"]([^'\"]+)['\"]", text)))
    locks = sorted(set(re.findall(r"lock\s*\(\s*resource:\s*['\"]([^'\"]+)", text)))
    lock_mode = "fixed"
    if "def hardwareLock" in text and "lock(resource: hardwareLock)" in text:
        lock_mode = "dynamic_target_board"
    elif not locks:
        lock_mode = "none"
    portal_paths = sorted(set(re.findall(r"https://[^'\"\s]+/portal/", text)))
    return {
        "job": job,
        "file": relative,
        "exists": True,
        "runner_branch": groovy_default(text, "RUNNER_BRANCH"),
        "act_branch": groovy_default(text, "ACT_BRANCH"),
        "portal_credential_id": groovy_default(text, "PORTAL_CREDENTIAL_ID"),
        "sail_bin": groovy_environment(text, "SAIL_BIN"),
        "sail_expected_version": groovy_environment(text, "SAIL_EXPECTED_VERSION"),
        "agent_labels": labels,
        "hardware_locks": locks,
        "hardware_lock_mode": lock_mode,
        "portal_urls": portal_paths,
    }


def build_inventory(repo: Path, policy: dict[str, str] | None = None) -> dict:
    if policy is None:
        policy = load_policy(DEFAULT_CONTRACT)
    stable_runner = policy["stable_runner_branch"]
    stable_act = policy["stable_act_branch"]
    required_sail = policy["required_sail_version"]
    boards = []
    board_root = repo / "cert_harness" / "boards"
    for path in sorted(board_root.glob("*.env")) if board_root.is_dir() else []:
        env = parse_env(path)
        board_id = env.get("BOARD_NAME", path.stem)
        adapter = env.get("BOARD_ADAPTER", board_id)
        adapter_path = repo / "cert_harness" / "board_adapters" / f"{adapter}.sh"
        fit_source = env.get("BOARD_FIT_SOURCE", "")
        fit_path = repo / fit_source if fit_source else None
        adapter_exists = adapter_path.is_file()
        fit_source_exists = bool(fit_path and fit_path.is_file())
        linker = env.get("BOARD_LINKER_SCRIPT", "")
        linker_candidates = [repo / linker, path.parent / board_id / linker, path.parent / linker]
        boards.append(
            {
                "id": board_id,
                "description": env.get("BOARD_DESCRIPTION"),
                "preset": str(path.relative_to(repo)),
                "adapter": f"cert_harness/board_adapters/{adapter}.sh",
                "adapter_exists": adapter_exists,
                "fit_source": fit_source or None,
                "fit_source_exists": fit_source_exists,
                "packaging_ready": adapter_exists or fit_source_exists,
                "linker_setting": linker or None,
                "linker_resolves": any(candidate.is_file() for candidate in linker_candidates if linker),
                "firmware_load_address": env.get("BOARD_FW_LOAD_ADDR"),
                "ram_base": env.get("BOARD_RAM_BASE"),
                "ram_limit": env.get("BOARD_RAM_LIMIT"),
                "uart_base": env.get("BOARD_UART_BASE"),
                "runner_hart": env.get("BOARD_RUNNER_HART_ID"),
                "monitor_hart": env.get("BOARD_MONITOR_HART_ID"),
                "staging_address": env.get("BOARD_EXT_PACK_ADDR") or env.get("BOARD_UART_ELF_BUFFER_ADDR"),
                "staging_max_bytes": env.get("BOARD_EXT_PACK_MAX_BYTES") or env.get("BOARD_UART_ELF_MAX_BYTES"),
                "default_transport": env.get("BOARD_PAYLOAD_TRANSPORT"),
                "boot_image": env.get("BOARD_BOOT_IMAGE"),
            }
        )

    pipelines = [inspect_pipeline(repo, job, path) for job, path in UART_PIPELINES.items()]
    warnings: list[str] = []
    for pipeline in pipelines:
        if not pipeline.get("exists"):
            warnings.append(f"missing UART pipeline: {pipeline['file']}")
            continue
        runner_branch = pipeline.get("runner_branch")
        if runner_branch and runner_branch != stable_runner:
            warnings.append(
                f"{pipeline['job']} runner default {runner_branch!r} differs from stable policy {stable_runner!r}"
            )
        act_branch = pipeline.get("act_branch")
        if act_branch and act_branch != stable_act:
            warnings.append(
                f"{pipeline['job']} ACT default {act_branch!r} differs from stable policy {stable_act!r}"
            )
        sail_version = pipeline.get("sail_expected_version")
        if sail_version and sail_version != required_sail:
            warnings.append(
                f"{pipeline['job']} expects Sail {sail_version!r}; current policy requires {required_sail!r}"
            )
    for board in boards:
        if not board["packaging_ready"]:
            warnings.append(f"{board['id']} has neither a board adapter nor a resolvable FIT source")
        if not board["linker_resolves"]:
            warnings.append(
                f"{board['id']} linker setting {board['linker_setting']!r} does not resolve to a board-specific file"
            )

    profiles = []
    profile_root = repo / "cert_harness" / "profiles"
    if profile_root.is_dir():
        profiles = [path.stem for path in sorted(profile_root.glob("*.env"))]

    return {
        "schema_version": 1,
        "repository": {
            "root": str(repo),
            "branch": git(repo, "branch", "--show-current"),
            "revision": git(repo, "rev-parse", "HEAD"),
            "origin": git(repo, "remote", "get-url", "origin"),
            "dirty_path_count": len(git(repo, "status", "--short").splitlines()),
        },
        "policy": {
            "contract": policy["contract"],
            "stable_runner_branch": stable_runner,
            "stable_act_branch": stable_act,
            "required_sail_version": required_sail,
        },
        "runtime_files": [
            {"path": path, "exists": (repo / path).is_file()} for path in RUNTIME_FILES
        ],
        "boards": boards,
        "profiles": profiles,
        "uart_pipelines": pipelines,
        "job_installer": "ci/jenkins/install_apollo_uart_job.py",
        "portal_publisher_present": (repo / "ci" / "jenkins" / "publish_single_elf_result.py").is_file(),
        "external_components": {
            "portal_repository": policy["portal_repository"],
            "portal_source_present_in_runner_repo": False,
        },
        "warnings": warnings,
    }


def as_markdown(data: dict) -> str:
    repo = data["repository"]
    policy = data["policy"]
    lines = [
        "# RISC-V hardware CI inventory",
        "",
        f"- Repository branch: `{repo['branch'] or 'detached'}`",
        f"- Repository revision: `{repo['revision'] or 'unknown'}`",
        f"- Dirty paths: `{repo['dirty_path_count']}`",
        f"- Stable runner branch policy: `{policy['stable_runner_branch']}`",
        f"- Stable ACT branch policy: `{policy['stable_act_branch']}`",
        f"- Required Sail version: `{policy['required_sail_version']}`",
        f"- Policy source: `{policy['contract']}`",
        "",
        "## Boards",
        "",
        "| Board | Firmware | RAM range | UART | Packaging | Linker |",
        "|---|---:|---:|---:|---|---|",
    ]
    for board in data["boards"]:
        ram = f"{board['ram_base']}..{board['ram_limit']}"
        lines.append(
            f"| `{board['id']}` | `{board['firmware_load_address']}` | `{ram}` | "
            f"`{board['uart_base']}` | {'ready' if board['packaging_ready'] else 'MISSING'} | "
            f"{'resolved' if board['linker_resolves'] else 'UNRESOLVED'} |"
        )
    lines.extend([
        "",
        "## UART pipelines",
        "",
        "| Job | Runner branch | ACT branch | Locks |",
        "|---|---|---|---|",
    ])
    for pipeline in data["uart_pipelines"]:
        lines.append(
            f"| `{pipeline['job']}` | `{pipeline.get('runner_branch') or '-'}` | "
            f"`{pipeline.get('act_branch') or '-'}` | "
            f"`{', '.join(pipeline.get('hardware_locks', [])) or pipeline.get('hardware_lock_mode', 'none')}` |"
        )
    lines.extend(["", "## Warnings", ""])
    if data["warnings"]:
        lines.extend(f"- {warning}" for warning in data["warnings"])
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    try:
        policy = load_policy(args.contract.resolve())
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"system_inventory=ERROR detail={exc}", file=sys.stderr)
        return 2
    data = build_inventory(repo, policy)
    rendered = json.dumps(data, indent=2, sort_keys=True) + "\n" if args.format == "json" else as_markdown(data)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
