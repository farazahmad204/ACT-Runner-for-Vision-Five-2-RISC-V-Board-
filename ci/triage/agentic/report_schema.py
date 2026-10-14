"""The triage report: the exact JSON the agent must return, and its strict validation.

TRIAGE_REPORT_SCHEMA is given to the model as the submit_analysis tool's schema (OpenAI strict
function, Anthropic input_schema, Codex output schema). validate_report() checks a payload
against it with no third-party package (pydantic and jsonschema are not installed on the CI
agent): types, enums, required keys, no extra keys, string patterns, and the "string or
integer" union. check_report() adds the rules a schema cannot express (target board, RTL only
on open cores, sanity checks). parse_report() returns the typed TriageReport.

    {
      "triage_status": "SUCCESS | FAILED_TO_ISOLATE",
      "target_board": "<the hardware profile's board>",
      "failure_classification": "DUT_RTL_Bug | UDB_Config_Mismatch | Test_Suite_Error | Environment_Flake",
      "tier_1_root_cause": {"failing_pc", "failing_instruction", "architectural_symptom", "divergence_cycle"},
      "tier_2_board_specific_remediation": {"remediation_type": "RTL_PATCH | FIRMWARE_WORKAROUND |
          TOOLCHAIN_FLAGS_UPDATE | CONFIG_REMAP", "target_repository_or_file", "actionable_fix_directive"}
    }

One addition: failure_classification may be "Unclassified", only with FAILED_TO_ISOLATE (a failed
investigation must not claim a class).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CLASSES = ["DUT_RTL_Bug", "UDB_Config_Mismatch", "Test_Suite_Error", "Environment_Flake"]
UNCLASSIFIED = "Unclassified"
REMEDIATIONS = ["RTL_PATCH", "FIRMWARE_WORKAROUND", "TOOLCHAIN_FLAGS_UPDATE", "CONFIG_REMAP"]
HEX_OR_EMPTY = r"^(0x[0-9a-fA-F]{1,16})?$"

TRIAGE_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["triage_status", "target_board", "failure_classification", "tier_1_root_cause",
                 "tier_2_board_specific_remediation"],
    "properties": {
        "triage_status": {"type": "string", "enum": ["SUCCESS", "FAILED_TO_ISOLATE"]},
        "target_board": {"type": "string", "minLength": 1,
                         "description": "The board_name of the Target Hardware Profile, exactly."},
        "failure_classification": {"type": "string", "enum": CLASSES + [UNCLASSIFIED],
                                   "description": "Unclassified only with FAILED_TO_ISOLATE."},
        "tier_1_root_cause": {
            "type": "object",
            "additionalProperties": False,
            "required": ["failing_pc", "failing_instruction", "architectural_symptom", "divergence_cycle"],
            "properties": {
                "failing_pc": {"type": "string", "pattern": HEX_OR_EMPTY,
                               "description": "Address in the board ELF, 0x-prefixed hex (empty only if not isolated)."},
                "failing_instruction": {"type": "string", "description": "Its assembly."},
                "architectural_symptom": {"type": "string", "minLength": 1,
                                          "description": "The register or memory drift: what the board produced vs "
                                                         "what was expected, with values, and the rule involved."},
                "divergence_cycle": {"type": ["string", "integer"],
                                     "description": "Sail's instruction step at the divergence if traceable (the "
                                                    "board has no cycle trace), else \"not traceable\"."},
            },
        },
        "tier_2_board_specific_remediation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["remediation_type", "target_repository_or_file", "actionable_fix_directive"],
            "properties": {
                "remediation_type": {"type": "string", "enum": REMEDIATIONS},
                "target_repository_or_file": {"type": "string", "minLength": 1,
                                              "description": "e.g. an OpenSBI platform directory, the runner file, "
                                                             "the ACT config, or an HDL module source path."},
                "actionable_fix_directive": {"type": "string", "minLength": 1,
                                             "description": "Step-by-step, within the board's constraints."},
            },
        },
    },
}


# ---------- typed model ----------

@dataclass(frozen=True)
class Tier1RootCause:
    failing_pc: str
    failing_instruction: str
    architectural_symptom: str
    divergence_cycle: str | int


@dataclass(frozen=True)
class Tier2Remediation:
    remediation_type: str
    target_repository_or_file: str
    actionable_fix_directive: str


@dataclass(frozen=True)
class TriageReport:
    triage_status: str
    target_board: str
    failure_classification: str
    tier_1_root_cause: Tier1RootCause
    tier_2_board_specific_remediation: Tier2Remediation


class SchemaError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# ---------- validation ----------

def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return False


def validate_report(payload: Any, schema: dict[str, Any] = TRIAGE_REPORT_SCHEMA, path: str = "") -> list[str]:
    """Every way payload departs from schema (empty when it matches exactly)."""
    where = path or "report"
    expected = schema.get("type")
    kinds = expected if isinstance(expected, list) else [expected]
    if expected and not any(_type_ok(payload, k) for k in kinds):
        return [f"{where} must be {' or '.join(kinds)}, got {type(payload).__name__}"]
    problems: list[str] = []
    if "enum" in schema and payload not in schema["enum"]:
        problems.append(f"{where} must be one of {schema['enum']}, got {payload!r}")
    if isinstance(payload, str):
        if len(payload.strip()) < schema.get("minLength", 0):
            problems.append(f"{where} must not be empty")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], payload):
            problems.append(f"{where} {payload!r} does not match {schema['pattern']}")
    if isinstance(payload, dict) and "properties" in schema:
        for key in schema.get("required", []):
            if key not in payload:
                problems.append(f"{where}.{key} is missing")
        if schema.get("additionalProperties") is False:
            for key in sorted(set(payload) - set(schema["properties"])):
                problems.append(f"{where}.{key} is not allowed")
        for key, sub in schema["properties"].items():
            if key in payload:
                problems += validate_report(payload[key], sub, f"{where}.{key}")
    return problems


HDL_FILE = re.compile(r"\.(sv|svh|v|vh|vhd|vhdl|scala)\b", re.I)


def check_report(payload: dict[str, Any], profile: dict[str, Any] | None = None,
                 sanity: list[dict[str, Any]] | None = None) -> list[str]:
    """validate_report, plus what the schema cannot say."""
    problems = validate_report(payload)
    if problems:
        return problems
    profile = profile or {}
    status, klass = payload["triage_status"], payload["failure_classification"]
    tier1, tier2 = payload["tier_1_root_cause"], payload["tier_2_board_specific_remediation"]
    board = profile.get("board_name")
    if board and payload["target_board"] != board:
        problems.append(f"target_board must be {board!r} (the hardware profile), got {payload['target_board']!r}")
    if klass == UNCLASSIFIED and status != "FAILED_TO_ISOLATE":
        problems.append("Unclassified is only for FAILED_TO_ISOLATE; a SUCCESS needs one of " + ", ".join(CLASSES))
    if status == "SUCCESS" and not tier1["failing_pc"]:
        problems.append("a SUCCESS must give tier_1_root_cause.failing_pc (0x-prefixed hex)")
    core = profile.get("core_type") or "unknown"
    if tier2["remediation_type"] == "RTL_PATCH":
        if core != "soft_core" or not profile.get("rtl_source"):
            problems.append(f"RTL_PATCH is not possible on this core (core type {core}): use FIRMWARE_WORKAROUND, "
                            "TOOLCHAIN_FLAGS_UPDATE or CONFIG_REMAP")
        elif not HDL_FILE.search(tier2["target_repository_or_file"]):
            problems.append("an RTL_PATCH must name the HDL module source file in target_repository_or_file")
    if status == "SUCCESS":
        for item in sanity or []:
            if klass not in item["allowed"]:
                problems.append(f"{klass} contradicts the architectural sanity check {item['check']} "
                                f"({item['message']}); allowed: {', '.join(item['allowed'])}")
    return problems


def parse_report(payload: dict[str, Any], profile: dict[str, Any] | None = None,
                 sanity: list[dict[str, Any]] | None = None) -> TriageReport:
    """The typed report; SchemaError lists every problem when the payload is not valid."""
    problems = check_report(payload, profile, sanity)
    if problems:
        raise SchemaError(problems)
    return TriageReport(
        triage_status=payload["triage_status"], target_board=payload["target_board"],
        failure_classification=payload["failure_classification"],
        tier_1_root_cause=Tier1RootCause(**payload["tier_1_root_cause"]),
        tier_2_board_specific_remediation=Tier2Remediation(**payload["tier_2_board_specific_remediation"]))


def unresolved_report(board: str, reason: str, findings: list[str] = ()) -> dict[str, Any]:
    """The report written when the agent ends without a valid answer (it still validates)."""
    return {
        "triage_status": "FAILED_TO_ISOLATE",
        "target_board": board or "unknown",
        "failure_classification": UNCLASSIFIED,
        "tier_1_root_cause": {"failing_pc": "", "failing_instruction": "",
                              "architectural_symptom": "; ".join(findings) or reason,
                              "divergence_cycle": "not traceable"},
        "tier_2_board_specific_remediation": {
            "remediation_type": "CONFIG_REMAP", "target_repository_or_file": "triage agent run",
            "actionable_fix_directive": reason + " Review agent_state.json and rerun the triage."},
    }
