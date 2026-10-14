"""Test intent and specification invariants for the failure the agent investigates.

extract_test_intent(test_source_path, failing_label=None) -> dict
    What the failing test checks, from the generated assembly and the ACT test plan:
    - the test header: suite, required extensions, MARCH, parameter constraints, boot mode;
    - the coverpoints the file covers (its `<Suite>_cg_<cp>_<bin>` check labels);
    - the failing check, when the failing PC's label is given: coverpoint and bin;
    - from testplans/priv/<suite>.yaml (or testplans/<ext>.csv for unprivileged tests): each
      coverpoint's goal, description, expectation, bins and normative rule ids.

fetch_isa_invariants(failure_symptom, profile=None, rule_ids=()) -> str
    The rules that govern the failing behavior: the normative rules the test plan names (exact
    text from the spec index) and the curated knowledge-base rules for the failure's topics
    (riscv_privileged_rules.md and act4_rules.md first; another board's notes never).

The agent's first message carries both under [TEST INTENT] and [SPECIFICATION INVARIANTS].
"""

from __future__ import annotations

import csv
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .. import spec as spec_index
from .knowledge import curated_knowledge_for, filter_knowledge, inject_curated_knowledge

CHECK_LABEL = re.compile(r"^([A-Za-z0-9]+)_cg_(cp_[A-Za-z0-9_]+?)(?:_([A-Za-z0-9]+))?:\s*$", re.M)
INVARIANT_FILES = ("riscv_privileged_rules.md", "act4_rules.md")


# ---------- where the test plans are ----------

def testplan_roots(run_root: Path | None = None) -> list[Path]:
    """testplans/ directories to search: the run's own copy (collect_case_files keeps one), then
    TRIAGE_ACT_ROOT, then ACT checkouts on the agent (the one at the run's ACT commit first)."""
    roots: list[Path] = []
    if run_root is not None:
        roots.append(run_root / "act" / "testplans")
    if os.environ.get("TRIAGE_ACT_ROOT"):
        roots.append(Path(os.environ["TRIAGE_ACT_ROOT"]) / "testplans")
    candidates = sorted(Path("/data/ci/agent/workspace").glob("*/external/riscv-arch-test/testplans"))
    candidates += [Path.home() / name / "testplans" for name in ("act-sail015", "riscv-arch-test")]
    commit = ""
    revision = run_root / "act" / "revision.txt" if run_root else None
    if revision and revision.is_file():
        found = re.search(r"act_commit=(\w+)", revision.read_text())
        commit = found.group(1) if found else ""

    def at_commit(path: Path) -> bool:
        head = path.parent / ".git" / "HEAD"
        return bool(commit) and head.is_file() and commit[:7] in head.read_text()

    roots += sorted(candidates, key=lambda p: not at_commit(p))
    return [r for r in roots if r.is_dir()]


@lru_cache(maxsize=8)
def _priv_index(root: str) -> dict[str, dict[str, Any]]:
    """Coverpoint name -> its test-plan entry (with its suite), across testplans/priv/*.yaml."""
    index: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(root, "priv").glob("*.yaml")):
        try:
            plan = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        for entry in plan.get("coverpoints") or []:
            if isinstance(entry, dict) and entry.get("name"):
                index.setdefault(entry["name"], {**entry, "suite": plan.get("suite") or path.stem,
                                                 "plan": f"testplans/priv/{path.name}"})
    return index


def _csv_plan(root: Path, extension: str, instruction: str) -> dict[str, Any] | None:
    """An unprivileged instruction's row in testplans/<ext>.csv: the coverpoints marked for it."""
    path = root / f"{extension}.csv"
    if not path.is_file():
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("Instruction", "").lower() == instruction.lower():
                return {"plan": f"testplans/{path.name}", "instruction": row["Instruction"],
                        "format": row.get("Type"),
                        "coverpoints": [k for k, v in row.items() if k.startswith(("cp_", "cr_", "cmp_"))
                                        and v and v.strip()]}
    return None


# ---------- 1. test intent ----------

def parse_header(text: str) -> dict[str, Any]:
    head: dict[str, Any] = {}
    name = re.search(r"^//\s*([A-Za-z0-9_]+)/([A-Za-z0-9_.-]+)\.S\s*$", text, re.M)
    if name:
        head.update(suite=name.group(1), test=name.group(2))
    config = re.search(r"#####\s*START_TEST_CONFIG\s*#####(.*?)#####\s*END_TEST_CONFIG", text, re.S)
    if config:
        block = config.group(1)
        required = re.search(r"REQUIRED_EXTENSIONS:\s*\[([^\]]*)\]", block)
        if required:
            head["required_extensions"] = re.findall(r"'([^']+)'", required.group(1))
        march = re.search(r"MARCH:\s*(\S+)", block)
        if march:
            head["march"] = march.group(1)
        params = [line.strip("# ").strip() for line in block.splitlines()
                  if line.strip().startswith("#") and "REQUIRED_EXTENSIONS" not in line
                  and "MARCH" not in line and line.strip("# ").strip()]
        if params and params != ["no param constraints"]:
            head["parameter_constraints"] = params
    boot = re.search(r"^#define\s+(BOOT_TO_[A-Z]+MODE)\b", text, re.M)
    if boot:
        head["boot_mode"] = boot.group(1)
    return head


