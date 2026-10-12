"""Step 4 of a failure analysis: what the reference model did at the failing instruction.

The ELF that ran on the board carries the board's boot code (UART, PLIC), which Sail does not
model, so the reference run uses the signature ELF ACT built for Sail (`test.sig.elf`) with the
board's sail.json. The two are linked differently, so the failing PC is moved across by symbol:
the nearest code label before it (check labels like <cg>_<cp>_<bin> exist in both) plus the
same offset.

reference() returns, for the first time Sail executes that instruction:
  window   the instructions just before it and the instruction itself, with their register,
           CSR and memory effects (instruction fetches left out)
  result   the effects of the failing instruction alone: what Sail read or wrote
  setup    earlier writes to the same CSR family and stores to memory-mapped devices, the
           usual way a test arranges what it checks (for example raising an interrupt)
Sail runs once per failure, bounded by an instruction limit and a timeout, and its trace is
read as a stream (never held in memory). Best effort: without
Sail, the signature ELF or the board's sail.json the part is simply left out.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any

SAIL_ROOTS = ("/data/ci/toolchains", str(Path.home()))
LEGACY_VERSION = "0.14.1"  # what built runs that do not record their Sail version
TRACE_FLAGS = ["--trace-instr", "--trace-reg", "--trace-csr", "--trace-mem", "--trace-exception",
               "--trace-interrupt", "--use-abi-names"]
INST_LIMIT = 3_000_000  # ACT tests retire far fewer; bounds the trace file
INSTRUCTION = re.compile(r"^\[(\d+)\] \[(\w+)\]: 0x([0-9A-Fa-f]+) \(0x([0-9A-Fa-f]+)\) (.*)$")
SYMBOL = re.compile(r"^([0-9a-fA-F]+) ([tT]) (\S+)$")
MMIO_BELOW = 0x80000000  # board RAM starts at or above this on every supported board


def sail_binary(version: str = "") -> str | None:
    """The Sail that built the run: riscv-sail-<version> beside the others (CI installs each
    version side by side), else TRIAGE_SAIL or the one on PATH."""
    configured = os.environ.get("TRIAGE_SAIL", "")
    if configured.lower() in {"off", "no", "0"}:
        return None
    by_version = [str(Path(root) / f"riscv-sail-{version or LEGACY_VERSION}" / "bin" / "sail_riscv_sim")
                  for root in SAIL_ROOTS]
    for candidate in (configured, *by_version, shutil.which("sail_riscv_sim") or ""):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _tool(name: str) -> str | None:
    return os.environ.get(f"TRIAGE_{name.upper()}") or shutil.which(f"riscv64-unknown-elf-{name}")


@lru_cache(maxsize=64)
def _symbols(elf: str) -> list[tuple[int, str]]:
    nm = _tool("nm")
    if not nm:
        return []
    try:
        out = subprocess.run([nm, "-n", elf], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        match = SYMBOL.match(line.strip())
        if match and not match.group(3).startswith((".L", "$")):
            rows.append((int(match.group(1), 16), match.group(3)))
    return rows


def map_pc(board_elf: Path, sig_elf: Path, pc: int) -> tuple[int, str] | None:
    """The address in sig_elf of the instruction at pc in board_elf, and the label it is under.
    A virtual pc (virtual-memory tests run their code at VA = high bits | PA) keeps its high bits."""
    symbols = _symbols(str(board_elf))
    if not symbols:
        return None
    high = 0
    if pc > symbols[-1][0] + 0x100000 and (pc & 0xFFFFFFFF) >= symbols[0][0]:
        high, pc = pc & ~0xFFFFFFFF, pc & 0xFFFFFFFF
    board = [(a, n) for a, n in symbols if a <= pc]
    if not board:
        return None
    base, name = max(board, key=lambda row: row[0])
    for address, other in _symbols(str(sig_elf)):
        if other == name:
            return high | (address + (pc - base)), f"{name}+{pc - base:#x}"
    return None


def _run_sail(sig_elf: Path, config: Path, work: Path, version: str = "") -> Path | None:
    sail = sail_binary(version)
    if not sail:
        return None
    trace = work / "trace.txt"
    try:
        subprocess.run([sail, "--config", str(config), "--inst-limit", str(INST_LIMIT), *TRACE_FLAGS,
                        "--trace-output", str(trace), "--test-signature", str(work / "sig"), str(sig_elf)],
                       capture_output=True, text=True,
                       timeout=int(os.environ.get("TRIAGE_SAIL_TIMEOUT", "120")))
    except (OSError, subprocess.SubprocessError):
        return None
    return trace if trace.is_file() else None


def _csr_family(name: str) -> set[str]:
    """A CSR and the CSRs that alias or shadow it (sip <-> mip, vsip <-> hip/hvip/mip)."""
    family = {name}
    if name[:1] in {"s", "h"}:
        family.add("m" + name[1:])
    if name.startswith("vs"):
        family |= {"h" + name[2:], "hv" + name[2:], "m" + name[2:]}
    if name.endswith("ip"):
        family |= {"mip", "hvip"} if name != "mip" else {"sip", "hvip"}
    return family


def _encoding(elf: Path, address: int) -> str:
    """The shape of the instruction at a physical address of an ELF (see shape())."""
    objdump = _tool("objdump")
    if not objdump:
        return ""
    address &= 0xFFFFFFFF if address >> 32 else address
    try:
        out = subprocess.run([objdump, "-d", "-M", "no-aliases", f"--start-address={address:#x}",
                              f"--stop-address={address + 4:#x}", str(elf)],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(rf"^\s*{address:x}:\s+[0-9a-f]+\s+(.*)$", out, re.M)
    return shape(match.group(1)) if match else ""


def shape(disassembly: str) -> str:
    """An instruction without its immediates and addresses: 'auipc ra,0x4b' -> 'auipc ra'. The
    two builds lay out data differently, so the same instruction can carry other immediates."""
    text = re.sub(r"<[^>]*>|#.*$", "", disassembly.lower())
    parts = [p for p in re.split(r"[\s,()]+", text) if p and not re.match(r"^-?(0x[0-9a-f]+|\d+)$", p)]
    return " ".join(parts)


def reference(board_elf: Path, sig_elf: Path, config: Path, pc: int | None,
              csrs: list[str] = (), before: int = 8, encoding: str = "",
              sail_version: str = "") -> dict[str, Any]:
    """Sail's behavior at the board's failing instruction (see the module docstring). `csrs` are
    the CSRs the failing instruction accesses; writes to them and their aliases count as setup."""
    if not pc or not board_elf.is_file() or not sig_elf.is_file() or not config.is_file():
        return {}
    mapped = map_pc(board_elf, sig_elf, pc)
    if not mapped:
        return {}
    target, label = mapped
    # The same label can hold different code in the two builds: ACT routines such as
    # rvtest_set_mext_int_* contain the board's RVMODEL macros, which the Sail build replaces.
    if encoding and _encoding(sig_elf, target) not in ("", shape(encoding)):
        return {"label": label, "sig_pc": f"{target:#x}", "executed": False, "window": [],
                "note": (f"The failing instruction is in {label.split('+')[0]}, whose code differs "
                         "between the board build and the Sail build (board-specific RVMODEL macro "
                         "code there is replaced by Sail's own); Sail has no counterpart instruction.")}
    family = set().union(*(_csr_family(c) for c in csrs)) if csrs else set()
    wanted = f"]: 0x{target:016X} ("
    found: dict[str, Any] = {"label": label, "sig_pc": f"{target:#x}"}
    with tempfile.TemporaryDirectory(prefix="triage-sail-") as work:
        trace = _run_sail(sig_elf, config, Path(work), sail_version)
        if not trace:
            return {}
        recent: deque[str] = deque(maxlen=400)
        setup: deque[str] = deque(maxlen=8)
        at, result, after = "", [], None
        with trace.open(encoding="utf-8", errors="replace") as stream:
            for raw in stream:
                if raw.startswith("mem[X,"):
                    continue
                line = raw.rstrip()
                if after is not None:  # collecting the failing instruction's effects
                    if INSTRUCTION.match(line):
                        break
                    result.append(line)
                    continue
                if wanted in line and INSTRUCTION.match(line):
                    at, after = line, True
                    continue
                recent.append(line)
                csr = re.match(r"^CSR (\w+) \(0x[0-9a-f]+\) <- ", line)
                store = re.match(r"^mem\[W,0x([0-9A-Fa-f]+)\]", line)
                if (csr and csr.group(1) in family) or (store and int(store.group(1), 16) < MMIO_BELOW):
                    setup.append(line)
    if not at:
        found.update(executed=False, window=list(recent)[-8:],
                     note="Sail never executed this instruction in the reference run.")
        return found
    window, seen = [], 0
    for line in reversed(recent):
        window.insert(0, re.sub(r"\s{2,}", "  ", line))
        if INSTRUCTION.match(line):
            seen += 1
            if seen >= before:
                break
    match = INSTRUCTION.match(at)
    found.update(executed=True, mode=match.group(2) if match else "",
                 at=re.sub(r"\s{2,}", "  ", at), result=result[:8], window=window[-40:],
                 setup=list(setup))
    return found


def render(found: dict[str, Any]) -> str:
    """Prompt text for reference()."""
    if not found:
        return ""
    if not found.get("executed"):
        return f"Reference model (Sail, this board's sail.json): {found.get('note')} ({found.get('label')})"
    parts = [f"Reference model (Sail, this board's sail.json) at the same instruction ({found['label']}), "
             f"in {found.get('mode')} mode:", found["at"]]
    if found.get("result"):
        parts.append("  Sail's result: " + "; ".join(found["result"]))
    if found.get("setup"):
        parts.append("Earlier in the reference run, writes to the same CSRs and device stores (setup):")
        parts += [f"  {line}" for line in found["setup"]]
    parts.append("Instructions just before it in the reference run:")
    parts += [f"  {line}" for line in found.get("window", [])[-10:]]
    return "\n".join(parts)
