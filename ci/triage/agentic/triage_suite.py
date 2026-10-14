#!/usr/bin/env python3
"""Triage one ACT failure end to end with the ReAct agent, from explicit files.

    python3 ci/triage/agentic/triage_suite.py \\
        --log per_case/ExceptionsSm-00/uart.log --elf per_case/ExceptionsSm-00/test.elf \\
        --board-config ci/triage/board_knowledge/vf2_jh7110.yaml [--board-config act/config/x.yaml] \\
        [--sig-elf test.sig.elf --sail-config act/config/sail.json --sail-version 0.15] \\
        [--test-source test.S] [--case ExceptionsSm-00] --out-dir triage_out \\
        [--backend codex|openai|anthropic] [--model M] [--max-steps 12] [--dry-run]

1. Ingestion: the files are checked first; a missing or unreadable one stops the run with a
   diagnostic that says which argument and why (exit 2).
2. Context: hardware.py builds the Target Hardware Profile from the board configs (board
   knowledge, onboarding spec, UDB config or ACT test_config, in the order given: the first
   that states a field wins); spec_alignment.py gives the test intent (failing check from the
   PC in the log) and the specification invariants.
3. Agent: react.run_react runs the ReAct loop with that context; Sail runs on demand when the
   Sail ELF and sail.json are given.
4. Output: <out-dir>/triage_report.json (the validated two-tier report, for CI) and
   agent_state.json (every step); a markdown summary on stdout for engineers.

Exit: 0 root cause isolated, 1 FAILED_TO_ISOLATE, 2 bad input, 3 the agent could not run.
--dry-run assembles and prints the context without calling a model.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a script: import the package from the repository root
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "ci.triage.agentic"

from ci.triage import sail_trace  # noqa: E402
from ci.triage.agentic import ci_bridge, hardware, react, report_schema, spec_alignment  # noqa: E402
from ci.triage.agentic import tools as agent_tools  # noqa: E402


class InputError(Exception):
    """A missing or unusable input; the message says which and why."""


# ---------- 1. ingestion ----------

def need_file(path: str | None, what: str, required: bool = True) -> Path | None:
    if not path:
        if required:
            raise InputError(f"{what}: no path given")
        return None
    file = Path(path).expanduser()
    if not file.exists():
        raise InputError(f"{what}: {file} does not exist")
    if not file.is_file():
        raise InputError(f"{what}: {file} is not a file")
    try:
        with file.open("rb") as handle:
            head = handle.read(4)
    except OSError as exc:
        raise InputError(f"{what}: {file} cannot be read ({exc})") from exc
    if what.endswith("ELF") and head != b"\x7fELF":
        raise InputError(f"{what}: {file} is not an ELF file (starts with {head!r})")
    return file.resolve()


def ingest(args: argparse.Namespace) -> dict[str, Any]:
    files = {
        "log": need_file(args.log, "--log (UART/RVCP failure log)"),
        "elf": need_file(args.elf, "--elf (the ELF that ran on the board): ELF"),
        "sig_elf": need_file(args.sig_elf, "--sig-elf (the Sail-built ELF): ELF", required=False),
        "sail_config": need_file(args.sail_config, "--sail-config (the board's sail.json)", required=False),
        "test_source": need_file(args.test_source, "--test-source (the test's .S)", required=False),
    }
    if not args.board_config:
        raise InputError("--board-config: give at least one board description (board_knowledge/<board>.yaml, "
                         "an onboarding board spec, the ACT UDB config or its test_config.yaml)")
    files["board_configs"] = [need_file(p, f"--board-config #{i + 1}") for i, p in enumerate(args.board_config)]
    if bool(files["sig_elf"]) != bool(files["sail_config"]):
        raise InputError("--sig-elf and --sail-config go together (Sail needs both to run the reference)")
    text = files["log"].read_text(errors="replace")
    if not text.strip():
        raise InputError(f"--log: {files['log']} is empty")
    return files


# ---------- 2. context ----------

def build_profile(configs: list[Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(profile, board knowledge for spec search) from the board configs, first source wins."""
    profile = hardware.HardwareProfile()
    knowledge: dict[str, Any] = {}
    for path in configs:
        try:
            profile.merge(hardware.load_profile_object(path))
        except (ValueError, OSError) as exc:
            raise InputError(f"--board-config {path}: {exc}") from exc
        if not knowledge:
            data = hardware.yaml.safe_load(path.read_text(encoding="utf-8")) if path.suffix in (".yaml", ".yml") else {}
            if isinstance(data, dict) and data.get("spec_versions"):
                knowledge = data
    hardware.finish(profile)
    from dataclasses import asdict
    data = asdict(profile)
    if not data.get("xlen"):
        raise InputError("--board-config: no XLEN in any board description; add the UDB config or the board "
                         "spec (missing: " + ", ".join(data.get("missing") or []) + ")")
    return data, knowledge


def failing_label(elf: Path, footprint: dict[str, Any]) -> tuple[str | None, str | None]:
    """(pc, label): the trap PC (else the failing PC) from the log, and the symbol it is under."""
    trap = footprint.get("trap") or {}
    for value in (trap.get("mepc"), footprint.get("failing_pc")):
        try:
            pc = int(str(value), 16)
        except (TypeError, ValueError):
            continue
        below = [(a, n) for a, n in sail_trace._symbols(str(elf)) if a <= pc]
        return f"0x{pc:x}", (max(below)[1] if below else None)
    return None, None


