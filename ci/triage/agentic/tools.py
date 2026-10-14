#!/usr/bin/env python3
"""The triage agent's diagnostic tools, for native function calling.

Four atomic tools; each takes JSON-typed arguments and returns a JSON-serializable dict
with "ok": true and its result, or "ok": false and "error" (a tool never raises to the agent):

  tool_parse_rvcp_log         the failure footprint of a UART/RVCP log
  tool_get_disassembly        N instructions before and after a PC in an ELF
  tool_decode_hardware_state  a register value, field by field (riscv_state_decoder)
  tool_query_sail_trace       what Sail executed around a PC (its .sig.trace)

TOOL_SCHEMAS holds the JSON Schema of each tool's arguments; openai_tools() and
anthropic_tools() format them for those APIs, and dispatch(name, arguments, hardware_profile)
validates the arguments against the schema before calling the tool.

Hardware awareness: tool_get_disassembly and tool_decode_hardware_state take the board's
hardware profile (hardware_registry.load_hardware_profile) as a mandatory parameter. The model
never passes it - it is not in their schemas; the runtime injects the active board's profile
in dispatch(), so the model cannot alter it and does not resend it every call. Hex arguments must be "0x" plus
1-16 hex digits. Paths must lie under TRIAGE_TOOL_ROOTS (colon-separated; default: this
repository), so a model cannot make a tool read arbitrary files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent

from . import decoder as riscv_state_decoder  # noqa: E402
from . import objdump_text as mepc_to_objdump  # noqa: E402
from . import rvcp_log as parse_rvcp_log  # noqa: E402

HEX = r"^0x[0-9a-fA-F]{1,16}$"
ISA = r"^[Rr][Vv](32|64)[A-Za-z0-9_]*$"
MAX_LOG_BYTES = 8 * 1024 * 1024
INSTRUCTION = re.compile(r"^\[(\d+)\] \[(\w+)\]: 0x([0-9A-Fa-f]+) \(0x([0-9A-Fa-f]+)\) (.*?)\s*$")
TRACE_POINTER = re.compile(r"^using (\S+\.trace) for trace output", re.M)


class ToolError(Exception):
    pass


# ---------- argument safety ----------

def roots() -> list[Path]:
    configured = os.environ.get("TRIAGE_TOOL_ROOTS", "")
    paths = [Path(p) for p in configured.split(":") if p] or [HERE.parents[2]]
    return [p.resolve() for p in paths]


def safe_path(value: str, name: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not any(path == root or root in path.parents for root in roots()):
        raise ToolError(f"{name} {value!r} is outside the allowed roots ({', '.join(map(str, roots()))})")
    if not path.is_file():
        raise ToolError(f"{name} {value!r} does not exist")
    return path


def hex_arg(value: str, name: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(HEX, value):
        raise ToolError(f"{name} must be a hex string like 0x80000040, got {value!r}")
    return int(value, 16)


def norm(value: Any) -> str | None:
    """A log hex string as 0x-lower-case, or None."""
    if value in (None, ""):
        return None
    try:
        return f"0x{int(str(value), 16):x}"
    except ValueError:
        return str(value)


# ---------- 1. failure footprint ----------

# Trap PCs first (the RVCP trap report gives xepc), the low-provenance FAILSCR address last but one.
PC_SOURCES = ("first_trap.pc", "mepc", "xepc", "approximate_address", "failscr_provenance.failing_addr",
              "act_first_epc")


def _get(data: dict[str, Any], dotted: str) -> Any:
    value: Any = data
    for part in dotted.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def tool_parse_rvcp_log(log_path: str) -> dict[str, Any]:
    """The failure footprint of one test's UART/RVCP log."""
    path = safe_path(log_path, "log_path")
    if path.stat().st_size > MAX_LOG_BYTES:
        raise ToolError(f"log_path is larger than {MAX_LOG_BYTES // (1024 * 1024)} MiB")
    data = parse_rvcp_log.parse_text(path.read_text(errors="replace"), str(path))
    pc_field = next((f for f in PC_SOURCES if _get(data, f) and int(str(_get(data, f)), 16) != 0), None)
    if data.get("register"):
        mismatch = {"kind": "register", "register": data["register"].strip(),
                    "expected": norm(data.get("expected_register_value")), "actual": norm(data.get("bad_value"))}
    elif data.get("mismatching_field") or data.get("expected_value"):
        mismatch = {"kind": "signature", "register": (data.get("mismatching_field") or "").strip() or None,
                    "expected": norm(data.get("expected_value")), "actual": norm(data.get("actual_value"))}
    elif data.get("expected_cause") or data.get("actual_cause"):
        mismatch = {"kind": "trap_cause", "register": "mcause",
                    "expected": data.get("expected_cause"), "actual": data.get("actual_cause")}
    else:
        mismatch = None
    trap = None
    cause = data.get("mcause") or _get(data, "first_trap.mcause") or data.get("xcause")
    if cause:
        trap = {"mcause": norm(cause),
                "mepc": norm(data.get("mepc") or _get(data, "first_trap.pc") or data.get("xepc")),
                "mtval": norm(data.get("mtval") or _get(data, "first_trap.tval") or data.get("xtval")),
                "mstatus": norm(data.get("mstatus") or _get(data, "first_trap.status") or data.get("xstatus")),
                "cause_name": riscv_state_decoder.decode_register("mcause", str(cause), "RV64")["fields"]["name"]}
    return {
        "case": data.get("case_name"), "status": data.get("status"), "category": data.get("category"),
        "failure": data.get("failure") or data.get("test_info"),
        "failing_pc": norm(_get(data, pc_field)) if pc_field else None, "failing_pc_source": pc_field,
        "instruction": norm(data.get("instruction")),
        "mismatch": mismatch, "trap": trap,
        "trap_handler_mode": data.get("trap_handler_mode"),
        "tohost": data.get("tohost_value"),
        "fields_available": sorted(k for k in data if not k.endswith("_int") and k not in ("sigq", "sigq_entries",
                                                                                         "failq", "source")),
    }