def extract_test_intent(test_source_path: str, failing_label: str | None = None,
                        run_root: Path | None = None) -> dict[str, Any]:
    """What the failing test checks (see the module docstring). Every field comes from the
    test source or the test plan; a coverpoint the plans do not list says so."""
    path = Path(test_source_path)
    intent: dict[str, Any] = {"source": path.name}
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    if not text:
        intent["note"] = "the test source was not kept"
        return intent
    intent.update(parse_header(text))
    labels = CHECK_LABEL.findall(text)
    covered = list(dict.fromkeys(cp for _suite, cp, _bin in labels))
    intent["coverpoints_in_test"] = covered[:20]
    target = None
    if failing_label:
        label = failing_label.split("+")[0]
        match = re.match(r"^([A-Za-z0-9]+)_cg_(cp_[A-Za-z0-9_]+)$", label)
        if match:
            # Split <cp>_<bin> at the longest coverpoint name the file has.
            name = match.group(2)
            target = max((cp for cp in covered if name == cp or name.startswith(cp + "_")), key=len, default=None)
            intent["failing_check"] = {"label": label, "coverpoint": target or name,
                                       "bin": name[len(target) + 1:] if target and name != target else None}
    roots = testplan_roots(run_root)
    wanted = [target] if target else covered[:6]
    plans = []
    for root in roots[:1]:
        index = _priv_index(str(root))
        for cp in wanted:
            entry = index.get(cp)
            if entry:
                plans.append({k: entry.get(k) for k in ("name", "suite", "plan", "goal", "description",
                                                        "expectation", "bins", "rules") if entry.get(k)})
        if not plans and intent.get("suite") and "-" in intent.get("test", ""):
            # An unprivileged test (I-add-00): its instruction's row in testplans/<ext>.csv.
            ext, instruction = intent["test"].split("-")[:2]
            row = _csv_plan(root, ext, instruction)
            if row:
                plans.append(row)
        intent["testplan_root"] = str(root)
    if plans:
        intent["testplan"] = plans
    elif roots:
        intent["note"] = "the test plans do not list " + (", ".join(wanted) or "this test")
    else:
        intent["note"] = "no ACT testplans/ directory is available"
    intent["rule_ids"] = sorted({r.strip("[]").removeprefix("norm:") for p in plans for r in p.get("rules") or []})
    return intent


# ---------- 2. specification invariants ----------

def symptom_text(failure_symptom: dict[str, Any]) -> str:
    """The failure as retrieval text: category, failure message, trap cause, mismatching field."""
    trap = failure_symptom.get("trap") or {}
    mismatch = failure_symptom.get("mismatch") or {}
    parts = [failure_symptom.get("case"), failure_symptom.get("category"), failure_symptom.get("failure"),
             f"mcause={trap['mcause']}" if trap.get("mcause") else None, trap.get("cause_name"),
             mismatch.get("register"), failure_symptom.get("trap_handler_mode"),
             failure_symptom.get("instruction_asm")]
    return " ".join(str(p) for p in parts if p)


def fetch_isa_invariants(failure_symptom: dict[str, Any], profile: dict[str, Any] | None = None,
                         rule_ids: list[str] = (), budget: int = 1800) -> str:
    """The normative rules the test plan names, verbatim from the spec index, then the
    knowledge-base rules for the failure's topics: riscv_privileged_rules.md and act4_rules.md
    first, then this board's own notes (never another board's)."""
    lines = []
    rules = {**spec_index._index().get("unpriv-current", {}).get("rules", {}),
             **spec_index._index().get("priv-current", {}).get("rules", {})}
    for rule in rule_ids[:4]:
        if rule in rules:
            lines.append(f"[norm:{rule}] {rules[rule][1][:600]}")
    text = symptom_text(failure_symptom)
    kept, _info = filter_knowledge(profile)
    core = [r for r in kept if r["file"] in INVARIANT_FILES]
    others = [r for r in kept if r["file"] not in INVARIANT_FILES]
    isa = str((profile or {}).get("implemented_isa") or "")
    block = inject_curated_knowledge(isa, text, budget=budget, rules=core)
    shown = {line.split("] ", 1)[1] for line in block.splitlines() if line.startswith("- [")}
    extra = inject_curated_knowledge(isa, text, budget=max(0, budget - len(block)), rules=others, exclude=shown)
    if lines:
        lines.insert(0, "Normative rules the test plan names (current ratified text):")
    for chunk in (block, extra):
        if chunk:
            lines.append(chunk)
    return "\n".join(lines)


def render_sections(intent: dict[str, Any], invariants: str) -> str:
    """The two blocks for the agent's first message, with explicit boundaries."""
    shown = {k: v for k, v in intent.items() if k not in ("testplan_root",)}
    return ("[TEST INTENT]\n```json\n" + json.dumps(shown, indent=1)[:3000] + "\n```\n[/TEST INTENT]\n\n"
            "[SPECIFICATION INVARIANTS]\n" + (invariants or "No rule matches this failure; search the "
                                             "specification with tool_search_spec.")
            + "\n[/SPECIFICATION INVARIANTS]")


__all__ = ["extract_test_intent", "fetch_isa_invariants", "render_sections", "testplan_roots",
           "curated_knowledge_for"]
