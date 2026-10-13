#!/usr/bin/env python3
"""Tests for collect_board_facts.py and csr_probe.py decode (synthetic inputs)."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import collect_board_facts as cbf  # noqa: E402

DTS = """
/dts-v1/;
/ {
\t#address-cells = <0x02>;
\t#size-cells = <0x02>;
\tmodel = "Test Board";
\tcpus {
\t\t#address-cells = <0x01>;
\t\t#size-cells = <0x00>;
\t\ttimebase-frequency = <0xf4240>;
\t\tcpu@0 {
\t\t\tdevice_type = "cpu";
\t\t\treg = <0x00>;
\t\t\triscv,isa = "rv64imafdc_zba";
\t\t\tmmu-type = "riscv,sv39";
\t\t\tinterrupt-controller {
\t\t\t\tcompatible = "riscv,cpu-intc";
\t\t\t\tphandle = <0x0c>;
\t\t\t};
\t\t};
\t\tcpu@1 {
\t\t\tdevice_type = "cpu";
\t\t\treg = <0x01>;
\t\t\tinterrupt-controller {
\t\t\t\tcompatible = "riscv,cpu-intc";
\t\t\t\tphandle = <0x0d>;
\t\t\t};
\t\t};
\t};
\tmemory@1000000 {
\t\tdevice_type = "memory";
\t\treg = <0x00 0x1000000 0x00 0x100000>;
\t};
\tmemory@80000000 {
\t\tdevice_type = "memory";
\t\treg = <0x00 0x80000000 0x01 0x00>;
\t};
\treserved-memory {
\t\t#address-cells = <0x02>;
\t\t#size-cells = <0x02>;
\t\tfw@80000000 {
\t\t\treg = <0x00 0x80000000 0x00 0x80000>;
\t\t\tno-map;
\t\t};
\t};
\tsoc {
\t\t#address-cells = <0x02>;
\t\t#size-cells = <0x02>;
\t\tplic@c000000 {
\t\t\tcompatible = "sifive,plic-1.0.0";
\t\t\treg = <0x00 0xc000000 0x00 0x4000000>;
\t\t\triscv,ndev = <0x40>;
\t\t\tinterrupts-extended = <0x0c 0xffffffff 0x0c 0x09 0x0d 0x0b 0x0d 0x09>;
\t\t\tphandle = <0x10>;
\t\t};
\t\tclint@2000000 {
\t\t\tcompatible = "sifive,clint0";
\t\t\treg = <0x00 0x2000000 0x00 0x10000>;
\t\t};
\t\tserial@10000000 {
\t\t\tcompatible = "snps,dw-apb-uart";
\t\t\treg = <0x00 0x10000000 0x00 0x10000>;
\t\t\tclock-frequency = <0x16e3600>;
\t\t\tinterrupts = <0x20>;
\t\t\treg-shift = <0x02>;
\t\t\treg-io-width = <0x04>;
\t\t};
\t};
\taliases {
\t\tserial0 = "/soc/serial@10000000";
\t};
\tchosen {
\t\tstdout-path = "serial0:115200n8";
\t};
};
"""

OPENSBI = """
Platform Name             : Test Board
Platform HART Count       : 2
Platform Timer Device     : aclint-mtimer @ 1000000Hz
Platform Console Device   : uart8250
Firmware Base             : 0x80000000
Firmware Size             : 300 KB
Domain0 Region00          : 0x0000000002000000-0x000000000200ffff M: (I,R,W) S/U: ()
Domain0 Region01          : 0x0000000080000000-0x000000008007ffff M: (R,W) S/U: ()
Boot HART ID              : 1
Boot HART Base ISA        : rv64imafdcx
Boot HART Priv Version    : v1.11
Boot HART PMP Count       : 8
Boot HART PMP Granularity : 12 bits
Boot HART PMP Address Bits: 39
Boot HART MHPM Info       : 4 (0x00000078)
"""

LINUX = """
[    0.000000] Machine model: Test Board
[    0.000000] earlycon: uart0 at MMIO32 0x0000000010000000 (options '115200n8')
[    0.000000] OF: reserved mem: 0x0000000080000000..0x000000008007ffff (512 KiB) nomap non-reusable fw@80000000
[    0.000000] riscv: base ISA extensions acdfim
[    0.076450] ASID allocator disabled (0 bits)
"""


class CollectBoardFactsTests(unittest.TestCase):
    def facts(self, runner_hart=1):
        raw = cbf.from_dts(cbf.parse_dts(DTS), "test.dts", runner_hart)
        raw += cbf.from_opensbi(OPENSBI, "sbi.log") + cbf.from_linux(LINUX, "linux.log")
        merged, conflicts = cbf.merge(raw)
        return merged, conflicts

    def test_uart_and_interrupt_routing(self):
        f, conflicts = self.facts()
        self.assertEqual(conflicts, [])
        self.assertEqual(f["uart.base"]["value"], "0x10000000")
        self.assertEqual(f["uart.register_width"]["value"], 4)
        self.assertEqual(f["uart.register_spacing"]["value"], 2)
        self.assertEqual(f["interrupt.uart_source"]["value"], 32)
        self.assertEqual(f["interrupt.plic_base"]["value"], "0xc000000")
        self.assertEqual(f["interrupt.runner_contexts"]["value"], {"M": 2, "S": 3})

    def test_memory_and_cpu(self):
        f, _ = self.facts()
        self.assertEqual(f["memory.dram_base"]["value"], "0x80000000")  # largest node, not the SRAM
        self.assertEqual(len(f["memory.reserved_regions"]["value"]), 1)  # DTB + Linux merged by base
        self.assertEqual(f["cpu.hart_ids"]["value"], [0, 1])
        self.assertEqual(f["cpu.phys_addr_bits"]["value"], 41)
        self.assertEqual(f["cpu.asid_bits"]["value"], 0)
        self.assertEqual(f["identity.priv_version"]["value"], "v1.11")
        self.assertEqual(f["timer.mswi_base"]["value"], "0x2000000")
        self.assertEqual(f["timer.mtime_address"]["value"], "0x200bff8")

    def test_dtb_and_log_disagreement_is_a_conflict(self):
        raw = cbf.from_dts(cbf.parse_dts(DTS), "test.dts", 1)
        raw += cbf.from_opensbi(OPENSBI.replace("1000000Hz", "4000000Hz"), "sbi.log")  # DTB 1 MHz vs 4 MHz
        _, conflicts = cbf.merge(raw)
        self.assertEqual([c["id"] for c in conflicts], ["timer.mtime_hz"])

    def test_cli_reports_unknowns_and_uart_owner(self):
        with tempfile.TemporaryDirectory() as d:
            dts = Path(d) / "b.dts"; dts.write_text(DTS)
            sbi = Path(d) / "sbi.log"; sbi.write_text(OPENSBI)
            out = Path(d) / "facts.yaml"
            subprocess.run([sys.executable, str(SCRIPT_DIR / "collect_board_facts.py"), "--dts", str(dts),
                            "--opensbi-log", str(sbi), "--runner-hart", "1", "--output", str(out)], check=True,
                           capture_output=True)
            report = yaml.safe_load(out.read_text())
        self.assertIn("runner.hart_entry", report["still_unknown"])
        self.assertIn("must initialize the UART", report["facts"]["uart.initialization_owner"]["value"])


class OpenSbiLogTests(unittest.TestCase):
    def test_indented_web_copy_and_domain_harts(self):
        # A boot log copied from a web page: every line indented, blank lines between.
        log = ("\n Platform HART Count : 4 \n\n Domain0 HARTs : 0*,1*,2*,3* \n\n"
               " Domain0 Region00 : 0x0000000080000000-0x000000008007ffff M: (R,W,X) S/U: () \n"
               " Boot HART ID : 2 \n Boot HART Priv Version : v1.11 \n")
        facts = {f.id: f for f in cbf.from_opensbi(log, "log")}
        self.assertEqual(facts["runner.stock_firmware_harts"].value, [0, 1, 2, 3])
        self.assertEqual(facts["runner.stock_firmware_harts"].evidence_status, "inferred")
        self.assertEqual(facts["identity.priv_version"].value, "v1.11")
        self.assertEqual(facts["runner.observed_boot_hart"].value, 2)
        self.assertIn("memory.firmware_window", facts)


class CsrProbeDecodeTests(unittest.TestCase):
    def test_decode_findings(self):
        import csr_probe
        base = 0x90001040
        lines = [f"[SIG] begin=0x{base:016x} end=0x{base + 1024:016x} bytes=0x400"]
        values = {0: (0x444, 0), 22: (0xFFFFFFFFFFFF, 0), 26: (0, 3), 36: (0, 6)}
        for i in range(len(csr_probe.SLOTS)):
            v, t = values.get(i, (0, 0))
            lines += [f"[SIGQ] 0x{base + 16 * i:016x} : 0x{v:016x}", f"[SIGQ] 0x{base + 16 * i + 8:016x} : 0x{t:016x}"]
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "uart.log"; log.write_text("\n".join(lines))
            out = Path(d) / "probe.json"
            r = subprocess.run([sys.executable, str(SCRIPT_DIR / "csr_probe.py"), "decode", str(log),
                                "--json", str(out)], capture_output=True, text=True, check=True)
        self.assertIn("clear H state", r.stdout)
        self.assertIn("mhpmcounter3 width = 48 bits", r.stdout)
        self.assertIn("time: traps (illegal instruction)", r.stdout)
        self.assertIn("address 0 faults (load access fault)", r.stdout)


if __name__ == "__main__":
    unittest.main()
