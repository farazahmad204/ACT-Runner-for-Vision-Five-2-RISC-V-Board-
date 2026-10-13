#!/usr/bin/env python3
"""Generate an ACT 4.1 DUT config folder from a short board spec.

Input: an ACT board spec (see assets/board_specs/*.yaml): ISA string, privileged
version, test hart, runner payload window, CLINT/PLIC/UART addresses and CSR
facts, each optionally with evidence. Output, in --out-dir:

  <dut>.yaml               UDB architecture config (test selection, expected behavior)
  sail.json                Sail reference-model config
  link.ld                  places tests in the runner payload window
  rvmodel_macros.h         halt, console, CLINT and PLIC/UART interrupt macros
  test_config.yaml         ACT entry point (CI paths); test_config.local.yaml ($PATH tools)
  suites.yaml              ACT suites split into priv / unpriv / vector / hypervisor
  generation_report.yaml   origin of every value (spec, computed, default from a reviewed board, missing)
  README.md                values, evidence and UNVERIFIED defaults

Deterministic: no network, no model calls. Parameter definitions and defaults
come from assets/act/udb_library.yaml (scripts/build_act_library.py).
Exit 0 when complete, 2 when required inputs are missing (files still written).
"""

from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

import yaml

SKILL_DIR = Path(__file__).resolve().parent.parent
ACT_ASSETS = SKILL_DIR / "assets" / "act"

# Single-letter ISA extensions and what they imply (UDB extension names).
LETTER_IMPLIES = {
    "i": ["I"], "e": ["E"], "m": ["M", "Zmmul"], "a": ["A", "Zaamo", "Zalrsc"],
    "f": ["F", "Zicsr"], "d": ["D", "F", "Zicsr"], "q": ["Q", "D", "F"],
    "c": ["C", "Zca"], "b": ["B", "Zba", "Zbb", "Zbs"], "v": ["V"], "h": ["H"],
    "g": ["I", "M", "Zmmul", "A", "Zaamo", "Zalrsc", "F", "D", "Zicsr", "Zifencei"],
}
PRIV_VERSIONS = {"1.11": "Privileged_ISA_1_11", "1.12": "Privileged_ISA_1_12", "1.13": "Privileged_ISA_1_13"}
SAIL_LETTER_KEYS = {"M", "A", "F", "D", "B", "S", "U", "H"}
SPIKE_PLIC = 0x0C000000          # Sail simple interrupt generator / Spike PLIC base
SAIL_CLINT_SIZE = 0xC0000


class SpecError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def num(v: Any) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


def hx(v: int, width: int = 8) -> str:
    return f"0x{v:0{width}x}"


def sail_hex32(v: int) -> str:
    return f"0x{v:08X}"


def sail_hex64(v: int) -> str:
    d = f"{v:016x}"
    return "0x" + "_".join(d[i:i + 4] for i in range(0, 16, 4))


def log2_exact(v: int, what: str) -> int:
    if v <= 0 or v & (v - 1):
        raise SpecError(f"{what} must be a power of two, got {v}")
    return v.bit_length() - 1


def get(d: dict, path: str, default: Any = None) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def need(d: dict, path: str) -> Any:
    v = get(d, path)
    if v is None:
        raise SpecError(f"spec is missing required field '{path}'")
    return v


def deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# ---------------------------------------------------------------- ISA

def parse_isa(isa: str, library: dict) -> tuple[int, set[str], list[str]]:
    """Return (xlen, extension set, non-standard 'x' extensions)."""
    m = re.fullmatch(r"rv(32|64)([a-z]+)((?:_[a-z0-9]+)*)", isa.strip().lower())
    if not m:
        raise SpecError(f"cannot parse ISA string '{isa}' (expected rv64imac_zba_... form)")
    xlen, letters, multi = int(m.group(1)), m.group(2), m.group(3)
    by_lower = {name.lower(): name for name in library["extensions"]}
    exts: set[str] = set()
    nonstd: list[str] = []
    # Single letters stop at the first multi-letter prefix written without '_'.
    tokens = [t for t in multi.split("_") if t]
    for i, ch in enumerate(letters):
        if ch in "zsx" and i > 0:
            tokens.insert(0, letters[i:])
            break
        if ch not in LETTER_IMPLIES:
            raise SpecError(f"unknown single-letter extension '{ch}' in {isa}")
        exts.update(LETTER_IMPLIES[ch])
    for tok in tokens:
        if tok.startswith("x"):
            nonstd.append(tok)
            continue
        name = by_lower.get(tok)
        if not name:
            raise SpecError(f"extension '{tok}' is not known to UDB {library['udb_version']}")
        exts.add(name)
    if "C" in exts and "D" in exts:
        exts.add("Zcd")
    if "C" in exts and "F" in exts and xlen == 32:
        exts.add("Zcf")
    return xlen, exts, nonstd


