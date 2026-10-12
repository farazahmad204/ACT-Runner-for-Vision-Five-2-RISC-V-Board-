"""Step 5 of a failure analysis: what the specification version the board implements requires.

The index (knowledge/spec_versions.json.gz, built by tools/build_spec_versions.py) holds the
privileged and unprivileged ISA as several versions: priv 1.11 and 1.12, the hypervisor
extension draft 0.6 and 1.0, and the current ratified release with its normative rules.
board_knowledge/<board>.yaml names the versions a board implements (`spec_versions`).

For a failure, ground() returns:
  applicable  the passages of the board's own versions that best match the failure
  current     the current ratified text on the same topic, only where it differs
  rules       current normative rules named by the test, or matching the failure, verbatim
verify_quote() checks that a passage an answer quotes really is in the version it names.
"""

from __future__ import annotations

import gzip
import json
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

INDEX = Path(__file__).resolve().parent / "knowledge" / "spec_versions.json.gz"
WORD = re.compile(r"[a-z][a-z0-9_.]{1,}")
STOP = set("""the and for with that this from into when are was were not but has have its any all
can may must will shall each which then than also only such other more most set clear none true
false one two zero bit bits field fields value values read write written mode modes register
registers implementation implementations defined see table figure section got expected cause
csr csrs hypervisor triggers trigger test tests check checks assert should when""".split())
HYP = re.compile(r"\b(h[a-z]*(?:tinst|tval|deleg|ideleg|status|vip|ip|ie|geip|geie|gatp|counteren|"
                 r"envcfg|timedelta)|vs[a-z]+|v[su]-?mode|hlv|hsv|hfence|guest|mtval2|mtinst|gva|mpv)\b")
CURRENT = {"priv": "priv-current", "unpriv": "unpriv-current", "hypervisor": "priv-current"}


@lru_cache(maxsize=1)
def _index() -> dict[str, Any]:
    if not INDEX.is_file():
        return {}
    with gzip.open(INDEX, "rt", encoding="utf-8") as stream:
        versions = json.load(stream)["versions"]
    for entry in versions.values():
        df: Counter[str] = Counter()
        for section in entry["sections"]:
            words = Counter(w.strip(".") for w in WORD.findall((section["t"] + " " + section["x"]).lower()))
            section["_w"] = words
            section["_n"] = _norm(section["x"])
            df.update(words.keys())
        count = max(len(entry["sections"]), 1)
        entry["_idf"] = {w: math.log(count / (1 + n)) for w, n in df.items()}
    return versions


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.]+", " ", text.lower())).strip()


def label(vid: str) -> str:
    return (_index().get(vid) or {}).get("label", vid)


def applicable(knowledge: dict[str, Any], hypervisor: bool) -> dict[str, str]:
    """{role: version id} the board implements; current versions when the board names none."""
    declared = knowledge.get("spec_versions") or {}
    roles = {"priv": declared.get("priv") or "priv-current",
             "unpriv": declared.get("unpriv") or "unpriv-current"}
    if hypervisor:
        roles["hypervisor"] = declared.get("hypervisor") or "priv-current"
    return {role: vid for role, vid in roles.items() if vid in _index()}


def _search(vid: str, terms: list[str], limit: int) -> list[tuple[float, dict[str, Any]]]:
    entry = _index().get(vid)
    if not entry or not terms:
        return []
    idf = entry["_idf"]
    scored = []
    for section in entry["sections"]:
        words = section["_w"]
        title = section["t"].lower()
        score = sum((1 + math.log(words[t])) * idf.get(t, 0) for t in terms if words.get(t))
        score += sum(1.5 * idf.get(t, 0) for t in terms if t in title)
        if score > 0:
            scored.append((score, section))
    scored.sort(key=lambda item: -item[0])
    return scored[:limit]


