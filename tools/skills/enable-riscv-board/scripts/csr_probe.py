#!/usr/bin/env python3
"""Build and decode the generic CSR probe (assets/probes/csr_probe.S).

The probe runs like any test ELF through the UART runner, in M-mode on the
runner hart. It records CSR values, writable masks and trap causes in its
signature region and reports FAIL on purpose, so the runner dumps that region
as [SIG]/[SIGQ] lines. It restores every CSR it writes.

  csr_probe.py build --base 0x90000000 --out DIR   # base = BOARD_PAYLOAD_BASE
  csr_probe.py decode UART_LOG [--json OUT]        # table of probe results
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "probes"

# Slot order must match assets/probes/csr_probe.S.
SLOTS = [
    "hvip", "hip", "hie", "hideleg", "hedeleg", "hstatus", "hgatp", "vsatp", "mip",
    "misa", "mvendorid", "marchid", "mimpid", "mideleg", "medeleg",
    "medeleg<-~0", "mideleg<-~0", "hedeleg<-~0", "hideleg<-~0", "hvip<-~0",
    "hgeie<-~0", "hgatp<-VMID~0", "mhpmcounter3<-~0", "stvec<-~0&~3",
    "stvec<-bit50", "stvec<-bit47",
    "time", "menvcfg", "henvcfg", "mconfigptr", "mtinst", "mtval2", "htinst", "htval",
    "stimecmp", "mcountinhibit", "load [0x0]", "misaligned lw",
    "htimedelta", "hcounteren", "hcounteren<-~0", "vsstatus", "vsie", "vstvec", "vsscratch", "vsepc",
    "vscause", "vstval", "vsip", "hgeip", "senvcfg", "scounteren", "mcounteren",
]
CAUSES = {0: "insn misaligned", 1: "insn access fault", 2: "illegal instruction", 4: "load misaligned",
          5: "load access fault", 6: "store misaligned", 7: "store access fault", 22: "virtual instruction"}


def build(args: argparse.Namespace) -> int:
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    elf = out / "csr_probe.elf"
    cmd = [args.cc, "-march=rv64gc_zicsr_zifencei", "-mabi=lp64d", "-nostdlib", "-nostartfiles", "-static",
           "-x", "assembler-with-cpp", "-T", str(ASSETS / "link.ld"),
           f"-Wl,--defsym=PROBE_BASE={args.base}", f"-DPROBE_FAULT_ADDR={args.fault_addr}",
           "-o", str(elf), str(ASSETS / "csr_probe.S")]
    subprocess.run(cmd, check=True)
    digest = hashlib.sha256(elf.read_bytes()).hexdigest()
    (out / "csr_probe.elf.sha256").write_text(f"{digest}  csr_probe.elf\n", encoding="utf-8")
    print(f"built {elf} base={args.base} sha256={digest}")
    print("Run it like any ELF (send_elf.py / diag_capture.py), then: csr_probe.py decode <uart log>")
    return 0


def interpret(rows: dict[str, dict]) -> list[str]:
    """Short findings that map directly onto manifest / ACT config fields."""
    notes = []
    def val(name):
        r = rows.get(name)
        return None if r is None or r["trap"] is not None else r["value"]
    if val("hvip") not in (None, 0):
        notes.append(f"hvip is non-zero at ELF entry ({val('hvip'):#x}): clear H state in the runner before each ELF")
    for name, field in (("mvendorid", "VENDOR_ID"), ("marchid", "ARCH_ID_VALUE"), ("mimpid", "IMP_ID_VALUE")):
        if val(name) is not None:
            notes.append(f"{name} = {val(name):#x} -> UDB {field}, sail.json platform")
    for name, field in (("medeleg<-~0", "sail base.medeleg.delegatable_bits"),
                        ("mideleg<-~0", "sail base.mideleg.delegatable_bits"),
                        ("hedeleg<-~0", "H hedeleg writable mask"), ("hideleg<-~0", "H hideleg writable mask")):
        if val(name) is not None:
            notes.append(f"{name} reads {val(name):#x} -> {field}")
    if val("hgeie<-~0") is not None:
        notes.append(f"hgeie writable mask {val('hgeie<-~0'):#x} -> NUM_EXTERNAL_GUEST_INTERRUPTS = {bin(val('hgeie<-~0')).count('1')}")
    if val("hgatp<-VMID~0") is not None:
        notes.append(f"hgatp.VMID writable bits = {bin((val('hgatp<-VMID~0') >> 44) & 0x3FFF).count('1')} -> VMID_WIDTH")
    if val("mhpmcounter3<-~0") is not None:
        notes.append(f"mhpmcounter3 width = {val('mhpmcounter3<-~0').bit_length()} bits -> HPM_COUNTER*_WIDTH")
    if val("stvec<-~0&~3") is not None:
        notes.append(f"stvec all-ones reads {val('stvec<-~0&~3'):#x} (WARL legalization of BASE)")
    for name in ("time", "menvcfg", "henvcfg", "mconfigptr", "stimecmp"):
        r = rows.get(name)
        if r:
            notes.append(f"{name}: {'present' if r['trap'] is None else 'traps (' + CAUSES.get(r['trap'], str(r['trap'])) + ')'}")
    r = rows.get("load [0x0]")
    if r:
        notes.append("candidate access-fault address " + ("faults (" + CAUSES.get(r["trap"], str(r["trap"])) + ")" if r["trap"] is not None
                     else "is readable: pick another RVMODEL_ACCESS_FAULT_ADDRESS"))
    r = rows.get("misaligned lw")
    if r:
        notes.append("misaligned load " + ("traps (" + CAUSES.get(r["trap"], str(r["trap"])) + ")" if r["trap"] is not None
                     else "is handled in hardware -> MISALIGNED_LDST: true"))
    return notes


def catalog_facts(rows: dict[str, dict]) -> dict[str, object]:
    """Probe results keyed by runner-requirements IDs, for resolve_requirements.py --probe."""
    def val(name):
        r = rows.get(name)
        return None if r is None or r["trap"] is not None else r["value"]
    facts: dict[str, object] = {}
    if val("medeleg<-~0") is not None:
        facts["act.medeleg_writable"] = hex(val("medeleg<-~0"))
    if val("mideleg<-~0") is not None:
        facts["act.mideleg_writable"] = hex(val("mideleg<-~0"))
    if all(val(n) is not None for n in ("mvendorid", "marchid", "mimpid")):
        facts["act.ids"] = {n: hex(val(n)) for n in ("mvendorid", "marchid", "mimpid")}
    if val("mhpmcounter3<-~0") is not None:
        facts["act.hpm_counter_width"] = val("mhpmcounter3<-~0").bit_length()
    if val("hgatp<-VMID~0") is not None:
        facts["act.vmid_bits"] = bin((val("hgatp<-VMID~0") >> 44) & 0x3FFF).count("1")
    if rows.get("time"):
        facts["act.time_csr"] = rows["time"]["trap"] is None
    return facts


def decode(args: argparse.Namespace) -> int:
    text = args.log.read_text(encoding="latin1")
    m = re.search(r"\[SIG\] begin=0x([0-9a-fA-F]+)", text)
    if not m:
        print("no [SIG] dump in the log: did the probe run and report FAIL?", file=sys.stderr)
        return 2
    begin = int(m.group(1), 16)
    words = {int(a, 16): int(v, 16) for a, v in re.findall(r"\[SIGQ\] 0x([0-9a-fA-F]+) : 0x([0-9a-fA-F]+)", text)}
    rows: dict[str, dict] = {}
    for i, name in enumerate(SLOTS):
        value, trapped = words.get(begin + 16 * i), words.get(begin + 16 * i + 8)
        if value is None:
            print(f"{i:2} {name:18} (missing)")
            continue
        trap = None if trapped == 0 else trapped - 1
        rows[name] = {"value": value, "trap": trap}
        shown = "-" if trap is None else f"TRAP mcause={trap} ({CAUSES.get(trap, '?')})"
        print(f"{i:2} {name:18} 0x{value:016x}  {shown}")
    notes = interpret(rows)
    print("\nFindings:")
    for n in notes:
        print("  - " + n)
    if args.json:
        args.json.write_text(json.dumps({"slots": rows, "findings": notes, "facts": catalog_facts(rows)}, indent=2)
                             + "\n", encoding="utf-8")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--base", required=True, help="payload window base, e.g. 0x90000000")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--cc", default="riscv64-unknown-elf-gcc")
    b.add_argument("--fault-addr", default="0",
                   help="address the 'load [0x0]' slot reads: candidate RVMODEL_ACCESS_FAULT_ADDRESS (default 0)")
    b.set_defaults(func=build)
    d = sub.add_parser("decode")
    d.add_argument("log", type=Path)
    d.add_argument("--json", type=Path)
    d.set_defaults(func=decode)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