# ---------------------------------------------------------------- definedBy evaluation

def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def version_ok(have: str, req: str) -> bool:
    """UDB version requirement such as '>= 1.12.0', '~> 1.0' or '= 2.0'."""
    m = re.fullmatch(r"\s*(>=|<=|>|<|=|~>)?\s*([0-9.]+)\s*", req)
    if not m:
        raise SpecError(f"unsupported version requirement '{req}'")
    op, want = m.group(1) or "=", _vtuple(m.group(2))
    got = _vtuple(have)
    n = max(len(got), len(want))
    got_p, want_p = got + (0,) * (n - len(got)), want + (0,) * (n - len(want))
    if op == "~>":   # pessimistic: same prefix up to the last given component
        return got_p >= want_p and got[:len(want) - 1] == want[:len(want) - 1]
    return {">=": got_p >= want_p, "<=": got_p <= want_p, ">": got_p > want_p,
            "<": got_p < want_p, "=": got_p == want_p}[op]


def _ext_cond(c: dict, exts: dict[str, str]) -> bool:
    if "name" in c:
        if c["name"] not in exts:
            return False
        return "version" not in c or version_ok(exts[c["name"]], c["version"])
    if "allOf" in c:
        return all(_ext_cond(x, exts) for x in c["allOf"])
    if "anyOf" in c:
        return any(_ext_cond(x, exts) for x in c["anyOf"])
    if "noneOf" in c:
        return not any(_ext_cond(x, exts) for x in c["noneOf"])
    raise SpecError(f"unsupported extension condition {c}")


def _param_cond(c: dict, params: dict) -> bool | None:
    if "allOf" in c:
        vals = [_param_cond(x, params) for x in c["allOf"]]
        return None if None in vals else all(vals)
    if "anyOf" in c:
        vals = [_param_cond(x, params) for x in c["anyOf"]]
        return True if True in vals else (None if None in vals else False)
    if c["name"] not in params:
        return None
    v = params[c["name"]]
    if "index" in c:
        v = v[c["index"]] if isinstance(v, list) and len(v) > c["index"] else None
    if "equal" in c:
        return v == c["equal"]
    if "includes" in c:
        return isinstance(v, list) and c["includes"] in v
    if "greaterThan" in c:
        return isinstance(v, int) and v > c["greaterThan"]
    raise SpecError(f"unsupported param condition {c}")


def defined(cond: dict, exts: dict[str, str], params: dict) -> bool | None:
    """True/False, or None while a parameter it depends on is still undecided."""
    if "allOf" in cond and "extension" not in cond and "param" not in cond:
        vals = [defined(x, exts, params) for x in cond["allOf"]]
        return False if False in vals else (None if None in vals else True)
    if "extension" in cond:
        return _ext_cond(cond["extension"], exts)
    if "param" in cond:
        return _param_cond(cond["param"], params)
    raise SpecError(f"unsupported definedBy {cond}")


# ---------------------------------------------------------------- spec -> model

