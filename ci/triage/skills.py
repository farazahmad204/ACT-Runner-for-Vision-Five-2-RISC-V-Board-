"""Debugging skills for the skill-driven triage loop (investigate.investigate_skills).

A skill is a specialized way to investigate one aspect of a failure, like an engineer's habit:
read the log, find the instruction, compare with Sail, read the spec... Each lives in
skills/<name>.md (frontmatter: name, title, summary, tools; body: how to investigate) and
produces *evidence items* from what the agent already collected (case file, value diff,
spec index, vault, board knowledge), each with an id such as `spec.2`. The model sees only
the skills it loads, cites evidence ids in its answer, and code checks the ids exist.

plan() is the default investigation order for a failure, chosen by code from its category,
test name and trap cause, so the investigation never depends on the model deciding to look.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

try:
    from . import sail_trace
except ImportError:  # run as a script from ci/triage
    import sail_trace  # type: ignore[no-redef]

SKILL_DIR = Path(__file__).resolve().parent / "skills"
CARD = ("log", "instruction")  # always loaded: the failure card
ITEM_CHARS = 700
CAUSES = {0: "instruction address misaligned", 1: "instruction access fault", 2: "illegal instruction",
          3: "breakpoint", 4: "load address misaligned", 5: "load access fault",
          6: "store/AMO address misaligned", 7: "store/AMO access fault", 8: "ecall from U/VU",
          9: "ecall from HS/S", 10: "ecall from VS", 11: "ecall from M", 12: "instruction page fault",
          13: "load page fault", 15: "store/AMO page fault", 18: "software check", 19: "hardware error",
          20: "instruction guest-page fault", 21: "load guest-page fault", 22: "virtual instruction",
          23: "store/AMO guest-page fault"}
MEMORY_CAUSES = {1, 5, 7, 12, 13, 15, 20, 21, 23}
MMU_TEST = re.compile(r"^(Sv|SvH|Sm?PMP|PMP|Svade|Svadu|Svnapot|Svpbmt|Svinval|Zicbo)", re.I)
TRANSLATION = {0: "Bare", 8: "Sv39", 9: "Sv48", 10: "Sv57"}
REGISTER = re.compile(r"\b(satp|vsatp|hgatp|pte\w*|gpa|gva|htval|mtval2)\s*[=:]\s*(0x[0-9a-fA-F]+)", re.I)


@lru_cache(maxsize=1)
def catalog() -> dict[str, dict[str, Any]]:
    skills = {}
    for path in sorted(SKILL_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not match:
            continue
        meta = yaml.safe_load(match.group(1)) or {}
        meta["body"] = match.group(2).strip()
        skills[meta["name"]] = meta
    return skills


def cause_number(ex: dict[str, Any]) -> int | None:
    for key in ("actual_cause", "trap_mcause", "xcause", "mcause"):
        value = str(ex.get(key) or "")
        try:
            return int(value, 0) & 0xFFF if value else None
        except ValueError:
            continue
    return None


def plan(evidence: dict[str, Any]) -> list[str]:
    """The skills to load after the failure card, in order (code's default investigation)."""
    category = evidence.get("deterministic_category", "")
    test = str(evidence.get("case", ""))
    ex = evidence.get("extracted", {})
    if category in ("trap_cause_mismatch", "trap_or_signature_failure"):
        order = ["reference", "csr_privilege", "spec", "history"]
    elif category in ("architectural_value_mismatch", "register_value_mismatch"):
        order = ["csr_privilege", "reference", "spec", "history"]
    elif category == "assertion_mismatch":
        order = ["act_framework", "csr_privilege", "spec", "history"]
    elif category == "transport_or_timeout":
        order = ["act_framework", "history"]
    else:
        order = ["reference", "act_framework", "spec", "history"]
    if MMU_TEST.match(test) or cause_number(ex) in MEMORY_CAUSES:
        order.insert(0, "memory_mmu")
    if test.startswith("Interrupts"):
        order = ["act_framework"] + [n for n in order if n != "act_framework"]  # raised by board macros
    return list(dict.fromkeys(order))


# ---------- the memory and MMU decoders ----------

def decode_atp(name: str, value: int) -> str:
    """satp / vsatp / hgatp fields (RV64)."""
    mode = value >> 60
    label = TRANSLATION.get(mode, f"mode {mode} (reserved)")
    if name.lower() == "hgatp":
        label = {0: "Bare", 8: "Sv39x4", 9: "Sv48x4", 10: "Sv57x4"}.get(mode, label)
        ident = f"VMID={(value >> 44) & 0x3FFF:#x}"
    else:
        ident = f"ASID={(value >> 44) & 0xFFFF:#x}"
    ppn = value & ((1 << 44) - 1)
    return f"{name}={value:#x}: MODE={label}, {ident}, root PPN={ppn:#x} (table at {ppn << 12:#x})"


def decode_pte(name: str, value: int) -> str:
    """Sv39/48/57 page-table entry fields."""
    flags = "".join(letter if value >> bit & 1 else "-" for bit, letter in enumerate("VRWXUGAD"))
    notes = []
    v, r, w, x = (value >> b & 1 for b in range(4))
    if not v:
        notes.append("not valid: any access faults")
    elif w and not r:
        notes.append("W without R is reserved: page fault")
    elif not (r or x):
        notes.append("pointer to the next level")
    else:
        notes.append("leaf")
        if not value >> 6 & 1:
            notes.append("A=0: access faults unless hardware updates A (Svadu)")
        if w and not value >> 7 & 1:
            notes.append("D=0: store faults unless hardware updates D (Svadu)")
    if value >> 54 & 0x7F:
        notes.append("reserved bits 60:54 set: page fault")
    pbmt = value >> 61 & 3
    if pbmt:
        notes.append(f"PBMT={pbmt} (needs Svpbmt)")
    if value >> 63:
        notes.append("N=1 (needs Svnapot)")
    ppn = value >> 10 & ((1 << 44) - 1)
    return f"{name}={value:#x}: flags {flags}, PPN={ppn:#x} (PA {ppn << 12:#x}); " + "; ".join(notes)


def mmu_items(ex: dict[str, Any]) -> list[str]:
    seen, items = set(), []
    text = json.dumps({k: v for k, v in ex.items() if k != "evidence_lines"}) + "\n" + \
        "\n".join(ex.get("evidence_lines", []))
    for name, raw in REGISTER.findall(text):
        key = (name.lower(), raw.lower())
        if key in seen:
            continue
        seen.add(key)
        value = int(raw, 16)
        lowered = name.lower()
        if lowered in ("satp", "vsatp", "hgatp"):
            items.append(decode_atp(lowered, value))
        elif lowered.startswith("pte"):
            items.append(decode_pte(lowered, value))
        else:
            items.append(f"{lowered}={value:#x}" + (f" (GPA >> 2 = {value << 2:#x})" if lowered in
                                                   ("htval", "mtval2") else ""))
    cause = cause_number(ex)
    if cause is not None:
        kind = ("guest-page fault (two-stage translation)" if cause in (20, 21, 23) else
                "page fault (first-stage translation)" if cause in (12, 13, 15) else
                "access fault (PMP/PMA or bus)" if cause in (1, 5, 7) else "")
        if kind:
            items.append(f"Observed cause {cause} ({CAUSES[cause]}): a {kind}.")
    tval = ex.get("trap_mtval") or ex.get("xtval")
    if tval:
        items.append(f"Faulting address (tval) = {tval}")
    return items


# ---------- evidence ----------

@dataclass
class SkillContext:
    """What the agent already collected for one failure (signature cluster)."""
    evidence: dict[str, Any]
    case_file: dict[str, Any]
    grounding: dict[str, Any]
    knowledge: dict[str, Any]
    related: list[str] = field(default_factory=list)


def _cap(text: str, limit: int = ITEM_CHARS) -> str:
    text = str(text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def items_for(name: str, ctx: SkillContext) -> list[str]:
    """The evidence a skill contributes, as plain text items (ids are given by the caller)."""
    ev, cf, g, k = ctx.evidence, ctx.case_file, ctx.grounding, ctx.knowledge
    ex = ev.get("extracted", {})
    items: list[str] = []
    if name == "log":
        if ex.get("failure") or ex.get("test_info"):
            items.append(f"Failed check: {ex.get('failure') or ex.get('test_info')}")
        pairs = [("expected_value", "actual_value", "Signature value"),
                 ("register_expected", "register_value", ex.get("register") or "Register"),
                 ("expected_cause", "actual_cause", "Trap cause")]
        for want, got, label in pairs:
            if ex.get(want) or ex.get(got):
                items.append(f"{label}: expected {ex.get(want, '?')}, observed {ex.get(got, '?')}")
        if ex.get("mismatching_field"):
            items.append(f"Mismatching field: {ex['mismatching_field']}")
        trap = {key: ex[key] for key in ("trap_mcause", "trap_mepc", "trap_mtval", "xcause", "xepc", "xtval",
                                         "trap_handler_mode") if ex.get(key)}
        if trap:
            cause = cause_number(ex)
            items.append("Trap: " + ", ".join(f"{a}={b}" for a, b in trap.items())
                         + (f" ({CAUSES.get(cause, 'cause ' + str(cause))})" if cause is not None else ""))
        if ex.get("damo_assert"):
            items.append(f"Assertion: {ex['damo_assert']}")
        items.append(f"Rule-based reading ({ev.get('deterministic_category')}): "
                     f"{ev.get('deterministic_explanation', '')}")
    elif name == "instruction":
        inst = cf.get("instruction") or {}
        if inst.get("window"):
            line = f" (source line {inst['source_line']})" if inst.get("source_line") else ""
            items.append(f"Disassembly at the failing PC{line}:\n" + "\n".join(inst["window"]))
        intent = cf.get("intent") or {}
        if intent.get("code"):
            items.append(f"Test code at the check ('>' marks it):\n{intent['code']}")
        elif g.get("source"):
            items.append(f"Test source ({g.get('source_label')}):\n{g['source']}")
        if not items:
            items.append("The ELF that ran was not kept: no disassembly or test source for this failure.")
    elif name == "csr_privilege":
        if ev.get("value_diff"):
            items.append("Value diff (fields that differ): " + json.dumps(ev["value_diff"], separators=(",", ":")))
        absent = k.get("absent_csrs") or {}
        if absent:
            items.append("CSRs this core does not implement (measured): "
                         + ", ".join(f"{v} (0x{a:03x})" for a, v in sorted(absent.items())))
        masks = k.get("writable_masks") or {}
        if masks:
            items.append("Measured writable masks: " + ", ".join(f"{n}={m:#x}" for n, m in sorted(masks.items())))
        facts = [f for f in k.get("facts") or [] if re.search(r"deleg|mstatus|csr|pmp|trap|mode", f, re.I)]
        items += facts[:4]
        if cf.get("sail_settings"):
            items.append("Board's Sail settings (sail.json) for this failure:\n" + "\n".join(cf["sail_settings"]))
    elif name == "memory_mmu":
        items += mmu_items(ex)
        facts = [f for f in k.get("facts") or [] if re.search(r"sv\d|satp|page|phys|pmp|address|svad", f, re.I)]
        items += facts[:4]
        settings = [s for s in cf.get("sail_settings") or [] if re.search(r"sv\d|satp|pmp|pte|svad|addr|pbmt",
                                                                              s, re.I)]
        if settings:
            items.append("Board's Sail settings for translation and protection:\n" + "\n".join(settings))
        if not items:
            items.append("No translation registers or PTE values in the evidence.")
    elif name == "reference":
        rendered = sail_trace.render(cf.get("reference") or {})
        items.append(rendered or "No Sail reference run for this failure (no signature ELF or sail.json kept).")
    elif name == "spec":
        rendered = g.get("spec_versions") or ""
        for block in re.split(r"\n(?=\[(?:[a-z0-9.-]+|norm:\w+)\])", rendered):
            if block.strip():
                items.append(block.strip())
        if g.get("norms"):
            items.append("Requirements this test checks:\n" + "\n".join(g["norms"]))
        if not items:
            items.append("No spec passage matched; search with the `spec` tool.")
    elif name == "act_framework":
        intent = cf.get("intent") or {}
        if intent.get("header"):
            items.append("Test header: " + "; ".join(intent["header"]))
        if g.get("isa"):
            items.append(g["isa"])
        if cf.get("board_macros"):
            items.append("Board macros this check relies on (rvmodel_macros.h):\n" + "\n".join(cf["board_macros"]))
        if cf.get("act_revision"):
            items.append(f"ACT revision: {cf['act_revision']}")
        if not items:
            items.append("No test header or board macros were kept for this test.")
    elif name == "history":
        items += list(g.get("known_issues") or [])
        items += list(g.get("issues") or [])
        if ctx.related:
            items.append("Earlier findings for the same category:\n" + "\n".join(ctx.related))
        if not items:
            items.append("No reported issue or earlier finding matches this failure.")
    limit = 1500 if name in ("instruction", "reference", "spec") else ITEM_CHARS
    return [_cap(item, limit) for item in items if item]
