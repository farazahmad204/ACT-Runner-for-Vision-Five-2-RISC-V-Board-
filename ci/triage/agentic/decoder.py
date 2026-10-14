#!/usr/bin/env python3
"""Decode RISC-V architectural state and the triage micro-probes' signature rows.

One module instead of a script per probe. Two layers:

1. decode_register(reg_name, hex_value, isa_string) -> dict
   Field-level decoding of a CSR or translation value captured from a log: mstatus/sstatus/
   vsstatus, mcause/scause/vscause, mtval/stval/vstval/htval/mtval2, medeleg/hedeleg,
   mideleg/hideleg/mie/mip/sie/sip/hvip/hie/hip/vsie/vsip, satp/vsatp/hgatp, a page-table entry
   (pte), mtvec/stvec/vstvec, misa, menvcfg/senvcfg/henvcfg. XLEN comes from the ISA string
   (RV32... / RV64...). Unknown registers return the raw value and its set bits.

2. decode_probe(log_text, probe=None) -> dict
   The micro-probes write a marker word into the signature, then one packed row per case.
   Each probe is one entry in PROBES (marker, layout, case names); every row is decoded and
   its trap cause, trap value or CSR read-back is run through decode_register. probe=None
   finds the probe by its marker.

    riscv_state_decoder.py register mstatus 0x8000000a00006080 --isa RV64GC
    riscv_state_decoder.py probe uart.log [--probe lrsc_mtval]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Architectural tables (privileged spec; codes 18/19 from the 1.13+ text)
# ---------------------------------------------------------------------------

EXCEPTIONS = {
    0: "instruction address misaligned", 1: "instruction access fault", 2: "illegal instruction",
    3: "breakpoint", 4: "load address misaligned", 5: "load access fault",
    6: "store/AMO address misaligned", 7: "store/AMO access fault", 8: "environment call from U/VU-mode",
    9: "environment call from HS/S-mode", 10: "environment call from VS-mode",
    11: "environment call from M-mode", 12: "instruction page fault", 13: "load page fault",
    15: "store/AMO page fault", 18: "software check", 19: "hardware error",
    20: "instruction guest-page fault", 21: "load guest-page fault", 22: "virtual instruction",
    23: "store/AMO guest-page fault",
}
INTERRUPTS = {
    1: "SSI (supervisor software)", 2: "VSSI (VS software)", 3: "MSI (machine software)",
    5: "STI (supervisor timer)", 6: "VSTI (VS timer)", 7: "MTI (machine timer)",
    9: "SEI (supervisor external)", 10: "VSEI (VS external)", 11: "MEI (machine external)",
    12: "SGEI (supervisor guest external)", 13: "LCOFI (counter overflow)",
}
PRIV = {0: "U", 1: "S", 2: "reserved", 3: "M"}
EXT_STATE = {0: "Off", 1: "Initial", 2: "Clean", 3: "Dirty"}
XLEN_FIELD = {1: 32, 2: 64, 3: 128}
ATP_MODES = {0: "Bare", 8: "Sv39", 9: "Sv48", 10: "Sv57", 11: "Sv64"}
HGATP_MODES = {0: "Bare", 8: "Sv39x4", 9: "Sv48x4", 10: "Sv57x4"}
VA_BITS = {"Sv39": 39, "Sv48": 48, "Sv57": 57}

# (name, low bit, width); positions for RV64, the RV32-only differences are handled below.
MSTATUS = [("SIE", 1, 1), ("MIE", 3, 1), ("SPIE", 5, 1), ("UBE", 6, 1), ("MPIE", 7, 1), ("SPP", 8, 1),
           ("VS", 9, 2), ("MPP", 11, 2), ("FS", 13, 2), ("XS", 15, 2), ("MPRV", 17, 1), ("SUM", 18, 1),
           ("MXR", 19, 1), ("TVM", 20, 1), ("TW", 21, 1), ("TSR", 22, 1), ("SPELP", 23, 1), ("SDT", 24, 1),
           ("UXL", 32, 2), ("SXL", 34, 2), ("SBE", 36, 1), ("MBE", 37, 1), ("GVA", 38, 1), ("MPV", 39, 1),
           ("MPELP", 41, 1), ("MDT", 42, 1)]
SSTATUS_FIELDS = {"SIE", "SPIE", "UBE", "SPP", "VS", "FS", "XS", "SUM", "MXR", "SPELP", "SDT", "UXL"}
MSTATUS_RV32_HIGH = {"UXL", "SXL"}  # do not exist on RV32 (mstatush holds the rest)
ENVCFG = [("FIOM", 0, 1), ("LPE", 2, 1), ("SSE", 3, 1), ("CBIE", 4, 2), ("CBCFE", 6, 1), ("CBZE", 7, 1),
          ("PMM", 32, 2), ("DTE", 59, 1), ("CDE", 60, 1), ("ADUE", 61, 1), ("PBMTE", 62, 1), ("STCE", 63, 1)]
REG_ALIASES = {"sstatus": "status", "mstatus": "status", "vsstatus": "status",
               "mcause": "cause", "scause": "cause", "vscause": "cause",
               "mtval": "tval", "stval": "tval", "vstval": "tval", "htval": "gpa", "mtval2": "gpa",
               "medeleg": "exceptions", "hedeleg": "exceptions",
               "mideleg": "interrupts", "hideleg": "interrupts", "mie": "interrupts", "mip": "interrupts",
               "sie": "interrupts", "sip": "interrupts", "hvip": "interrupts", "hie": "interrupts",
               "hip": "interrupts", "vsie": "interrupts", "vsip": "interrupts",
               "satp": "atp", "vsatp": "atp", "hgatp": "atp", "pte": "pte",
               "mtvec": "tvec", "stvec": "tvec", "vstvec": "tvec", "misa": "misa",
               "menvcfg": "envcfg", "senvcfg": "envcfg", "henvcfg": "envcfg"}


def xlen_of(isa_string: str) -> int:
    match = re.match(r"^\s*rv(32|64|128)", isa_string or "", re.I)
    return int(match.group(1)) if match else 64


def bits_set(value: int) -> list[int]:
    return [i for i in range(value.bit_length()) if value >> i & 1]


def _field(value: int, low: int, width: int) -> int:
    return value >> low & ((1 << width) - 1)


# ---------------------------------------------------------------------------
# Layer 1: one register value
# ---------------------------------------------------------------------------

class WidthError(ValueError):
    """The value does not fit the register at this XLEN."""


def decode_register(reg_name: str, hex_value: str | int, isa_string: str = "RV64",
                    xlen: int | None = None, strict: bool = False) -> dict[str, Any]:
    """Field-level decoding of one register value. Returns
    {"reg", "xlen", "raw", "fields": {...}, "notes": [...]}. XLEN is `xlen` if given, else
    taken from the ISA string; the RV32 layouts are used for 32 (e.g. mstatus.SD at bit 31,
    no UXL/SXL; mcause's interrupt bit at 31). With strict, a value wider than XLEN raises
    WidthError instead of being truncated."""
    reg = reg_name.strip().lower()
    xlen = xlen or xlen_of(isa_string)
    value = hex_value if isinstance(hex_value, int) else int(str(hex_value).strip().replace("_", ""), 16)
    if value >> xlen:
        if strict:
            raise WidthError(f"{reg} value 0x{value:x} is wider than XLEN {xlen}")
        value &= (1 << xlen) - 1
    out: dict[str, Any] = {"reg": reg, "xlen": xlen, "raw": f"0x{value:0{xlen // 4}x}", "fields": {}, "notes": []}
    kind = REG_ALIASES.get(reg)
    f, notes = out["fields"], out["notes"]

    if reg == "mstatush":
        if xlen != 32:
            notes.append("mstatush exists only on RV32; on RV64 these fields are in mstatus")
        for name, low, width in MSTATUS:
            if low >= 32:
                f[name] = _field(value, low - 32, width)
        for name in ("UXL", "SXL"):
            f.pop(name, None)
    elif kind == "status":
        for name, low, width in MSTATUS:
            if xlen == 32 and (low >= 32 or name in MSTATUS_RV32_HIGH):
                continue
            if reg != "mstatus" and name not in SSTATUS_FIELDS:
                continue
            f[name] = _field(value, low, width)
        f["SD"] = value >> (xlen - 1) & 1
        if "MPP" in f:
            f["MPP"] = f"{f['MPP']} ({PRIV[f['MPP']]})"
        f["SPP"] = f"{f['SPP']} ({'S' if f['SPP'] else 'U'})"
        for name in ("FS", "VS", "XS"):
            if name in f:
                f[name] = f"{f[name]} ({EXT_STATE[f[name]]})"
        for name in ("UXL", "SXL"):
            if name in f and f[name]:
                f[name] = f"{f[name]} (XLEN {XLEN_FIELD.get(f[name], '?')})"
        dirty = any(_field(value, low, 2) == 3 for _n, low, _w in MSTATUS if _n in ("FS", "VS", "XS"))
        if f["SD"] != int(dirty):
            notes.append(f"SD={f['SD']} but FS/VS/XS {'contain' if dirty else 'contain no'} Dirty (3)")
    elif kind == "cause":
        interrupt = value >> (xlen - 1) & 1
        code = value & ((1 << (xlen - 1)) - 1)
        f.update(interrupt=interrupt, code=code,
                 name=(INTERRUPTS if interrupt else EXCEPTIONS).get(code, "reserved/custom"))
    elif kind == "tval":
        f["value"] = f"0x{value:x}"
        if xlen == 64:
            for mode, bits in VA_BITS.items():
                upper = value >> (bits - 1)
                canonical = upper in (0, (1 << (65 - bits)) - 1)
                f[f"canonical_{mode}"] = canonical
                f[f"low{bits}"] = f"0x{value & ((1 << bits) - 1):x}"
            if not f["canonical_Sv39"]:
                notes.append("not a canonical Sv39 address: bits 63:39 differ from bit 38")
        f["byte_offset_in_word"] = value & 3
        f["byte_offset_in_doubleword"] = value & 7
    elif kind == "gpa":
        f.update(value=f"0x{value:x}", guest_physical_address=f"0x{value << 2:x}")
        notes.append("htval/mtval2 hold the guest physical address shifted right by 2")
    elif kind == "exceptions":
        f["delegated"] = {code: EXCEPTIONS.get(code, "reserved/custom") for code in bits_set(value)}
        if reg == "medeleg" and value >> 11 & 1:
            notes.append("bit 11 (ecall from M) is read-only zero in medeleg")
        if reg == "hedeleg" and value & (0x7 << 9 | 0xF << 20):
            notes.append("hedeleg bits 9-11 and 20-23 are read-only zero")
    elif kind == "interrupts":
        f["bits"] = {bit: INTERRUPTS.get(bit, "platform/custom" if bit >= 16 else "reserved")
                     for bit in bits_set(value)}
    elif kind == "atp":
        if xlen == 32:
            mode = value >> 31
            label = ("Sv32x4" if reg == "hgatp" else "Sv32") if mode else "Bare"
            ident = (value >> 22 & 0x1FF) if reg != "hgatp" else (value >> 22 & 0x7F)
            ppn = value & 0x3FFFFF
        else:
            mode = value >> 60
            label = (HGATP_MODES if reg == "hgatp" else ATP_MODES).get(mode, f"reserved ({mode})")
            ident = (value >> 44 & 0x3FFF) if reg == "hgatp" else (value >> 44 & 0xFFFF)
            ppn = value & ((1 << 44) - 1)
        f.update(MODE=label, **{"VMID" if reg == "hgatp" else "ASID": ident},
                 PPN=f"0x{ppn:x}", root_table=f"0x{ppn << 12:x}")
        if reg == "hgatp" and (ppn << 12) & 0x3FFF:
            notes.append("hgatp root table must be 16 KiB aligned (PPN[1:0] = 0)")
    elif kind == "pte":
        flags = {name: value >> bit & 1 for bit, name in enumerate("VRWXUGAD")}
        f.update(flags, RSW=_field(value, 8, 2))
        if xlen == 64:
            f.update(PPN=f"0x{_field(value, 10, 44):x}", PA=f"0x{_field(value, 10, 44) << 12:x}",
                     reserved_60_54=_field(value, 54, 7), PBMT=_field(value, 61, 2), N=value >> 63)
            if f["reserved_60_54"]:
                notes.append("reserved bits 60:54 set: page fault")
            if f["PBMT"]:
                notes.append("PBMT set: needs Svpbmt, else page fault")
            if f["N"]:
                notes.append("N set: needs Svnapot, else page fault")
        else:
            f.update(PPN=f"0x{value >> 10:x}", PA=f"0x{(value >> 10) << 12:x}")
        if not flags["V"]:
            notes.append("V=0: invalid, any access page-faults")
        elif flags["W"] and not flags["R"]:
            notes.append("W=1 with R=0 is reserved: page fault")
        elif not (flags["R"] or flags["X"]):
            notes.append("pointer to the next level")
        else:
            if not flags["A"]:
                notes.append("leaf with A=0: faults unless the hardware updates A (Svadu)")
            if flags["W"] and not flags["D"]:
                notes.append("writable leaf with D=0: stores fault unless the hardware updates D (Svadu)")
    elif kind == "tvec":
        mode = value & 3
        f.update(BASE=f"0x{value & ~3 & ((1 << xlen) - 1):x}",
                 MODE={0: "Direct", 1: "Vectored"}.get(mode, f"reserved ({mode})"))
    elif kind == "misa":
        f["MXL"] = XLEN_FIELD.get(value >> (xlen - 2), "?")
        f["extensions"] = "".join(chr(ord("A") + i) for i in range(26) if value >> i & 1)
    elif kind == "envcfg":
        for name, low, width in ENVCFG:
            if xlen == 32 and low >= 32:
                continue
            if reg == "senvcfg" and low >= 8 and name != "PMM":
                continue  # senvcfg has no STCE/PBMTE/ADUE/CDE/DTE
            f[name] = _field(value, low, width)
    else:
        f["set_bits"] = bits_set(value)
        notes.append(f"no field table for {reg}; raw bits only")
    return out


# ---------------------------------------------------------------------------
# Layer 2: micro-probe signature rows
# ---------------------------------------------------------------------------

SIGQ = re.compile(r"\[SIGQ\]\s+0x([0-9a-fA-F]+)\s+:\s+0x([0-9a-fA-F]+)")
FILLER = (0, 0xDEADBEEFDEADBEEF)


@dataclass
class Probe:
    """One micro-probe's signature format.

    layout  cause_tval      case_id[63:56] | cause[55:48] | tval[47:0], one word per case
            written_readback case_id[63:56] | written[55:32] | readback[31:0]
            records         `record` words per case, field names in `record`, case id in
                            `case_field` (top byte when case_in_top_byte)
    """
    name: str
    test: str
    marker: int
    layout: str
    cases: dict[int, str]
    window: int = 64              # words after the marker to read
    end: int | None = None        # final marker, if the probe writes one
    cause_reg: str = "mcause"
    tval_reg: str = "mtval"
    readback_reg: str | dict[int, str] = ""   # register of the read-back (per case if a dict)
    record: tuple[str, ...] = ()
    record_offset: int = 1        # first record, in words after the marker
    case_in_top_byte: bool = False
    stop_on_unknown: bool = False
    dedupe: bool = False
    expect_all: bool = False      # report missing cases
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


PROBES = {
    "amo_mtval": Probe(
        "amo_mtval", "AmoMtvalProbe-00", 0x414D4F4D545700, "cause_tval", window=11,
        cases={1: "amoadd.w @ 0x0", 2: "amoadd.w @ 0x1", 3: "amoadd.w @ 0x2", 4: "amoadd.w @ 0x4",
               5: "amoadd.w @ valid_ram+1", 6: "amoadd.w @ valid_ram+2", 7: "amoadd.w @ valid_ram+4"}),
    "lrsc_mtval": Probe(
        "lrsc_mtval", "LrscMtvalProbe-00", 0x4C5253434D545700, "cause_tval", window=9,
        cases={1: "lr.w @ 0x0", 2: "lr.d @ 0x0", 3: "lr.w @ 0x1", 4: "lr.d @ 0x1", 5: "lr.d @ 0x2",
               6: "lr.d @ 0x4", 7: "lr.d @ 0x8", 8: "lr.w @ valid_ram+1", 9: "lr.d @ valid_ram+1"}),
    # The first LR/SC probe version: full records after a header (initial mtvec at +1).
    "lrsc_mtval_full": Probe(
        "lrsc_mtval_full", "LrscMtvalProbe-00", 0x4C5253434D545601, "records", window=200,
        end=0x4C5253434D5456FF, record=("case_id", "address", "mcause", "mepc", "mtval", "mstatus"),
        record_offset=8, cases={1: "lr.w @ 0x0", 2: "lr.d @ 0x0", 3: "lr.w @ 0x1", 4: "lr.d @ 0x1",
                                5: "lr.d @ 0x2", 6: "lr.d @ 0x4", 7: "lr.d @ 0x8", 8: "lr.w @ valid_ram+1",
                                9: "lr.d @ valid_ram+1"},
        extra={"header": {"initial_mtvec": 1}}),
    "medeleg_bits": Probe(
        "medeleg_bits", "MedelegBitsProbe-00", 0x4D44454C45475400, "written_readback", end=0x4D44454C454700FF,
        readback_reg="medeleg", stop_on_unknown=True,
        cases={1: "clear medeleg[3] breakpoint", 2: "set medeleg[3] breakpoint", 3: "clear medeleg[2] illegal",
               4: "set medeleg[2] illegal", 5: "clear medeleg[2:3]", 6: "set medeleg[2:3]"}),
    "s_ebreak_mret": Probe(
        "s_ebreak_mret", "SEbreakMretProbe-00", 0x5342545241505400, "cause_tval", stop_on_unknown=True,
        cases={1: "S-mode ebreak", 2: "S-mode mret", 3: "S-mode ecall return-to-M"}),
    "s_interrupt_bits": Probe(
        "s_interrupt_bits", "SInterruptBitsProbe-00", 0x53494E5442495400, "written_readback",
        end=0x53494E54424954FF, stop_on_unknown=True,
        readback_reg={1: "sie", 2: "mie", 3: "mideleg", 4: "sie", 5: "sie", 6: "sie", 7: "sie", 8: "mie",
                      9: "sie", 10: "mideleg", 11: "mideleg", 12: "mideleg", 13: "sie", 14: "sie", 15: "sie",
                      16: "sie", 17: "sie"},
        cases={1: "M initial sie", 2: "M initial mie", 3: "M initial mideleg", 4: "M write sie=-1",
               5: "M write sie=0x2222", 6: "M write sie=0x0222", 7: "M clear sie", 8: "M write mie=-1",
               9: "M read sie after mie=-1", 10: "M set mideleg[13]", 11: "M write mideleg=0x2222",
               12: "M write mideleg=0x0222", 13: "S initial sie", 14: "S write sie=-1", 15: "S write sie=0x2222",
               16: "S write sie=0x0222", 17: "S clear sie"},
        note="tracked bits: 1=SSIP, 5=STIP, 9=SEIP, 13=platform/local"),
    "sv39_amo_priority": Probe(
        "sv39_amo_priority", "Sv39AmoPriorityProbe-00", 0x00414D4F50524900, "cause_tval", window=19,
        dedupe=True, expect_all=True,
        cases={1: "aligned, valid PTE -> RAM", 2: "misaligned, valid PTE -> RAM",
               3: "aligned, valid PTE -> inaccessible PA", 4: "misaligned, valid PTE -> inaccessible PA",
               5: "aligned, invalid PTE", 6: "misaligned, invalid PTE", 7: "repeat case 4 after sfence.vma",
               8: "repeat case 6 after sfence.vma"}),
    "sv39_canonical_stval": Probe(
        "sv39_canonical_stval", "Sv39CanonicalStvalProbe-00", 0x535456414C0000, "records", window=200,
        end=0x535456414CFFFF, record=("case_word", "input_va", "scause", "sepc", "stval", "sstatus"),
        case_in_top_byte=True, cause_reg="scause", tval_reg="stval",
        cases={1: "ACT failing VA shape", 2: "VF2 observed high-bit form", 3: "canonical low-half control",
               4: "noncanonical high bit 62", 5: "canonical negative control", 6: "noncanonical bit38=1 high-zero"}),
    "sv39_store_priority": Probe(
        "sv39_store_priority", "Sv39StorePriorityProbe-00", 0x53565052494F00, "cause_tval", window=15,
        cases={1: "valid RAM sw @ va_data+0x12", 2: "valid PTE->PA0 sw @ fault_base+0x10",
               3: "valid PTE->PA0 sw @ fault_base+0x12", 4: "invalid PTE sw @ invalid+0x10",
               5: "invalid PTE sw @ invalid+0x12"}),
}


def signature_words(log_text: str) -> list[tuple[int, int]]:
    """(address, value) pairs from [SIGQ] lines; bare 16-hex-digit lines as a fallback."""
    words = [(int(a, 16), int(v, 16)) for a, v in SIGQ.findall(log_text)]
    if not words:
        words = [(i * 8, int(line.strip(), 16)) for i, line in enumerate(log_text.splitlines())
                 if re.fullmatch(r"[0-9a-fA-F]{16}", line.strip())]
    return words


def find_probe(values: list[int]) -> Probe | None:
    present = set(values)
    return next((p for p in PROBES.values() if p.marker in present), None)


def _cause(probe: Probe, cause: int, isa: str) -> dict[str, Any]:
    decoded = decode_register(probe.cause_reg, cause, isa)["fields"]
    return {probe.cause_reg: cause, "cause_name": decoded["name"] if cause else "no trap / completed"}


def decode_probe(log_text: str, probe: str | None = None, isa_string: str = "RV64") -> dict[str, Any]:
    """Decode a micro-probe's rows. Returns {"probe", "test", "base", "rows", "missing", "notes"}."""
    words = signature_words(log_text)
    values = [v for _a, v in words]
    spec = PROBES.get(probe) if probe else find_probe(values)
    if spec is None or spec.marker not in values:
        wanted = f"0x{spec.marker:016x}" if spec else "any known probe marker"
        return {"error": f"marker {wanted} not found", "probe": probe}
    start = values.index(spec.marker)
    out: dict[str, Any] = {"probe": spec.name, "test": spec.test, "base": f"0x{words[start][0]:016x}",
                           "rows": [], "missing": [], "notes": [spec.note] if spec.note else []}
    for name, offset in (spec.extra.get("header") or {}).items():
        if start + offset < len(values):
            out[name] = f"0x{values[start + offset]:016x}"
    seen: set[int] = set()
    if spec.layout in ("cause_tval", "written_readback"):
        for value in values[start + 1:start + 1 + spec.window]:
            if spec.end is not None and value == spec.end:
                out["notes"].append("final marker seen")
                break
            if value in FILLER:
                if spec.stop_on_unknown:  # these probes end at the first non-row word
                    break
                continue
            case = value >> 56 & 0xFF
            if case not in spec.cases:
                if spec.stop_on_unknown:
                    break
                if spec.dedupe:
                    continue
            if spec.dedupe and case in seen:
                continue
            seen.add(case)
            row: dict[str, Any] = {"case": case, "name": spec.cases.get(case, f"case {case}")}
            if spec.layout == "cause_tval":
                cause, tval = value >> 48 & 0xFF, value & 0xFFFFFFFFFFFF
                row.update(_cause(spec, cause, isa_string), **{spec.tval_reg: f"0x{tval:012x}"})
                if cause:
                    row["tval_decoded"] = decode_register(spec.tval_reg, tval, isa_string)["fields"]
            else:
                written, readback = value >> 32 & 0xFFFFFF, value & 0xFFFFFFFF
                reg = spec.readback_reg.get(case, "") if isinstance(spec.readback_reg, dict) else spec.readback_reg
                row.update(register=reg, written=f"0x{written:06x}", readback=f"0x{readback:08x}")
                if reg:
                    decoded = decode_register(reg, readback, isa_string)["fields"]
                    row["readback_decoded"] = decoded.get("delegated") or decoded.get("bits") or decoded
            out["rows"].append(row)
    else:  # records
        i = start + spec.record_offset
        while i + len(spec.record) - 1 < len(values):
            head = values[i]
            if spec.end is not None and head == spec.end:
                out["notes"].append("final marker seen")
                break
            if head in FILLER:
                i += 1
                continue
            case = head >> 56 & 0xFF if spec.case_in_top_byte else head
            if spec.case_in_top_byte and case not in spec.cases:
                i += 1
                continue
            raw = dict(zip(spec.record, values[i:i + len(spec.record)]))
            row = {"case": case, "name": spec.cases.get(case, f"case {case}")}
            row.update({k: f"0x{v:016x}" for k, v in raw.items() if k not in ("case_id", "case_word",
                                                                                spec.cause_reg)})
            row.update(_cause(spec, raw[spec.cause_reg], isa_string))
            if raw.get(spec.tval_reg) is not None:
                row["tval_decoded"] = decode_register(spec.tval_reg, raw[spec.tval_reg], isa_string)["fields"]
            if "input_va" in raw:  # the canonical-stval probe compares the trap value with its input
                row["low39_match"] = (raw["input_va"] ^ raw[spec.tval_reg]) & ((1 << 39) - 1) == 0
            seen.add(case)
            out["rows"].append(row)
            i += len(spec.record)
    if spec.expect_all:
        out["missing"] = sorted(set(spec.cases) - seen)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register", help="decode one register value")
    reg.add_argument("reg_name")
    reg.add_argument("hex_value")
    reg.add_argument("--isa", default="RV64GC")
    probe = sub.add_parser("probe", help="decode a micro-probe's signature rows from a UART log")
    probe.add_argument("uart_log", type=Path)
    probe.add_argument("--probe", choices=sorted(PROBES), help="default: found by its marker")
    probe.add_argument("--isa", default="RV64GC")
    args = parser.parse_args()
    if args.command == "register":
        result = decode_register(args.reg_name, args.hex_value, args.isa)
    else:
        result = decode_probe(args.uart_log.read_text(errors="replace"), args.probe, args.isa)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result or result.get("missing") else 0


if __name__ == "__main__":
    sys.exit(main())
