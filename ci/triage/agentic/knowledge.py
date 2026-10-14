"""Curated knowledge for the ReAct triage agent, retrieved just in time and per board.

knowledge/*.md are split into rules and tagged by topic; filter_knowledge(profile) keeps the
rules this board may see (its own platform file, the universal files, never another board's
notes; only the universal baselines for a board without notes), and curated_knowledge_for()
returns the rules that match one failure, compressed to a character budget.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Curated knowledge, retrieved just in time
# ---------------------------------------------------------------------------
#
# knowledge/*.md are split into rules (one bullet, or one "## section" block of a file whose
# bullets are not self-contained), each tagged by the topics its text names. A failure is
# mapped to topics the same way, and only the best-matching rules are returned, within a
# character budget, as one compact block for the agent's context. A rule can pin its topics
# with an HTML comment: <!-- tags: translation, priority -->.

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
TOPICS = {
    "translation": r"\bsv(32|39|48|57)\b|\bsatp\b|\bpage.?(fault|table)|\bpte\b|translation|\bvirtual memory",
    "protection": r"\bpmp\b|\bpma\b|access.?fault|inaccessible",
    "priority": r"priorit|misaligned|alignment",
    "delegation": r"deleg|trap (mode|routing)|\bm-mode\b|s-mode handling|routes? it",
    "provenance": r"provenance|first.?trap|failq|\bmepc\b|\bmtval\b|stale|signature helper|selected pc",
    "interrupt": r"interrupt|\bplic\b|mideleg|\bmie\b|\bmip\b|timer",
    "fp": r"\bfs\b|fflags|\bfrm\b|fcsr|floating|\bfp\b",
    "compressed": r"compressed|c-extension|instruction length|halfword",
    "atomic": r"\bamo|atomic|\blr\b|\bsc\b|zaamo|zalrsc",
    "ecall": r"\becall\b|environment call",
    "illegal": r"illegal",
    "profile": r"profile|rva23|unsupported|warl|capabilit",
    "sail": r"\bsail\b",
    "act": r"\bact\b|rvcp|signature|\bbin\b|test intent",
}
EXTENSION_TOPICS = {"A": "atomic", "C": "compressed", "F": "fp", "D": "fp"}
# Failure topics -> the files that hold the rules for them, first file first.
FILE_PRIORITY = ["vf2_u74_platform.md", "known_failure_patterns.md", "riscv_privileged_rules.md",
                 "sail_config_rules.md", "act4_rules.md", "rva23_profile_rules.md"]
CAUSE_TOPICS = {0: ["priority"], 1: ["protection"], 2: ["illegal"], 3: [], 4: ["priority"],
                5: ["protection", "priority"], 6: ["priority"], 7: ["protection", "priority"],
                8: ["ecall", "delegation"], 9: ["ecall", "delegation"], 11: ["ecall"],
                12: ["translation"], 13: ["translation"], 15: ["translation"],
                20: ["translation"], 21: ["translation"], 22: ["illegal"], 23: ["translation"]}


def topics_of(text: str) -> set[str]:
    lowered = text.lower()
    return {topic for topic, pattern in TOPICS.items() if re.search(pattern, lowered)}


def knowledge_rules(directory: Path = KNOWLEDGE_DIR) -> list[dict[str, Any]]:
    """Every rule in the knowledge files: {file, section, text, topics}. In a file with "## "
    sections each section is one rule (its bullets belong together); otherwise each bullet is."""
    rules: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if any(line.startswith("## ") for line in lines):
            section, body = "", []
            for line in lines + ["## "]:
                if line.startswith("## "):
                    if section and body:
                        rules.append({"file": path.name, "section": section, "text": "; ".join(body)})
                    section, body = line[3:].strip(), []
                elif section and line.strip() and not line.startswith("```"):
                    text = line.strip().removeprefix("- ")
                    if line.startswith(("  ", "\t")) and body and not line.lstrip().startswith("- "):
                        body[-1] += " " + text  # a wrapped bullet line
                    else:
                        body.append(text.rstrip(":") if text.endswith("Rules:") else text)
        else:
            current: list[str] = []
            for line in lines + [""]:
                if line.lstrip().startswith("- ") or not line.strip() or line.startswith("#"):
                    if current:
                        rules.append({"file": path.name, "section": "", "text": " ".join(current)})
                    current = [line.strip()[2:]] if line.lstrip().startswith("- ") else []
                elif current:
                    current.append(line.strip())
    for rule in rules:
        pinned = re.search(r"<!--\s*tags:\s*([^>]*)-->", rule["text"])
        rule["text"] = " ".join(re.sub(r"<!--.*?-->", "", rule["text"]).replace("`", "").split()).lstrip("; ")
        rule["topics"] = ({t.strip() for t in pinned.group(1).split(",")} if pinned
                          else topics_of(rule["section"] + " " + rule["text"]))
    return rules


# ---------- platform isolation ----------

BASELINE_FILES = ("act4_rules.md", "riscv_privileged_rules.md")  # universal; the fallback
GENERIC_WORDS = {"platform", "knowledge", "rules", "notes", "board"}


def _tokens(text: str) -> set[str]:
    """Distinctive names in a text: alphanumeric words with a digit (vf2, u74, jh7110,
    visionfive2, eic7700x), also with spaces removed ("VisionFive 2" -> visionfive2). Vendor
    words alone (SiFive, StarFive) are not signatures: one vendor makes many cores."""
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9]+", lowered))
    words |= {re.sub(r"[^a-z0-9]", "", chunk) for chunk in re.split(r"[/,;()]", lowered)}
    return {w for w in words if re.search(r"\d", w) and re.search(r"[a-z]", w) and w not in GENERIC_WORDS}


def platform_signatures(directory: Path = KNOWLEDGE_DIR) -> dict[str, set[str]]:
    """Platform-specific knowledge files and the names that identify their platform: the file
    name (vf2_u74_platform.md -> vf2, u74), its "Platform:" line, and an optional
    <!-- platform: name, name --> comment. Files without such names are universal."""
    signatures = {}
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        line = re.search(r"^Platform:\s*(.+)$", text, re.M)
        pinned = re.search(r"<!--\s*platform:\s*([^>]*)-->", text)
        if not (line or pinned or "platform" in path.stem.lower()):
            continue  # a file is platform-specific only if it says so (act4/rva23 are not boards)
        names = _tokens(path.stem.replace("_", " ")) - _tokens("platform")
        if line:
            names |= _tokens(line.group(1))
        if pinned:
            names |= {re.sub(r"[^a-z0-9]", "", n.lower()) for n in pinned.group(1).split(",") if n.strip()}
        if names:
            signatures[path.name] = names
    return signatures


def profile_signature(profile: dict[str, Any] | None) -> set[str]:
    if not profile:
        return set()
    return set().union(*(_tokens(str(profile.get(k) or "")) for k in ("board_name", "core_name", "soc")))


def filter_knowledge(profile: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The knowledge rules this board may see, and what was filtered out.

    - A platform file (vf2_u74_platform.md...) is used only when the profile's board, core or
      SoC names match its signature.
    - A rule anywhere that names a platform ("... on VF2/U74") is used only on that platform.
    - If no platform file matches the profile (an unknown board, an FPGA core, no profile),
      only the universal baselines (act4_rules.md, riscv_privileged_rules.md) are used.
    """
    signatures = platform_signatures()
    mine = profile_signature(profile)
    matched = sorted(name for name, sig in signatures.items() if sig & mine)
    all_platform_names = set().union(*signatures.values()) if signatures else set()
    own_names = set().union(*(signatures[m] for m in matched)) if matched else set()
    kept, dropped_rules = [], 0
    for rule in knowledge_rules():
        if rule["file"] in signatures and rule["file"] not in matched:
            continue
        if not matched and rule["file"] not in BASELINE_FILES:
            continue
        named = _tokens(rule["section"] + " " + rule["text"]) & all_platform_names
        if named and not named & own_names:
            dropped_rules += 1  # a rule about another platform, inside a universal file
            continue
        kept.append(rule)
    files = sorted({r["file"] for r in knowledge_rules()})
    info = {"mode": "platform" if matched else "baseline", "platforms": matched,
            "profile_names": sorted(mine),
            "files": sorted({r["file"] for r in kept}),
            "excluded_files": [f for f in files if f not in {r["file"] for r in kept}],
            "excluded_platform_rules": dropped_rules}
    return kept, info


