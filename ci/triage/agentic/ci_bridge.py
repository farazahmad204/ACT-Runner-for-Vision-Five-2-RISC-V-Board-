"""Run the ReAct agent inside the CI triage (ci_triage.run_triage).

The deterministic layers (people's verdicts, vault notes, memory, board rules) still answer
first; each remaining failure signature is investigated by the ReAct agent (react.py) on the
case files the job keeps (per_case/<case>/test.elf, test.sig.elf, test.S; act/config), and its
answer is mapped to the report fields the portal, EVALUATE and memory read.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .. import casefile, sail_trace, vault
from .. import spec as spec_index
from ..context import issue_terms, related_issues
from . import decoder, hardware, react, spec_alignment
from . import tools as agent_tools

BOARD_KNOWLEDGE = Path(__file__).resolve().parents[1] / "board_knowledge"
# The agent's four classes onto the portal's classes (and their workbook verdicts).
CLASS_MAP = {"DUT_RTL_Bug": "DUT noncompliance", "UDB_Config_Mismatch": "Reference-model mismatch",
             "Test_Suite_Error": "Test issue", "Environment_Flake": "Environment issue"}


def board_profile(run_root: Path, board: str) -> dict[str, Any]:
    """The board's hardware profile: its board knowledge and onboarding spec (the hardware as
    measured), completed from the ACT config the run used (act/config/*.yaml)."""
    profile = hardware.HardwareProfile(board_name=board)
    known = BOARD_KNOWLEDGE / f"{board}.yaml"
    if known.is_file():
        profile = hardware.load_profile_object(known)
    for config in sorted((run_root / "act" / "config").glob("*.yaml")):
        try:
            profile.merge(hardware.load_profile_object(config))
            break
        except (ValueError, OSError):
            continue
    hardware.finish(profile)
    from dataclasses import asdict
    return asdict(profile)


def case_inputs(run_root: Path, record: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    case = record["evidence"]["case"]
    folder = casefile.case_dir(run_root, case)
    log = run_root / str(record["case"].get("uart_log") or "")
    checks = (record.get("context") or {}).get("checks") or {}
    elf = folder / "test.elf"
    notes = list((record.get("context") or {}).get("items") or [])
    return {
        "case": case,
        "board": profile.get("board_name"),
        "log_path": str(log) if log.is_file() else None,
        # A kept ELF that is not the one the board ran is withheld (validate_context checked it).
        "elf_path": str(elf) if elf.is_file() and checks.get("elf_matches") is not False else None,
        # Only when a Sail reference can be run for this case (its Sail ELF and the board's sail.json).
        "sail_trace_path": (str(run_root / "triage" / "sail_traces" / f"{folder.name}.sig.trace")
                            if (folder / "test.sig.elf").is_file()
                            and (run_root / "act" / "config" / "sail.json").is_file() else None),
        "isa": profile.get("implemented_isa") or "",
        "test_source_path": str(folder / "test.S") if (folder / "test.S").is_file() else None,
        "context_checks": [n for n in notes if n.startswith(("MISMATCH", "UNCHECKED"))],
    }


def set_hooks(board_elf: Path | None, sig_elf: Path | None, sail_config: Path | None, sail_version: str,
              knowledge: dict[str, Any], profile: dict[str, Any], roots: list[Path]) -> None:
    """Point the tools at one failure's files: Sail runs on demand (sig_elf with sail_config) into
    the trace path the agent asks for, a board PC is mapped to the Sail ELF by symbol, spec
    searches use the board's spec versions, and tools may read only under `roots`."""
    def generate(trace: Path) -> bool:
        binary = sail_trace.sail_binary(sail_version)
        if not (binary and sig_elf and sig_elf.is_file() and sail_config and sail_config.is_file()):
            return False
        trace.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([binary, "--config", str(sail_config), "--inst-limit", str(sail_trace.INST_LIMIT),
                            *sail_trace.TRACE_FLAGS, "--trace-output", str(trace), str(sig_elf)],
                           capture_output=True, timeout=int(os.environ.get("TRIAGE_SAIL_TIMEOUT", "180")))
        except (OSError, subprocess.SubprocessError):
            return False
        return trace.is_file()

    def map_pc(pc: int):
        if board_elf and sig_elf and board_elf.is_file() and sig_elf.is_file():
            return sail_trace.map_pc(board_elf, sig_elf, pc)
        return None

    hypervisor = "H" in (profile.get("extensions") or [])

    def search(query: str) -> str:
        found = spec_index.ground(spec_index.terms_for(query.split()), knowledge, hypervisor=hypervisor)
        return spec_index.render(found)

    agent_tools.SAIL_HOOKS.clear()
    agent_tools.SAIL_HOOKS.update(generate=generate, map_pc=map_pc)
    agent_tools.SPEC_HOOK.clear()
    agent_tools.SPEC_HOOK.update(search=search)
    os.environ["TRIAGE_TOOL_ROOTS"] = ":".join(str(r.resolve()) for r in roots)


def install_hooks(run_root: Path, case: str, knowledge: dict[str, Any], profile: dict[str, Any]) -> None:
    """set_hooks for a case in a CI run directory (its kept case files)."""
    folder = casefile.case_dir(run_root, case)
    version = re.search(r"sail_version=(\S+)", casefile.act_revision(run_root))
    set_hooks(folder / "test.elf", folder / "test.sig.elf", run_root / "act" / "config" / "sail.json",
              version.group(1) if version else "", knowledge, profile, [run_root])


def failure_symptom(record: dict[str, Any]) -> dict[str, Any]:
    """The failure footprint from the deterministic evidence, for fetch_isa_invariants."""
    evidence = record["evidence"]
    ex = evidence.get("extracted", {})
    cause = ex.get("trap_mcause") or ex.get("xcause") or ex.get("mcause")
    trap = {}
    if cause:
        try:
            trap = {"mcause": cause, "cause_name": decoder.decode_register("mcause", str(cause))["fields"]["name"]}
        except ValueError:
            trap = {"mcause": cause}
    return {"case": evidence.get("case"), "category": evidence.get("deterministic_category"),
            "failure": ex.get("failure") or ex.get("test_info"), "trap": trap,
            "mismatch": {"register": ex.get("mismatching_field") or ex.get("register")},
            "trap_handler_mode": ex.get("trap_handler_mode")}


def test_alignment(run_root: Path, record: dict[str, Any], profile: dict[str, Any], inputs: dict[str, Any]) -> str:
    """[TEST INTENT] and [SPECIFICATION INVARIANTS] for the agent's first message. The failing
    check is the label at the trap PC (else the reported failing address) in the board ELF."""
    if not inputs.get("test_source_path"):
        return ""
    roots = spec_alignment.testplan_roots(run_root)
    if roots:
        os.environ["TRIAGE_ACT_ROOT"] = str(roots[0].parent)
    ex = record["evidence"].get("extracted", {})
    label = None
    elf = inputs.get("elf_path")
    for key in ("xepc", "trap_mepc", "mepc", "approx_address"):
        try:
            pc = int(str(ex.get(key)), 16)
        except (TypeError, ValueError):
            continue
        if elf and pc:
            below = [(a, n) for a, n in sail_trace._symbols(elf) if a <= pc]
            if below:
                label = max(below)[1]
                break
    intent = spec_alignment.extract_test_intent(inputs["test_source_path"], label, run_root)
    invariants = spec_alignment.fetch_isa_invariants(failure_symptom(record), profile, intent.get("rule_ids") or [])
    return spec_alignment.render_sections(intent, invariants)


def to_answer(final: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """The validated triage report as the portal/memory answer; the report itself is kept whole
    under "triage_report". FAILED_TO_ISOLATE is Inconclusive whatever its class."""
    isolated = final.get("triage_status") == "SUCCESS"
    klass = CLASS_MAP.get(final.get("failure_classification"), "Inconclusive") if isolated else "Inconclusive"
    steps = [s for s in state.get("steps", []) if s.get("tool") not in ("(none)",)]
    searched_spec = any(s.get("tool") == "tool_search_spec" and s.get("ok") for s in steps)
    notes = []
    if klass == "Inconclusive":
        confidence = "low"
    elif klass == "DUT noncompliance" and not searched_spec:
        confidence = "low"
        notes.append("a hardware bug without a specification search")
    else:
        confidence = "medium"
    if state.get("status") == "budget_exhausted":
        notes.append("no valid report within the step budget")
    tier1 = final.get("tier_1_root_cause") or {}
    tier2 = final.get("tier_2_board_specific_remediation") or {}
    where = " ".join(p for p in (tier1.get("failing_instruction"), f"at {tier1['failing_pc']}"
                                 if tier1.get("failing_pc") else "") if p)
    answer = {
        "classification": klass,
        "agent_classification": final.get("failure_classification"),
        "failure_reason": (f"{where}: " if where else "") + str(tier1.get("architectural_symptom", "")),
        "root_cause": str(tier1.get("architectural_symptom", "")),
        "next_step": f"[{tier2.get('remediation_type')}: {tier2.get('target_repository_or_file')}] "
                     f"{tier2.get('actionable_fix_directive', '')}" if tier2 else "",
        "confidence": confidence,
        "triage_report": final,
        "investigation": [{"step": s["step"], "hypothesis": (s.get("thought") or "")[:300], "tool": s["tool"],
                           "argument": str(s.get("arguments"))[:200]} for s in steps],
        "sanity": state.get("sanity", []),
        "findings": [f["text"] for f in state.get("findings", [])],
    }
    if notes:
        answer["checked"] = "; ".join(notes)
    return answer


def analyze(run_root: Path, record: dict[str, Any], knowledge: dict[str, Any], profile: dict[str, Any],
            out_dir: Path, provider: str, model: str, max_steps: int, codex_bin: str,
            backend_factory: Callable[[Any], Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Investigate one failure signature (its lead record) with the ReAct agent."""
    inputs = case_inputs(run_root, record, profile)
    install_hooks(run_root, inputs["case"], knowledge, profile)
    hardware_text = hardware.get_hardware_context_string(profile)
    alignment = test_alignment(run_root, record, profile, inputs)
    # The team's issue notes that match (not ones that already explain it: those answered
    # before the agent) and similar riscv-arch-test issues.
    lines = vault.context_lines(record.get("known_issues") or [])
    lines += related_issues(inputs["case"], issue_terms(record["evidence"]),
                            exclude={n["id"] for n in record.get("known_issues") or []})
    backend = None
    if backend_factory is not None:
        backend = backend_factory(react.CaseState(out_dir / "agent_state.json", inputs["case"], inputs))
    final = react.run_react(inputs["case"], inputs, out_dir, backend_name=provider, model=model,
                            max_steps=max_steps, backend=backend, hardware=hardware_text, profile=profile,
                            codex_bin=codex_bin, known_issues="\n".join(lines), alignment=alignment)
    state = react.CaseState(out_dir / "agent_state.json").data
    metadata = {"provider": provider, "model": model or f"{provider} default", "agent": "react",
                "steps": len(state.get("steps", [])), "status": state.get("status"),
                "first_message_chars": (state.get("payload") or {}).get("chars"),
                "knowledge_filter": state.get("knowledge_filter")}
    return to_answer(final, state), metadata