class Generator:
    def __init__(self, spec: dict, library: dict) -> None:
        self.spec = spec
        self.lib = library
        self.origin: dict[str, str] = {}      # UDB param -> origin
        self.sail_origin: dict[str, str] = {}
        self.warnings: list[str] = []
        self.missing: list[str] = []
        self.deviations: list[str] = []

        self.dut = need(spec, "dut.name")
        self.xlen, isa_exts, self.nonstd = parse_isa(need(spec, "isa"), library)
        modes = [m.upper() for m in spec.get("privilege_modes", ["M", "S", "U"])]
        exts = set(isa_exts) | {"Sm"}
        if "S" in modes:
            exts.add("S")
        if "U" in modes:
            exts.add("U")
        for e in spec.get("extra_extensions", []):
            if e not in library["extensions"]:
                raise SpecError(f"extra extension '{e}' is not known to UDB {library['udb_version']}")
            exts.add(e)
        vm = [v.capitalize() for v in spec.get("virtual_memory", [])]
        for v in vm:
            if v not in library["extensions"]:
                raise SpecError(f"virtual memory mode '{v}' is not known to UDB")
            exts.add(v)
        if "S" in exts:
            exts.add("Svbare")
        if "Zicntr" in exts or "Zihpm" in exts:
            exts.add("Zicsr")
        self.hw_exts = exts                  # what the hardware implements (Sail)
        self.vm = vm
        self.priv = str(spec.get("priv_version", "1.12"))
        if self.priv not in PRIV_VERSIONS:
            raise SpecError(f"priv_version {self.priv} not one of {sorted(PRIV_VERSIONS)}")

        # Hypervisor: UDB/ACT model ratified H 1.0 only.
        self.udb_exts = set(exts)
        self.udb_priv = self.priv
        self.h_status = "absent"
        if "H" in exts:
            if get(spec, "hypervisor.hardware_version") is None:
                self.missing.append("hypervisor.hardware_version (the ISA has H: ratified 1.0 or a draft?)")
            hw = str(get(spec, "hypervisor.hardware_version", "1.0"))
            if hw in ("1.0", "1.0.0"):
                self.h_status = "tested"
            elif get(spec, "hypervisor.test_as_ratified"):
                self.h_status = "tested_as_ratified"
                self.deviations.append(
                    f"H is draft {hw} in hardware but declared as ratified 1.0 for UDB; tests check 1.0 behavior.")
            else:
                self.h_status = "excluded"
                self.udb_exts.discard("H")
                self.deviations.append(
                    f"H is draft {hw} in hardware: excluded from the UDB config (no H tests); "
                    "kept enabled in sail.json so misa.H matches the hardware.")
            if self.h_status != "excluded" and self.priv == "1.11":
                self.udb_priv = "1.12"
                self.deviations.append("UDB requires S/Sm >= 1.12 with H: S/Sm declared 1.12 in the UDB config "
                                       "only; sail.json keeps the hardware privileged version 1.11.")

    # -------------------------------------------------------- UDB params
    def computed_params(self) -> dict[str, Any]:
        s, e = self.spec, self.udb_exts
        p: dict[str, Any] = {"MXLEN": self.xlen}
        if "S" in e:
            p["SXLEN"] = [self.xlen]
        if "U" in e:
            p["UXLEN"] = [self.xlen]
        if "H" in e:
            p["VSXLEN"] = [self.xlen]
            p["VUXLEN"] = [self.xlen]
        p["NON_STANDARD_EXTENSION_IMPLEMENTED"] = bool(self.nonstd or get(s, "non_standard_extensions"))
        for letter in "ABCDFHMSUV":
            if letter in e:
                p[f"MUTABLE_MISA_{letter}"] = False
        if "V" not in e:
            p["MSTATUS_VS_LEGAL_VALUES"] = [0]
        if get(s, "csr.phys_addr_bits") is not None:
            p["PHYS_ADDR_WIDTH"] = num(get(s, "csr.phys_addr_bits"))
        if "S" in e and get(s, "csr.asid_bits") is not None:
            p["ASID_WIDTH"] = num(get(s, "csr.asid_bits"))
        if "H" in e and get(s, "csr.vmid_bits") is not None:
            p["VMID_WIDTH"] = num(get(s, "csr.vmid_bits"))
        pmp = get(s, "csr.pmp")
        if pmp is not None:
            usable = num(pmp["entries"])
            p["NUM_PMP_ENTRIES"] = num(pmp.get("encoding_entries", 0 if usable == 0 else (16 if usable <= 16 else 64)))
            if usable:
                p["NUM_USABLE_PMP_ENTRIES"] = usable
                p["PMP_GRANULARITY"] = log2_exact(num(pmp["granularity_bytes"]), "csr.pmp.granularity_bytes")
                p["PMP_NA4_SUPPORTED"] = bool(pmp.get("na4", False))
                p["PMP_TOR_SUPPORTED"] = bool(pmp.get("tor", True))
                p["PMP_NAPOT_SUPPORTED"] = bool(pmp.get("napot", True))
        hpm = get(s, "csr.hpm_counters")
        if hpm is not None:
            hpm = [num(x) for x in hpm]
            bits = [i in hpm for i in range(32)]
            p["HPM_COUNTER_EN"] = bits
            p["MCOUNTENABLE_EN"] = list(bits)
            if "S" in e:
                p["SCOUNTENABLE_EN"] = list(bits)
            inhibit = get(s, "csr.hpm_inhibit")
            if inhibit is None:
                inhibit = True
                self.warnings.append("csr.hpm_inhibit not supplied: assuming mcountinhibit can stop the HPM "
                                     "counters (UNVERIFIED)")
            p["COUNTINHIBIT_EN"] = [True, False, True] + [b and bool(inhibit) for b in bits[3:]]
            width = get(s, "csr.hpm_counter_width")
            for i in hpm:
                if width is not None:
                    p[f"HPM_COUNTER{i}_WIDTH"] = num(width)
        for csr, key in (("mtvec", "MTVEC"), ("stvec", "STVEC")):
            t = get(s, f"csr.{csr}")
            if t is None or (csr == "stvec" and "S" not in e):
                continue
            p[f"{key}_MODES"] = [num(x) for x in t["modes"]]
            if 1 in p[f"{key}_MODES"]:
                p[f"{key}_BASE_ALIGNMENT_VECTORED"] = num(t["vectored_alignment"])
        ids = get(s, "platform.ids") or {}
        if "mvendorid" in ids:
            v = num(ids["mvendorid"])
            p["VENDOR_ID_BANK"], p["VENDOR_ID_OFFSET"] = v >> 7, v & 0x7F
        if "marchid" in ids:
            p["MARCHID_IMPLEMENTED"] = num(ids["marchid"]) != 0
            if p["MARCHID_IMPLEMENTED"]:
                p["ARCH_ID_VALUE"] = num(ids["marchid"])
        if "mimpid" in ids:
            p["MIMPID_IMPLEMENTED"] = num(ids["mimpid"]) != 0
            if p["MIMPID_IMPLEMENTED"]:
                p["IMP_ID_VALUE"] = num(ids["mimpid"])
        if get(s, "csr.time_csr_implemented") is not None:
            p["TIME_CSR_IMPLEMENTED"] = bool(get(s, "csr.time_csr_implemented"))
        if any(x in e for x in ("Zicbom", "Zicbop", "Zicboz")) and get(s, "platform.cache_block_bytes"):
            p["CACHE_BLOCK_SIZE"] = num(get(s, "platform.cache_block_bytes"))
        if "V" in e and get(s, "vector.vlen"):
            p["VLEN"] = num(get(s, "vector.vlen"))
            p["ELEN"] = num(get(s, "vector.elen", 64))
        if "H" in e:
            for mode in ("Sv39", "Sv48", "Sv57"):
                p[f"{mode.upper()}X4_TRANSLATION"] = mode in self.vm
                p[f"{mode.upper()}_VSMODE_TRANSLATION"] = mode in self.vm
        return p

    def udb_params(self) -> dict[str, Any]:
        library = self.lib["params"]
        values: dict[str, Any] = {}
        for k, v in self.computed_params().items():
            if k in library:
                values[k], self.origin[k] = v, "computed from spec"
        for k, v in (self.spec.get("params") or {}).items():
            if k not in library:
                raise SpecError(f"params.{k} is not a UDB {self.lib['udb_version']} parameter")
            values[k], self.origin[k] = v, "spec override"
        versions = {e["name"]: e["version"].lstrip("= ") for e in self.udb_extensions()}
        # Add defaults and drop undefined params until stable.
        for _ in range(8):
            changed = False
            for name, entry in library.items():
                ok = defined(entry["defined_by"], versions, values)
                if ok is False and name in values:
                    if self.origin[name] == "spec override":
                        self.warnings.append(f"params.{name} does not apply to this configuration; dropped")
                    del values[name]
                    self.origin.pop(name)
                    changed = True
                elif ok and name not in values and "default" in entry:
                    values[name] = entry["default"]
                    self.origin[name] = f"default from {entry['default_from']} (UNVERIFIED)"
                    changed = True
            if not changed:
                break
        self.missing += sorted(f"UDB param {n}" for n, entry in library.items()
                               if n not in values and defined(entry["defined_by"], versions, values))
        return dict(sorted(values.items()))

    def udb_extensions(self) -> list[dict[str, str]]:
        out = []
        for name in sorted(self.udb_exts, key=ext_sort_key):
            if name in ("S", "Sm"):
                version = f"{self.udb_priv}.0"
            else:
                info = self.lib["extensions"][name]
                ratified = info["ratified"] or info["versions"]
                version = ratified[-1]
            out.append({"name": name, "version": f"= {version}"})
        return out

    # -------------------------------------------------------- Sail
    def sail(self) -> dict:
        s, e = self.spec, self.hw_exts
        cfg = json.loads((ACT_ASSETS / "sail_base.json").read_text())
        cfg.pop("_comment", None)
        b, mem, plat, ext = cfg["base"], cfg["memory"], cfg["platform"], cfg["extensions"]
        b["xlen"] = self.xlen
        b["privileged_isa_version"] = PRIV_VERSIONS[self.priv]
        hpm = [num(x) for x in (get(s, "csr.hpm_counters") or [])]
        hpm_mask = sum(1 << i for i in hpm)
        b["writable_hpm_counters"]["value"] = sail_hex32(hpm_mask)
        for key in ("mcounteren_writable_bits", "scounteren_writable_bits") + (
                ("hcounteren_writable_bits",) if "H" in e else ()):
            b[key]["value"] = sail_hex32(0x7 | hpm_mask)
        for csr in ("mtvec", "stvec"):
            align = get(s, f"csr.{csr}.vectored_alignment")
            if align is not None:
                b[csr]["vectored"]["base_alignment"] = log2_exact(num(align), f"csr.{csr}.vectored_alignment")
        medeleg = get(s, "csr.medeleg_writable")
        if medeleg is None:
            self.missing.append("csr.medeleg_writable (Sail medeleg delegatable bits)")
        else:
            b["medeleg"]["delegatable_bits"]["value"] = sail_hex64(num(medeleg))
        mideleg = get(s, "csr.mideleg_writable")
        if mideleg is None:
            mideleg = 0x222 | (0x2000 if "Sscofpmf" in e else 0)
            self.sail_origin["base.mideleg"] = "computed: SSI/STI/SEI (+LCOFI with Sscofpmf)"
        mideleg = num(mideleg)
        if "H" in e and mideleg & 0x1444:
            # Sail's H model owns the VS (2, 6, 10) and SGEI (12) bits and rejects them in this mask.
            self.sail_origin["base.mideleg"] = f"{mideleg:#x} without VS/SGEI bits 0x1444 (Sail H model sets them)"
            mideleg &= ~0x1444
        b["mideleg"]["delegatable_bits"]["value"] = sail_hex64(mideleg)
        b["mstatus"]["vs_legal_states"] = "ExtContext_FourState" if "V" in e else "ExtContext_Off"
        mem["physaddr_bits"] = num(need(s, "csr.phys_addr_bits"))
        mem["asidlen"] = num(get(s, "csr.asid_bits", 0))
        if get(s, "csr.vmid_bits") is not None:
            mem["vmidlen"] = num(get(s, "csr.vmid_bits"))
        pmp = need(s, "csr.pmp")
        usable = num(pmp["entries"])
        mem["pmp"].update({
            "count": num(pmp.get("encoding_entries", 16 if usable <= 16 else 64)),
            "usable_count": usable,
            "grain": log2_exact(num(pmp.get("granularity_bytes", 4)), "csr.pmp.granularity_bytes") - 2,
            "na4_supported": bool(pmp.get("na4", False)),
            "tor_supported": bool(pmp.get("tor", True)),
            "napot_supported": bool(pmp.get("napot", True)),
        })
        base, size = num(need(s, "memory.base")), num(need(s, "memory.size"))
        ram = mem["regions"][-1]
        ram["base"]["value"], ram["size"]["value"] = hex(base), hex(size)
        clint = num(need(s, "platform.clint.base"))
        io = mem["regions"][2]
        io_lo = min(clint, num(io["base"]["value"]))
        io_hi = max(clint + SAIL_CLINT_SIZE, num(io["base"]["value"]) + num(io["size"]["value"]))
        io["base"]["value"], io["size"]["value"] = hex(io_lo), hex(io_hi - io_lo)
        if io_lo < base + size and base < io_hi:
            raise SpecError("memory window overlaps the Sail CLINT/interrupt-generator IO region")
        ids = get(s, "platform.ids") or {}
        for key, field in (("mvendorid", "vendorid"), ("marchid", "archid"), ("mimpid", "impid")):
            if key in ids:
                plat[field] = num(ids[key])
            else:
                plat[field] = 0
                self.sail_origin[f"platform.{field}"] = "0 (id not supplied)"
        if get(s, "platform.cache_block_bytes"):
            plat["cache_block_size_exp"] = log2_exact(num(get(s, "platform.cache_block_bytes")),
                                                      "platform.cache_block_bytes")
        plat["clint"]["base"] = clint
        for name, node in ext.items():
            if name == "V":
                if "V" in e:
                    node["support_level"] = "Full"
                    node["vlen_exp"] = log2_exact(num(need(s, "vector.vlen")), "vector.vlen")
                    node["elen_exp"] = log2_exact(num(get(s, "vector.elen", 64)), "vector.elen")
                continue
            if name == "Stateen":
                for sub in ("Smstateen", "Ssstateen"):
                    node[sub]["supported"] = sub in e
                continue
            if isinstance(node, dict) and "supported" in node:
                node["supported"] = name in e
        if "H" in e:
            for mode in ("Sv39", "Sv48", "Sv57"):
                ext["H"]["hgatp_modes"][f"{mode}x4"] = mode in self.vm
                ext["H"]["vsatp_modes"][mode] = mode in self.vm
            ext["H"]["hgatp_modes"]["Sv32x4"] = False
            ext["H"]["vsatp_modes"]["Sv32"] = False
            geilen = get(s, "hypervisor.geilen")
            if geilen is None:
                self.warnings.append(f"hypervisor.geilen not supplied: Sail keeps {ext['H']['geilen']} (UNVERIFIED)")
            else:
                ext["H"]["geilen"] = num(geilen)
        known = set(ext) | {"Smstateen", "Ssstateen", "C", "I", "Sm", "Zcd", "Zca"}
        ignored = sorted(x for x in e if x not in known)
        if ignored:
            self.warnings.append("Sail 0.14.1 has no switch for: " + ", ".join(ignored) + " (UDB-only)")
        deep_merge(cfg, self.spec.get("sail") or {})
        for k in (self.spec.get("sail") or {}):
            self.sail_origin[k] = "spec override"
        return cfg

    # -------------------------------------------------------- macros / link
    def macros(self) -> str:
        s = self.spec
        hart = num(need(s, "test_hart"))
        clint = num(need(s, "platform.clint.base"))
        plic = num(need(s, "platform.plic.base"))
        nsrc = num(need(s, "platform.plic.sources"))
        ctx_m = num(need(s, "platform.plic.test_hart_contexts.m"))
        ctx_s = num(need(s, "platform.plic.test_hart_contexts.s"))
        uart = num(need(s, "platform.uart.base"))
        shift = num(need(s, "platform.uart.reg_shift"))
        src = num(need(s, "platform.uart.plic_source"))
        width = num(get(s, "platform.uart.access_bytes", 4))
        load, store = {1: ("lbu", "sb"), 2: ("lhu", "sh"), 4: ("lw", "sw")}[width]
        cpu_hz = num(need(s, "platform.cpu_hz_max"))
        mtime_hz = num(need(s, "platform.mtime_hz"))
        cycles = math.ceil(cpu_hz / mtime_hz)
        word, bit = src // 32, src % 32
        if bit >= 11:
            # andi/ori immediates are 12-bit signed; the template uses them with BOARD_UART_BIT.
            raise SpecError(f"UART PLIC source {src} is bit {bit} of its word: the ori/andi templates need "
                            "bit < 11; extend rvmodel_macros.h.in to load the mask with li")
        defines = [
            f"// {get(s, 'dut.description', self.dut).strip()}",
            f"// The UART runner dispatches ACT payloads on hart {hart}; Sail/Spike run on hart 0.",
            f"// UART @{hx(uart)} reg-shift {shift}, {width}-byte access, PLIC source {src};",
            f"// PLIC @{hx(plic)}, {nsrc} sources, test hart contexts M={ctx_m}, S={ctx_s}.",
            "// Generated by gen_act_config.py from the board's ACT spec.",
            f"#define BOARD_UART_IER        {hx(uart + (1 << shift))}",
            f"#define BOARD_PLIC_PRIO_UART  {hx(plic + 4 * src)}  /* priority, source {src} */",
            f"#define BOARD_PLIC_PEND_UART  {hx(plic + 0x1000 + 4 * word)}  /* pending word {word} */",
            f"#define BOARD_PLIC_EN_M_BASE  {hx(plic + 0x2000 + 0x80 * ctx_m)}  /* ctx {ctx_m} enables */",
            f"#define BOARD_PLIC_EN_S_BASE  {hx(plic + 0x2000 + 0x80 * ctx_s)}  /* ctx {ctx_s} enables */",
            f"#define BOARD_PLIC_EN_M_UART  {hx(plic + 0x2000 + 0x80 * ctx_m + 4 * word)}",
            f"#define BOARD_PLIC_EN_S_UART  {hx(plic + 0x2000 + 0x80 * ctx_s + 4 * word)}",
            f"#define BOARD_PLIC_EN_WORDS   {(nsrc + 1 + 31) // 32}",
            f"#define BOARD_PLIC_THR_M      {hx(plic + 0x200000 + 0x1000 * ctx_m)}",
            f"#define BOARD_PLIC_CLAIM_M    {hx(plic + 0x200004 + 0x1000 * ctx_m)}",
            f"#define BOARD_PLIC_THR_S      {hx(plic + 0x200000 + 0x1000 * ctx_s)}",
            f"#define BOARD_PLIC_CLAIM_S    {hx(plic + 0x200004 + 0x1000 * ctx_s)}",
            f"#define BOARD_UART_BIT        {hx(1 << bit, 2)}  /* source {src} = bit {bit} of word {word} */",
            "",
        ]
        fault = get(s, "platform.access_fault_address")
        if fault is None:
            fault = 0
            self.warnings.append("platform.access_fault_address not supplied: using 0x0 (UNVERIFIED)")
        subs = {
            "BOARD_DEFINES": "\n".join(defines), "CLINT_BASE": hx(clint), "UART_BASE": hx(uart),
            "UART_REG_SHIFT": str(shift), "UART_LOAD": load, "UART_STORE": store,
            "ACCESS_FAULT_ADDRESS": hx(num(fault)), "TEST_HART": str(hart),
            "MTIMECMP": hx(clint + 0x4000 + 8 * hart), "MTIME": hx(clint + 0xBFF8).upper().replace("0X", "0x"),
            "CPU_HZ": str(cpu_hz), "MTIME_HZ": str(mtime_hz), "CYCLES_PER_TICK": str(cycles),
            "MAX_CYCLES_PER_TICK": str(1 << (cycles - 1).bit_length()),
        }
        return render((ACT_ASSETS / "rvmodel_macros.h.in").read_text(), subs)

    def linker(self) -> str:
        base, size = num(need(self.spec, "memory.base")), num(need(self.spec, "memory.size"))
        return render((ACT_ASSETS / "link.ld.in").read_text(), {
            "DUT_NAME": self.dut, "RAM_ORIGIN": hx(base), "RAM_LENGTH": hx(size), "RAM_END": hx(base + size)})


