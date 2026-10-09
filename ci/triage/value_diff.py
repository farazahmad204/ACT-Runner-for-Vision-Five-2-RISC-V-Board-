"""Explain an ACT value mismatch from the bits that differ.

Most ACT failures on real boards are one value compared against Sail's
expectation: a CSR read back in a CSR walk, or a trap-signature word (xIP,
xSTATUS, ...). This module:

  1. finds the CSR (trap-signature field label, encoded CSR instruction, or the
     coverage bin name),
  2. computes the differing bits and maps them to architectural fields with
     knowledge/csr_fields.json (built from UDB, including field aliases),
  3. recognizes the mechanism (address width, sign-extension legalization,
     writable-mask difference, extra pending/status state),
  4. checks the board's ACT config (sail.json, UDB yaml, generation_report.yaml)
     for the key that controls it and whether its current value was verified.

Deterministic: no model calls. Callers decide closure through the verifier.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CSR_TABLE = Path(__file__).resolve().parent / "knowledge" / "csr_fields.json"

# Trap-signature labels printed by RVCP -> CSR per trap-handler mode.
SIG_LABELS = {
    "XIP": {"M": "mip", "S": "sip", "VS": "vsip"},
    "XIE": {"M": "mie", "S": "sie", "VS": "vsie"},
    "XSTATUS": {"M": "mstatus", "S": "sstatus", "VS": "vsstatus"},
    "XCAUSE": {"M": "mcause", "S": "scause", "VS": "vscause"},
    "XTVAL": {"M": "mtval", "S": "stval", "VS": "vstval"},
    "XEPC": {"M": "mepc", "S": "sepc", "VS": "vsepc"},
}
# CSRs whose readback after a write is a writable-mask question -> config keys.
MASK_KEYS = {
    "medeleg": {"sail": "base.medeleg.delegatable_bits", "spec": "csr.medeleg_writable"},
    "mideleg": {"sail": "base.mideleg.delegatable_bits", "spec": "csr.mideleg_writable"},
    "mcounteren": {"sail": "base.mcounteren_writable_bits", "udb": "MCOUNTENABLE_EN", "spec": "csr.hpm_counters"},
    "scounteren": {"sail": "base.scounteren_writable_bits", "udb": "SCOUNTENABLE_EN", "spec": "csr.hpm_counters"},
    "hcounteren": {"sail": "base.hcounteren_writable_bits"},
    "mcountinhibit": {"udb": "COUNTINHIBIT_EN", "spec": "csr.hpm_inhibit"},
}
ADDRESS_CSRS = {"mepc", "sepc", "vsepc", "mtval", "stval", "vstval", "mtvec", "stvec", "vstvec", "satp", "vsatp",
                "hgatp", "mscratch", "sscratch"}
PENDING_STATUS = {"mip", "sip", "vsip", "hip", "hvip", "mie", "sie", "mstatus", "sstatus", "vsstatus", "hstatus"}


@lru_cache(maxsize=1)
def csr_table() -> dict[str, Any]:
    return json.loads(CSR_TABLE.read_text())["csrs"]


@lru_cache(maxsize=1)
def csr_by_address() -> dict[int, str]:
    out: dict[int, str] = {}
    for name, d in csr_table().items():
        out.setdefault(d["address"], name)
    return out


# ------------------------------------------------------------------ parsing

def parse_rvcp(text: str) -> dict[str, Any]:
    """RVCP failure block of the current UART runner (CSR-walk and trap-diagnostics forms)."""
    def grab(pattern: str) -> str | None:
        m = re.search(pattern, text, re.M)
        return m.group(1).strip() if m else None
    out = {
        "test_info": grab(r'RVCP: Test Info: "([^"]*)"'),
        "instruction": grab(r"RVCP: Instruction: (0x[0-9a-fA-F]+)"),
        "approx_address": grab(r"RVCP: Approximate address[^:]*: (0x[0-9a-fA-F]+)"),
        "register": grab(r"RVCP: Register: (\S+)"),
        "actual": grab(r"RVCP: (?:Bad Value|Actual value):\s+(0x[0-9a-fA-F]+)"),
        "expected": grab(r"RVCP: Expected [Vv]alue:\s+(0x[0-9a-fA-F]+)"),
        "failure": grab(r'RVCP: Failure: "([^"]*)"'),
        "handler_mode": grab(r"RVCP: Trap handler mode: (\S+)"),
        "mismatching_field": grab(r"RVCP: Mismatching field: (.+)$"),
        # Current RVCP prints XEPC/XCAUSE/...; older builds printed MEPC/MCAUSE/...
        "xepc": grab(r"RVCP: [XM]EPC:\s+(0x[0-9a-fA-F]+)"),
        "xcause": grab(r"RVCP: [XM]CAUSE:\s+(0x[0-9a-fA-F]+)"),
        "xtval": grab(r"RVCP: [XM]TVAL:\s+(0x[0-9a-fA-F]+)"),
        "xstatus": grab(r"RVCP: [XM]STATUS:\s+(0x[0-9a-fA-F]+)"),
    }
    if out["handler_mode"]:
        out["handler_mode"] = out["handler_mode"].split("-")[0].upper()
    m = re.search(r"\bbin: (\S+)", out["test_info"] or "")
    out["bin"] = m.group(1) if m else None
    m = re.search(r"\bcp: (\S+)", out["test_info"] or "")
    out["coverpoint"] = m.group(1).rstrip(";") if m else None
    return out


def identify_csr(rv: dict[str, Any]) -> tuple[str | None, str]:
    field = rv.get("mismatching_field") or ""
    label = field.split()[0] if field else ""
    if label in SIG_LABELS:
        return SIG_LABELS[label].get(rv.get("handler_mode") or "M"), f"trap signature field {label}"
    insn = int(rv["instruction"], 16) if rv.get("instruction") else None
    if insn is not None and insn & 0x7F == 0x73 and (insn >> 12) & 7 not in (0, 4):
        name = csr_by_address().get(insn >> 20)
        if name:
            return name, f"CSR instruction {insn:#010x} (csr {insn >> 20:#x})"
    for token in (rv.get("bin"), rv.get("coverpoint")):
        if not token:
            continue
        best = max((n for n in csr_table() if token.lower().startswith(n)), key=len, default=None)
        if best:
            return best, f"coverage bin {token}"
    return None, "not identified"


# ------------------------------------------------------------------ bit analysis

def bit_list(mask: int) -> list[int]:
    return [i for i in range(64) if mask >> i & 1]


def fields_for(csr: str, mask: int) -> list[dict[str, Any]]:
    out = []
    for name, f in (csr_table().get(csr, {}).get("fields") or {}).items():
        lo, hi = f["bits"]
        width_mask = ((1 << (hi - lo + 1)) - 1) << lo
        if mask & width_mask:
            out.append({"field": name, "bits": [lo, hi], "type": f["type"], "extensions": f["extensions"],
                        "alias": f["alias"], "aliases_all": alias_chain(csr, name)})
    return out


def alias_chain(csr: str, field: str) -> list[str]:
    """Follow UDB aliases transitively (mip.VSEIP -> hip.VSEIP -> hvip.VSEIP ...)."""
    start = f"{csr}.{field}"
    seen, todo = {start}, [start]
    while todo:
        c, f = todo.pop().split(".", 1)
        for a in ((csr_table().get(c, {}).get("fields") or {}).get(f) or {}).get("alias") or []:
            if "." in a and a not in seen:
                seen.add(a)
                todo.append(a)
    seen.discard(start)
    return sorted(seen)


def sign_extended_from(expected: int, actual: int) -> int | None:
    """Bit k if actual == the low k+1 bits of expected sign-extended from bit k (hardware keeps k+1 bits)."""
    if expected == actual:
        return None
    for k in range(62, 11, -1):
        low = expected & ((1 << (k + 1)) - 1)
        ext = low | ((((1 << 64) - 1) ^ ((1 << (k + 1)) - 1)) if low >> k & 1 else 0)
        if ext == actual:
            return k
    return None


def truncated_to(expected: int, actual: int) -> tuple[int, int] | None:
    """(min, max) widths w with actual == expected & ((1 << w) - 1): hardware keeps only low bits."""
    # A read of 0 matches "keeps the low w bits" for any expected value whose low bits are 0,
    # so it proves nothing about width (e.g. a pending bit that never got set).
    if expected == actual or actual == 0:
        return None
    ws = [w for w in range(1, 64) if expected & ((1 << w) - 1) == actual]
    return (min(ws), max(ws)) if ws else None


CAUSES = {0: "instruction address misaligned", 1: "instruction access fault", 2: "illegal instruction",
          3: "breakpoint", 4: "load address misaligned", 5: "load access fault",
          6: "store/AMO address misaligned", 7: "store/AMO access fault", 8: "ecall from U", 9: "ecall from S",
          10: "ecall from VS", 11: "ecall from M", 12: "instruction page fault", 13: "load page fault",
          15: "store/AMO page fault", 20: "instruction guest-page fault", 21: "load guest-page fault",
          23: "store/AMO guest-page fault"}
INTERRUPTS = {1: "SSI", 2: "VSSI", 3: "MSI", 5: "STI", 6: "VSTI", 7: "MTI", 9: "SEI", 10: "VSEI", 11: "MEI",
              12: "SGEI", 13: "LCOFI"}
MODES = {3: "M", 1: "S/HS", 2: "VS", 0: "U?"}


def decode_word0(v: int) -> dict[str, Any]:
    """ACT trap signature word 0 (rvtest_trap_handler.h): mode, entry size, vector, xIE, xIP, xstatus[17:0]."""
    return {"mode": v & 3, "entry_size": (v >> 2) & 0xF, "vector": (v >> 6) & 0x1F, "xie": (v >> 11) & 1,
            "xip": (v >> 12) & 1, "xstatus_17_0": (v >> 13) & 0x3FFFF}


def contiguous_ones(v: int) -> tuple[int, int] | None:
    if v == 0:
        return None
    lo = (v & -v).bit_length() - 1
    hi = v.bit_length() - 1
    return (lo, hi) if v == ((1 << (hi + 1)) - 1) ^ ((1 << lo) - 1) else None


# ------------------------------------------------------------------ board config

def load_board_config(config_dir: Path | None) -> dict[str, Any]:
    if not config_dir or not config_dir.is_dir():
        return {}
    cfg: dict[str, Any] = {"dir": str(config_dir)}
    sail = config_dir / "sail.json"
    if sail.is_file():
        cfg["sail"] = json.loads(sail.read_text())
    for f in config_dir.glob("*.yaml"):
        data = yaml.safe_load(f.read_text().replace("\r", "")) or {}
        if isinstance(data, dict) and data.get("kind") == "architecture configuration":
            cfg["udb"] = data
            cfg["udb_file"] = f.name
    rep = config_dir / "generation_report.yaml"
    if rep.is_file():
        cfg["origin"] = (yaml.safe_load(rep.read_text()) or {}).get("udb_param_origin", {})
    return cfg


def sail_get(cfg: dict[str, Any], dotted: str) -> Any:
    cur: Any = cfg.get("sail", {})
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    if isinstance(cur, dict) and "value" in cur:
        return cur["value"]
    return cur


def config_check(cfg: dict[str, Any], sail_key: str | None, udb_key: str | None, proposed: Any,
                 spec_key: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"sail_key": sail_key, "udb_key": udb_key, "spec_key": spec_key, "proposed": proposed}
    if sail_key and cfg.get("sail") is not None:
        out["sail_current"] = sail_get(cfg, sail_key)
    if udb_key and cfg.get("udb") is not None:
        out["udb_current"] = (cfg["udb"].get("params") or {}).get(udb_key)
        if cfg.get("origin"):
            out["udb_origin"] = cfg["origin"].get(udb_key)
    return out


def as_int(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).replace("_", ""), 0)
    except ValueError:
        return None


def overlap_proof(cfg: dict[str, Any], tval: int | None, store: bool) -> tuple[bool, str]:
    """Is tval both misaligned and outside every accessible region of the board's sail.json?"""
    if tval is None:
        return False, "no trap value (xtval) in the log"
    if tval & 7 == 0:
        return False, f"xtval {tval:#x} is 8-byte aligned: the access may not be misaligned"
    regions = ((cfg.get("sail") or {}).get("memory") or {}).get("regions") or []
    if not regions:
        return False, "board sail.json not available to check accessibility"
    for r in regions:
        base, size = as_int(r["base"]["value"]), as_int(r["size"]["value"])
        attrs = r.get("attributes") or {}
        ok = attrs.get("writable") if store else attrs.get("readable")
        if base is not None and size is not None and base <= tval < base + size and ok:
            return False, f"xtval {tval:#x} is inside an accessible region at {base:#x}: not an access-fault address"
    return True, f"xtval {tval:#x} is misaligned and outside every accessible region"