def failure_topics(failure_type: str) -> set[str]:
    """Topics of a failure: words in the failure description, and the trap cause it names."""
    topics = topics_of(failure_type.replace("_", " "))
    for match in re.finditer(r"(?:mcause|scause|cause)[=: ]+(0x[0-9a-fA-F]+|\d+)", failure_type, re.I):
        code = int(match.group(1), 0)
        if code >> 63 or code >> 31 == 1:  # interrupt bit (RV64 or RV32): not an exception code
            topics.add("interrupt")
        else:
            topics.update(CAUSE_TOPICS.get(code & 0xFFF, []))
    if re.search(r"^(Sv|SvH)|\bsv\d+", failure_type):
        topics.add("translation")
    if re.search(r"Zaamo|Zalrsc|\bA-?ext|amo[a-z]*\.|\blr\.|\bsc\.", failure_type, re.I):
        topics.add("atomic")
    if re.search(r"\bZc|\bc\.[a-z]+|compressed", failure_type, re.I):
        topics.add("compressed")
    return topics


def curated_knowledge_for(profile: dict[str, Any] | None, failure_type: str, budget: int = 1800,
                          exclude: set[str] = frozenset()) -> tuple[str, dict[str, Any]]:
    """The wrapper the agent uses: filter the knowledge base to this board (filter_knowledge),
    then select and compress the rules for this failure (inject_curated_knowledge). Returns the
    block and the filter report (mode, platforms matched, files and rules excluded)."""
    rules, info = filter_knowledge(profile)
    isa = str((profile or {}).get("implemented_isa") or "")
    block = inject_curated_knowledge(isa, failure_type, budget, exclude, rules=rules)
    return block, info