# ---------- 2. disassembly ----------

TOOLCHAIN_DIRS = [Path.home() / "riscv64" / "bin", Path.home() / "riscv32" / "bin",
                  Path("/data/ci/toolchains/riscv64/bin"), Path("/data/ci/toolchains/riscv32/bin")]
PRIV_SPECS = ("1.10", "1.11", "1.12", "1.13")


def profile_xlen(profile: dict[str, Any]) -> int:
    """The board's XLEN from its hardware profile; 32 or 64, else an error."""
    if not isinstance(profile, dict):
        raise ToolError("no hardware profile is active for this board")
    xlen = profile.get("xlen")
    if xlen not in (32, 64):
        raise ToolError(f"the hardware profile has no usable xlen ({xlen!r}); load a profile that states it")
    return xlen


def find_tool(name: str) -> str | None:
    """A toolchain binary: TRIAGE_<NAME> (e.g. TRIAGE_RISCV32_UNKNOWN_ELF_OBJDUMP), PATH, then the
    usual toolchain directories."""
    env = os.environ.get("TRIAGE_" + name.upper().replace("-", "_"))
    if env:
        return env
    found = shutil.which(name)
    if found:
        return found
    for directory in TOOLCHAIN_DIRS:
        if (directory / name).is_file():
            return str(directory / name)
    return None


def objdump_for(xlen: int) -> tuple[str, str]:
    """(objdump, note): the objdump of the board's XLEN; the other one disassembles both ELF
    classes, so it is the fallback."""
    preferred = f"riscv{xlen}-unknown-elf-objdump"
    found = find_tool(preferred)
    if found:
        return found, ""
    other = find_tool(f"riscv{96 - xlen}-unknown-elf-objdump")
    if other:
        return other, f"{preferred} not found; used {Path(other).name} (it reads both ELF classes)"
    raise ToolError(f"{preferred} is not installed (set TRIAGE_{preferred.upper().replace('-', '_')})")


