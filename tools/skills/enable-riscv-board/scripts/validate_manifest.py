#!/usr/bin/env python3
"""Validate the evidence and readiness gates in a board-onboarding manifest."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import yaml


VALID_EVIDENCE = {"unknown", "partial", "inferred", "confirmed"}
VALID_STATES = (
    "researching",
    "offline_prepared",
    "hardware_smoke_passed",
    "hypervisor_smoke_passed",
    "ci_operational",
)
SECTIONS = ("board", "cpu", "memory", "uart", "boot")
OPTIONAL_SECTIONS = ("power", "hypervisor", "ci")


def nested(data: dict, path: str):
    value = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def load_manifest(manifest: Path) -> dict:
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("manifest root must be a mapping")
    return data


def validate_manifest(data: dict) -> tuple[list[str], list[str], str | None, list[str], list[str]]:
    """Return errors, warnings, state, blockers, and missing offline requirements."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict):
        errors.append("manifest root must be a mapping")
        data = {}

    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    for section in SECTIONS:
        value = data.get(section)
        if not isinstance(value, dict):
            errors.append(f"missing mapping: {section}")
            continue
        status = value.get("evidence_status")
        if status not in VALID_EVIDENCE:
            errors.append(
                f"{section}.evidence_status must be one of {sorted(VALID_EVIDENCE)}"
            )

    for section in OPTIONAL_SECTIONS:
        value = data.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            errors.append(f"optional section must be a mapping: {section}")
            continue
        status = value.get("evidence_status")
        if status is not None and status not in VALID_EVIDENCE:
            errors.append(
                f"{section}.evidence_status must be one of {sorted(VALID_EVIDENCE)}"
            )

    for path in ("board.id", "board.vendor", "board.name", "board.soc", "cpu.isa"):
        if nested(data, path) in (None, ""):
            errors.append(f"required value is missing: {path}")

    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("at least one source is required")
    else:
        for index, source in enumerate(sources):
            if not isinstance(source, dict) or not source.get("url"):
                errors.append(f"sources[{index}] must contain a URL")

    readiness = data.get("readiness")
    if not isinstance(readiness, dict):
        errors.append("missing mapping: readiness")
        state = None
        blockers = []
    else:
        state = readiness.get("state")
        blockers = readiness.get("blockers")
        if state not in VALID_STATES:
            errors.append(f"readiness.state must be one of {list(VALID_STATES)}")
        if not isinstance(blockers, list):
            errors.append("readiness.blockers must be a list")
            blockers = []

    offline_required = (
        "cpu.runner_hart_id",
        "cpu.monitor_hart_id",
        "cpu.boot_privilege",
        "memory.dram_base",
        "memory.usable_limit",
        "memory.firmware_load_address",
        "memory.stack_bytes",
        "memory.staging_address",
        "memory.staging_max_bytes",
        "uart.controller",
        "uart.base",
        "uart.register_width",
        "timer.mtime_address",
        "boot.image_format",
        "boot.packaging_command",
        "boot.recovery_procedure",
    )
    missing_offline = [path for path in offline_required if nested(data, path) in (None, "")]
    if (
        nested(data, "uart.input_clock_hz") in (None, "")
        and nested(data, "uart.initialization_owner") in (None, "")
    ):
        missing_offline.append("uart.input_clock_hz_or_initialization_owner")

    if state in VALID_STATES and VALID_STATES.index(state) >= VALID_STATES.index("offline_prepared"):
        for path in missing_offline:
            errors.append(f"{state} requires: {path}")

    if state in {"hardware_smoke_passed", "hypervisor_smoke_passed", "ci_operational"}:
        if not nested(data, "board.physical_available"):
            errors.append(f"{state} requires board.physical_available=true")
        if not nested(data, "uart.bidirectional_validated"):
            errors.append(f"{state} requires uart.bidirectional_validated=true")

    if state in {"hypervisor_smoke_passed", "ci_operational"}:
        for path in (
            "hypervisor.csr_probe_validated",
            "hypervisor.test_suite_url",
            "hypervisor.execution_model",
            "hypervisor.smoke_validated",
        ):
            if nested(data, path) in (None, "", False):
                errors.append(f"{state} requires: {path}")

    if state == "researching" and not blockers:
        warnings.append("researching state should list unresolved blockers")
    if not nested(data, "board.physical_available"):
        warnings.append("physical board unavailable: hardware validation is blocked")
    for path in missing_offline:
        warnings.append(f"offline readiness missing: {path}")

    return errors, warnings, state, blockers, missing_offline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()

    try:
        data = load_manifest(args.manifest)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    errors, warnings, state, blockers, _ = validate_manifest(data)

    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)

    print(
        f"manifest={args.manifest} state={state or 'invalid'} "
        f"errors={len(errors)} warnings={len(warnings)} blockers={len(blockers)}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
