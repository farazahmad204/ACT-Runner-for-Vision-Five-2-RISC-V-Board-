#!/usr/bin/env python3
"""Generate the runner board layer from a board spec.

Reads the `platform`, `test_hart`, `memory` (payload window), `runner` and
`boot` sections of assets/board_specs/<board>.yaml (the same file
gen_act_config.py reads) and writes, under --out-root (a runner checkout or
any directory), the paths build_runner.sh expects:

  cert_harness/boards/<id>.env               preset; every BOARD_* key explicit
  cert_harness/boards/<id>/link.ld           only when runner.firmware_window_limit is set
  <boot.fit.source>                          boot pattern "fit": U-Boot FIT source
  cert_harness/board_adapters/<id>.sh        boot patterns "eswin_nsign" and "custom"
  cert_harness/boards/<id>/runner_report.yaml  checks, derived values, manual steps

Boot patterns: "fit" (VF2 SD FIT, BPI-F3 GPT partition), "eswin_nsign" (Megrez
signed SPI boot chain), "custom" (skeleton adapter to complete by hand).
Deterministic: no network, no model calls. Exit 0 when complete, 2 when the
layout fails a check or a manual step remains (files still written).
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import Any

import yaml

from resolve_requirements import check_memory

SKILL_DIR = Path(__file__).resolve().parent.parent
RUNNER_ASSETS = SKILL_DIR / "assets" / "runner"
BOOT_PATTERNS = ("fit", "eswin_nsign", "custom")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_]{1,62}$")


class SpecError(Exception):
    pass


def num(v: Any) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


def hx(v: int) -> str:
    return f"0x{v:08X}"


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


def render(template: str, subs: dict[str, str]) -> str:
    out = re.sub(r"\{\{(\w+)\}\}", lambda m: subs[m.group(1)], template)
    left = re.findall(r"\{\{\w+\}\}", out)
    if left:
        raise SpecError(f"unrendered template keys: {left}")
    return out


class BoardFiles:
    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.board = need(spec, "runner.board_id")
        if not SAFE_ID.match(self.board):
            raise SpecError(f"runner.board_id '{self.board}' must be lower-case letters, digits and '_'")
        self.problems: list[str] = []
        self.manual: list[str] = []
        self.notes: list[str] = []
        self.platform_id = num(get(spec, "runner.platform_id", 0))
        self.registration_patch: str | None = None
        self.pattern = need(spec, "boot.pattern")
        if self.pattern not in BOOT_PATTERNS:
            raise SpecError(f"boot.pattern must be one of {BOOT_PATTERNS}")

    # ---------------------------------------------------------- values
    def values(self) -> dict[str, Any]:
        s = self.spec
        r = s["runner"]
        load = num(need(s, "runner.load_address"))
        ram_base, ram_limit = num(need(s, "runner.ram.base")), num(need(s, "runner.ram.limit"))
        if get(s, "runner.payload_window") is not None:
            pay_base = num(need(s, "runner.payload_window.base"))
            pay_limit = num(need(s, "runner.payload_window.limit"))
        else:
            pay_base = num(need(s, "memory.base"))
            pay_limit = pay_base + num(need(s, "memory.size"))
        clint = num(need(s, "platform.clint.base"))
        uart_irq = bool(r.get("external_irq_cleanup", True))
        v: dict[str, Any] = {
            "BOARD_NAME": self.board,
            "BOARD_DESCRIPTION": f"\"{need(s, 'runner.description')}\"",
            "BOARD_ADAPTER": self.board,
            "BOARD_PAYLOAD_TRANSPORT": "uart_stream",
            "BOARD_PLATFORM_ID": self.platform_id,
            "BOARD_FW_LOAD_ADDR": hx(load),
            "BOARD_FW_STACK_BYTES": hex(num(r.get("stack_bytes", 0x4000))),
            "BOARD_RAM_BASE": hx(ram_base),
            "BOARD_RAM_LIMIT": hx(ram_limit),
            "BOARD_PAYLOAD_BASE": hx(pay_base),
            "BOARD_PAYLOAD_LIMIT": hx(pay_limit),
            "BOARD_DDR_EXCLUDE_BASE": hx(num(get(s, "runner.excluded.base", 0))),
            "BOARD_DDR_EXCLUDE_LIMIT": hx(num(get(s, "runner.excluded.limit", 0))),
            "BOARD_UART_BASE": hx(num(need(s, "platform.uart.base"))),
            "BOARD_UART_SIZE": hex(num(get(s, "runner.uart.size", 0x1000))),
            "BOARD_UART_REG_IO_WIDTH": num(get(s, "runner.uart.access_bytes", get(s, "platform.uart.access_bytes", 1))),
            "BOARD_UART_8250_INIT_ENABLE": int(bool(get(s, "runner.uart.init_8250", False))),
            "BOARD_UART_INPUT_CLOCK_HZ": num(get(s, "runner.uart.input_clock_hz", 0)),
            "BOARD_UART_BAUD": num(get(s, "runner.uart.baud", 115200)),
            "BOARD_UART_RX_DIAG": int(bool(get(s, "runner.uart.rx_diag", False))),
            "BOARD_PLIC_BASE": hx(num(need(s, "platform.plic.base"))),
            "BOARD_UART_PLIC_SOURCE": num(need(s, "platform.uart.plic_source")) if uart_irq else 0,
            "BOARD_RUNNER_M_PLIC_CONTEXT": num(need(s, "platform.plic.test_hart_contexts.m")) if uart_irq else 0,
            "BOARD_RUNNER_S_PLIC_CONTEXT": num(need(s, "platform.plic.test_hart_contexts.s")) if uart_irq else 0,
            "BOARD_EXTERNAL_IRQ_CLEANUP_ENABLE": int(uart_irq),
            "BOARD_CLINT_MSIP_BASE": hx(clint),
            "BOARD_CLINT_MTIMECMP_BASE": hx(clint + 0x4000),
            "BOARD_CLINT_MTIME_ADDR": hx(clint + 0xBFF8),
            "BOARD_RUNNER_HART_ID": num(need(s, "test_hart")),
            "BOARD_MONITOR_HART_ID": num(need(s, "runner.monitor_hart")),
            "BOARD_FIXED_TOHOST_ADDR": hx(num(need(s, "runner.tohost_fallback"))),
            "BOARD_UART_ELF_BUFFER_ADDR": hx(num(need(s, "runner.staging.address"))),
            "BOARD_UART_ELF_MAX_BYTES": num(need(s, "runner.staging.max_bytes")),
            "BOARD_TEST_STACK_BYTES": num(r.get("test_stack_bytes", 262144)),
            "BOARD_TRAP_STACK_BYTES": num(r.get("trap_stack_bytes", 1024)),
        }
        wdt = r.get("watchdog")
        v["BOARD_WDT_ENABLE"] = int(bool(wdt))
        for key in ("base", "load", "ctrl", "lock", "unlock_key"):
            v[f"BOARD_WDT_{key.upper()}"] = hex(num(wdt[key])) if wdt else 0
        v["BOARD_K1_HART_WAKEUP_ENABLE"] = int(bool(r.get("k1_hart_wakeup", False)))
        if v["BOARD_UART_8250_INIT_ENABLE"] and not v["BOARD_UART_INPUT_CLOCK_HZ"]:
            self.problems.append("runner.uart.init_8250 needs runner.uart.input_clock_hz")
        if v["BOARD_RUNNER_HART_ID"] == v["BOARD_MONITOR_HART_ID"]:
            self.problems.append("runner hart and monitor hart must differ")
        return v

    # ---------------------------------------------------------- checks
    def check_layout(self, v: dict[str, Any]) -> None:
        s = self.spec
        def i(k: str) -> int:
            return num(v[k])
        cov = {
            "memory.ram_window": {"value": {"base": i("BOARD_RAM_BASE"), "limit": i("BOARD_RAM_LIMIT")}},
            "memory.payload_window": {"value": {"base": i("BOARD_PAYLOAD_BASE"), "limit": i("BOARD_PAYLOAD_LIMIT")}},
            "memory.staging_buffer": {"value": {"base": i("BOARD_UART_ELF_BUFFER_ADDR"),
                                                "bytes": i("BOARD_UART_ELF_MAX_BYTES")}},
        }
        if i("BOARD_DDR_EXCLUDE_LIMIT"):
            cov["memory.excluded_range"] = {"value": {"base": i("BOARD_DDR_EXCLUDE_BASE"),
                                                      "limit": i("BOARD_DDR_EXCLUDE_LIMIT")}}
        self.problems += check_memory(cov)
        load = i("BOARD_FW_LOAD_ADDR")
        win = get(s, "runner.firmware_window_limit")
        fw_end = num(win) if win is not None else load + num(get(s, "runner.max_image_bytes", 0x100000))
        if win is None:
            self.notes.append(f"no runner.firmware_window_limit: assuming the runner fits in "
                              f"{fw_end - load:#x} bytes for overlap checks")
        regions = [("payload_window", i("BOARD_PAYLOAD_BASE"), i("BOARD_PAYLOAD_LIMIT")),
                   ("staging_buffer", i("BOARD_UART_ELF_BUFFER_ADDR"),
                    i("BOARD_UART_ELF_BUFFER_ADDR") + i("BOARD_UART_ELF_MAX_BYTES"))]
        for name, lo, hi in regions:
            if load < hi and lo < fw_end:
                self.problems.append(f"runner image {load:#x}-{fw_end:#x} overlaps {name} {lo:#x}-{hi:#x}")
        if not i("BOARD_RAM_BASE") <= load < i("BOARD_RAM_LIMIT"):
            self.problems.append("runner load address is outside the RAM window")
        tohost = i("BOARD_FIXED_TOHOST_ADDR")
        if not i("BOARD_PAYLOAD_BASE") <= tohost < i("BOARD_PAYLOAD_LIMIT"):
            # VF2 keeps it at payload base + 0x1008000, inside the payload window; flag anything else.
            self.problems.append(f"fallback tohost {tohost:#x} is outside the payload window")

    # ---------------------------------------------------------- outputs
    def preset(self, v: dict[str, Any], linker: str, boot_keys: dict[str, str]) -> str:
        r = self.spec["runner"]
        groups = [
            ["BOARD_NAME", "BOARD_DESCRIPTION", "BOARD_ADAPTER", "BOARD_PAYLOAD_TRANSPORT", "BOARD_PLATFORM_ID"],
            list(boot_keys) + ["BOARD_LINKER_SCRIPT"],
            ["BOARD_FW_LOAD_ADDR", "BOARD_FW_STACK_BYTES", "BOARD_RAM_BASE", "BOARD_RAM_LIMIT",
             "BOARD_PAYLOAD_BASE", "BOARD_PAYLOAD_LIMIT", "BOARD_DDR_EXCLUDE_BASE", "BOARD_DDR_EXCLUDE_LIMIT"],
            ["BOARD_UART_BASE", "BOARD_UART_SIZE", "BOARD_UART_REG_IO_WIDTH", "BOARD_UART_8250_INIT_ENABLE",
             "BOARD_UART_INPUT_CLOCK_HZ", "BOARD_UART_BAUD", "BOARD_UART_RX_DIAG"],
            ["BOARD_PLIC_BASE", "BOARD_UART_PLIC_SOURCE", "BOARD_RUNNER_M_PLIC_CONTEXT",
             "BOARD_RUNNER_S_PLIC_CONTEXT", "BOARD_EXTERNAL_IRQ_CLEANUP_ENABLE"],
            ["BOARD_CLINT_MSIP_BASE", "BOARD_CLINT_MTIMECMP_BASE", "BOARD_CLINT_MTIME_ADDR",
             "BOARD_RUNNER_HART_ID", "BOARD_MONITOR_HART_ID"],
            ["BOARD_FIXED_TOHOST_ADDR", "BOARD_UART_ELF_BUFFER_ADDR", "BOARD_UART_ELF_MAX_BYTES",
             "BOARD_TEST_STACK_BYTES", "BOARD_TRAP_STACK_BYTES"],
            ["BOARD_WDT_ENABLE", "BOARD_WDT_BASE", "BOARD_WDT_LOAD", "BOARD_WDT_CTRL", "BOARD_WDT_LOCK",
             "BOARD_WDT_UNLOCK_KEY"],
            ["BOARD_K1_HART_WAKEUP_ENABLE"],
        ]
        allv = {**v, **boot_keys, "BOARD_LINKER_SCRIPT": linker}
        out = [f"# {self.board} runner preset, generated by gen_board_files.py from the board spec.",
               "# Change the spec and regenerate instead of editing by hand."]
        for g in groups:
            out.append("")
            out += [f"{k}={allv[k]}" for k in g]
        out.append("")
        if r.get("serial_note"):
            out += [f"# {line}" for line in str(r["serial_note"]).strip().splitlines()]
        out.append(f"DEFAULT_SERIAL_DEV={r.get('serial_dev', '/dev/ttyUSB0')}")
        out += ["", f"TOOLCHAIN_DIR={r.get('toolchain_dir', '')}", "TOOLCHAIN_TRIPLE=riscv64-unknown-elf", ""]
        return "\n".join(out)

    def linker(self) -> tuple[str, str | None]:
        win = get(self.spec, "runner.firmware_window_limit")
        if win is None:
            return "link.ld", None
        load = num(need(self.spec, "runner.load_address"))
        note = get(self.spec, "runner.firmware_window_note", "firmware window from the board evidence")
        text = f"""/* {self.board}: runner at {load:#x}, generated by gen_board_files.py. */