def elf_class(path: Path) -> int | None:
    header = path.read_bytes()[:5]
    if header[:4] != b"\x7fELF":
        raise ToolError(f"{path.name} is not an ELF file")
    return {1: 32, 2: 64}.get(header[4])


def elf_arch(path: Path) -> str | None:
    """The ISA the ELF was built for (its Tag_RISCV_arch attribute), if recorded."""
    readelf = find_tool("riscv64-unknown-elf-readelf") or find_tool("riscv32-unknown-elf-readelf")
    if not readelf:
        return None
    out = subprocess.run([readelf, "-A", str(path)], capture_output=True, text=True, timeout=30).stdout
    match = re.search(r"Tag_RISCV_arch:\s*\"?([a-z0-9_]+)", out)
    return match.group(1) if match else None


def arch_extensions(arch: str) -> set[str]:
    """'rv64i2p1_m2p0_a2p1_zicsr2p0' -> {'I','M','A','Zicsr'} (versions dropped)."""
    parts = arch.lower().split("_")
    head = re.sub(r"^rv(32|64)", "", parts[0])
    letters = {ch.upper() for ch in re.sub(r"\d+p\d+", "", head)}
    if "G" in letters:
        letters |= {"I", "M", "A", "F", "D", "Zicsr", "Zifencei"}
    for part in parts[1:]:
        name = re.sub(r"\d+p\d+$", "", part)
        if len(name) == 1:
            letters.add(name.upper())
        elif name:
            letters.add(name[:1].upper() + name[1:])
    return letters


IMPLIES = {"M": {"Zmmul"}, "A": {"Zaamo", "Zalrsc"}, "C": {"Zca"}, "B": {"Zba", "Zbb", "Zbs"},
           "F": {"Zicsr"}, "G": {"I", "M", "A", "F", "D", "Zicsr", "Zifencei"}}


def with_implied(extensions: set[str]) -> set[str]:
    """An extension set plus what its members imply (C with F/D also gives Zcf/Zcd)."""
    out = set(extensions)
    for _ in range(2):
        for ext in list(out):
            out |= IMPLIES.get(ext, set())
    if "C" in out:
        out |= {"Zcf"} if "F" in out else set()
        out |= {"Zcd"} if "D" in out else set()
    return out


def beyond_board(elf_exts: set[str], board_exts: set[str]) -> list[str]:
    """Extensions the ELF uses that the board lacks, without listing parts of a listed parent."""
    missing = with_implied(elf_exts) - with_implied(board_exts | {"Zicsr", "Zifencei"})
    parts = {part for parent in missing for part in IMPLIES.get(parent, set())}
    return sorted(missing - parts)


def disassembler_flags(profile: dict[str, Any], arch: str | None) -> list[str]:
    """-M options for this board: CSR names of the privileged spec it implements, and `max`
    when the ELF records no ISA (objdump would otherwise decode only the base ISA)."""
    options = []
    priv = str(profile.get("priv_version") or "")
    version = re.match(r"^(\d+\.\d+)", priv)
    if version and version.group(1) in PRIV_SPECS:
        options.append(f"priv-spec={version.group(1)}")
    if not arch:
        options.append("max")
    return ["-M", ",".join(options)] if options else []


