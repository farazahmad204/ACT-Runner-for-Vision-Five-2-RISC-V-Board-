#!/usr/bin/env python3
"""Extract board facts for the UART runner from a device tree and boot logs.

Inputs (all optional, use what exists for the board):
  --dtb / --dts     vendor device tree (DTB needs `dtc` on PATH)
  --opensbi-log     UART log of a stock boot that prints the OpenSBI banner
  --linux-log       UART log of a stock Linux boot (dmesg on the console)
  --uboot-log       UART log with the U-Boot banner

Output: a YAML report of fact records (id, value, source, evidence_status)
using the fact IDs from references/research-scope.md, plus the gaps that are
still unknown. Values are read, never guessed: anything derived from another
value is marked `inferred` and says how.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# Facts the runner port needs (see references/research-scope.md).
REQUIRED = [
    "identity.board_soc", "identity.isa", "identity.priv_version",
    "runner.hart_entry", "cpu.hart_ids",
    "uart.base", "uart.register_width", "uart.register_spacing", "uart.clock",
    "uart.initialization_owner",
    "timer.mtime_address", "timer.mtime_hz", "timer.mswi_base", "timer.mtimecmp_base",
    "interrupt.plic_base", "interrupt.uart_source", "interrupt.contexts",
    "memory.dram_base", "memory.documented_window", "memory.firmware_window",
    "memory.reserved_regions",
]


@dataclass
class Fact:
    id: str
    value: Any
    source: str
    evidence_status: str = "confirmed"
    note: str | None = None


@dataclass
class Node:
    name: str
    props: dict[str, Any] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None

    @property
    def path(self) -> str:
        if self.parent is None:
            return "/"
        base = self.parent.path
        return (base if base.endswith("/") else base + "/") + self.name

    def cells(self, key: str, default: int) -> int:
        value = self.props.get(key)
        return value[0] if isinstance(value, list) and value and isinstance(value[0], int) else default


# ----------------------------------------------------------------------------
# Device tree
# ----------------------------------------------------------------------------

def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("<"):
        cells: list[int] = []
        for group in re.findall(r"<([^>]*)>", raw):
            for token in group.split():
                cells.append(int(token, 0) if re.fullmatch(r"(0x[0-9a-fA-F]+|\d+)", token) else 0)
        return cells
    if raw.startswith('"'):
        strings = re.findall(r'"((?:[^"\\]|\\.)*)"', raw)
        parts: list[str] = []
        for s in strings:
            parts.extend(s.split("\\0"))
        return [p for p in parts if p != ""]
    if raw.startswith("["):
        return bytes(int(b, 16) for b in raw.strip("[]").split())
    return raw


def parse_dts(text: str) -> Node:
    root = Node("/")
    stack = [root]
    for line in text.splitlines():
        line = line.split("//")[0].strip()
        if not line or line.startswith("/dts-v1/") or line.startswith("/memreserve/"):
            continue
        m = re.match(r"^(?:[\w,.-]+:\s*)?([\w,.@+-]+|/)\s*\{$", line)
        if m:
            name = m.group(1)
            if name == "/":
                continue
            node = Node(name, parent=stack[-1])
            stack[-1].children.append(node)
            stack.append(node)
            continue
        if line.startswith("};"):
            if len(stack) > 1:
                stack.pop()
            continue
        m = re.match(r"^([#\w,.+-]+)\s*=\s*(.*);$", line)
        if m:
            stack[-1].props[m.group(1)] = _parse_value(m.group(2))
            continue
        m = re.match(r"^([#\w,.+-]+);$", line)
        if m:
            stack[-1].props[m.group(1)] = True
    return root


def walk(node: Node):
    yield node
    for child in node.children:
        yield from walk(child)


def regs(node: Node) -> list[tuple[int, int]]:
    reg = node.props.get("reg")
    if not isinstance(reg, list) or node.parent is None:
        return []
    ac = node.parent.cells("#address-cells", 2)
    sc = node.parent.cells("#size-cells", 1)
    out, step = [], ac + sc
    for i in range(0, len(reg) - step + 1, step):
        addr = 0
        for c in reg[i:i + ac]:
            addr = (addr << 32) | c
        size = 0
        for c in reg[i + ac:i + step]:
            size = (size << 32) | c
        out.append((addr, size))
    return out


def compatible(node: Node) -> list[str]:
    value = node.props.get("compatible", [])
    return value if isinstance(value, list) else []


def first_str(node: Node, key: str) -> str | None:
    value = node.props.get(key)
    return value[0] if isinstance(value, list) and value and isinstance(value[0], str) else None


def first_int(node: Node, key: str) -> int | None:
    value = node.props.get(key)
    return value[0] if isinstance(value, list) and value and isinstance(value[0], int) else None


def from_dts(root: Node, src: str, runner_hart: int | None) -> list[Fact]:
    facts: list[Fact] = []
    nodes = list(walk(root))
    by_phandle = {first_int(n, "phandle"): n for n in nodes if first_int(n, "phandle") is not None}

    model = first_str(root, "model")
    if model:
        facts.append(Fact("identity.board_soc", model, f"{src}: /model"))

    cpus = [n for n in nodes if first_str(n, "device_type") == "cpu"]
    harts = sorted(regs(c)[0][0] if regs(c) else first_int(c, "reg") for c in cpus)
    if cpus:
        facts.append(Fact("cpu.hart_ids", harts, f"{src}: cpu nodes"))
        cpu0 = cpus[0]
        for key, fid in (("riscv,isa", "identity.isa"), ("mmu-type", "cpu.mmu_type")):
            if first_str(cpu0, key):
                facts.append(Fact(fid, first_str(cpu0, key), f"{src}: {cpu0.path} {key}"))
        for key, fid in (("riscv,pmpregions", "cpu.pmp_regions"),
                         ("riscv,pmpgranularity", "cpu.pmp_granularity"),
                         ("d-cache-block-size", "cpu.cache_block_size")):
            if first_int(cpu0, key) is not None:
                facts.append(Fact(fid, first_int(cpu0, key), f"{src}: {cpu0.path} {key}"))
        tb = first_int(cpu0, "timebase-frequency")
        if tb is None and cpu0.parent is not None:
            tb = first_int(cpu0.parent, "timebase-frequency")
        if tb:
            facts.append(Fact("timer.mtime_hz", tb, f"{src}: timebase-frequency"))

    # cpu interrupt-controller phandle -> hart id
    intc_hart: dict[int, int] = {}
    for cpu in cpus:
        hart = regs(cpu)[0][0] if regs(cpu) else first_int(cpu, "reg")
        for child in cpu.children:
            ph = first_int(child, "phandle")
            if ph is not None and "riscv,cpu-intc" in compatible(child):
                intc_hart[ph] = hart

    mem = [r for n in nodes if first_str(n, "device_type") == "memory" for r in regs(n)]
    if mem:
        main_base, main_size = max(mem, key=lambda r: r[1])
        facts.append(Fact("memory.dram_base", hex(main_base), f"{src}: largest memory node",
                          note=f"size {hex(main_size)}; smaller memory nodes are usually on-chip SRAM"))
        facts.append(Fact("memory.documented_window",
                          [{"base": hex(a), "size": hex(s)} for a, s in mem], f"{src}: memory nodes"))

    reserved = []
    for n in nodes:
        if n.parent is not None and n.parent.name == "reserved-memory":
            for a, s in regs(n):
                reserved.append({"name": n.name, "base": hex(a), "size": hex(s), "no_map": bool(n.props.get("no-map"))})
    if reserved:
        facts.append(Fact("memory.reserved_regions", reserved, f"{src}: /reserved-memory"))

    # Console UART via /chosen stdout-path and /aliases.
    chosen = next((n for n in nodes if n.path == "/chosen"), None)
    aliases = next((n for n in nodes if n.path == "/aliases"), None)
    uart = None
    if chosen and first_str(chosen, "stdout-path"):
        target = first_str(chosen, "stdout-path").split(":")[0]
        if not target.startswith("/") and aliases and first_str(aliases, target):
            target = first_str(aliases, target)
        uart = next((n for n in nodes if n.path == target), None)
    if uart:
        where = f"{src}: {uart.path}"
        if regs(uart):
            facts.append(Fact("uart.base", hex(regs(uart)[0][0]), where))
        width = first_int(uart, "reg-io-width")
        facts.append(Fact("uart.register_width", width if width else 1, where + " reg-io-width",
                          "confirmed" if width else "inferred", None if width else "property absent: DT default 1"))
        shift = first_int(uart, "reg-shift")
        facts.append(Fact("uart.register_spacing", shift if shift is not None else 0, where + " reg-shift",
                          "confirmed" if shift is not None else "inferred"))
        if first_int(uart, "clock-frequency"):
            facts.append(Fact("uart.clock", first_int(uart, "clock-frequency"), where + " clock-frequency"))
        facts.append(Fact("uart.compatible", compatible(uart), where))
        src_irq = first_int(uart, "interrupts")
        if src_irq is not None:
            facts.append(Fact("interrupt.uart_source", src_irq, where + " interrupts"))

    plic = next((n for n in nodes if any("plic" in c for c in compatible(n))), None)
    if plic:
        where = f"{src}: {plic.path}"
        if regs(plic):
            facts.append(Fact("interrupt.plic_base", hex(regs(plic)[0][0]), where))
        if first_int(plic, "riscv,ndev"):
            facts.append(Fact("interrupt.plic_sources", first_int(plic, "riscv,ndev"), where + " riscv,ndev"))
        ext = plic.props.get("interrupts-extended", [])
        contexts = []
        for ctx, i in enumerate(range(0, len(ext) - 1, 2)):
            hart = intc_hart.get(ext[i])
            irq = ext[i + 1]
            mode = {11: "M", 9: "S"}.get(irq, "unused" if irq == 0xFFFFFFFF else f"irq{irq}")
            contexts.append({"context": ctx, "hart": hart, "mode": mode if irq != 0xFFFFFFFF else "M (unused by Linux)"})
        if contexts:
            facts.append(Fact("interrupt.contexts", contexts, where + " interrupts-extended",
                              note="0xffffffff entries are contexts Linux does not use (usually M-mode)"))
            if runner_hart is not None:
                mine = [c["context"] for c in contexts if c["hart"] == runner_hart]
                if len(mine) == 2:
                    facts.append(Fact("interrupt.runner_contexts", {"M": mine[0], "S": mine[1]},
                                      where + f" (hart {runner_hart})", "inferred",
                                      "M listed before S for each hart in interrupts-extended"))

    clint = next((n for n in nodes if any(k in c for c in compatible(n) for k in ("clint", "aclint-mswi"))), None)
    if clint and regs(clint):
        base = regs(clint)[0][0]
        facts.append(Fact("timer.mswi_base", hex(base), f"{src}: {clint.path}"))
        facts.append(Fact("timer.mtimecmp_base", hex(base + 0x4000), f"{src}: {clint.path}", "inferred", "SiFive CLINT layout"))
        facts.append(Fact("timer.mtime_address", hex(base + 0xBFF8), f"{src}: {clint.path}", "inferred", "SiFive CLINT layout"))
    return facts


# ----------------------------------------------------------------------------
# Boot logs
# ----------------------------------------------------------------------------

def clean(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).replace("\r", "")


def from_opensbi(text: str, src: str) -> list[Fact]:
    facts: list[Fact] = []
    kv = dict(re.findall(r"^(Platform Name|Platform HART Count|Platform IPI Device|Platform Timer Device|"
                         r"Platform Console Device|Firmware Base|Firmware Size|Boot HART ID|Boot HART Base ISA|"
                         r"Boot HART ISA Extensions|Boot HART Priv Version|Boot HART PMP Count|"
                         r"Boot HART PMP Granularity|Boot HART PMP Address Bits|Boot HART MHPM Info|"
                         r"Boot HART MIDELEG|Boot HART MEDELEG|Runtime SBI Version)\s*:\s*(.+)$", text, re.M))
    if not kv:
        return facts
    def add(fid, value, key, status="confirmed", note=None):
        facts.append(Fact(fid, value, f"{src}: OpenSBI '{key}'", status, note))
    if "Platform Name" in kv: add("identity.board_soc", kv["Platform Name"].strip(), "Platform Name")
    if "Platform HART Count" in kv: add("runner.hart_count", int(kv["Platform HART Count"]), "Platform HART Count")
    if "Boot HART ID" in kv: add("runner.observed_boot_hart", int(kv["Boot HART ID"]), "Boot HART ID",
                                 note="cold-boot lottery winner for this boot only")
    if "Boot HART Base ISA" in kv: add("identity.misa", kv["Boot HART Base ISA"].strip(), "Boot HART Base ISA")
    if "Boot HART ISA Extensions" in kv: add("identity.isa_extensions", kv["Boot HART ISA Extensions"].strip(), "Boot HART ISA Extensions")
    if "Boot HART Priv Version" in kv: add("identity.priv_version", kv["Boot HART Priv Version"].strip(), "Boot HART Priv Version")
    if "Boot HART PMP Count" in kv: add("cpu.pmp_regions", int(kv["Boot HART PMP Count"]), "Boot HART PMP Count")
    g = re.match(r"(\d+)", kv.get("Boot HART PMP Granularity", ""))
    if g: add("cpu.pmp_granularity", 1 << int(g.group(1)), "Boot HART PMP Granularity")
    b = re.match(r"(\d+)", kv.get("Boot HART PMP Address Bits", ""))
    if b: add("cpu.phys_addr_bits", int(b.group(1)) + 2, "Boot HART PMP Address Bits", "inferred",
              "pmpaddr holds PA[N-1:2], so N = printed bits + 2")
    m = re.match(r"(\d+)\s*\((0x[0-9a-fA-F]+)\)", kv.get("Boot HART MHPM Info", ""))
    if m: add("cpu.hpm_counter_mask", m.group(2), "Boot HART MHPM Info", note=f"{m.group(1)} counters")
    t = re.search(r"([\w-]+)\s*@\s*(\d+)\s*Hz", kv.get("Platform Timer Device", ""))
    if t:
        add("timer.mtime_hz", int(t.group(2)), "Platform Timer Device")
        add("timer.device", t.group(1), "Platform Timer Device")
    if "Platform Console Device" in kv: add("uart.opensbi_driver", kv["Platform Console Device"].strip(), "Platform Console Device")
    base = kv.get("Firmware Base", "").strip()
    size = kv.get("Firmware Size", "").strip()
    if base: add("runner.load_address", base, "Firmware Base", "inferred",
                 "address of the stock M-mode stage the runner replaces")
    regions = re.findall(r"^Domain0 Region\d+\s*:\s*(0x[0-9a-f]+)-(0x[0-9a-f]+)\s+M:\s*\(([^)]*)\)\s+S/U:\s*\(([^)]*)\)", text, re.M)
    if regions:
        rows = [{"start": a, "end": e, "m": m, "su": su} for a, e, m, su in regions]
        add("memory.firmware_window", [r for r in rows if r["su"] == "" and "I" not in r["m"] and r["m"]],
            "Domain0 Region", note="M-only RAM regions protected by OpenSBI (stock firmware window)")
        mmio = [r for r in rows if "I" in r["m"]]
        if mmio:
            add("timer.aclint_region", mmio, "Domain0 Region", "inferred",
                "M-only I/O regions; on SiFive-style platforms the first is the CLINT/ACLINT")
            first = int(mmio[0]["start"], 16)
            facts.append(Fact("timer.mswi_base", hex(first), f"{src}: OpenSBI Domain0 Region (I/O)", "inferred",
                              "confirm with the DT clint node or vendor manual"))
    if base and size:
        add("memory.stock_firmware", {"base": base, "size": size}, "Firmware Base/Size")
    return facts


def from_linux(text: str, src: str) -> list[Fact]:
    facts: list[Fact] = []
    m = re.search(r"Machine model:\s*(.+)", text)
    if m: facts.append(Fact("identity.board_soc", m.group(1).strip(), f"{src}: Linux 'Machine model'"))
    m = re.search(r"riscv: base ISA extensions\s+(\w+)", text)
    if m: facts.append(Fact("identity.linux_base_isa", m.group(1), f"{src}: Linux 'riscv: base ISA extensions'"))
    m = re.search(r"ASID allocator (?:disabled|using) \((\d+) bits\)", text)
    if m: facts.append(Fact("cpu.asid_bits", int(m.group(1)), f"{src}: Linux 'ASID allocator'"))
    m = re.search(r"earlycon: (\w+) at MMIO(\d+)? (0x[0-9a-fA-F]+)", text)
    if m:
        facts.append(Fact("uart.base", hex(int(m.group(3), 16)), f"{src}: Linux earlycon"))
        if m.group(2):
            facts.append(Fact("uart.register_width", int(m.group(2)) // 8, f"{src}: Linux earlycon MMIO{m.group(2)}"))
    res = re.findall(r"OF: reserved mem: (0x[0-9a-f]+)\.\.(0x[0-9a-f]+) \(\d+ KiB\) (nomap|map) [\w-]+ ([\w@,.-]+)", text)
    if res:
        facts.append(Fact("memory.reserved_regions",
                          [{"name": n, "base": a, "end": e, "no_map": k == "nomap"} for a, e, k, n in res],
                          f"{src}: Linux 'OF: reserved mem'"))
    nodes = re.findall(r"node\s+\d+: \[mem (0x[0-9a-f]+)-(0x[0-9a-f]+)\]", text)
    if nodes:
        facts.append(Fact("memory.linux_ranges", [{"start": a, "end": e} for a, e in nodes],
                          f"{src}: Linux 'Early memory node ranges'"))
    return facts


def from_uboot(text: str, src: str) -> list[Fact]:
    facts: list[Fact] = []
    m = re.search(r"U-Boot (\d{4}\.\d{2}[^\s]*) \(([^)]+)\)", text)
    if m: facts.append(Fact("boot.uboot_version", f"{m.group(1)} ({m.group(2)})", f"{src}: U-Boot banner"))
    m = re.search(r"^DRAM:\s+(.+)$", text, re.M)
    if m: facts.append(Fact("memory.dram_size", m.group(1).strip(), f"{src}: U-Boot 'DRAM:'"))
    return facts


# ----------------------------------------------------------------------------

LIST_UNION = {"memory.reserved_regions"}


def _region_key(r: dict) -> int:
    return int(r.get("base", "0"), 16)


def merge(facts: list[Fact]) -> tuple[dict[str, dict], list[dict]]:
    """First confirmed value wins; differing scalar values become conflicts.

    Region lists in LIST_UNION are merged by base address instead (a DTB and a
    Linux log describe the same reservations differently)."""
    merged: dict[str, dict] = {}
    conflicts: list[dict] = []
    order = {"confirmed": 0, "partial": 1, "inferred": 2, "unknown": 3}
    for f in sorted(facts, key=lambda f: order.get(f.evidence_status, 9)):
        record = {"value": f.value, "source": f.source, "evidence_status": f.evidence_status}
        if f.note:
            record["note"] = f.note
        if f.id in LIST_UNION:
            cur = merged.setdefault(f.id, {"value": [], "source": f.source, "evidence_status": f.evidence_status})
            if f.source not in cur["source"]:
                cur["source"] += "; " + f.source
            seen = {_region_key(r) for r in cur["value"]}
            for region in f.value:
                if _region_key(region) not in seen:
                    cur["value"].append(region)
                    seen.add(_region_key(region))
            cur["value"].sort(key=_region_key)
            continue
        if f.id not in merged:
            merged[f.id] = record
            continue
        cur = merged[f.id]
        same = str(cur["value"]).lower() == str(f.value).lower()
        if not same and f.evidence_status == "confirmed" and cur["evidence_status"] == "confirmed":
            conflicts.append({"id": f.id, "values": [cur, record]})
        else:
            cur.setdefault("also_seen_in", []).append(f.source)
    return merged, conflicts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dtb", type=Path)
    ap.add_argument("--dts", type=Path)
    ap.add_argument("--opensbi-log", type=Path)
    ap.add_argument("--linux-log", type=Path)
    ap.add_argument("--uboot-log", type=Path)
    ap.add_argument("--runner-hart", type=int, help="runner hart, to pick its PLIC contexts")
    ap.add_argument("--board-id", default="unknown")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()

    facts: list[Fact] = []
    inputs = []
    if args.dtb or args.dts:
        if args.dtb:
            dts = subprocess.run(["dtc", "-I", "dtb", "-O", "dts", str(args.dtb)], capture_output=True,
                                 text=True, check=True).stdout
            src = str(args.dtb)
        else:
            dts, src = args.dts.read_text(encoding="utf-8", errors="replace"), str(args.dts)
        facts += from_dts(parse_dts(dts), src, args.runner_hart)
        inputs.append(src)
    for path, fn in ((args.opensbi_log, from_opensbi), (args.linux_log, from_linux), (args.uboot_log, from_uboot)):
        if path:
            facts += fn(clean(path.read_text(encoding="latin1")), str(path))
            inputs.append(str(path))
    if not inputs:
        ap.error("give at least one of --dtb/--dts/--opensbi-log/--linux-log/--uboot-log")

    merged, conflicts = merge(facts)
    if "timer.mswi_base" in merged and "timer.mtime_address" not in merged:
        base = int(merged["timer.mswi_base"]["value"], 16)
        merged["timer.mtimecmp_base"] = {"value": hex(base + 0x4000), "source": "derived from timer.mswi_base",
                                         "evidence_status": "inferred", "note": "SiFive CLINT layout"}
        merged["timer.mtime_address"] = {"value": hex(base + 0xBFF8), "source": "derived from timer.mswi_base",
                                         "evidence_status": "inferred", "note": "SiFive CLINT layout"}
    if "uart.initialization_owner" not in merged and "uart.opensbi_driver" in merged and "runner.load_address" in merged:
        merged["uart.initialization_owner"] = {
            "value": "stage replaced by the runner (OpenSBI %s console): the runner must initialize the UART"
                     % merged["uart.opensbi_driver"]["value"],
            "source": "derived from OpenSBI 'Platform Console Device' and 'Firmware Base'",
            "evidence_status": "inferred",
            "note": "set BOARD_UART_8250_INIT_ENABLE=1 unless an earlier stage is shown to leave the UART configured"}
    missing = [fid for fid in REQUIRED if fid not in merged]
    report = {"schema_version": 1, "board_id": args.board_id, "inputs": inputs,
              "facts": merged, "conflicts": conflicts, "still_unknown": missing}
    text = yaml.safe_dump(report, sort_keys=False, width=110)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text if not args.output else f"facts={len(merged)} conflicts={len(conflicts)} still_unknown={len(missing)} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