def ext_sort_key(name: str) -> tuple[int, str]:
    order = "IEMAFDQCBVHUS"
    if len(name) == 1:
        return (0, f"{order.index(name):02d}" if name in order else name)
    rank = {"Sm": 1, "Z": 2, "S": 3, "X": 4}
    return (rank.get(name if name == "Sm" else name[0], 5), name)


def render(template: str, subs: dict[str, str]) -> str:
    out = re.sub(r"\{\{(\w+)\}\}", lambda m: subs[m.group(1)], template)
    left = re.findall(r"\{\{\w+\}\}", out)
    if left:
        raise SpecError(f"unrendered template keys: {left}")
    return out


# ---------------------------------------------------------------- suites

SUITE_PSEUDO = {"Exceptions", "Interrupts", "Misalign", "Ssstrict", "Gei", "Common"}
SUITE_ALIASES = {"Zicbo": {"Zicbom", "Zicbop", "Zicboz"}, "Zc": {"Zca"}, "Sv": {"Sv39", "Sv48", "Sv57", "Sv32"},
                 "PMP": {"PMP"}, "Vf": {"V"}, "Vls": {"V"}, "Vx": {"V"}, "Vfmin": {"V"}, "Zbc64": {"Zbc"},
                 "Zvbc64": {"Zvbc"}}


