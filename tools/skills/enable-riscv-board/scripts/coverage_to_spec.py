#!/usr/bin/env python3
"""Turn resolve_requirements.py coverage into a board spec draft.

Maps each runner requirement (assets/runner-requirements.yaml ID) in
coverage.yaml to its field in the board spec read by gen_board_files.py and
gen_act_config.py. Values are converted only when they parse unambiguously:
conflicts, open items and free text that does not parse become blockers, never
guesses. With --base-spec, fields the coverage does not carry (dut, csr, boot
pattern options, evidence) are kept from that spec, and every field where the
coverage disagrees with it is listed.

Writes <out>.yaml (the draft) and <out>.md (mapped, blockers, differences).
Exit 0 when the draft has no blockers, 2 otherwise.
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
import re
import sys
from typing import Any, Callable

import yaml


class Unparsed(Exception):
    pass


def as_int(v: Any) -> int:
    if isinstance(v, bool):
        raise Unparsed(f"expected a number, got {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"\s*(0[xX][0-9a-fA-F_]+|\d+)\s*", v):
        return int(v.replace("_", ""), 0)
    raise Unparsed(f"expected a number, got {v!r}")


def as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        t = v.lower()
        if re.search(r"\bmust initiali[sz]e\b|^(yes|true|enabled?)\b", t):
            return True
        if re.search(r"^(no|false|disabled?|none)\b|\bnot needed\b", t):
            return False
    raise Unparsed(f"expected yes/no, got {v!r}")


def window(v: Any, size_key: str | None = None) -> dict[str, int]:
    if not isinstance(v, dict) or "base" not in v:
        raise Unparsed(f"expected {{base, limit}}, got {v!r}")
    base = as_int(v["base"])
    if "limit" in v:
        return {"base": base, "limit": as_int(v["limit"])}
    if size_key and size_key in v:
        return {"base": base, "limit": base + as_int(v[size_key])}
    raise Unparsed(f"window without limit: {v!r}")


def context(key: str) -> Callable[[Any], int]:
    def conv(v: Any) -> int:
        if isinstance(v, dict):
            return as_int(v[key])
        return as_int(v)
    return conv


def firmware_window_limit(v: Any, cov: dict) -> int:
    """End of the reserved region holding the load address (exclusive)."""
    if not isinstance(v, list):
        return as_int(v)
    load = as_int(cov["boot.load_address"]["value"])
    for region in v:
        start, end = as_int(region["start"]), as_int(region["end"])
        if start <= load <= end:
            return end + 1
    raise Unparsed(f"no firmware window contains the load address {load:#x}")


def boot_pattern(v: Any) -> str:
    t = str(v).lower()
    if "nsign" in t:
        return "eswin_nsign"
    if "fit" in t or "itb" in t:
        return "fit"
    return "custom"


def watchdog(v: Any) -> Any:
    if v in (0, None, False) or (isinstance(v, str) and re.match(r"\s*(disabled|none|no)\b", v.lower())):
        return None
    if isinstance(v, dict) and {"base", "load", "ctrl", "lock", "unlock_key"} <= set(v):
        return v
    raise Unparsed(f"watchdog needs {{base, load, ctrl, lock, unlock_key}} or 'disabled', got {v!r}")


def dev_path(v: Any) -> str:
    m = re.search(r"(/dev/\S+)", str(v))
    if not m:
        raise Unparsed(f"no /dev path in {v!r}")
    return m.group(1).rstrip(",;")


def first_path(v: Any) -> str:
    m = re.match(r"\s*(/[^\s,;]+)", str(v))
    if not m:
        raise Unparsed(f"no directory in {v!r}")
    return m.group(1)


def priv_version(v: Any) -> str:
    m = re.fullmatch(r"\s*v?(1\.1[1-3])\s*", str(v))
    if not m:
        raise Unparsed(f"privileged version {v!r}")
    return m.group(1)


def stacks(v: Any) -> int:
    if isinstance(v, dict):
        return as_int(v["firmware"])
    m = re.search(r"\bfw\s+(0x[0-9a-fA-F]+|\d+)", str(v))     # catalog default "fw 0x4000, test ..., trap ..."
    return as_int(m.group(1)) if m else as_int(v)


def platform_id(v: Any) -> int:
    if isinstance(v, str) and v.strip().lower().startswith("next unused"):
        return 0          # generic; gen_board_files.py lists the runner_shared.h registration step
    return as_int(v)


def vm_modes(v: Any) -> list[str]:
    """'riscv,sv48' -> [sv39, sv48]: an implementation of SvN also implements the narrower RV64 modes."""
    if isinstance(v, list):
        return [str(x).lower() for x in v]
    m = re.search(r"sv(32|39|48|57)", str(v).lower())
    if not m:
        raise Unparsed(f"no Sv mode in {v!r}")
    widest = int(m.group(1))
    return ["sv32"] if widest == 32 else [f"sv{n}" for n in (39, 48, 57) if n <= widest]


def hpm_counters(v: Any) -> list[int]:
    if isinstance(v, list):
        return [as_int(x) for x in v]
    mask = as_int(v)
    return [i for i in range(3, 32) if mask >> i & 1]


def tvec(v: Any) -> dict:
    return {"modes": [0, 1], "vectored_alignment": as_int(v)}


def ids(v: Any) -> dict:
    if not isinstance(v, dict) or not {"mvendorid", "marchid", "mimpid"} <= set(v):
        raise Unparsed(f"expected {{mvendorid, marchid, mimpid}}, got {v!r}")
    return {k: as_int(v[k]) for k in ("mvendorid", "marchid", "mimpid")}


def version(v: Any) -> str:
    m = re.search(r"(\d+\.\d+)", str(v))
    if not m:
        raise Unparsed(f"no version number in {v!r}")
    return m.group(1)


def misa_x(v: Any) -> bool:
    m = re.match(r"\s*rv(?:32|64)([a-z]+)", str(v).lower())
    if not m:
        return as_bool(v)
    return "x" in m.group(1)


# requirement ID -> (spec path, converter). Converters taking (value, cov) get the coverage too.
MAP: dict[str, tuple[str, Callable]] = {
    "boot.load_address": ("runner.load_address", as_int),
    "boot.firmware_window": ("runner.firmware_window_limit", firmware_window_limit),
    "boot.image_format": ("boot.pattern", boot_pattern),
    "harts.runner": ("test_hart", as_int),
    "harts.monitor": ("runner.monitor_hart", as_int),
    "harts.secondary_wakeup": ("runner.k1_hart_wakeup", as_bool),
    "uart.base": ("platform.uart.base", as_int),
    "uart.size": ("runner.uart.size", as_int),
    "uart.register_width": ("runner.uart.access_bytes", as_int),
    "uart.register_spacing": ("platform.uart.reg_shift", as_int),
    "uart.input_clock_hz": ("runner.uart.input_clock_hz", as_int),
    "uart.baud": ("runner.uart.baud", as_int),
    "uart.init_by_runner": ("runner.uart.init_8250", as_bool),
    "uart.plic_source": ("platform.uart.plic_source", as_int),
    "timer.mswi_base": ("platform.clint.base", as_int),
    "timer.mtime_hz": ("platform.mtime_hz", as_int),
    "interrupt.plic_base": ("platform.plic.base", as_int),
    "interrupt.runner_m_context": ("platform.plic.test_hart_contexts.m", context("M")),
    "interrupt.runner_s_context": ("platform.plic.test_hart_contexts.s", context("S")),
    "interrupt.cleanup_enable": ("runner.external_irq_cleanup", as_bool),
    "memory.ram_window": ("runner.ram", window),
    "memory.payload_window": ("memory", lambda v: (lambda w: {"base": w["base"], "size": w["limit"] - w["base"]})(
        window(v))),
    "memory.excluded_range": ("runner.excluded", window),
    "memory.staging_buffer": ("runner.staging", lambda v: {"address": as_int(v["base"]), "max_bytes": as_int(v["bytes"])}
                              if isinstance(v, dict) else (_ for _ in ()).throw(Unparsed(repr(v)))),
    "memory.fixed_tohost": ("runner.tohost_fallback", as_int),
    "memory.stacks": ("runner.stack_bytes", stacks),
    "platform.watchdog": ("runner.watchdog", watchdog),
    "platform.id": ("runner.platform_id", platform_id),
    "platform.rx_diagnostic": ("runner.uart.rx_diag", as_bool),
    "bench.console_path": ("runner.serial_dev", dev_path),
    "bench.toolchain": ("runner.toolchain_dir", first_path),
    "identity.isa": ("isa", str),
    "identity.priv_version": ("priv_version", priv_version),
    "act.vm_modes": ("virtual_memory", vm_modes),
    "act.phys_addr_bits": ("csr.phys_addr_bits", as_int),
    "act.asid_bits": ("csr.asid_bits", as_int),
    "act.vmid_bits": ("csr.vmid_bits", as_int),
    "act.pmp_entries": ("csr.pmp.entries", as_int),
    "act.pmp_granularity": ("csr.pmp.granularity_bytes", as_int),
    "act.hpm_counters": ("csr.hpm_counters", hpm_counters),
    "act.hpm_counter_width": ("csr.hpm_counter_width", as_int),
    "act.medeleg_writable": ("csr.medeleg_writable", as_int),
    "act.mideleg_writable": ("csr.mideleg_writable", as_int),
    "act.mtvec_vectored_alignment": ("csr.mtvec", tvec),
    "act.stvec_vectored_alignment": ("csr.stvec", tvec),
    "act.time_csr": ("csr.time_csr_implemented", as_bool),
    "act.ids": ("platform.ids", ids),
    "act.cpu_hz_max": ("platform.cpu_hz_max", as_int),
    "act.cache_block_bytes": ("platform.cache_block_bytes", as_int),
    "act.plic_sources": ("platform.plic.sources", as_int),
    "act.hypervisor_version": ("hypervisor.hardware_version", version),
    "act.non_standard": ("non_standard_extensions", misa_x),
}
# ACT-only items: an open one is reported for the ACT phase but does not block the runner.
ACT_ONLY = {rid for rid in MAP if rid.startswith("act.")}
TAKES_COVERAGE = {firmware_window_limit}


def get(d: dict, path: str) -> Any:
    cur: Any = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def put(d: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def show(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, int):
        return hex(v) if v >= 0x1000 and v % 0x1000 == 0 else str(v)   # addresses hex, rates decimal
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {show(x)}" for k, x in v.items()) + "}"
    return str(v)


def bridge(cov: dict, base: dict | None, prefer: str = "coverage") -> tuple[dict, list[str], list[str], list[str], list[str]]:
    """prefer='base' keeps a reviewed spec's value where the coverage differs (the difference is still listed)."""
    reqs = cov["requirements"]
    spec = copy.deepcopy(base) if base else {}
    mapped, blockers, defaults, diffs = [], [], [], []
    for rid, (path, conv) in MAP.items():
        rec = reqs.get(rid)
        if rec is None:
            continue
        status = rec.get("status")
        if status in ("open", "choice_open") and rid in ACT_ONLY:
            continue          # gen_act_config.py reports what the ACT config still lacks
        if status in ("conflict", "open", "choice_open"):
            others = "; ".join(f"{show(c.get('value'))} ({c.get('source')})" for c in rec.get("conflicts", []))
            blockers.append(f"`{rid}` -> `{path}`: {status}"
                            + (f": {show(rec.get('value'))} ({rec.get('source')}) vs {others}" if others else ""))
            continue
        try:
            value = conv(rec["value"], reqs) if conv in TAKES_COVERAGE else conv(rec["value"])
        except (Unparsed, KeyError, TypeError) as exc:
            blockers.append(f"`{rid}` -> `{path}`: cannot convert ({exc})")
            continue
        old = get(spec, path)
        if base is not None and old is not None and old != value:
            kept = "kept spec" if prefer == "base" else "used coverage"
            diffs.append(f"`{path}`: spec {show(old)}, coverage {show(value)} (`{rid}` via {rec.get('via')}; {kept})")
            if prefer == "base":
                continue
        put(spec, path, value)
        label = {"default": "catalog default", "decided": "rule decision"}.get(status)
        (defaults if label else mapped).append(f"`{path}` = {show(value)} (`{rid}`, {label or rec.get('via')})")
    clint = get(spec, "platform.clint.base")
    for rid, offset in (("timer.mtimecmp_base", 0x4000), ("timer.mtime_address", 0xBFF8)):
        rec = reqs.get(rid)
        if clint is not None and rec and rec.get("status") == "resolved":
            try:
                if as_int(rec["value"]) != clint + offset:
                    blockers.append(f"`{rid}` = {rec['value']} is not CLINT base + {offset:#x}: the generators "
                                    "assume the standard SiFive CLINT layout")
            except Unparsed as exc:
                blockers.append(f"`{rid}`: {exc}")
    for path in ("runner.board_id", "runner.description", "boot.image", "dut.name"):
        if get(spec, path) is None:
            blockers.append(f"`{path}`: not in the coverage; set it in the spec")
    return spec, mapped, defaults, blockers, diffs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("coverage", type=Path, help="coverage.yaml from resolve_requirements.py")
    ap.add_argument("--base-spec", type=Path, help="existing board spec to merge into and compare with")
    ap.add_argument("--out", type=Path, required=True, help="output path without extension")
    ap.add_argument("--prefer", choices=("coverage", "base"), default="coverage",
                    help="on a difference keep the coverage value (default) or the reviewed base spec value")
    args = ap.parse_args()
    cov = yaml.safe_load(args.coverage.read_text())
    base = yaml.safe_load(args.base_spec.read_text()) if args.base_spec else None
    spec, mapped, defaults, blockers, diffs = bridge(cov, base, args.prefer)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    head = (f"# Board spec draft for {cov.get('board_id')}, generated by coverage_to_spec.py from {args.coverage}.\n"
            "# Review blockers and differences in the .md file before using it.\n")
    args.out.with_suffix(".yaml").write_text(head + yaml.safe_dump(spec, sort_keys=False, width=200))
    md = [f"# Board spec draft: {cov.get('board_id')}", "",
          f"{len(mapped)} mapped, {len(defaults)} from catalog defaults, {len(blockers)} blockers, "
          f"{len(diffs)} differences from the base spec.", ""]
    for title, rows in (("Blockers", blockers), ("Differences from the base spec", diffs),
                        ("Catalog defaults (confirm)", defaults), ("Mapped", mapped)):
        if rows:
            md += [f"## {title}", ""] + [f"- {r}" for r in rows] + [""]
    args.out.with_suffix(".md").write_text("\n".join(md))
    print(f"{args.out}.yaml: mapped={len(mapped)} defaults={len(defaults)} blockers={len(blockers)} "
          f"differences={len(diffs)}")
    return 2 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
