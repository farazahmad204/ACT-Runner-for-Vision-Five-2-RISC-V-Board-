#!/usr/bin/env python3
"""Resolve every runner requirement for a board from the evidence available.

Walks assets/runner-requirements.yaml. For each requirement it tries the
sources listed under `resolve`, in order, using what was supplied:

  user       --user answers.yaml   {id: value} or {id: {value, source}}
  extract    --facts facts.yaml    output of collect_board_facts.py
  manifest   --manifest board.yaml existing board manifest
  research   --research DIR        validated research reports (same IDs)
  probe      --probe probe.json    output of `csr_probe.py decode --json`

`document`, `web` and `probe` steps that cannot be answered from files become
questions for the agent, in that order. Writes:

  <out>/coverage.yaml   status/value/source per requirement
  <out>/coverage.md     summary table
  <out>/questions.md    open items grouped by the next source to use
  <out>/preset.draft    BOARD_* lines for resolved values (never buildable as-is)
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys
from typing import Any

import yaml

DEFAULT_CATALOG = Path(__file__).resolve().parent.parent / "assets" / "runner-requirements.yaml"
NEED_ORDER = ["offline_build", "hardware_smoke", "install", "act", "ci"]


def nested(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def load_yaml(path: Path | None) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path else None


def load_research(directory: Path | None) -> dict[str, dict]:
    found: dict[str, dict] = {}
    if not directory:
        return found
    for report in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(report.read_text(encoding="utf-8")) or {}
        for record in data.get("evidence", []) or []:
            if record.get("evidence_status") in (None, "unknown"):
                continue
            found.setdefault(record["id"], {
                "value": record.get("value"),
                "source": f"{report.name}: {record.get('source')}",
                "evidence_status": record.get("evidence_status"),
            })
    return found


def as_hex_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.split()[0], 0)
        except (ValueError, IndexError):
            return None
    return None


def comparable(value: Any) -> Any:
    number = as_hex_int(value)
    return number if number is not None else str(value).strip().lower()


def is_checkable(value: Any) -> bool:
    return as_hex_int(value) is not None or (isinstance(value, str) and len(value) <= 48 and not any(c.isspace() for c in value.strip()))


def resolve_one(req: dict, src: dict) -> dict:
    """Return the coverage record for one requirement.

    Every available source is read; the first in `resolve` order wins and any
    scalar value that differs is reported as a conflict (never hidden)."""
    steps = req.get("resolve") or []
    next_steps: list[dict] = []
    candidates: list[dict] = []
    for step in steps:
        if step == "user":
            hit = (src["user"] or {}).get(req["id"])
            if hit is not None:
                rec = hit if isinstance(hit, dict) and "value" in hit else {"value": hit}
                candidates.append({"via": "user", "value": rec["value"], "source": rec.get("source", "user input"),
                                   "evidence_status": rec.get("evidence_status", "confirmed")})
            continue
        if not isinstance(step, dict):
            continue
        (kind, arg), = step.items()
        if kind == "extract":
            fact = ((src["facts"] or {}).get("facts") or {}).get(arg)
            if fact is not None:
                candidates.append({"via": "extract", "value": fact["value"], "source": fact["source"],
                                   "evidence_status": fact["evidence_status"]})
        elif kind == "manifest":
            value = nested(src["manifest"], arg)
            if value not in (None, "", []):
                candidates.append({"via": "manifest", "value": value, "source": f"manifest {arg}",
                                   "evidence_status": "as recorded in manifest"})
        elif kind == "probe" and req["id"] in ((src["probe"] or {}).get("facts") or {}):
            candidates.append({"via": "probe", "value": src["probe"]["facts"][req["id"]],
                               "source": "csr_probe.py on the board", "evidence_status": "confirmed"})
        elif kind in ("document", "web", "probe"):
            next_steps.append({kind: arg})
    research = src["research"].get(req["id"])
    if research is not None:
        candidates.append({"via": "research", **research})
    if candidates:
        chosen = candidates[0]
        record = {"status": "resolved", **chosen}
        conflicts, also_seen = [], []
        for other in candidates[1:]:
            if isinstance(other["value"], (dict, list)) or isinstance(chosen["value"], (dict, list)):
                continue
            if comparable(other["value"]) == comparable(chosen["value"]):
                continue
            entry = {"value": other["value"], "source": other["source"], "via": other["via"]}
            # Numbers and short tokens (ISA strings, enums) must agree; free-text
            # descriptions are often the same fact worded differently.
            if is_checkable(chosen["value"]) and is_checkable(other["value"]):
                conflicts.append(entry)
            else:
                also_seen.append(entry)
        if conflicts:
            record["status"] = "conflict"
            record["conflicts"] = conflicts
        if also_seen:
            record["also_seen"] = also_seen
        return record
    if req.get("default") is not None:
        return {"status": "default", "via": "default", "value": req["default"],
                "source": "catalog default", "evidence_status": "inferred", "next": next_steps}
    if req["kind"] == "choice":
        return {"status": "choice_open", "rules": req.get("rules", []), "next": next_steps}
    return {"status": "open", "next": next_steps}


def check_memory(cov: dict[str, dict]) -> list[str]:
    """Overlap/containment checks for memory choices when values are numeric."""
    problems: list[str] = []
    def rng(rid: str, base_key: str, end_key: str | None = None, size_key: str | None = None):
        val = cov.get(rid, {}).get("value")
        if not isinstance(val, dict):
            return None
        base = as_hex_int(val.get(base_key))
        end = as_hex_int(val.get(end_key)) if end_key else None
        if size_key and base is not None and as_hex_int(val.get(size_key)) is not None:
            end = base + as_hex_int(val.get(size_key))
        return (base, end) if base is not None and end is not None else None
    ram = rng("memory.ram_window", "base", "limit")
    payload = rng("memory.payload_window", "base", "limit")
    staging = rng("memory.staging_buffer", "base", size_key="bytes")
    excluded = rng("memory.excluded_range", "base", "limit")
    for name, r in (("payload_window", payload), ("staging_buffer", staging)):
        if ram and r and not (ram[0] <= r[0] and r[1] <= ram[1]):
            problems.append(f"{name} {r[0]:#x}-{r[1]:#x} is outside ram_window {ram[0]:#x}-{ram[1]:#x}")
    pairs = [("payload_window", payload, "staging_buffer", staging),
             ("payload_window", payload, "excluded_range", excluded),
             ("staging_buffer", staging, "excluded_range", excluded)]
    for an, a, bn, b in pairs:
        if a and b and a[0] < b[1] and b[0] < a[1]:
            problems.append(f"{an} overlaps {bn}")
    return problems


def manifest_memory_views(manifest: Any) -> dict[str, dict]:
    """Expose manifest memory fields as structured choice values."""
    m = (manifest or {}).get("memory") or {}
    views = {}
    if m.get("dram_base") is not None and m.get("usable_limit") is not None:
        views["memory.ram_window"] = {"base": m["dram_base"], "limit": m["usable_limit"]}
    if m.get("payload_base") is not None and m.get("payload_limit") is not None:
        views["memory.payload_window"] = {"base": m["payload_base"], "limit": m["payload_limit"]}
    if m.get("staging_address") is not None and m.get("staging_max_bytes") is not None:
        views["memory.staging_buffer"] = {"base": m["staging_address"], "bytes": m["staging_max_bytes"]}
    for region in m.get("reserved_regions") or []:
        start, size = as_hex_int(region.get("start")), as_hex_int(region.get("bytes"))
        if start is not None and size and m.get("payload_base") is not None and m.get("staging_address") is not None \
                and as_hex_int(m["payload_base"]) <= start < as_hex_int(m["staging_address"]):
            views["memory.excluded_range"] = {"base": start, "limit": start + size}
    return views


def fmt(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        # Addresses/sizes are page-aligned; rates and counts stay decimal.
        return hex(value) if value >= 0x1000 and value % 0x1000 == 0 else str(value)
    if isinstance(value, list):
        text = "; ".join(fmt(v) for v in value)
        return text if len(text) <= 70 else text[:67] + "..."
    if isinstance(value, dict):
        return ", ".join(f"{k}={fmt(v)}" for k, v in value.items())
    text = str(value)
    return text if len(text) <= 70 else text[:67] + "..."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board-id", required=True)
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--user", type=Path)
    ap.add_argument("--facts", type=Path)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--research", type=Path)
    ap.add_argument("--probe", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    catalog = load_yaml(args.catalog)
    src = {"user": load_yaml(args.user), "facts": load_yaml(args.facts), "manifest": load_yaml(args.manifest),
           "research": load_research(args.research), "probe": load_yaml(args.probe)}

    coverage: dict[str, dict] = {}
    views = manifest_memory_views(src["manifest"])
    for req in catalog["requirements"]:
        rec = resolve_one(req, src)
        if req["id"] in views:  # structured base/limit from the manifest beats a bare scalar
            rec = {"status": "resolved", "via": "manifest", "value": views[req["id"]],
                   "source": "manifest memory fields", "evidence_status": "chosen (recorded in manifest)"}
        rec.update({"kind": req["kind"], "group": req["group"], "consumer": req["consumer"],
                    "needed_for": req["needed_for"]})
        coverage[req["id"]] = rec
    problems = check_memory(coverage)

    counts: dict[str, int] = defaultdict(int)
    for rec in coverage.values():
        counts[rec["status"]] += 1
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "board_id": args.board_id,
              "inputs": {k: str(getattr(args, k)) for k in ("user", "facts", "manifest", "research", "probe")
                         if getattr(args, k)},
              "summary": dict(counts), "rule_violations": problems, "requirements": coverage}
    (args.out / "coverage.yaml").write_text(yaml.safe_dump(report, sort_keys=False, width=110), encoding="utf-8")

    # Markdown summary, grouped by phase that needs the value.
    md = [f"# Runner requirement coverage: {args.board_id}", "",
          "| Status | Count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(counts.items())]
    if problems:
        md += ["", "## Rule violations", ""] + [f"- {p}" for p in problems]
    for need in NEED_ORDER:
        rows = [(rid, r) for rid, r in coverage.items() if r["needed_for"][0] == need]
        if not rows:
            continue
        md += ["", f"## Needed for {need}", "", "| Requirement | Status | Value | Source |", "|---|---|---|---|"]
        for rid, r in rows:
            md.append(f"| `{rid}` | {r['status']} | {fmt(r.get('value', '')) if 'value' in r else ''} | "
                      f"{fmt(r.get('source', ''))} |")
    (args.out / "coverage.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # Questions: open items grouped by their next source.
    by_next: dict[str, list[str]] = defaultdict(list)
    for req in catalog["requirements"]:
        r = coverage[req["id"]]
        if r["status"] == "resolved":
            continue
        if r["status"] == "conflict":
            alts = "; ".join(f"{fmt(c['value'])} ({c['via']}: {fmt(c['source'])})" for c in r["conflicts"])
            by_next["conflict"].append(f"- `{req['id']}`: using {fmt(r['value'])} ({r['via']}: {fmt(r['source'])}); "
                                       f"also seen {alts}. Decide with the most direct evidence (hardware > TRM > DTS > log).")
            continue
        if r["status"] == "choice_open":
            by_next["decide"].append(f"- `{req['id']}` -> {req['consumer']}: " + "; ".join(r["rules"]))
            continue
        nxt = r.get("next") or []
        if r["status"] == "default":
            by_next["confirm default"].append(f"- `{req['id']}` = {fmt(r['value'])} ({req['consumer']})")
            continue
        if not nxt:
            by_next["ask user"].append(f"- `{req['id']}`: {req['description']}")
            continue
        (kind, arg), = nxt[0].items()
        later = ", then ".join(f"{k}: {fmt(v)}" for s in nxt[1:] for k, v in s.items())
        hint = f" If not found, then {later}." if later else ""
        by_next[kind].append(f"- `{req['id']}`: {req['description']} Look in: {fmt(arg)}.{hint}")
    titles = {"conflict": "Resolve conflicting values first",
              "document": "Search the documents the user supplied",
              "web": "Search the web (official vendor sources; cite URL + revision)",
              "probe": "Measure on hardware", "decide": "Agent decisions (apply the rules, record them)",
              "confirm default": "Confirm or override catalog defaults", "ask user": "Ask the user"}
    q = [f"# Open requirements: {args.board_id}", "",
         "Resolve in this order. Record each answer in a research report (same IDs) or the user answers file,",
         "then rerun resolve_requirements.py.", ""]
    for key in ("conflict", "ask user", "document", "web", "probe", "decide", "confirm default"):
        if by_next.get(key):
            q += [f"## {titles[key]}", ""] + by_next[key] + [""]
    (args.out / "questions.md").write_text("\n".join(q), encoding="utf-8")

    # Draft preset from resolved single-key values.
    draft = ["# DRAFT from resolve_requirements.py: review every line; unresolved keys are missing.",
             f"BOARD_NAME={args.board_id}"]
    for rid, r in coverage.items():
        keys = [k.strip() for k in r["consumer"].split(",")]
        if len(keys) != 1 or not keys[0].startswith("BOARD_") or " " in keys[0]:
            continue
        if r["status"] in ("resolved", "default", "conflict") and not isinstance(r.get("value"), (dict, list)):
            flag = "  CONFLICT, see questions.md" if r["status"] == "conflict" else ""
            draft.append(f"{keys[0]}={fmt(r['value'])}  # {rid} ({r['status']}: {fmt(r.get('source', ''))}){flag}")
    (args.out / "preset.draft").write_text("\n".join(draft) + "\n", encoding="utf-8")

    print(f"board={args.board_id} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
          + f" rule_violations={len(problems)} -> {args.out}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