def suite_tokens(name: str) -> list[str]:
    return re.findall(r"PMP|[A-Z][a-z0-9]*", name)


def discover_suites(act_root: Path) -> dict[str, str]:
    """ACT suite name -> source kind (unpriv/priv)."""
    suites: dict[str, str] = {}
    for f in (act_root / "testplans").glob("*.csv"):
        suites[f.stem] = "unpriv"
    for f in (act_root / "testplans" / "priv").glob("*.csv"):
        suites[f.stem] = "priv"
    gen = act_root / "generators" / "testgen" / "src" / "testgen" / "priv" / "extensions"
    for f in gen.glob("*.py"):
        if not f.stem.endswith("Common") and f.stem not in ("__init__", "PrivCommon"):
            suites[f.stem] = "priv"
    for d in (act_root / "tests" / "priv").glob("*"):
        if d.is_dir():
            suites[d.name] = "priv"
    return suites


def plan_suites(act_root: Path, exts: set[str], has_pmp: bool, h_status: str) -> dict:
    have = set(exts) | ({"PMP"} if has_pmp else set())
    plan: dict[str, list[str]] = {"priv": [], "unpriv": [], "vector": [], "hypervisor": []}
    skipped: dict[str, list[str]] = {}
    for name, kind in sorted(discover_suites(act_root).items()):
        toks = suite_tokens(name)
        if "".join(toks) != name:
            skipped.setdefault("unparsed", []).append(name)
            continue
        missing = [t for t in toks if t not in SUITE_PSEUDO
                   and not (SUITE_ALIASES.get(t, {t}) & have)]
        if missing:
            skipped.setdefault("not implemented", []).append(f"{name} (needs {', '.join(missing)})")
            continue
        if "H" in toks or name.startswith("Sh"):
            plan["hypervisor"].append(name)
        elif any(t == "V" or t in ("Vf", "Vls", "Vx", "Vfmin") or t.startswith("Zv") for t in toks):
            plan["vector"].append(name)
        else:
            plan[kind].append(name)
    result: dict[str, Any] = {
        "act_root": str(act_root),
        "note": "Candidate suites by name; ACT's own UDB-based selection is final.",
        "suites": plan,
        "commands": {k: f"uv run testgen testplans --extensions {','.join(v)}  # then act ... -e <same>"
                     for k, v in plan.items() if v},
        "skipped": skipped,
    }
    if h_status == "excluded":
        result["hypervisor_blocked"] = "H is a draft version in hardware and excluded from the UDB config"
    return result