# ------------------------------------------------------------------ analysis

def analyze(text: str, test: str = "", board: dict[str, Any] | None = None,
            config_dir: Path | None = None, fallback_cause: Any = None) -> dict[str, Any]:
    """board: optional facts such as {'medeleg_mask': 0xb15d, 'replaced_firmware': 'OpenSBI'}.
    fallback_cause: trap cause found elsewhere ([TRAP] lines, FAILQ) when RVCP prints no XCAUSE."""
    board = board or {}
    rv = parse_rvcp(text)
    if not rv.get("xcause") and as_int(fallback_cause) is not None:
        rv["xcause"] = hex(as_int(fallback_cause))
        rv["xcause_source"] = "fallback (not printed by RVCP)"
    # A zero exception PC is genuine for instruction-fetch faults: ACT jumps to RVMODEL_ACCESS_FAULT_ADDRESS,
    # often 0x0, so xepc = xtval = 0. For any other cause it means a stale capture: do not reason from it.
    FETCH_CAUSES = {0, 1, 12, 20}
    if rv.get("xcause") and as_int(rv.get("xepc")) == 0 and (as_int(rv["xcause"]) & ((1 << 63) - 1)) not in FETCH_CAUSES:
        rv["xcause_stale"] = rv.pop("xcause")
    result: dict[str, Any] = {"schema": "value-diff-v1", "test": test, "rvcp": rv}
    exp, act = as_int(rv.get("expected")), as_int(rv.get("actual"))
    if exp is None or act is None:
        result.update(applicable=False, reason="no expected/actual value pair in the RVCP block")
        return result
    csr, source = identify_csr(rv)
    diff = exp ^ act
    result.update(applicable=True, csr=csr, csr_source=source, expected=hex(exp), actual=hex(act), diff=hex(diff),
                  extra_bits=bit_list(act & ~exp), missing_bits=bit_list(exp & ~act))
    if csr:
        result["extra_fields"] = fields_for(csr, act & ~exp)
        result["missing_fields"] = fields_for(csr, exp & ~act)
    cfg = load_board_config(config_dir)
    hyp = None

    # 1. pmpaddr all-ones readback: address width and granularity.
    if csr and csr.startswith("pmpaddr") and contiguous_ones(act) and contiguous_ones(exp):
        (alo, ahi), (elo, ehi) = contiguous_ones(act), contiguous_ones(exp)
        hw_pa, model_pa = ahi + 3, ehi + 3
        hw_grain, model_grain = 1 << (alo + 2), 1 << (elo + 2)
        checks = []
        parts = []
        if hw_pa != model_pa:
            parts.append(f"hardware implements {hw_pa} physical-address bits, the config {model_pa}")
            checks.append(config_check(cfg, "memory.physaddr_bits", "PHYS_ADDR_WIDTH", hw_pa, "csr.phys_addr_bits"))
        if hw_grain != model_grain:
            parts.append(f"PMP granularity is {hw_grain} bytes in hardware, {model_grain} in the config")
            checks.append(config_check(cfg, "memory.pmp.grain", "PMP_GRANULARITY", hw_grain.bit_length() - 1,
                                       "csr.pmp.granularity_bytes"))
        if parts:
            hyp = {"pattern": "pmpaddr_width_or_grain",
                   "root_cause": f"{csr} all-ones readback shows " + "; ".join(parts) +
                                 ": the ACT/Sail config describes a different PMP address range than the board.",
                   "owner": "ACT/Sail board config", "config": checks, "explains_observation": True,
                   "evidence": f"readback {act:#x} = ones in bits {alo}..{ahi}; expected bits {elo}..{ehi}"}

    # 2. Address-holding CSR legalized by sign-extension.
    if hyp is None and csr in ADDRESS_CSRS:
        k = sign_extended_from(exp, act)
        if k is not None:
            hyp = {"pattern": "address_sign_extension",
                   "root_cause": f"Hardware legalizes {csr} by sign-extending from bit {k} (it keeps a "
                                 f"{k + 1}-bit address); Sail stores the written value unchanged. WARL address "
                                 "legalization is implementation-defined, so this is a model/config difference, "
                                 "not a DUT bug.",
                   "owner": "Sail model / spec-allowed implementation difference", "config": [],
                   "explains_observation": True,
                   "evidence": f"actual {act:#x} == expected {exp:#x} sign-extended from bit {k}"}

    # 3. Writable-mask CSR: readback differs from the modeled mask.
    if hyp is None and csr in MASK_KEYS:
        keys = MASK_KEYS[csr]
        missing, extra = exp & ~act, act & ~exp
        names = lambda m: [f["field"] for f in fields_for(csr, m)] or bit_list(m)
        row = config_check(cfg, keys.get("sail"), keys.get("udb"),
                           f"clear bits {hex(missing)}" + (f", set bits {hex(extra)}" if extra else ""), keys.get("spec"))
        cur = as_int(row.get("sail_current"))
        stale = cur is not None and not cur & missing and cur & extra == extra
        if stale:
            row["already_matches"] = True
        hyp = {"pattern": "writable_mask_mismatch",
               "root_cause": f"{csr}: bits {names(missing)} are not writable on the board"
                             + (f" and bits {names(extra)} are" if extra else "")
                             + ", but the config used for this run declared otherwise (readback = written value "
                               "AND the hardware mask).",
               "owner": "ACT/Sail board config", "config": [row], "explains_observation": True,
               "evidence": f"expected {exp:#x}, read {act:#x}"}

    # 4. Extra pending/status bits: state the model does not start with.
    if hyp is None and csr in PENDING_STATUS and act & ~exp and not exp & ~act:
        extra = result.get("extra_fields", [])
        h_fields = [f for f in extra if "H" in f["extensions"] or any(a.startswith(("hvip", "hip")) for a in f["aliases_all"])]
        if extra and len(h_fields) == len(extra):
            src = sorted({a for f in h_fields for a in f["aliases_all"] if a.startswith(("hvip", "hip", "hgeip"))})
            hyp = {"pattern": "hypervisor_pending_state",
                   "root_cause": f"{csr} has hypervisor-level pending bits set ({', '.join(f['field'] for f in extra)}). "
                                 f"These alias {', '.join(src) or 'H-extension state'}; Sail starts with them clear. "
                                 "The board left H-extension state non-zero"
                                 + (f" after the runner replaced {board['replaced_firmware']}" if board.get("replaced_firmware") else "")
                                 + ": the runner does not reset H CSRs before each ELF.",
                   "owner": "runner/board environment",
                   "config": [], "explains_observation": True,
                   "evidence": f"extra bits {bit_list(act & ~exp)} map to {[f['field'] for f in extra]}",
                   "closure": "read hvip (and hip) at ELF entry with csr_probe.py, or clear hvip in the runner "
                              "and rerun the test"}
        elif extra:
            hyp = {"pattern": "extra_status_bits",
                   "root_cause": f"{csr} has extra bits set in hardware: {[f['field'] for f in extra]}.",
                   "owner": "undetermined", "config": [], "explains_observation": False,
                   "evidence": f"extra bits {bit_list(act & ~exp)}"}

    # 5. Packed trap-signature word 0.
    label = (rv.get("mismatching_field") or "")
    if hyp is None and label.startswith("Vector+Mode"):
        hyp = word0_hypothesis(rv, exp, act, cfg, result, board)

    # 6. Exception-code pairs: misaligned vs access fault priority.
    if hyp is None and csr in ("mcause", "scause", "vscause") and exp < 64 and act < 64 and rv.get("handler_mode"):
        pair = {exp, act}
        if pair in ({4, 5}, {6, 7}, {0, 1}):
            proven, why = overlap_proof(cfg, as_int(rv.get("xtval")), store=6 in pair)
            hyp = {"pattern": "misaligned_vs_access_fault_priority" if proven else "misaligned_vs_access_fault_unproven",
                   "root_cause": f"For an access that is both misaligned and faulting, hardware reports "
                                 f"{CAUSES[act]} ({act}) where the model expects {CAUSES[exp]} ({exp}). Which one "
                                 "wins is implementation-defined (EEI), so the ACT/Sail config's misaligned/"
                                 "access-fault priority does not match the board.",
                   "owner": "ACT/Sail board config (spec-allowed priority)",
                   "config": [config_check(cfg, "memory.misaligned", "MISALIGNED_LDST_EXCEPTION_PRIORITY",
                                           "check per access type (scalar, AMO, LR/SC may differ)")],
                   "explains_observation": proven,
                   "evidence": f"xcause {exp}->{act}; {why}",
                   "closure": None if proven else "prove the faulting address is both misaligned and inaccessible "
                                                  "(xtval, PMP/PMA, page state)"}
        elif exp != act:
            hyp = {"pattern": "cause_mismatch", "owner": "undetermined", "config": [], "explains_observation": False,
                   "root_cause": f"Hardware reported {CAUSES.get(act, act)} ({act}); the model expected "
                                 f"{CAUSES.get(exp, exp)} ({exp}).",
                   "evidence": f"xcause {exp}->{act}"}

    # 7. Address-holding CSR legalized by keeping fewer bits (any sign-extension point).
    if hyp is None and csr in ADDRESS_CSRS | {"stval", "mtval", "vstval"}:
        k = sign_extended_from(exp, act)
        if k is not None:
            hyp = {"pattern": "address_sign_extension",
                   "root_cause": f"Hardware keeps {k + 1} address bits in {csr} and sign-extends from bit {k}; "
                                 "the model keeps the full value. The address-width legalization of xepc/xtval/"
                                 "xtvec is implementation-defined, so this is a model/config difference.",
                   "owner": "Sail model / spec-allowed implementation difference", "config": [],
                   "explains_observation": True,
                   "evidence": f"actual {act:#x} == expected's low {k + 1} bits sign-extended"}

    # 7b. Upper address bits collapsed to the sign bit (non-canonical address reported canonicalized).
    if hyp is None and csr in ADDRESS_CSRS | {"stval", "mtval", "vstval"}:
        for k in range(62, 30, -1):
            low_mask = (1 << k) - 1
            if act & low_mask == exp & low_mask and act >> k == (((1 << (64 - k)) - 1) if exp >> 63 else 0):
                hyp = {"pattern": "address_canonicalized",
                       "root_cause": f"For the non-canonical address {exp:#x}, hardware writes {csr} with bits 63:{k} "
                                     f"replaced by copies of bit 63 (it stores {k} address bits plus a sign); the "
                                     "model reports the raw address. How xtval holds an invalid address is "
                                     "implementation-defined: model/config difference, not a DUT bug.",
                       "owner": "Sail model / spec-allowed implementation difference", "config": [],
                       "explains_observation": True, "evidence": f"low {k} bits equal; bits 63:{k} = sign"}
                break

    # 8. satp.PPN narrower than the model: physical address width.
    if hyp is None and csr in ("satp", "vsatp", "hgatp") and (exp >> 60) == (act >> 60):
        ep, ap = exp & ((1 << 44) - 1), act & ((1 << 44) - 1)
        if contiguous_ones(ep) and contiguous_ones(ap) and ep != ap:
            hw_pa, model_pa = contiguous_ones(ap)[1] + 13, contiguous_ones(ep)[1] + 13
            hyp = {"pattern": "satp_ppn_width",
                   "root_cause": f"{csr}.PPN keeps {hw_pa - 12} bits in hardware, {model_pa - 12} in the model: "
                                 f"the board has {hw_pa} physical-address bits, the config {model_pa}.",
                   "owner": "ACT/Sail board config",
                   "config": [config_check(cfg, "memory.physaddr_bits", "PHYS_ADDR_WIDTH", hw_pa,
                                           "csr.phys_addr_bits")],
                   "explains_observation": True, "evidence": f"PPN {ap:#x} vs {ep:#x}"}

    # 9. pmpaddr/PMP walk: single high bit missing -> not implemented address bit.
    if hyp is None and (csr or "").startswith("pmpaddr") is False and test.lower().startswith("pmp") \
            and act == 0 and exp and exp & (exp - 1) == 0 and exp.bit_length() - 1 >= 30:
        b = exp.bit_length() - 1
        hyp = {"pattern": "pmpaddr_bit_not_implemented",
               "root_cause": f"PMP address bit {b} reads 0 in hardware: pmpaddr holds PA[{b + 2}] there, so the "
                             f"board implements at most {b + 2} physical-address bits while the config models more.",
               "owner": "ACT/Sail board config",
               "config": [config_check(cfg, "memory.physaddr_bits", "PHYS_ADDR_WIDTH", f"<= {b + 2}",
                                       "csr.phys_addr_bits")],
               "explains_observation": True, "evidence": f"expected bit {b} set, actual 0"}

    # 10. pmpaddr write ignored (value unrelated to what was written): locked or owned entry.
    if hyp is None and (csr or "").startswith("pmpaddr") and contiguous_ones(exp) and act and act & exp != exp \
            and not contiguous_ones(act):
        hyp = {"pattern": "pmp_write_ignored",
               "root_cause": f"{csr} kept {act:#x} after the test wrote all ones: the entry looks locked "
                             "(pmpcfg.L) or still owned by earlier boot firmware.",
               "owner": "runner/board environment", "config": [], "explains_observation": False,
               "evidence": f"readback {act:#x} is not a mask of the written value",
               "closure": "read pmpcfg0 at ELF entry (L bit) with a probe"}

    # 11. WARL field keeps fewer bits (truncation), e.g. mcause exception code.
    if hyp is None and csr:
        w = truncated_to(exp, act)
        if w is not None:
            width = f"{w[0]}" if w[0] == w[1] else f"{w[0]}-{w[1]}"
            hyp = {"pattern": "warl_field_truncation",
                   "root_cause": f"{csr} keeps only the low {width} bits of the written value in hardware (WARL "
                                 "legalization); the model keeps more. Legal implementation choice: config/model "
                                 "difference, not a DUT bug.",
                   "owner": "Sail model / spec-allowed implementation difference", "config": [],
                   "explains_observation": True,
                   "evidence": f"wrote {exp:#x}, read {act:#x} (= low {width} bits)"}

    # 12. Counter reads zero after a write.
    if hyp is None and csr and re.match(r"m?hpmcounter\d+$", csr) and act == 0 and exp:
        n = int(re.search(r"\d+$", csr).group(0))
        hyp = {"pattern": "hpm_counter_not_writable",
               "root_cause": f"{csr} reads 0 after the test wrote {exp:#x}: the counter is hardwired to zero, "
                             "inhibited, or not implemented on the board, but the config enables it.",
               "owner": "ACT/Sail board config",
               "config": [config_check(cfg, "base.writable_hpm_counters", "HPM_COUNTER_EN", f"bit {n} clear",
                                       "csr.hpm_counters")],
               "explains_observation": False, "evidence": f"write {exp:#x}, read 0",
               "closure": "probe the counter (write, read back) or check mcountinhibit at ELF entry"}

    if hyp:
        cfg_rows = hyp.get("config") or []
        hyp["config_verified"] = bool(cfg_rows) and all(
            ("sail_current" in c or "udb_current" in c) for c in cfg_rows)
        hyp["config_disagrees"] = any(
            (c.get("sail_current") is not None and as_int(c["sail_current"]) not in (None, as_int(c["proposed"])))
            or (c.get("udb_current") is not None and c.get("udb_current") != c["proposed"]) for c in cfg_rows)
        hyp["config_unverified_default"] = any(str(c.get("udb_origin", "")).startswith("default") for c in cfg_rows)
        def matches(c: dict[str, Any]) -> bool:
            cur = as_int(c.get("sail_current")) if c.get("sail_current") is not None else as_int(c.get("udb_current"))
            prop = c.get("proposed")
            if cur is None:
                return False
            if isinstance(prop, int):
                return cur == prop
            m = re.fullmatch(r"<= (\d+)", str(prop))
            return bool(m) and cur <= int(m.group(1))
        numeric = [c for c in cfg_rows if isinstance(c.get("proposed"), int) or str(c.get("proposed", "")).startswith("<=")]
        already = any(c.get("already_matches") for c in cfg_rows) or (numeric and all(matches(c) for c in numeric))
        if already and "fixed in current config" not in hyp["owner"]:
            hyp["owner"] += " (fixed in current config)"
            hyp["root_cause"] += (" The current board config already matches the hardware: the failing run used "
                                  "an older config; regenerate and rerun to confirm.")
        hyp["fixed_in_current_config"] = bool(already) or "fixed in current config" in hyp["owner"]
    result["hypothesis"] = hyp
    if cfg:
        result["board_config"] = {"dir": cfg.get("dir"), "udb_file": cfg.get("udb_file"),
                                  "has_sail": "sail" in cfg, "has_origin": "origin" in cfg}
    return result