OUTPUT_ARCH(riscv)
ENTRY(_start)

SECTIONS
{{
  . = DEFINED(BOARD_FW_LOAD_ADDR) ? BOARD_FW_LOAD_ADDR : {load:#x};

  __text_start = .;
  .text : ALIGN(16) {{ *(.text .text.*) }}
  __text_end = .;

  __rodata_start = .;
  .rodata : ALIGN(16) {{ *(.rodata .rodata.*) *(.srodata .srodata.*) }}
  __rodata_end = .;

  __data_start = .;
  .data : ALIGN(16) {{ *(.data .data.*) *(.sdata .sdata.*) }}
  __data_end = .;

  __bss_start = .;
  .bss : ALIGN(16) {{
    *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON)
    __bss_end = .;
  }}

  .stack (NOLOAD) : ALIGN(16) {{
    __stack_bottom = .;
    . = . + (DEFINED(BOARD_FW_STACK_BYTES) ? BOARD_FW_STACK_BYTES : 0x4000);
    __stack_top = .;
  }}

  /* {note} */
  ASSERT(__stack_top <= {num(win):#x},
         "{self.board} runner exceeds its {num(win) - load:#x}-byte firmware window")

  /DISCARD/ : {{ *(.comment*) *(.note*) *(.eh_frame*) }}
}}
"""
        return f"cert_harness/boards/{self.board}/link.ld", text

    def boot(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return (preset keys, files to write)."""
        s, b = self.spec, self.spec["boot"]
        load = num(need(s, "runner.load_address"))
        desc = need(s, "runner.description")
        if self.pattern == "fit":
            f = b.get("fit") or {}
            source = f.get("source", f"cert_harness/boards/{self.board}/{self.board}.its")
            cells = num(f.get("address_cells", 1))
            load_cells = f"{load:#x}" if cells == 1 else f"0x00 {load:#010x}"
            if cells == 1 and load > 0xFFFFFFFF:
                raise SpecError("boot.fit.address_cells 1 cannot hold a load address above 4 GiB")
            hash_node = '      hash-1 {\n        algo = "crc32";\n      };\n' if f.get("crc32") else ""
            its = render((RUNNER_ASSETS / "fit.its.in").read_text(), {
                "DESCRIPTION": f.get("description", f"Minimal UART ELF runner for {desc}"),
                "ADDRESS_CELLS": str(cells), "IMAGE_NODE": f.get("image_node", "firmware"),
                "IMAGE_DESCRIPTION": f.get("image_description", f"{self.board}-uart-elf-runner"),
                "OS": f.get("os", "u-boot"), "LOAD_CELLS": load_cells, "HASH_NODE": hash_node,
                "CONFIG_NAME": f.get("config_name", "config-1")})
            keys = {"BOARD_FIT_SOURCE": source, "BOARD_BOOT_IMAGE": need(s, "boot.image"),
                    "BOARD_BOOT_IMAGE_PAD_BYTES": str(f.get("pad_bytes", ""))}
            if f.get("keep_source"):
                self.notes.append(f"{source} kept as-is (boot.fit.keep_source); generated FIT not written")
                return keys, {}
            return keys, {source: its}
        keys = {"BOARD_PACKAGE_MODE": "adapter", "BOARD_BOOT_IMAGE": need(s, "boot.image")}
        baud = str(get(s, "runner.uart.baud", 115200))
        adapter_path = f"cert_harness/board_adapters/{self.board}.sh"
        if self.pattern == "eswin_nsign":
            n = b.get("eswin_nsign") or {}
            flash = n.get("flash_message") or ["flashing is not automated; it needs a reviewed procedure and approval."]
            text = render((RUNNER_ASSETS / "adapter_eswin_nsign.sh.in").read_text(), {
                "DESCRIPTION": desc, "SHORT_NAME": n.get("short_name", self.board),
                "PACKAGING_ROOT_ENV": need(s, "boot.eswin_nsign.root_env"), "BOOT_IMAGE": need(s, "boot.image"),
                "SUMS_FILE": n.get("sums_file", f"{self.board}_packaging_sha256sums.txt"), "BAUD": baud,
                "FLASH_MESSAGE": "\n".join(f'  echo "{("ERROR: " if i == 0 else "")}{line}" >&2'
                                           for i, line in enumerate(flash))})
            self.notes.append(f"packaging needs ${n['root_env']} pointing at a pinned vendor signing tree")
            return keys, {adapter_path: text}
        text = render((RUNNER_ASSETS / "adapter_custom.sh.in").read_text(), {
            "DESCRIPTION": desc, "BOARD_ID": self.board, "BAUD": baud, "LOAD_ADDR": f"{load:#x}",
            "SLOT": b.get("slot", "unknown slot")})
        self.manual.append(f"complete board_package_image in {adapter_path} (custom boot pattern)")
        return keys, {adapter_path: text}

    def platform_registration(self, runner_root: Path | None) -> None:
        """Make READY print board=<id>: reuse an existing registration or propose one as a patch."""
        import difflib
        header = runner_root / "runner_shared.h" if runner_root else None
        if not header or not header.is_file():
            self.notes.append("runner_shared.h not checked (no --runner-root): confirm the platform name")
            return
        text = header.read_text()
        existing = re.search(rf'#(?:el)?if BOARD_PLATFORM_ID == (RUNNER_PLATFORM_\w+)\n#define RUNNER_PLATFORM_NAME '
                             rf'"{self.board}"', text)
        if existing:
            pid = re.search(rf"#define {existing.group(1)}\s+(\d+)", text)
            if pid and int(pid.group(1)) != self.platform_id:
                self.notes.append(f"runner.platform_id {self.platform_id} replaced by the registered id {pid.group(1)}")
                self.platform_id = int(pid.group(1))
            return
        ids = [int(x) for x in re.findall(r"#define RUNNER_PLATFORM_\w+\s+(\d+)", text)]
        defines = list(re.finditer(r"#define RUNNER_PLATFORM_\w+\s+\d+\n", text))
        generic = re.search(r'#else\n#define RUNNER_PLATFORM_NAME "generic_riscv"', text)
        if not ids or not defines or not generic:
            self.manual.append("register the board in runner_shared.h by hand (layout not recognized)")
            return
        new_id = max(ids) + 1
        macro = f"RUNNER_PLATFORM_{self.board.upper()}"
        at = defines[-1].end()
        new = text[:at] + f"#define {macro} {new_id}\n" + text[at:]
        g = new.index('#else\n#define RUNNER_PLATFORM_NAME "generic_riscv"')
        new = new[:g] + f'#elif BOARD_PLATFORM_ID == {macro}\n#define RUNNER_PLATFORM_NAME "{self.board}"\n' + new[g:]
        self.registration_patch = "".join(difflib.unified_diff(
            text.splitlines(True), new.splitlines(True), "a/runner_shared.h", "b/runner_shared.h"))
        self.platform_id = new_id
        self.manual.append(f"review and apply cert_harness/boards/{self.board}/runner_shared.registration.patch "
                           f"(registers platform id {new_id} so READY prints board={self.board}; shared-code change)")


def generate(spec: dict, out_root: Path, runner_root: Path | None = None) -> int:
    bf = BoardFiles(spec)
    bf.platform_registration(runner_root)
    v = bf.values()
    bf.check_layout(v)
    linker_path, linker_text = bf.linker()
    boot_keys, boot_files = bf.boot()
    files = {f"cert_harness/boards/{bf.board}.env": bf.preset(v, linker_path, boot_keys), **boot_files}
    if bf.registration_patch:
        files[f"cert_harness/boards/{bf.board}/runner_shared.registration.patch"] = bf.registration_patch
    if linker_text:
        files[linker_path] = linker_text
    for rel, text in files.items():
        path = out_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if rel.endswith(".sh"):
            path.chmod(0o755)
    report = {"board": bf.board, "boot_pattern": bf.pattern, "files": sorted(files),
              "problems": bf.problems, "manual_steps": bf.manual, "notes": bf.notes}
    rpath = out_root / "cert_harness" / "boards" / bf.board / "runner_report.yaml"
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(yaml.safe_dump(report, sort_keys=False, width=200))
    print(f"{bf.board}: {len(files)} files, boot={bf.pattern}, problems={len(bf.problems)}, "
          f"manual={len(bf.manual)}")
    for line in bf.problems + bf.manual:
        print(f"  - {line}")
    return 2 if bf.problems or bf.manual else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("spec", type=Path, help="board spec YAML (assets/board_specs/<board>.yaml)")
    p.add_argument("--out-root", type=Path, required=True, help="runner checkout or output directory")
    p.add_argument("--runner-root", type=Path, help="runner checkout to check platform registration "
                   "(defaults to --out-root)")
    args = p.parse_args()
    try:
        spec = yaml.safe_load(args.spec.read_text())
        return generate(spec, args.out_root, args.runner_root or args.out_root)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
