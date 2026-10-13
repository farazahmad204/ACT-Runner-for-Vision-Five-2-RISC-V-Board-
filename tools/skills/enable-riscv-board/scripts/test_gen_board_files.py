#!/usr/bin/env python3
"""Tests for gen_board_files.py (runner preset, linker script, boot packaging)."""

from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

import yaml

import gen_board_files as gen

SPECS = Path(__file__).resolve().parent.parent / "assets" / "board_specs"
RUNNER_SHARED_H = """#define RUNNER_PLATFORM_GENERIC    0
#define RUNNER_PLATFORM_MILKV_MEGREZ_EIC7700X 3
#if BOARD_PLATFORM_ID == RUNNER_PLATFORM_MILKV_MEGREZ_EIC7700X
#define RUNNER_PLATFORM_NAME "milkv_megrez_eic7700x"
#else
#define RUNNER_PLATFORM_NAME "generic_riscv"
#endif
"""


def spec(name: str) -> dict:
    return yaml.safe_load((SPECS / f"{name}.yaml").read_text())


def preset(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


class BoardFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runner_shared.h").write_text(RUNNER_SHARED_H)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def report(self, board: str) -> dict:
        return yaml.safe_load((self.root / "cert_harness" / "boards" / board / "runner_report.yaml").read_text())

    def test_megrez_preset_linker_and_adapter(self) -> None:
        rc = gen.generate(spec("milkv_megrez"), self.root, self.root)
        self.assertEqual(rc, 0, self.report("milkv_megrez_eic7700x"))
        p = preset(self.root / "cert_harness" / "boards" / "milkv_megrez_eic7700x.env")
        self.assertEqual(p["BOARD_PAYLOAD_BASE"], "0x90000000")
        self.assertEqual(p["BOARD_PAYLOAD_LIMIT"], "0xB0000000")
        self.assertEqual(p["BOARD_UART_REG_IO_WIDTH"], "2")
        self.assertEqual(p["BOARD_EXTERNAL_IRQ_CLEANUP_ENABLE"], "0")
        self.assertEqual(p["BOARD_WDT_ENABLE"], "0")
        self.assertEqual(p["BOARD_CLINT_MTIME_ADDR"], "0x0200BFF8")
        self.assertEqual(p["BOARD_PACKAGE_MODE"], "adapter")
        self.assertEqual(p["BOARD_LINKER_SCRIPT"], "cert_harness/boards/milkv_megrez_eic7700x/link.ld")
        link = (self.root / "cert_harness" / "boards" / "milkv_megrez_eic7700x" / "link.ld").read_text()
        self.assertIn("ASSERT(__stack_top <= 0x80080000", link)
        adapter = (self.root / "cert_harness" / "board_adapters" / "milkv_megrez_eic7700x.sh").read_text()
        self.assertIn('"${MEGREZ_NSIGN_ROOT:-}"', adapter)
        self.assertIn("Megrez SPI flashing is not automated: it needs Recovery mode,", adapter)
        self.assertNotIn("{{", adapter)

    def test_vf2_keeps_production_values_and_flags_layout(self) -> None:
        rc = gen.generate(spec("visionfive2"), self.root, self.root)
        self.assertEqual(rc, 2)
        p = preset(self.root / "cert_harness" / "boards" / "vf2_jh7110.env")
        self.assertEqual(p["BOARD_WDT_ENABLE"], "1")
        self.assertEqual(p["BOARD_WDT_UNLOCK_KEY"], "0x1acce551")
        self.assertEqual((p["BOARD_UART_PLIC_SOURCE"], p["BOARD_RUNNER_M_PLIC_CONTEXT"]), ("32", "1"))
        self.assertEqual(p["BOARD_LINKER_SCRIPT"], "link.ld")
        self.assertEqual(p["BOARD_BOOT_IMAGE_PAD_BYTES"], "4194304")
        problems = " ".join(self.report("vf2_jh7110")["problems"])
        self.assertIn("payload_window overlaps staging_buffer", problems)
        self.assertIn("runner image", problems)
        its = (self.root / "vf2_act4.its").read_text()
        self.assertIn("load = <0x40000000>;", its)

    def test_bpif3_fit_two_cells_with_crc(self) -> None:
        gen.generate(spec("bananapi_f3"), self.root, self.root)
        its = (self.root / "bpif3_opensbi.its").read_text()
        self.assertIn("#address-cells = <2>;", its)
        self.assertIn("load = <0x00 0x04000000>;", its)
        self.assertIn('algo = "crc32";', its)
        p = preset(self.root / "cert_harness" / "boards" / "bpif3_k1.env")
        self.assertEqual(p["BOARD_K1_HART_WAKEUP_ENABLE"], "1")
        self.assertEqual(p["BOARD_RUNNER_HART_ID"], "0")

    def test_new_board_custom_pattern_lists_manual_steps(self) -> None:
        s = copy.deepcopy(spec("milkv_megrez"))
        s["runner"].update(board_id="acme_x1", platform_id=0)
        s["boot"] = {"pattern": "custom", "image": "acme_boot.bin", "slot": "BL31 slot"}
        rc = gen.generate(s, self.root, self.root)
        self.assertEqual(rc, 2)
        manual = " ".join(self.report("acme_x1")["manual_steps"])
        self.assertIn("runner_shared.registration.patch", manual)
        patch = (self.root / "cert_harness" / "boards" / "acme_x1" / "runner_shared.registration.patch").read_text()
        self.assertIn("+#define RUNNER_PLATFORM_ACME_X1 4", patch)
        self.assertIn('+#define RUNNER_PLATFORM_NAME "acme_x1"', patch)
        self.assertIn("BOARD_PLATFORM_ID=4", (self.root / "cert_harness" / "boards" / "acme_x1.env").read_text())
        self.assertIn("board_package_image", manual)
        adapter = (self.root / "cert_harness" / "board_adapters" / "acme_x1.sh").read_text()
        self.assertIn("NOT IMPLEMENTED", adapter)

    def test_staging_inside_payload_is_a_problem(self) -> None:
        s = copy.deepcopy(spec("milkv_megrez"))
        s["runner"]["staging"]["address"] = 0xA0000000
        self.assertEqual(gen.generate(s, self.root, self.root), 2)
        self.assertIn("payload_window overlaps staging_buffer", self.report("milkv_megrez_eic7700x")["problems"])

    def test_bad_board_id_rejected(self) -> None:
        s = copy.deepcopy(spec("milkv_megrez"))
        s["runner"]["board_id"] = "Bad-Id"
        with self.assertRaises(gen.SpecError):
            gen.generate(s, self.root, self.root)


if __name__ == "__main__":
    unittest.main()