def word0_hypothesis(rv: dict[str, Any], exp: int, act: int, cfg: dict[str, Any],
                     result: dict[str, Any], board: dict[str, Any]) -> dict[str, Any] | None:
    e, a = decode_word0(exp), decode_word0(act)
    result["word0"] = {"expected": e, "actual": a}
    if exp == 0xDEADBEEFDEADBEEF:
        return {"pattern": "unexpected_trap", "owner": "undetermined", "config": [], "explains_observation": False,
                "root_cause": "A trap was taken where the test expected none (signature slot still holds the "
                              "DEADBEEF fill).",
                "evidence": f"xcause {rv.get('xcause')}"}
    xcause = as_int(rv.get("xcause"))
    if xcause is None:
        diffs = [f"mode {MODES.get(a['mode'])} vs expected {MODES.get(e['mode'])}"] if e["mode"] != a["mode"] else []
        return {"pattern": "trap_signature_fields", "owner": "undetermined", "config": [],
                "explains_observation": False, "evidence": "; ".join(diffs) or "word 0 differs",
                "root_cause": "Trap signature word 0 differs, but the trap cause is not in the log, so the "
                              "delegation/priority mechanism cannot be decided.",
                "closure": "capture xcause for this trap (runner trap log or RVCP XCAUSE)"}
    intr = xcause >> 63 == 1
    code = (xcause or 0) & 0xFF
    status_csr = "mstatus" if a["mode"] == 3 else "sstatus"
    diffs = []
    if e["mode"] != a["mode"]:
        diffs.append(f"trap taken in {MODES.get(a['mode'])}-mode, expected {MODES.get(e['mode'])}-mode")
    if e["vector"] != a["vector"] or e["entry_size"] != a["entry_size"]:
        diffs.append(f"vector entry {a['vector']}/{a['entry_size']} vs expected {e['vector']}/{e['entry_size']}")
    if e["xip"] != a["xip"]:
        diffs.append(f"x{'IP'}[{code}] ({INTERRUPTS.get(code, code)}) = {a['xip']} in the handler, expected {e['xip']}")
    if e["xie"] != a["xie"]:
        diffs.append(f"xIE[{code}] = {a['xie']}, expected {e['xie']}")
    sdiff = e["xstatus_17_0"] ^ a["xstatus_17_0"]
    status_fields = [f["field"] for f in fields_for(status_csr, sdiff)] if sdiff else []
    if sdiff:
        diffs.append(f"{status_csr} fields differ: {status_fields or bit_list(sdiff)}")
    base = {"evidence": "; ".join(diffs), "config": [], "word0_diffs": diffs}
    sail_mask = as_int(sail_get(cfg, "base.medeleg.delegatable_bits")) if cfg.get("sail") else None
    hw_mask = as_int(board.get("medeleg_mask")) if board.get("medeleg_mask") is not None else None
    if e["mode"] != a["mode"] and not intr and a["mode"] != 3 and e["mode"] == 3 and code in (0, 4, 6):
        partner = code + 1
        if sail_mask is not None and not sail_mask >> partner & 1:
            return {**base, "pattern": "priority_shows_as_delegation",
                    "root_cause": f"Hardware reported {CAUSES[code]} ({code}), which is delegated, so it trapped to "
                                  f"S-mode; the model chose {CAUSES[partner]} ({partner}), which this board cannot "
                                  "delegate, so it expected M-mode. The mode difference is the misaligned/access-"
                                  "fault priority difference (implementation-defined).",
                    "owner": "ACT/Sail board config (spec-allowed priority)",
                    "config": [config_check(cfg, "memory.misaligned", "MISALIGNED_LDST_EXCEPTION_PRIORITY",
                                            "check per access type (scalar, AMO, LR/SC may differ)")],
                    "explains_observation": True}
    if e["mode"] != a["mode"] and not intr and a["mode"] == 3 and (a["xstatus_17_0"] >> 11) & 3 == 3:
        return {**base, "pattern": "trap_from_mmode",
                "root_cause": f"The {CAUSES.get(code, code)} ({code}) trap came from code running in M-mode "
                              "(mstatus.MPP = M), and M-mode traps are never delegated, so it was handled in M-mode. "
                              "The model expected the instruction to execute in S/U-mode: the privilege switch "
                              "before it did not happen on the board (test/runner flow).",
                "owner": "test/runner flow", "explains_observation": True,
                "closure": f"map xepc {rv.get('xepc')} to the test objdump and check the mode switch before it"}
    if e["mode"] != a["mode"] and not intr and a["mode"] == 3:
        cfg_says = None if sail_mask is None else bool(sail_mask >> code & 1)
        if hw_mask is not None and hw_mask >> code & 1:
            return {**base, "pattern": "delegation_not_in_effect",
                    "root_cause": f"The board can delegate {CAUSES.get(code, code)} ({code}) (medeleg mask "
                                  f"{hw_mask:#x}), yet the trap went to M-mode: delegation was not in effect when "
                                  "it trapped (test/runner flow, or the trap came from M-mode code).",
                    "owner": "undetermined", "explains_observation": False,
                    "closure": "read medeleg and the privilege mode just before the faulting instruction"}
        if cfg_says is False:
            rc = (f"{CAUSES.get(code, code)} ({code}) cannot be delegated on this board, so it trapped to M-mode. "
                  "The current board config already marks it non-delegatable: the failing run used an older "
                  "config; regenerate and rerun.")
            owner = "ACT/Sail board config (fixed in current config)"
        else:
            return {**base, "pattern": "delegation_not_taken",
                    "root_cause": f"Hardware handled {CAUSES.get(code, code)} ({code}) in M-mode although the model "
                                  "delegated it to S-mode. Either medeleg bit " + str(code) + " is not writable on "
                                  "the board or delegation was not in effect at the trap.",
                    "owner": "undetermined", "explains_observation": False,
                    "config": [config_check(cfg, "base.medeleg.delegatable_bits", None, f"bit {code} clear?",
                                            "csr.medeleg_writable")],
                    "closure": "probe the medeleg writable mask (csr_probe.py medeleg<-~0)"}
        return {**base, "pattern": "delegation_not_taken", "root_cause": rc, "owner": owner,
                "config": [config_check(cfg, "base.medeleg.delegatable_bits", None, f"bit {code} clear",
                                        "csr.medeleg_writable")],
                "explains_observation": True}
    if e["mode"] == a["mode"] and intr and e["xip"] != a["xip"] and not sdiff:
        return {**base, "pattern": "interrupt_pending_dropped",
                "root_cause": f"The {INTERRUPTS.get(code, code)} interrupt was taken but its pending bit was "
                              f"{'clear' if not a['xip'] else 'set'} when the handler read xIP: the board's "
                              "interrupt source changed between trap and handler (source deasserted or cleared by "
                              "another agent, e.g. the monitor hart or firmware timer setup).",
                "owner": "runner/board environment", "explains_observation": True,
                "closure": "probe the interrupt source (mtimecmp/PLIC) around the trap"}
    return {**base, "pattern": "trap_signature_fields", "owner": "undetermined", "explains_observation": False,
            "root_cause": "Trap signature word 0 differs: " + "; ".join(diffs) + "."}


