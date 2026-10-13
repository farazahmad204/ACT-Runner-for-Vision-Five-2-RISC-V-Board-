#!/usr/bin/env python3
"""Apply the runner-requirements `choice` rules to resolved facts.

Fills `choice_open` items in coverage.yaml with rule-derived decisions:

  harts.runner / harts.monitor   lowest two harts that enter the payload slot
  memory.payload_window          first 256 MiB boundary after the firmware window, 512 MiB
                                 (shrunk to fit, at least 64 MiB)
  memory.excluded_range          a known reserved region between payload and staging, or none
  memory.staging_buffer          next 256 MiB boundary after payload/exclusion, 128 MiB
  memory.ram_window              DRAM base of the load region up to the staging end
  memory.fixed_tohost            payload base + 0x8000

Every window stays inside the DRAM region that holds the load address and
clear of every reserved region the coverage knows. The decisions are only as
safe as that reserved-region list: a region the DTB and logs do not show (for
example a secure PMP region set by replaced firmware) is invisible here, so
decided items are marked `decided` and must be confirmed by the hardware
smoke test. Choices already resolved (by the user, manifest or research) are
never changed.
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
import re
from typing import Any

import yaml

MIB = 1 << 20
ALIGN = 256 * MIB
PAYLOAD = 512 * MIB
MIN_PAYLOAD = 64 * MIB
STAGING = 128 * MIB


def as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"\s*(0[xX][0-9a-fA-F_]+|\d+)\s*", v):
        return int(v.replace("_", ""), 0)
    return None


def regions(value: Any) -> list[tuple[int, int, str]]:
    """[(start, end_exclusive, name)] from coverage region records (base/size, base/end or start/end)."""
    out = []
    for r in value or []:
        if not isinstance(r, dict):
            continue
        start = as_int(r.get("base", r.get("start")))
        if start is None:
            continue
        if as_int(r.get("size")) is not None:
            end = start + as_int(r["size"])
        elif as_int(r.get("end")) is not None:
            end = as_int(r["end"]) + 1
        elif as_int(r.get("bytes")) is not None:
            end = start + as_int(r["bytes"])
        else:
            continue
        out.append((start, end, str(r.get("name", f"{start:#x}"))))
    return out


def align_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


def value_of(cov: dict, rid: str) -> Any:
    rec = cov.get(rid) or {}
    return rec.get("value") if rec.get("status") in ("resolved", "default", "decided") else None


def decide(coverage: dict) -> tuple[dict, list[str], list[str]]:
    cov = coverage["requirements"]
    decisions: dict[str, Any] = {}
    notes: list[str] = []
    blockers: list[str] = []

    def is_open(rid: str) -> bool:
        return (cov.get(rid) or {}).get("status") in ("choice_open", "open", None)

    # Harts
    entering = value_of(cov, "harts.entering")
    harts = sorted({int(x) for x in (entering if isinstance(entering, list) else re.findall(r"\b\d+\b", str(entering or "")))})
    known_ids = value_of(cov, "harts.ids")
    if isinstance(known_ids, list):
        harts = [h for h in harts if h in known_ids] or harts
    if is_open("harts.runner") or is_open("harts.monitor"):
        if len(harts) < 2:
            blockers.append("harts: need two harts that enter the payload slot (harts.entering); "
                            f"have {harts or 'none'}")
        else:
            if is_open("harts.runner"):
                decisions["harts.runner"] = harts[0]
            if is_open("harts.monitor"):
                runner = decisions.get("harts.runner", as_int(value_of(cov, "harts.runner")))
                decisions["harts.monitor"] = next(h for h in harts if h != runner)
            notes.append(f"harts: lowest entering harts {harts[:2]} (rule: in harts.entering, runner != monitor)")

    # Memory
    memory_ids = ("memory.payload_window", "memory.staging_buffer", "memory.ram_window", "memory.excluded_range",
                  "memory.fixed_tohost")
    if any(is_open(r) for r in memory_ids):
        load = as_int(value_of(cov, "boot.load_address"))
        dram = regions(value_of(cov, "memory.dram_window"))
        reserved = regions(value_of(cov, "memory.reserved_regions")) + regions(value_of(cov, "boot.firmware_window"))
        region = next(((s, e, n) for s, e, n in dram if load is not None and s <= load < e), None)
        if load is None or region is None:
            blockers.append("memory: need boot.load_address inside a known memory.dram_window region")
        else:
            holding = [e for s, e, _ in reserved if s <= load < e]
            fw_end = max(holding) if holding else load + MIB   # no known window: assume 1 MiB
            above = sorted((s, e, n) for s, e, n in reserved if e > fw_end and s < region[1])
            def free_until(addr: int) -> int:
                nxt = [s for s, e, _ in above if s >= addr]
                return min(nxt + [region[1]])
            pay_base = as_int((value_of(cov, "memory.payload_window") or {}).get("base")) \
                if isinstance(value_of(cov, "memory.payload_window"), dict) else None
            if pay_base is None:
                pay_base = align_up(fw_end, ALIGN)
                while any(s < pay_base + MIN_PAYLOAD and pay_base < e for s, e, _ in above):
                    blocking = max(e for s, e, _ in above if s < pay_base + MIN_PAYLOAD and pay_base < e)
                    pay_base = align_up(blocking, ALIGN)
                pay_end = min(pay_base + PAYLOAD, free_until(pay_base))
                if pay_end - pay_base < MIN_PAYLOAD:
                    blockers.append(f"memory: no {MIN_PAYLOAD >> 20} MiB payload window after {fw_end:#x}")
                    return decisions, notes, blockers
                decisions["memory.payload_window"] = {"base": pay_base, "limit": pay_end}
            else:
                pay_end = as_int(value_of(cov, "memory.payload_window").get("limit"))
            # Staging: next aligned free span of 128 MiB after the payload; reserved regions in between
            # become the excluded range.
            cand = align_up(pay_end, ALIGN)
            excluded = None
            for _ in range(64):
                clash = [(s, e, n) for s, e, n in above if s < cand + STAGING and cand < e]
                if not clash:
                    break
                cand = align_up(max(e for s, e, _ in clash), ALIGN)
            between = [(s, e, n) for s, e, n in above if s < cand and e > pay_end]
            if cand + STAGING > region[1]:
                blockers.append("memory: no 128 MiB staging buffer fits after the payload window")
                return decisions, notes, blockers
            if is_open("memory.staging_buffer"):
                decisions["memory.staging_buffer"] = {"base": cand, "bytes": STAGING}
            if is_open("memory.excluded_range"):
                if between:
                    excluded = {"base": min(s for s, _, _ in between), "limit": max(e for _, e, _ in between)}
                decisions["memory.excluded_range"] = excluded or {"base": 0, "limit": 0}   # 0/0 = none
            if is_open("memory.ram_window"):
                decisions["memory.ram_window"] = {"base": region[0], "limit": cand + STAGING}
            if is_open("memory.fixed_tohost"):
                decisions["memory.fixed_tohost"] = pay_base + 0x8000
            notes.append(
                f"memory: firmware window ends {fw_end:#x}; payload {pay_base:#x}-{pay_end:#x}; staging "
                f"{cand:#x}+{STAGING >> 20} MiB; excluded {excluded or 'none'}; based on {len(reserved)} known "
                "reserved regions (a region invisible to the DTB/logs is not avoided: confirm by smoke test)")
    return decisions, notes, blockers


def apply(coverage: dict, decisions: dict[str, Any], notes: list[str]) -> dict:
    out = copy.deepcopy(coverage)
    for rid, value in decisions.items():
        rec = out["requirements"].setdefault(rid, {})
        rec.update({"status": "decided", "via": "decision", "value": value,
                    "source": "decide_choices.py (catalog rules)", "evidence_status": "inferred"})
    out.setdefault("decision_notes", []).extend(notes)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("coverage", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="coverage with decisions applied")
    args = ap.parse_args()
    coverage = yaml.safe_load(args.coverage.read_text())
    decisions, notes, blockers = decide(coverage)
    args.out.write_text(yaml.safe_dump(apply(coverage, decisions, notes), sort_keys=False, width=200))
    for rid, v in decisions.items():
        print(f"decided {rid} = {v}")
    for n in notes:
        print(f"note: {n}")
    for b in blockers:
        print(f"blocker: {b}")
    return 2 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
