"""Steps 2 and 3 of a failure analysis, from the files collect_case_files.py keeps:

  instruction   the failing instruction from the ELF that ran (objdump at the trapping or
                checking PC), with the source line it came from
  intent        what the test does at the failing check: the generated .S around the check's
                label (labels are <covergroup>_<coverpoint>_<bin>, as in the log's test_info),
                the test's header (required extensions, defines) and its intent comments
  macros        definitions of the board macros (rvmodel_macros.h) the check relies on, found
                by following the RVTEST_* macros it uses through ACT's env headers
  config        the board's sail.json settings that concern this failure

Everything is read from RUN/per_case/<case>/ and RUN/act/, so a re-triage of a retained run
sees exactly what that build ran. Every part is optional and capped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from . import sail_trace
except ImportError:  # run as a script from ci/triage
    import sail_trace  # type: ignore[no-redef]

FUNCTION = re.compile(r"^\s*(?:static\s+)?(?:bool|void|int|uint\w*|long)\s+\w+\s*\([^;]*\)\s*\{?\s*$")
CONSTANT = re.compile(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]{2,})\b")
MACRO_USE = re.compile(r"\b((?:RVTEST|RVMODEL|RVCP|SIG|LA|LI)_?[A-Z0-9_]*)\b")
DEFINE = re.compile(r"^\s*#\s*define\s+([A-Z_][A-Z0-9_]*)\b")
GENERIC = {"supported", "count", "base", "len", "value", "size", "extensions", "zero", "true",
           "false", "memory", "platform", "define", "required", "march", "xlen", "boot", "smode"}
INTERRUPT_KEYS = ["clint", "plic", "interrupt", "interrupts", "wfi", "mtime", "mtimecmp", "sstc"]
# Which sail.json keys concern which failure, beyond the CSR and extension names themselves.
CAUSE_KEYS = {
    0: ["misaligned", "ialign"], 1: ["pmp", "memory", "region"], 2: ["supported", "csr"],
    4: ["misaligned"], 5: ["pmp", "memory", "region", "access"], 6: ["misaligned", "amo", "lrsc"],
    7: ["pmp", "memory", "region", "access"], 12: ["satp", "pte", "sv39"], 13: ["satp", "pte", "sv39"],
    15: ["satp", "pte", "sv39", "misaligned", "amo"],
}


def case_dir(run_root: Path, case: str) -> Path:
    return run_root / "per_case" / (re.sub(r"[^A-Za-z0-9_.-]+", "_", case).strip("._") or "case")


def _tool(name: str) -> str | None:
    return os.environ.get(f"TRIAGE_{name.upper()}") or shutil.which(f"riscv64-unknown-elf-{name}")


def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------- step 2: the failing instruction ----------

def instruction(elf: Path, address: int | None, before: int = 12, after: int = 8) -> dict[str, Any]:
    """The instruction at `address` and its neighbours, and the source line it was built from."""
    objdump, addr2line = _tool("objdump"), _tool("addr2line")
    if not address or not elf.is_file() or not objdump:
        return {}
    out = _run([objdump, "-d", "-M", "no-aliases", f"--start-address={address - before:#x}",
                f"--stop-address={address + after:#x}", str(elf)])
    lines, at = [], ""
    for raw in out.splitlines():
        match = re.match(r"^\s*([0-9a-f]+):\s+([0-9a-f]+)\s+(.*)$", raw)
        if match:
            pc = int(match.group(1), 16)
            text = f"{pc:#x}: {match.group(2)}  {' '.join(match.group(3).split())}"
            if pc == address:
                at = text
                text += "   <== failing"
            lines.append(text)
    line = None
    if addr2line:
        found = re.match(r"^(.*):(\d+)", _run([addr2line, "-e", str(elf), f"{address:#x}"]).strip())
        if found and not found.group(1).startswith("??"):
            line = int(found.group(2))
    return {"pc": f"{address:#x}", "at": at, "window": lines[:12], "source_line": line}


# ---------- step 3: what the test does there ----------

def _header(lines: list[str]) -> list[str]:
    keep = []
    for line in lines[:60]:
        text = line.strip()
        if text.startswith(("# REQUIRED_EXTENSIONS", "# MARCH", "#define")) or "PARAM" in text:
            keep.append(text)
    return keep[:8]


def intent(source: Path, bin_label: str | None, line: int | None,
           before: int = 18, after: int = 4) -> dict[str, Any]:
    """The test's code at the failing check. Anchored on the check's label when the log names
    the bin, otherwise on the source line of the failing PC."""
    if not source.is_file():
        return {}
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    anchor = None
    if bin_label:
        for number, text in enumerate(lines, 1):
            if text.startswith(bin_label + ":"):
                anchor = number
                break
    if anchor is None and line and 0 < line <= len(lines):
        anchor = line
    if anchor is None:
        return {"header": _header(lines)}
    start = anchor - 1
    if source.suffix == ".c":
        # Back to the start of the test function the assertion is in (damo: one per case).
        for number in range(anchor - 1, max(anchor - 80, 1) - 1, -1):
            if FUNCTION.match(lines[number - 1]):
                start = number
                break
        else:
            start = max(anchor - before, 1)
        if anchor - start > 60:  # a long case: keep its header and the lines before the check
            start = anchor - before
    else:
        # Back to the previous check's label: that is this check's setup.
        while start > 1 and anchor - start < before and not re.match(r"^[A-Za-z0-9_]+:\s*$", lines[start - 2]):
            start -= 1
    stop = min(anchor + after, len(lines))
    snippet = "\n".join(f"{n:5}{'>' if n == anchor else ' '} {lines[n - 1].rstrip()[:150]}"
                        for n in range(max(start, 1), stop + 1))
    return {"header": _header(lines), "anchor": anchor, "code": snippet[:1800],
            "macros_used": sorted(set(MACRO_USE.findall("\n".join(lines[max(start, 1) - 1:stop]))))}


def bin_label(test_info: str) -> str | None:
    """'test: 37; cg: Sstvecd_cg; cp: cp_stvec_mode; bin: stvec_set_bit_38' ->
    'Sstvecd_cg_cp_stvec_mode_stvec_set_bit_38'."""
    parts = dict(re.findall(r"\b(cg|cp|bin):\s*([A-Za-z0-9_]+)", test_info or ""))
    if {"cg", "cp", "bin"} <= parts.keys():
        return f"{parts['cg']}_{parts['cp']}_{parts['bin']}"
    return None


# ---------- the macros behind the check ----------

JUMP = re.compile(r"\b(?:jal|call|j|tail)\s+(?:[a-z0-9]+\s*,\s*)?([A-Za-z_][A-Za-z0-9_]*)")
LABEL = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*):")


@lru_cache(maxsize=8)
def _routines(directory: str) -> dict[str, str]:
    """Label -> the code after it (to the next label, at most 40 lines), from the env headers:
    RVTEST_* macros often just `jal` to such a routine, which calls the board's macros."""
    table: dict[str, str] = {}
    for path in sorted(Path(directory).glob("*.h")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            match = LABEL.match(line)
            if not match:
                continue
            body = []
            for following in lines[index + 1:index + 41]:
                if LABEL.match(following):
                    break
                body.append(following)
            table.setdefault(match.group(1), "\n".join(body))
    return table


@lru_cache(maxsize=8)
def _definitions(directory: str) -> dict[str, str]:
    """Macro name -> its #define text (continuation lines joined), from all headers there."""
    table: dict[str, str] = {}
    for path in sorted(Path(directory).glob("*.h")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        index = 0
        while index < len(lines):
            match = DEFINE.match(lines[index])
            if match:
                body = [lines[index]]
                while body[-1].rstrip().endswith("\\") and index + 1 < len(lines):
                    index += 1
                    body.append(lines[index])
                table.setdefault(match.group(1), f"// {path.name}\n" + "\n".join(body))
            index += 1
    return table


def board_macros(run_root: Path, used: list[str], depth: int = 4, limit: int = 1600) -> list[str]:
    """Definitions of the board's RVMODEL_* macros reached from the macros the check uses."""
    env = _definitions(str(run_root / "act" / "env"))
    routines = _routines(str(run_root / "act" / "env"))
    board = _definitions(str(run_root / "act" / "config"))
    if not board:
        return []
    seen, frontier, reached, constants = set(), set(used), [], set()
    for _ in range(depth):
        following = set()
        for name in frontier - seen:
            seen.add(name)
            text = board.get(name) or env.get(name) or routines.get(name) or ""
            if name in board and (name.startswith("RVMODEL_") or name in constants):
                reached.append(name)
            following.update(MACRO_USE.findall(text))
            following.update(JUMP.findall(text))
            if name in board:  # constants a board macro uses: SIG_ADDRESS, CLINT_BASE_ADDRESS
                found = set(CONSTANT.findall(text)) - {name}
                constants.update(found)
                following.update(found)
        frontier = following
    # Every reached macro gets a line; long bodies (PLIC programming) are cut, not dropped.
    out, size = [], 0
    for name in reached:
        text = board[name]
        if len(text) > 420:
            text = text[:420] + " …"
        if size + len(text) > limit:
            out.append(f"// {name}: defined in rvmodel_macros.h (not shown)")
            continue
        out.append(text)
        size += len(text)
    return out


# ---------- the board's reference-model configuration ----------

def _flatten(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            rows += _flatten(item, f"{prefix}.{key}" if prefix else str(key))
        return rows
    return [(prefix, json.dumps(value))]


def sail_settings(run_root: Path, terms: list[str], cause: int | None, extensions: list[str] = (),
                  limit: int = 18) -> list[str]:
    """sail.json settings named by the failure: its CSRs and fields, the extensions the test
    requires, and the settings that decide its trap cause (e.g. misaligned priority)."""
    path = run_root / "act" / "config" / "sail.json"
    if not path.is_file():
        return []
    try:
        text = re.sub(r"(?m)^\s*//.*$", "", path.read_text(encoding="utf-8"))
        rows = _flatten(json.loads(text))
    except ValueError:
        return []
    wanted = {t.lower() for t in terms if len(t) > 2 and t.lower() not in GENERIC}
    wanted |= set(CAUSE_KEYS.get(cause, []))
    exts = {e.lower() for e in extensions} - {"i", "zicsr", "zifencei"}
    out = []
    for key, value in rows:
        segments = key.lower().split(".")
        named = segments[0] == "extensions" and len(segments) > 1 and segments[1] in exts
        if (named or set(segments) & wanted) and len(value) < 120:
            out.append(f"{key} = {value}")
    return out[:limit]


def act_revision(run_root: Path) -> str:
    path = run_root / "act" / "revision.txt"
    return path.read_text(encoding="utf-8").strip().replace("\n", "; ") if path.is_file() else ""


# ---------- all of it for one failure ----------

# ---------- damo-rv-priv-ats ----------

@lru_cache(maxsize=4)
def _damo_norms(directory: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for path in Path(directory).glob("*.md"):
        for match in re.finditer(r"\|\s*`norm:([A-Za-z0-9_]+)`\s*\|\s*([^|]+?)\s*\|",
                                 path.read_text(encoding="utf-8", errors="replace")):
            table.setdefault(match.group(1), match.group(2).strip())
    return table


def build_damo(run_root: Path, suite: str, extracted: dict[str, Any], address: int | None) -> dict[str, Any]:
    """A damo case: the C source at the failed assertion (file:line in the log) and, when the
    log shows a trap, the instruction at the trapping PC in the suite's ELF."""
    folder = run_root / "damo" / suite
    found: dict[str, Any] = {}
    inst = instruction(folder / "suite.elf", address)
    if inst:
        found["instruction"] = inst
    hint = re.search(r"\(([^()\s]+):(\d+)\)\s*$", str(extracted.get("damo_assert") or ""))
    if hint:
        relative = hint.group(1)
        for candidate in (folder / relative, run_root / "damo" / relative):
            if candidate.is_file():
                test = intent(candidate, None, int(hint.group(2)), before=24, after=3)
                if test.get("code"):
                    test["file"] = relative
                    found["intent"] = test
                break
    code = (found.get("intent") or {}).get("code", "")
    table = _damo_norms(str(run_root / "damo" / "NORM"))
    ids = dict.fromkeys(re.findall(r"norm:([A-Za-z0-9_]+)", code + " " + str(extracted.get("damo_assert", ""))))
    norms = [f"norm:{n}: {table[n][:300]}" for n in ids if n in table]
    if norms:
        found["norms"] = norms[:4]
    revision = run_root / "damo" / "revision.txt"
    if revision.is_file():
        found["act_revision"] = revision.read_text(encoding="utf-8").strip()
    return found


def build(run_root: Path, case: str, extracted: dict[str, Any], address: int | None,
          extra_terms: list[str], suite: str = "") -> dict[str, Any]:
    if suite and (run_root / "damo" / suite).is_dir():
        return build_damo(run_root, suite, extracted, address)
    folder = case_dir(run_root, case)
    found: dict[str, Any] = {}
    inst = instruction(folder / "test.elf", address)
    if inst:
        found["instruction"] = inst
    label = bin_label(str(extracted.get("test_info", "")))
    test = intent(folder / "test.S", label, inst.get("source_line") if inst else None)
    if test:
        found["intent"] = test
    used = (test or {}).get("macros_used", [])
    macros = board_macros(run_root, used)
    if macros:
        found["board_macros"] = macros
    # CSR operands of the failing instruction (csrrs a3,sip,zero -> sip) name settings too.
    words = re.findall(r"\bcsrr[sciw]i?\s+[a-z0-9]+,\s*([a-z0-9]+)", inst.get("at", "") if inst else "")
    cause, interrupt = None, False
    for key in ("trap_mcause", "xcause", "mcause"):
        value = str(extracted.get(key) or "")
        if value:
            try:
                number = int(value, 16)
                cause, interrupt = number & 0xFFF, bool(number >> 63)
            except ValueError:
                pass
            break
    if interrupt or case.startswith("Interrupts"):
        words += INTERRUPT_KEYS
        cause = None
    header = " ".join((test or {}).get("header", []))
    extensions = re.findall(r"'([A-Za-z0-9]+)'", header)
    settings = sail_settings(run_root, words + extra_terms, cause, extensions)
    if settings:
        found["sail_settings"] = settings
    # Step 4: what the reference model did at the same instruction.
    config = run_root / "act" / "config" / "sail.json"
    if inst and (folder / "test.sig.elf").is_file() and config.is_file():
        csrs = re.findall(r"\bcsrr[sciw]i?\s+[a-z0-9]+,\s*([a-z0-9]+)", inst.get("at", ""))
        text = re.match(r"^0x[0-9a-f]+:\s+[0-9a-f]+\s+(.*)$", inst.get("at", ""))
        ref = sail_trace.reference(folder / "test.elf", folder / "test.sig.elf", config, address, csrs,
                                   encoding=text.group(1) if text else "")
        if ref:
            found["reference"] = ref
    revision = act_revision(run_root)
    if revision:
        found["act_revision"] = revision
    return found
