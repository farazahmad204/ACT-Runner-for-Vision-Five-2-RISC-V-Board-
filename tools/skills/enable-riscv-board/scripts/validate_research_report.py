#!/usr/bin/env python3
"""Validate one independent board-research evidence report."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import yaml


VALID_STATUS = {"unknown", "inferred", "partial", "confirmed"}
VALID_SOURCE_TYPES = {
    "vendor_documentation",
    "vendor_source",
    "project_source",
    "boot_log",
    "community",
}
ROLE_FACT_IDS = {
    "boot_image": {
        "identity.board_soc",
        "identity.isa",
        "boot.medium",
        "boot.chain",
        "boot.payload_slot",
        "boot.image_format",
        "boot.packaging",
        "boot.install",
        "boot.recovery",
    },
    "runner_contract": {
        "runner.load_address",
        "runner.entry_address",
        "runner.privilege",
        "runner.hart_entry",
        "runner.runner_hart",
        "runner.monitor_hart",
        "uart.base",
        "uart.register_width",
        "uart.register_spacing",
        "uart.registers",
        "uart.clock",
        "uart.initialization_owner",
        "timer.mswi_base",
        "timer.mtimecmp_base",
        "timer.mtime_address",
        "interrupt.external_cleanup",
    },
    "ddr_placement": {
        "memory.dram_base",
        "memory.documented_window",
        "memory.runner_range",
        "memory.payload_range",
        "memory.staging_range",
        "memory.intersecting_reservations",
        "memory.overlap_result",
    },
}
# Recommended facts: accepted for the role, but not required, so older
# reports stay valid. Each one was a real bring-up blocker on Milk-V Megrez.
RECOMMENDED_ROLE_FACT_IDS = {
    "boot_image": {
        "identity.priv_version",      # UDB/Sail priv version; H 1.0 needs >= 1.12
        "identity.h_version",         # draft vs ratified Hypervisor
        "boot.host_console",          # which USB-UART reaches the console UART
        "boot.recovery_requires",     # e.g. USB-C must be connected in recovery mode
    },
    "runner_contract": {
        "timer.mtime_hz",             # RX/test timeouts are in mtime ticks
        "uart.host_bridge",           # onboard USB-UART that drives the console RX line
        "interrupt.plic_base",
        "interrupt.uart_source",      # for ACT external-interrupt macros
        "interrupt.runner_contexts",  # PLIC M/S contexts of the runner hart
        "cpu.phys_addr_bits",
        "cpu.asid_bits",
    },
    "ddr_placement": {
        "memory.firmware_window",     # M-mode window of the stage the runner replaces
    },
}

# Catalog groups owned by each research role; bench items are user answers.
GROUP_ROLE = {"boot": "boot_image", "identity": "boot_image",
              "harts": "runner_contract", "uart": "runner_contract", "timer": "runner_contract",
              "interrupt": "runner_contract", "platform": "runner_contract",
              "memory": "ddr_placement", "act": "runner_contract"}


def _catalog_role_ids() -> dict[str, set[str]]:
    """Requirement IDs from assets/runner-requirements.yaml, by owning role."""
    path = Path(__file__).resolve().parent.parent / "assets" / "runner-requirements.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    by_role: dict[str, set[str]] = {}
    for req in data.get("requirements", []):
        role = GROUP_ROLE.get(req.get("group")) if isinstance(req, dict) else None
        if role:
            by_role.setdefault(role, set()).add(req["id"])
    return by_role


CATALOG_ROLE_IDS = _catalog_role_ids()

REQUIRED_RECORD_KEYS = {
    "id",
    "claim",
    "value",
    "source",
    "revision",
    "evidence_status",
    "impact",
    "open_question",
}


def validate(data: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["report root must be a mapping"]
    if data.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    for key in ("board_id", "agent_role"):
        if not data.get(key):
            errors.append(f"{key} is required")
    agent_role = data.get("agent_role")
    if agent_role not in ROLE_FACT_IDS:
        errors.append(f"agent_role must be one of {sorted(ROLE_FACT_IDS)}")
    sources = data.get("source_revisions")
    if not isinstance(sources, list) or not sources:
        errors.append("source_revisions must be a non-empty list")
    else:
        for index, source in enumerate(sources):
            if not isinstance(source, dict) or not source.get("name") or not source.get("url_or_path"):
                errors.append(f"source_revisions[{index}] requires name and url_or_path")
                continue
            if source.get("source_type") not in VALID_SOURCE_TYPES:
                errors.append(
                    f"source_revisions[{index}].source_type must be one of "
                    f"{sorted(VALID_SOURCE_TYPES)}"
                )
    records = data.get("evidence")
    if not isinstance(records, list) or not records:
        errors.append("evidence must be a non-empty list")
    else:
        seen: set[str] = set()
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                errors.append(f"evidence[{index}] must be a mapping")
                continue
            missing = REQUIRED_RECORD_KEYS - set(record)
            if missing:
                errors.append(f"evidence[{index}] missing keys: {sorted(missing)}")
            record_id = record.get("id")
            if not record_id:
                errors.append(f"evidence[{index}].id is required")
            elif record_id in seen:
                errors.append(f"duplicate evidence id: {record_id}")
            else:
                seen.add(str(record_id))
                allowed = (ROLE_FACT_IDS.get(agent_role, set()) | RECOMMENDED_ROLE_FACT_IDS.get(agent_role, set())
                           | CATALOG_ROLE_IDS.get(agent_role, set()))
                if agent_role in ROLE_FACT_IDS and record_id not in allowed:
                    errors.append(
                        f"evidence[{index}].id {record_id!r} is outside "
                        f"the {agent_role!r} research scope"
                    )
            if record.get("evidence_status") not in VALID_STATUS:
                errors.append(
                    f"evidence[{index}].evidence_status must be one of {sorted(VALID_STATUS)}"
                )
            if not record.get("source"):
                errors.append(f"evidence[{index}].source is required")
            if not record.get("claim"):
                errors.append(f"evidence[{index}].claim is required")
            if not record.get("impact"):
                errors.append(f"evidence[{index}].impact is required")
        if agent_role in ROLE_FACT_IDS:
            missing_ids = ROLE_FACT_IDS[agent_role] - seen
            if missing_ids:
                errors.append(
                    f"evidence is missing required {agent_role!r} fact IDs: "
                    f"{sorted(missing_ids)}; record unavailable facts as unknown"
                )
    for key in ("blockers", "conflicts", "recommendations"):
        items = data.get(key)
        if not isinstance(items, list):
            errors.append(f"{key} must be a list")
        else:
            for index, item in enumerate(items):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"{key}[{index}] must be a non-empty string")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    failed = False
    for report in args.reports:
        try:
            data = yaml.safe_load(report.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            print(f"ERROR {report}: {exc}", file=sys.stderr)
            failed = True
            continue
        errors = validate(data)
        for error in errors:
            print(f"ERROR {report}: {error}", file=sys.stderr)
        print(f"report={report} errors={len(errors)}")
        failed = failed or bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