GENERIC_CATEGORIES = {"unknown", "hardware_or_platform_difference", "act_generated_signature_layout_mismatch"}
# Older specific rules that stay open; a closable value-diff mechanism supersedes them.
WEAKER_CATEGORIES = {"tvec_warl_readback_mismatch", "satp_warl_readback_mismatch", "mtval_behavior_difference",
                     "counteren_warl_readback_mismatch"}
# Patterns whose mechanism is fully determined by the values (no hardware probe needed to close).
CLOSABLE = {"pmpaddr_width_or_grain", "address_sign_extension", "address_canonicalized", "writable_mask_mismatch",
            "misaligned_vs_access_fault_priority", "priority_shows_as_delegation", "satp_ppn_width",
            "pmpaddr_bit_not_implemented", "warl_field_truncation", "delegation_not_taken"}


def apply_to_result(result: dict[str, Any], vd: dict[str, Any]) -> dict[str, Any]:
    """Replace a generic classification with the value-diff mechanism, if there is one."""
    hyp = (vd or {}).get("hypothesis")
    if not hyp:
        return result
    replace = result.get("category") in GENERIC_CATEGORIES or (
        result.get("category") in WEAKER_CATEGORIES and hyp["pattern"] in CLOSABLE and hyp.get("explains_observation"))
    if not replace:
        return result
    out = dict(result)
    out["category_before_value_diff"] = result.get("category")
    out["category"] = f"value_diff_{hyp['pattern']}"
    out["root_cause"] = hyp["root_cause"]
    rows = hyp.get("config") or []
    def short(v: Any) -> str:
        return "see sail.json" if isinstance(v, (dict, list)) else str(v)
    fix = "; ".join(f"{c.get('sail_key') or c.get('udb_key')}: current {short(c.get('sail_current', c.get('udb_current')))}, "
                    f"should be {c.get('proposed')}" for c in rows if c.get("sail_key") or c.get("udb_key"))
    if hyp.get("fixed_in_current_config"):
        out["recommendation"] = "Regenerate the ACT ELFs with the current board config and rerun this test."
    elif hyp.get("closure"):
        out["recommendation"] = hyp["closure"]
    elif not hyp.get("explains_observation"):
        out["recommendation"] = ("Collect the trap tuple (xcause, xepc, xtval) and the Sail state at the same step; "
                                 "the values alone do not determine the mechanism.")
    elif fix:
        out["recommendation"] = f"Fix the board ACT config ({fix}), regenerate and rerun."
    else:
        out["recommendation"] = "Record as a model/implementation difference; no DUT bug."
    out["triggered_rules"] = list(result.get("triggered_rules") or []) + [
        {"rule": f"value_diff.{hyp['pattern']}", "summary": hyp.get("evidence", "")}]
    return out