def tool_get_disassembly(elf_path: str, target_pc: str, hardware_profile: dict[str, Any],
                         window: int = 10) -> dict[str, Any]:
    """`window` instructions before and after target_pc, disassembled for the board in
    hardware_profile: its XLEN picks the objdump, its privileged spec names the CSRs, and the
    ELF's own ISA is checked against what the board implements."""
    xlen = profile_xlen(hardware_profile)
    path = safe_path(elf_path, "elf_path")
    pc = hex_arg(target_pc, "target_pc")
    if pc >> xlen:
        raise ToolError(f"target_pc {target_pc} does not fit a {xlen}-bit address")
    if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= 64:
        raise ToolError("window must be an integer from 1 to 64")
    notes = []
    cls = elf_class(path)
    if cls and cls != xlen:
        notes.append(f"MISMATCH: {path.name} is a {cls}-bit ELF but the board is {xlen}-bit")
    arch = elf_arch(path)
    beyond: list[str] = []
    if arch and hardware_profile.get("extensions"):
        beyond = beyond_board(arch_extensions(arch), set(hardware_profile["extensions"]))
        if beyond:
            notes.append(f"the ELF was built for extensions the board does not implement: {', '.join(beyond)}")
    objdump, note = objdump_for(xlen)
    if note:
        notes.append(note)
    flags = disassembler_flags(hardware_profile, arch)
    # Compressed instructions are 2 bytes: read twice the needed span, then slice by count.
    span = 8 * window + 64
    out = subprocess.run([objdump, "-d", *flags, f"--start-address={max(0, pc - span):#x}",
                          f"--stop-address={pc + span:#x}", str(path)],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise ToolError(f"objdump failed: {out.stderr.strip()[:300]}")
    rows = [r for r in mepc_to_objdump.parse_objdump_text(out.stdout) if r["type"] == "insn"]
    if not rows:
        raise ToolError(f"no instructions near {target_pc} in {path.name}")
    index = max((i for i, r in enumerate(rows) if r["addr"] <= pc), default=0)
    hit = rows[index]
    chosen = rows[max(0, index - window):index + window + 1]
    return {
        "elf": path.name, "target_pc": f"0x{pc:x}", "exact": hit["addr"] == pc,
        "board_xlen": xlen, "elf_xlen": cls, "elf_arch": arch, "objdump": Path(objdump).name,
        "objdump_flags": flags, "notes": notes,
        "symbol": hit.get("label"), "instruction": {"addr": f"0x{hit['addr']:x}", "encoding": hit["encoding"],
                                                     "asm": hit["asm"]},
        "listing": [{"addr": f"0x{r['addr']:x}", "encoding": r["encoding"], "asm": r["asm"],
                     "symbol": r.get("label"), "target": r is hit} for r in chosen],
        "before": index - max(0, index - window), "after": len(chosen) - 1 - (index - max(0, index - window)),
    }


# ---------- 3. register decode ----------

def tool_decode_hardware_state(reg_name: str, hex_val: str, hardware_profile: dict[str, Any]) -> dict[str, Any]:
    """A CSR or translation value, field by field, in the layout of the board's XLEN (RV32:
    mstatus.SD at bit 31 and no UXL/SXL, mstatush, mcause's interrupt bit at 31, Sv32 satp). A
    value wider than the board's XLEN is an error, not silently truncated."""
    if not isinstance(reg_name, str) or not re.fullmatch(r"^[a-z][a-z0-9]{1,15}$", reg_name):
        raise ToolError(f"reg_name must be a lower-case CSR name such as mstatus, got {reg_name!r}")
    hex_arg(hex_val, "hex_val")
    xlen = profile_xlen(hardware_profile)
    try:
        result = riscv_state_decoder.decode_register(reg_name, hex_val, xlen=xlen, strict=True)
    except riscv_state_decoder.WidthError as exc:
        raise ToolError(str(exc)) from exc
    result["board"] = hardware_profile.get("board_name")
    return result


# ---------- 4. Sail reference trace ----------

def trace_file(path: Path) -> Path:
    """A .sig.trace, or the trace a .sig.log names (Sail prints 'using X for trace output')."""
    if path.suffix != ".log":
        return path
    head = path.read_text(errors="replace")[:4000]
    named = TRACE_POINTER.search(head)
    for candidate in ([Path(named.group(1))] if named else []) + [path.with_suffix(".trace")]:
        sibling = path.with_name(candidate.name)
        for option in (candidate, sibling):
            if option.is_file():
                return safe_path(str(option), "trace_path")
    raise ToolError(f"{path.name} names no trace file that exists (run Sail with --trace-output)")


# Set by the CI runtime for one case: a missing trace is generated on first use (CI keeps the
# Sail-built ELF and sail.json, not the trace), and a board PC is mapped to the Sail ELF (the two
# are linked differently) by symbol + offset.
SAIL_HOOKS: dict[str, Callable[..., Any]] = {}


def tool_query_sail_trace(trace_path: str, target_pc: str, window: int = 6, occurrence: int = 1) -> dict[str, Any]:
    """What Sail executed around the n-th execution of target_pc, with each instruction's effects."""
    requested = Path(trace_path)
    if not requested.exists() and "generate" in SAIL_HOOKS:
        if not SAIL_HOOKS["generate"](requested):
            raise ToolError("no Sail reference run is available for this case (no Sail ELF, sail.json or Sail)")
    path = trace_file(safe_path(trace_path, "trace_path"))
    pc = hex_arg(target_pc, "target_pc")
    mapped_from = None
    if "map_pc" in SAIL_HOOKS:
        mapped = SAIL_HOOKS["map_pc"](pc)
        if mapped:
            mapped_from, (pc, label) = f"0x{pc:x}", mapped
            mapped_from += f" (board ELF) -> {label} in the Sail ELF"
    for name, value, high in (("window", window, 32), ("occurrence", occurrence, 1000)):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= high:
            raise ToolError(f"{name} must be an integer from 1 to {high}")
    recent: deque[dict[str, Any]] = deque(maxlen=window)
    found: dict[str, Any] | None = None
    after: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    count = 0

    def close(step: dict[str, Any] | None) -> None:
        nonlocal found
        if step is None:
            return
        if found is None:
            if step.get("is_target"):
                found = step
            else:
                recent.append(step)
        elif len(after) < window:
            after.append(step)

    with path.open(errors="replace") as stream:
        for raw in stream:
            line = raw.rstrip()
            if not line or line.startswith("mem[X,"):
                continue
            match = INSTRUCTION.match(line)
            if match:
                close(current)
                if found is not None and len(after) >= window:
                    current = None
                    count += int(int(match.group(3), 16) == pc)
                    continue
                addr = int(match.group(3), 16)
                text = match.group(5)
                symbol = text.split()[-1] if re.search(r"\S+\+\d+$", text) else None
                asm = text[: text.rfind(symbol)].strip() if symbol else text
                is_hit = addr == pc
                count += int(is_hit)
                current = {"step": int(match.group(1)), "mode": match.group(2), "pc": f"0x{addr:x}",
                           "encoding": f"0x{match.group(4).lower()}", "asm": asm, "symbol": symbol,
                           "effects": [], "is_target": is_hit and count == occurrence}
            elif current is not None:
                current["effects"].append(line[:200])
        close(current)
    if found is None:
        raise ToolError(f"Sail never executed {target_pc}" + (f" {occurrence} times (only {count})" if count else "")
                        + " - PCs differ between the board ELF and the Sail-built .sig.elf; use the symbol+offset")
    for step in [*recent, found, *after]:
        step.pop("is_target", None)
        step["effects"] = step["effects"][:12]
    return {"trace": path.name, "target_pc": f"0x{pc:x}", "mapped_from": mapped_from,
            "occurrence": occurrence, "executions": count,
            "mode": found["mode"], "symbol": found["symbol"], "at": found,
            "before": list(recent), "after": after}


# ---------- 5. specification text ----------

SPEC_HOOK: dict[str, Callable[[str], str]] = {}  # set by the CI runtime: the board's spec versions


def tool_search_spec(query: str) -> dict[str, Any]:
    """Passages of the specification versions this board implements that match the query."""
    if not isinstance(query, str) or len(query.split()) == 0 or len(query) > 200:
        raise ToolError("query must be a few words, e.g. 'misaligned access fault priority'")
    if "search" not in SPEC_HOOK:
        raise ToolError("no specification index is available")
    text = SPEC_HOOK["search"](query)
    return {"query": query, "passages": text or "no passage matches; try other words (CSR, field or exception names)"}


# ---------- registry ----------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "tool_parse_rvcp_log",
        "description": (
            "Read one ACT test's UART/RVCP log and return its failure footprint: status, failure text, "
            "failing PC (and which log field it came from), the mismatching register or signature field "
            "with expected and actual hex values, the trap (mcause/mepc/mtval/mstatus and cause name) and "
            "the list of other fields present. Call this first for a failed test."),
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["log_path"],
            "properties": {"log_path": {"type": "string", "minLength": 1,
                                        "description": "Path of the test's uart.log or events.log."}},
        },
    },
    {
        "name": "tool_get_disassembly",
        "description": (
            "Disassemble the ELF that ran on the board around a PC: `window` instructions before and after "
            "it, each with address, encoding, assembly and the symbol it is under; the target is marked. "
            "The board's profile (applied automatically) selects the 32- or 64-bit objdump and the CSR names "
            "of its privileged spec, and the result notes an ELF built for a different XLEN or for "
            "extensions the board lacks. Use it to see the failing instruction and the setup before it."),
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["elf_path", "target_pc"],
            "properties": {
                "elf_path": {"type": "string", "minLength": 1, "description": "Path of the test ELF."},
                "target_pc": {"type": "string", "pattern": HEX,
                              "description": "Program counter as 0x-prefixed hex, e.g. 0x80000a3c."},
                "window": {"type": "integer", "minimum": 1, "maximum": 64, "default": 10,
                           "description": "Instructions to return on each side of the target."},
            },
        },
    },
    {
        "name": "tool_decode_hardware_state",
        "description": (
            "Decode a register value captured from the log field by field: mstatus/sstatus/vsstatus "
            "(MPP, FS, SD...), mcause/scause (interrupt, code, name), mtval/stval (canonical Sv39/48/57 "
            "checks), htval/mtval2 (guest physical address), medeleg/hedeleg and mideleg/mie/mip/sie/sip/"
            "hvip (named bits), satp/vsatp/hgatp (mode, ASID/VMID, root table), pte (V R W X U G A D, PPN, "
            "reserved bits), mtvec/stvec, misa, menvcfg/senvcfg/henvcfg, mstatush (RV32). The layout follows "
            "the board's XLEN from its profile (applied automatically). Use it instead of decoding bits "
            "yourself."),
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["reg_name", "hex_val"],
            "properties": {
                "reg_name": {"type": "string", "pattern": r"^[a-z][a-z0-9]{1,15}$",
                             "description": "Lower-case register name, e.g. mstatus, scause, satp, pte."},
                "hex_val": {"type": "string", "pattern": HEX,
                            "description": "The value as 0x-prefixed hex, at most XLEN bits."},
            },
        },
    },
    {
        "name": "tool_query_sail_trace",
        "description": (
            "Read the Sail reference model's execution trace (a .sig.trace, or the .sig.log that names it) "
            "around a PC: the instructions just before, the instruction itself and the ones after, each "
            "with privilege mode and its register, CSR and memory effects, plus how often the PC ran. PCs "
            "are those of the Sail-built .sig.elf; a board ELF can differ, so locate the instruction by "
            "its symbol+offset first if needed."),
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["trace_path", "target_pc"],
            "properties": {
                "trace_path": {"type": "string", "minLength": 1,
                               "description": "Path of the test's .sig.trace or .sig.log."},
                "target_pc": {"type": "string", "pattern": HEX,
                              "description": "Program counter as 0x-prefixed hex."},
                "window": {"type": "integer", "minimum": 1, "maximum": 32, "default": 6,
                           "description": "Instructions to return on each side."},
                "occurrence": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 1,
                               "description": "Which execution of the PC, for loops (1 = first)."},
            },
        },
    },
]
TOOL_SCHEMAS.append({
    "name": "tool_search_spec",
    "description": (
        "Search the RISC-V specification text of the versions this board implements (privileged, "
        "unprivileged, hypervisor), with the current ratified text where it differs. Use it to decide "
        "whether the specification requires, allows or forbids the observed behavior; quote what you "
        "rely on."),
    "parameters": {
        "type": "object", "additionalProperties": False, "required": ["query"],
        "properties": {"query": {"type": "string", "minLength": 3, "maxLength": 200,
                                 "description": "Precise words: CSR or field names, instruction, exception, "
                                                "'priority', 'delegation', 'WARL'..."}},
    },
})
FUNCTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "tool_parse_rvcp_log": tool_parse_rvcp_log,
    "tool_get_disassembly": tool_get_disassembly,
    "tool_decode_hardware_state": tool_decode_hardware_state,
    "tool_query_sail_trace": tool_query_sail_trace,
    "tool_search_spec": tool_search_spec,
}