def isa_extensions(isa: str) -> set[str]:
    match = re.match(r"^rv(?:32|64)([a-z]*)", (isa or "").lower())
    letters = set(match.group(1).upper()) if match else set()
    return letters | ({"I", "M", "A", "F", "D"} if "G" in letters else set())


def inject_curated_knowledge(isa: str, failure_type: str, budget: int = 1800,
                             exclude: set[str] = frozenset(), profile: dict[str, Any] | None = None,
                             rules: list[dict[str, Any]] | None = None) -> str:
    """A compact block of only the knowledge rules that match this failure, for the agent's
    context. failure_type is free text: the parsed category, failure message, trap cause
    (e.g. "mcause=0xd"), test name or instruction. Rules for an extension the failure involves
    but the ISA lacks are kept and flagged, since "the board does not implement it" is then
    the likely answer. `exclude` skips rules already shown (by their text). The rules come from
    filter_knowledge(profile) unless given: another board's notes are never offered."""
    wanted = failure_topics(failure_type)
    if not wanted:
        return ""
    extensions = isa_extensions(isa)
    missing = sorted(ext for ext, topic in EXTENSION_TOPICS.items() if topic in wanted and ext not in extensions
                     and not (ext == "D" and "F" not in extensions))
    scored = []
    if rules is None:
        rules, _info = filter_knowledge(profile)
    for rule in rules:
        overlap = rule["topics"] & wanted
        if not overlap or rule["text"] in exclude:
            continue
        rank = FILE_PRIORITY.index(rule["file"]) if rule["file"] in FILE_PRIORITY else len(FILE_PRIORITY)
        # A rule focused on the failure's topics first (shared / all its topics: a table that names
        # every topic is not about this one), then more shared topics, then the file.
        focus = len(overlap) / len(rule["topics"] | wanted)
        if rule["section"]:
            focus += 0.25  # a curated "## section" (symptom, action, owner) is the most actionable
        scored.append((-focus, -len(overlap), rank, rule))
    scored.sort(key=lambda item: item[:3])
    lines = [f"Relevant rules (topics: {', '.join(sorted(wanted))}):"]
    if missing:
        lines.append(f"- NOTE: the failure involves {', '.join(missing)}, which the ISA {isa} does not list.")
    size = sum(len(line) + 1 for line in lines)
    for _o, _f, _r, rule in scored:
        where = rule["file"].removesuffix(".md") + (f" / {rule['section']}" if rule["section"] else "")
        line = f"- [{where}] {rule['text']}"
        if size + len(line) + 1 > budget:
            continue
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""