# ---------------------------------------------------------------- output

def readme(g: Generator, udb: dict, suites: dict | None) -> str:
    s = g.spec
    ev = s.get("evidence") or {}
    lines = [f"# {g.dut} ACT configuration", "",
             f"Generated by `gen_act_config.py` from the board's ACT spec. Do not edit by hand: change the",
             f"spec and regenerate. ISA `{s['isa']}`, privileged {g.priv}, test hart {s['test_hart']},",
             f"payload window `{hx(num(s['memory']['base']))}`+`{hx(num(s['memory']['size']))}`.", ""]
    if g.deviations:
        lines += ["## Deviations", ""] + [f"- {d}" for d in g.deviations] + [""]
    if ev:
        lines += ["## Evidence (from the spec)", "", "| Item | Status | Source |", "|---|---|---|"]
        for k, v in ev.items():
            lines.append(f"| `{k}` | {v.get('status', '')} | {v.get('source', '')} |")
        lines.append("")
    defaults = sorted(k for k, o in g.origin.items() if o.startswith("default"))
    if defaults:
        lines += ["## UNVERIFIED defaults", "",
                  "Taken from a reviewed board config; confirm on hardware (csr_probe.py) or in documents and",
                  "move the confirmed value into the spec:", ""]
        lines += [f"- `{k}` = `{json.dumps(udb[k])}` ({g.origin[k].split(' (')[0]})" for k in defaults]
        lines.append("")
    if g.missing:
        lines += ["## Missing inputs (config incomplete)", ""] + [f"- {m}" for m in g.missing] + [""]
    if g.warnings:
        lines += ["## Warnings", ""] + [f"- {w}" for w in g.warnings] + [""]
    if suites:
        lines += ["## Test suites", ""]
        for k, v in suites["suites"].items():
            lines.append(f"- **{k}** ({len(v)}): {', '.join(v) if v else 'none'}")
        if suites.get("hypervisor_blocked"):
            lines.append(f"- hypervisor blocked: {suites['hypervisor_blocked']}")
        lines.append("")
    return "\n".join(lines)