def assemble(files: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    profile, knowledge = build_profile(files["board_configs"])
    roots = sorted({p.parent for p in [files["log"], files["elf"], *(files[k] for k in ("sig_elf", "sail_config",
                                                                                        "test_source") if files[k])]})
    out_dir = Path(args.out_dir).resolve()
    ci_bridge.set_hooks(files["elf"], files["sig_elf"], files["sail_config"], args.sail_version, knowledge,
                        profile, [*roots, out_dir])
    footprint = agent_tools.dispatch("tool_parse_rvcp_log", {"log_path": str(files["log"])}, profile)
    if not footprint.get("ok"):
        raise InputError(f"--log: {footprint.get('error')}")
    case = args.case or footprint.get("case") or files["log"].parent.name
    pc, label = failing_label(files["elf"], footprint)
    intent = spec_alignment.extract_test_intent(str(files["test_source"]), label) if files["test_source"] else {
        "note": "no --test-source given"}
    invariants = spec_alignment.fetch_isa_invariants(footprint, profile, intent.get("rule_ids") or [])
    inputs = {"case": case, "board": profile.get("board_name"), "log_path": str(files["log"]),
              "elf_path": str(files["elf"]), "test_source_path": str(files["test_source"] or "") or None,
              "sail_trace_path": str(out_dir / "sail" / f"{case}.sig.trace") if files["sig_elf"] else None,
              "isa": profile.get("implemented_isa")}
    return {"case": case, "profile": profile, "inputs": inputs, "out_dir": out_dir, "pc": pc, "label": label,
            "hardware": hardware.get_hardware_context_string(profile),
            "alignment": spec_alignment.render_sections(intent, invariants), "intent": intent}


# ---------- 4. output ----------

def markdown(report: dict[str, Any], state: dict[str, Any], out_dir: Path) -> str:
    t1, t2 = report["tier_1_root_cause"], report["tier_2_board_specific_remediation"]
    lines = [f"# Triage: {state.get('case')} on {report['target_board']}", "",
             f"**{report['triage_status']}**: `{report['failure_classification']}`", "",
             "## Tier 1: root cause", "",
             f"- Failing PC: `{t1['failing_pc'] or 'not isolated'}`  {t1['failing_instruction']}",
             f"- Divergence: {t1['divergence_cycle']}",
             f"- Symptom: {t1['architectural_symptom']}", "",
             "## Tier 2: remediation", "",
             f"- {t2['remediation_type']} in `{t2['target_repository_or_file']}`", "",
             t2["actionable_fix_directive"], ""]
    sanity = state.get("sanity") or []
    if sanity:
        lines += ["## Architectural sanity checks", ""] + [f"- {s['check']}: {s['message']}" for s in sanity] + [""]
    lines += [f"{len(state.get('steps') or [])} agent steps ({state.get('status')}). "
              f"Report: `{out_dir / 'triage_report.json'}`; steps: `{out_dir / 'agent_state.json'}`."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", required=True, help="the failure's UART/RVCP log")
    parser.add_argument("--elf", required=True, help="the ELF that ran on the board")
    parser.add_argument("--board-config", action="append", default=[],
                        help="board description (repeatable; first source of a field wins)")
    parser.add_argument("--sig-elf", help="the Sail-built ELF of the same test (with --sail-config)")
    parser.add_argument("--sail-config", help="the board's sail.json")
    parser.add_argument("--sail-version", default="", help="Sail version of the ACT revision (default: legacy)")
    parser.add_argument("--test-source", help="the test's generated .S (for the test intent)")
    parser.add_argument("--case", help="test name (default: from the log)")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--backend", choices=["codex", "openai", "anthropic"], default="codex")
    parser.add_argument("--model", default="")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true", help="assemble and print the context; no model call")
    args = parser.parse_args(argv)

    try:
        files = ingest(args)
        context = assemble(files, args)
    except InputError as exc:
        print(f"triage_suite: input error: {exc}", file=sys.stderr)
        return 2
    out_dir = context["out_dir"]
    if args.dry_run:
        print(react.build_user_payload(context["case"], context["inputs"], args.max_steps, context["hardware"],
                                       react.platform_errata(context["profile"]), alignment=context["alignment"]))
        return 0
    if args.backend == "codex" and not shutil.which(args.codex_bin):
        print(f"triage_suite: the Codex CLI ({args.codex_bin}) is not installed; use --backend openai or "
              "anthropic, or --dry-run", file=sys.stderr)
        return 3
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        report = react.run_react(context["case"], context["inputs"], out_dir, args.backend, args.model,
                                 args.max_steps, hardware=context["hardware"], profile=context["profile"],
                                 codex_bin=args.codex_bin, alignment=context["alignment"])
        report_schema.parse_report(report, context["profile"])
    except report_schema.SchemaError as exc:
        print(f"triage_suite: the report does not match the schema: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # the model or its CLI failed: say so, keep what was saved
        print(f"triage_suite: the agent could not run: {type(exc).__name__}: {exc}\n"
              f"  steps so far: {out_dir / 'agent_state.json'}", file=sys.stderr)
        return 3
    state = json.loads((out_dir / "agent_state.json").read_text())
    print(markdown(report, state, out_dir))
    return 0 if report["triage_status"] == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