def openai_tools(strict: bool = True) -> list[dict[str, Any]]:
    """Function definitions for the OpenAI API. Strict mode needs every property required, so
    optional integers become nullable (null = the default)."""
    tools = []
    for schema in TOOL_SCHEMAS:
        params = json.loads(json.dumps(schema["parameters"]))
        if strict:
            for name, prop in params["properties"].items():
                if name not in params["required"]:
                    prop["type"] = [prop["type"], "null"]
                prop.pop("default", None)
                for key in ("minimum", "maximum", "minLength", "pattern"):
                    if key in prop:
                        prop["description"] = prop.get("description", "") + f" ({key} {prop.pop(key)})"
            params["required"] = list(params["properties"])
        tools.append({"type": "function", "function": {"name": schema["name"], "description": schema["description"],
                                                         "parameters": params, "strict": strict}})
    return tools


def anthropic_tools() -> list[dict[str, Any]]:
    return [{"name": s["name"], "description": s["description"], "input_schema": s["parameters"]}
            for s in TOOL_SCHEMAS]


def validate(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Check arguments against a tool's schema (types, patterns, ranges, unknown keys); returns
    them with defaults filled in."""
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be a JSON object")
    props = schema["properties"]
    unknown = sorted(set(arguments) - set(props))
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(unknown)}")
    out = {}
    for name, prop in props.items():
        value = arguments.get(name)
        if value is None:
            if name in schema["required"]:
                raise ToolError(f"missing required argument {name}")
            if "default" in prop:
                out[name] = prop["default"]
            continue
        if prop["type"] == "string":
            if not isinstance(value, str) or len(value) < prop.get("minLength", 0):
                raise ToolError(f"{name} must be a non-empty string")
            if len(value) > prop.get("maxLength", 10_000):
                raise ToolError(f"{name} is longer than {prop['maxLength']} characters")
            if "pattern" in prop and not re.fullmatch(prop["pattern"], value):
                raise ToolError(f"{name}={value!r} does not match {prop['pattern']}")
        elif prop["type"] == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ToolError(f"{name} must be an integer")
            if not prop.get("minimum", value) <= value <= prop.get("maximum", value):
                raise ToolError(f"{name} must be from {prop['minimum']} to {prop['maximum']}")
        out[name] = value
    return out


HARDWARE_AWARE = {"tool_get_disassembly", "tool_decode_hardware_state"}


def dispatch(name: str, arguments: dict[str, Any] | str,
             hardware_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a tool call from a model: validate, call, and always return a dict. The runtime
    passes the active board's hardware_profile; hardware-aware tools receive it here."""
    try:
        if isinstance(arguments, str):
            arguments = json.loads(arguments or "{}")
        schema = next((s for s in TOOL_SCHEMAS if s["name"] == name), None)
        if schema is None:
            raise ToolError(f"unknown tool {name!r}; tools: {', '.join(FUNCTIONS)}")
        kwargs = validate(schema["parameters"], arguments)
        if name in HARDWARE_AWARE:
            kwargs["hardware_profile"] = hardware_profile
        result = FUNCTIONS[name](**kwargs)
        return {"ok": True, "tool": name, **result}
    except (ToolError, json.JSONDecodeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"ok": False, "tool": name, "error": str(exc)}


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--schemas":
        print(json.dumps(TOOL_SCHEMAS, indent=2))
    elif len(sys.argv) in (3, 4):
        profile = None
        if len(sys.argv) == 4:  # a board description for hardware_registry
            from .hardware import load_hardware_profile
            profile = load_hardware_profile(sys.argv[3])
        print(json.dumps(dispatch(sys.argv[1], sys.argv[2], profile), indent=2))
    else:
        print("usage: triage_tools.py --schemas | triage_tools.py TOOL_NAME '{json arguments}' [BOARD_DESCRIPTION]",
              file=sys.stderr)
        sys.exit(2)
