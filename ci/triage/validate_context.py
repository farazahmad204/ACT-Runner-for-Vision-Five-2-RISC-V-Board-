"""Can the evidence be trusted? Checked by code before the AI sees any of it.

The triage reads files kept with the build (the test ELF, the ACT config, Sail) and
compares them with what happened on the board. If they do not belong together - the kept
ELF is not the one the board received, Sail for that ACT revision is missing - a root cause
built on them would be wrong however good the reasoning. check() returns:

  items      plain statements for the `environment` skill (OK or MISMATCH)
  checks     {"elf_matches": bool | None, "board_matches": bool | None, "reference": bool}
  revisions  {"runner_build", "act_commit", "sail_version", "spec_versions"}: what the analysis
             applies to; memory keys AI answers by them
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path
from typing import Any

try:
    from . import casefile, sail_trace
except ImportError:  # run as a script from ci/triage
    import casefile  # type: ignore[no-redef]
    import sail_trace  # type: ignore[no-redef]

READY = re.compile(r"\[UART_STREAM\] READY .*?board=(\S+).*?runner_build=(\S+)")
RECEIVED = re.compile(r"\[UART_STREAM\] (?:RX_OK|HEADER_OK) name=(\S+) size=(\d+) crc32=(0x[0-9a-fA-F]+)")


def revision_fields(run_root: Path) -> dict[str, str]:
    text = casefile.act_revision(run_root)
    return dict(re.findall(r"(\w+)=([^;\s]+)", text))


def check(run_root: Path, case: str, board: str, log_text: str,
          knowledge: dict[str, Any] | None = None) -> dict[str, Any]:
    items: list[str] = []
    checks: dict[str, Any] = {"elf_matches": None, "board_matches": None, "reference": False}
    fields = revision_fields(run_root)
    ready = READY.search(log_text or "")
    revisions = {"runner_build": ready.group(2) if ready else "", "act_commit": fields.get("act_commit", ""),
                 "sail_version": fields.get("sail_version", ""),
                 "spec_versions": ", ".join(f"{k}={v}" for k, v in sorted(((knowledge or {}).get("spec_versions")
                                                                          or {}).items()) if k != "source")}

    # 1. The kept ELF is the one the board received (the runner logs its size and CRC32).
    elf = casefile.case_dir(run_root, case) / "test.elf"
    received = RECEIVED.search(log_text or "")
    if received and elf.is_file():
        data = elf.read_bytes()
        size, crc = len(data), zlib.crc32(data) & 0xFFFFFFFF
        want_size, want_crc = int(received.group(2)), int(received.group(3), 16) & 0xFFFFFFFF
        checks["elf_matches"] = size == want_size and crc == want_crc
        if checks["elf_matches"]:
            items.append(f"OK: the kept test.elf is the ELF the board received (size {size}, crc32 {crc:#010x}).")
        else:
            items.append(f"MISMATCH: the kept test.elf (size {size}, crc32 {crc:#010x}) is not the ELF the board "
                         f"received (size {want_size}, crc32 {want_crc:#010x}): disassembly and the Sail "
                         "comparison may describe a different build; do not rely on them.")
    elif not elf.is_file():
        items.append("UNCHECKED: the test ELF was not kept; there is no disassembly or Sail comparison.")
    else:
        items.append("UNCHECKED: the UART log does not record the received ELF's size and CRC32.")

    # 2. The log is from this board.
    if ready:
        checks["board_matches"] = ready.group(1) == board
        items.append(("OK" if checks["board_matches"] else "MISMATCH") +
                     f": the runner reported board={ready.group(1)}, runner_build={ready.group(2)}"
                     + ("" if checks["board_matches"] else f" (triaged as {board})") + ".")

    # 3. The reference model of this ACT revision is available.
    version = fields.get("sail_version", "")
    config = run_root / "act" / "config" / "sail.json"
    binary = sail_trace.sail_binary(version)
    checks["reference"] = bool(binary and config.is_file())
    if fields.get("act_commit"):
        items.append(f"ACT commit {fields['act_commit'][:12]}, config {fields.get('config_dir', '?')}.")
    if not config.is_file():
        items.append("UNCHECKED: the board's sail.json was not kept; no reference comparison.")
    elif not binary:
        items.append(f"MISMATCH: Sail {version or '(version not recorded)'} used by this ACT revision is not "
                     "installed on the agent; no reference comparison.")
    else:
        items.append(f"OK: reference model Sail {version or '(legacy 0.14.1)'} with the board's sail.json.")

    # 4. The spec versions the board implements, the ones the analysis is judged against.
    if revisions["spec_versions"]:
        items.append(f"Spec versions this board implements: {revisions['spec_versions']}.")
    else:
        items.append("UNCHECKED: no spec versions recorded for this board; judge against the current text "
                     "only with care.")
    return {"items": items, "checks": checks, "revisions": revisions}