def write_udb(path: Path, g: Generator, exts: list[dict], params: dict) -> None:
    head = ["# yaml-language-server: $schema=https://raw.githubusercontent.com/riscv/riscv-unified-db/main/spec/schemas/config_schema.json",
            "# Generated by gen_act_config.py; origins in generation_report.yaml.", "---",
            "$schema: config_schema.json#", "kind: architecture configuration", "type: fully configured",
            f"name: {g.dut}",
            "description: " + json.dumps(get(g.spec, "dut.description", g.dut).strip()),
            "implemented_extensions:"]
    head += [f"  - {{ name: {e['name']}, version: \"{e['version']}\" }}" for e in exts]
    body = yaml.safe_dump({"params": params}, sort_keys=False, width=200, default_flow_style=None)
    path.write_text("\n".join(head) + "\n\n" + body)


def generate(spec: dict, library: dict, out_dir: Path, act_root: Path | None) -> int:
    g = Generator(spec, library)
    params = g.udb_params()
    exts = g.udb_extensions()
    sail = g.sail()
    macros, link = g.macros(), g.linker()
    suites = None
    if act_root:
        has_pmp = num(get(spec, "csr.pmp.entries", 0)) > 0
        suites = plan_suites(act_root, g.udb_exts, has_pmp, g.h_status)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_udb(out_dir / f"{g.dut}.yaml", g, exts, params)
    (out_dir / "sail.json").write_text(json.dumps(sail, indent=2) + "\n")
    (out_dir / "link.ld").write_text(link)
    (out_dir / "rvmodel_macros.h").write_text(macros)
    tc = {"name": g.dut, "compiler_exe": "riscv64-unknown-elf-gcc", "objdump_exe": "riscv64-unknown-elf-objdump",
          "ref_model_exe": spec.get("sail_exe", "sail_riscv_sim"),
          "udb_config": f"{g.dut}.yaml", "linker_script": "link.ld", "dut_include_dir": "."}
    (out_dir / "test_config.yaml").write_text(yaml.safe_dump(tc, sort_keys=False))
    (out_dir / "test_config.local.yaml").write_text(yaml.safe_dump({**tc, "ref_model_exe": "sail_riscv_sim"},
                                                                   sort_keys=False))
    if suites:
        (out_dir / "suites.yaml").write_text(yaml.safe_dump(suites, sort_keys=False, width=200))
    report = {"dut": g.dut, "udb_version": library["udb_version"], "hypervisor": g.h_status,
              "hardware_extensions": sorted(g.hw_exts), "udb_extensions": [e["name"] for e in exts],
              "non_standard": g.nonstd, "deviations": g.deviations, "warnings": g.warnings,
              "missing": g.missing, "udb_param_origin": dict(sorted(g.origin.items())),
              "sail_origin": g.sail_origin}
    (out_dir / "generation_report.yaml").write_text(yaml.safe_dump(report, sort_keys=False, width=200))
    (out_dir / "README.md").write_text(readme(g, params, suites))
    n_default = sum(o.startswith("default") for o in g.origin.values())
    print(f"{out_dir}: {len(exts)} UDB extensions, {len(params)} params "
          f"({n_default} UNVERIFIED defaults), hypervisor={g.h_status}, "
          f"missing={len(g.missing)}, warnings={len(g.warnings)}")
    for m in g.missing:
        print(f"  missing: {m}")
    return 2 if g.missing else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("spec", type=Path, help="ACT board spec YAML")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--act-root", type=Path, help="ACT checkout, to plan test suites")
    p.add_argument("--library", type=Path, default=ACT_ASSETS / "udb_library.yaml")
    args = p.parse_args()
    try:
        spec = yaml.safe_load(args.spec.read_text())
        library = yaml.safe_load(args.library.read_text())
        return generate(spec, library, args.out_dir, args.act_root)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