def _excerpt(text: str, terms: list[str], width: int) -> str:
    lower = text.lower()
    hits = [lower.find(t) for t in terms if t in lower]
    start = max(min(hits) - width // 4, 0) if hits else 0
    piece = text[start:start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


def _similar(a: dict[str, Any], b: dict[str, Any]) -> float:
    left, right = set(a["_w"]), set(b["_w"])
    return len(left & right) / max(len(left | right), 1)


CAUSES = {
    0: "instruction address misaligned", 1: "instruction access fault", 2: "illegal instruction",
    3: "breakpoint", 4: "load address misaligned", 5: "load access fault",
    6: "store amo address misaligned", 7: "store amo access fault", 8: "environment call",
    9: "environment call", 11: "environment call", 12: "instruction page fault",
    13: "load page fault", 15: "store amo page fault", 20: "instruction guest page fault",
    21: "load guest page fault", 22: "virtual instruction", 23: "store amo guest page fault",
}
INTERRUPTS = {1: "supervisor software interrupt", 3: "machine software interrupt",
              5: "supervisor timer interrupt", 7: "machine timer interrupt",
              9: "supervisor external interrupt", 11: "machine external interrupt",
              2: "virtual supervisor software interrupt", 6: "virtual supervisor timer interrupt",
              10: "virtual supervisor external interrupt", 12: "supervisor guest external interrupt"}


def cause_words(value: Any) -> list[str]:
    try:
        number = int(str(value), 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return []
    code = number & 0xFFF
    name = (INTERRUPTS if number >> 63 else CAUSES).get(code, "")
    return name.split() + (["priority"] if not number >> 63 and code in (4, 5, 6, 7, 13, 15) else [])


def failure_terms(extracted: dict[str, Any], value_diff: dict[str, Any], case: dict[str, Any],
                  test: str) -> list[str]:
    """Search words from the structure of the failure, most specific first: the instruction and
    its CSR, the differing CSR fields, the trap causes involved, the coverpoint, the test name."""
    words: list[str] = []
    at = ((case.get("instruction") or {}).get("at") or "").lower()
    match = re.search(r"\s([a-z][a-z0-9.]+)\s+([a-z0-9,()+-]+)", at)
    if match:
        words.append(match.group(1))
        words += [o for o in re.split(r"[,()]", match.group(2)) if re.match(r"^[a-z][a-z0-9]+$", o)
                  and not re.match(r"^(?:zero|ra|sp|gp|tp|[ast]\d+|x\d+|fp)$", o)]
    if value_diff.get("csr"):
        words.append(str(value_diff["csr"]))
    for field in (value_diff.get("missing_fields") or []) + (value_diff.get("extra_fields") or []):
        words.append(str(field.get("field", "")))
    for key in ("xcause", "trap_mcause", "mcause", "expected_cause"):
        words += cause_words(extracted.get(key))
    # damo assertions say what was checked ("trap cause == virtual-inst (22)").
    assert_text = re.sub(r"\([^()]*:\d+\)\s*$", "", str(extracted.get("damo_assert", "")))
    words += [w for w in re.findall(r"[a-z][a-z0-9.]+", assert_text.lower().replace("-", " "))
              if len(w) > 2]
    info = str(extracted.get("test_info", ""))
    for token in re.findall(r"\b(?:cp|bin):\s*([A-Za-z0-9_]+)", info):
        words += [t for t in token.lower().split("_") if len(t) > 2 and t != "cp"]
    words += [t for t in re.split(r"[_\-]", test.lower()) if len(t) > 2 and not t.isdigit()]
    return terms_for(words)


def terms_for(words: list[str]) -> list[str]:
    out = []
    for word in words:
        word = word.lower().strip(".,;:()")
        if (len(word) > 1 and word not in STOP and word not in out
                and not re.match(r"^(?:x|a|t|s)\d+$|^\d", word)):
            out.append(word)
    return out[:14]


def ground(terms: list[str], knowledge: dict[str, Any], rule_ids: list[str] = (),
           hypervisor: bool = False, width: int = 900) -> dict[str, Any]:
    versions = applicable(knowledge, hypervisor or any(HYP.search(t) for t in terms))
    found: dict[str, Any] = {"versions": versions, "applicable": [], "current": [], "rules": []}
    if not versions:
        return found
    hits = []
    for role, vid in versions.items():
        for score, section in _search(vid, terms, 3):
            # A hypervisor test is first about the hypervisor chapter the board implements.
            weight = 1.6 if role == "hypervisor" else 1.0
            hits.append((score * weight, role, vid, section))
    hits.sort(key=lambda item: -item[0])
    seen = set()
    for _score, role, vid, section in hits:
        if len(found["applicable"]) >= 2 or (vid, section["t"]) in seen:
            continue
        seen.add((vid, section["t"]))
        # The best passage gets the full width, the next one less.
        found["applicable"].append({"version": vid, "title": section["t"],
                                    "text": _excerpt(section["x"], terms,
                                                     width if not found["applicable"] else width // 2)})
        current_vid = CURRENT[role]
        if current_vid == vid:
            continue
        query = terms + [w for w in WORD.findall(section["t"].lower()) if w not in STOP][:6]
        best = _search(current_vid, query, 1)
        if best:
            counterpart = best[0][1]
            if _similar(section, counterpart) < 0.6:
                found["current"].append({"version": current_vid, "title": counterpart["t"],
                                         "text": _excerpt(counterpart["x"], terms, width * 2 // 3),
                                         "versus": vid, "for": len(found["applicable"]) - 1})
    rules = {**_index().get("unpriv-current", {}).get("rules", {}),
             **_index().get("priv-current", {}).get("rules", {})}
    for rule in rule_ids:
        if rule in rules and len(found["rules"]) < 4:
            found["rules"].append({"id": rule, "text": rules[rule][1][:700]})
    return found


def render(found: dict[str, Any]) -> str:
    """The prompt text for ground()'s result; version ids in brackets for citation."""
    if not found.get("applicable"):
        return ""
    lines = ["This board implements: " + "; ".join(
        f"{role}: {label(vid)} [{vid}]" for role, vid in found["versions"].items())]
    for index, item in enumerate(found["applicable"]):
        lines.append(f"[{item['version']}] {item['title'][:120]}:\n{item['text']}")
        for other in (c for c in found["current"] if c.get("for") == index):
            lines.append(f"[{other['version']}] (current ratified text; differs from {other['versus']}, "
                         f"for comparison only) {other['title'][:100]}:\n{other['text']}")
    for rule in found["rules"]:
        lines.append(f"[norm:{rule['id']}] (current normative rule) {rule['text']}")
    return "\n".join(lines)


def verify_quote(vid: str, quote: str) -> bool | None:
    """True if the quote is in that version (whitespace and punctuation ignored); None if there
    is nothing to check."""
    wanted = _norm(quote or "")
    entry = _index().get(vid or "")
    if len(wanted) < 15 or not entry:
        return None
    if any(wanted in section["_n"] for section in entry["sections"]):
        return True
    return any(wanted in _norm(text) for _section, text in entry.get("rules", {}).values())
