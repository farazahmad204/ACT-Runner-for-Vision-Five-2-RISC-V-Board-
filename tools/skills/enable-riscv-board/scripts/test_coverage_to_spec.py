#!/usr/bin/env python3
"""Tests for coverage_to_spec.py (resolver coverage -> board spec draft)."""

from __future__ import annotations

import unittest

from coverage_to_spec import bridge


def rec(value, status="resolved", via="extract", **kw):
    return {"status": status, "value": value, "via": via, "source": "test", **kw}


BASE = {"runner": {"board_id": "b", "description": "d", "uart": {"size": 0x10000}},
        "boot": {"image": "x.bin"}, "dut": {"name": "d"}}


class BridgeTests(unittest.TestCase):
    def test_values_convert_and_layout_checks(self) -> None:
        cov = {"requirements": {
            "uart.base": rec("0x50900000"), "uart.size": rec(65535, via="manifest"),
            "uart.init_by_runner": rec("stage replaced by the runner: the runner must initialize the UART"),
            "interrupt.runner_m_context": rec({"M": 2, "S": 3}), "interrupt.runner_s_context": rec({"M": 2, "S": 3}),
            "memory.payload_window": rec({"base": 0x90000000, "limit": 0xB0000000}, via="manifest"),
            "memory.staging_buffer": rec({"base": 0xC0000000, "bytes": 0x8000000}),
            "boot.load_address": rec(0x80000000), "boot.image_format": rec("eswin_nsign_bootchain_bin"),
            "boot.firmware_window": rec([{"start": "0x80000000", "end": "0x8007ffff"}]),
            "timer.mswi_base": rec("0x2000000"), "timer.mtimecmp_base": rec("0x2004000"),
            "timer.mtime_address": rec("0x200bff8"),
            "platform.watchdog": rec("disabled (BOARD_WDT_ENABLE=0)", status="default", via="default"),
        }}
        spec, mapped, defaults, blockers, diffs = bridge(cov, BASE)
        self.assertEqual(blockers, [])
        self.assertEqual(spec["platform"]["uart"]["base"], 0x50900000)
        self.assertTrue(spec["runner"]["uart"]["init_8250"])
        self.assertEqual(spec["platform"]["plic"]["test_hart_contexts"], {"m": 2, "s": 3})
        self.assertEqual(spec["memory"], {"base": 0x90000000, "size": 0x20000000})
        self.assertEqual(spec["runner"]["staging"], {"address": 0xC0000000, "max_bytes": 0x8000000})
        self.assertEqual(spec["runner"]["firmware_window_limit"], 0x80080000)
        self.assertEqual(spec["boot"]["pattern"], "eswin_nsign")
        self.assertIsNone(spec["runner"]["watchdog"])
        self.assertEqual(len(defaults), 1)
        self.assertEqual(len(diffs), 1)                      # uart.size 0x10000 vs 0xffff
        self.assertIn("runner.uart.size", diffs[0])

    def test_conflicts_free_text_and_odd_clint_block(self) -> None:
        cov = {"requirements": {
            "uart.register_width": rec(4, status="conflict", conflicts=[{"value": 2, "source": "manifest"}]),
            "bench.console_path": rec("the onboard USB-C CH340", via="user"),
            "timer.mswi_base": rec(0xE4000000), "timer.mtime_address": rec(0xE4010000),
        }}
        spec, _, _, blockers, _ = bridge(cov, BASE)
        text = " ".join(blockers)
        self.assertIn("uart.register_width", text)
        self.assertIn("no /dev path", text)
        self.assertIn("standard SiFive CLINT layout", text)
        self.assertNotIn("access_bytes", spec["runner"].get("uart", {}))

    def test_identity_fields_required_without_base(self) -> None:
        _, _, _, blockers, _ = bridge({"requirements": {}}, None)
        self.assertTrue(any("runner.board_id" in b for b in blockers))


if __name__ == "__main__":
    unittest.main()
