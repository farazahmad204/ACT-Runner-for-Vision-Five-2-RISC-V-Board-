#!/usr/bin/env python3
"""Unit tests for the offline board-onboarding workflow."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from onboard import inspect_hypervisor_source, status_label, write_draft_scaffold  # noqa: E402
from validate_manifest import load_manifest, validate_manifest  # noqa: E402


class OnboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest_path = SCRIPT_DIR.parent / "assets" / "manifests" / "milkv_megrez.yaml"
        cls.manifest = load_manifest(cls.manifest_path)

    def test_megrez_manifest_has_complete_minimal_offline_inputs(self) -> None:
        errors, _, state, blockers, missing = validate_manifest(self.manifest)
        self.assertEqual(errors, [])
        # The real Megrez manifest has moved past offline_prepared on hardware.
        self.assertIn(state, ("offline_prepared", "hardware_smoke_passed", "hypervisor_smoke_passed", "ci_operational"))
        self.assertTrue(blockers)
        self.assertEqual(missing, [])
        self.assertNotIn("memory.usable_limit", missing)
        self.assertNotIn("memory.staging_address", missing)
        self.assertNotIn("boot.image_format", missing)

    def test_hardware_state_cannot_be_promoted_without_evidence(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["board"]["physical_available"] = False
        manifest["uart"]["bidirectional_validated"] = False
        manifest["readiness"]["state"] = "hardware_smoke_passed"
        errors, _, _, _, _ = validate_manifest(manifest)
        self.assertTrue(any("physical_available" in error for error in errors))
        self.assertTrue(any("bidirectional_validated" in error for error in errors))

    def test_hypervisor_checkout_detection_requires_tsbi_and_suites(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            trap = root / "tests" / "env" / "rvtest_trap_handler.h"
            trap.parent.mkdir(parents=True)
            trap.write_text("TSBI_GOTO_VSMODE\nTSBI_GOTO_VUMODE\n", encoding="utf-8")
            for suite in ("H", "ExceptionsH", "ExceptionsHV", "InterruptsH", "PMPH", "SvH"):
                (root / "tests" / "priv" / suite).mkdir(parents=True)
            state, check = inspect_hypervisor_source(root)
            self.assertEqual(check.status, "pass")
            self.assertTrue(state["tsbi_mode_transitions"])

    def test_draft_scaffold_uses_proven_values_and_keeps_unknowns_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            files = write_draft_scaffold(self.manifest, Path(temp_dir))
            env_text = Path(files[0]).read_text(encoding="utf-8")
            adapter_text = Path(files[1]).read_text(encoding="utf-8")
            self.assertIn("BOARD_FW_LOAD_ADDR=0x80000000", env_text)
            self.assertIn("BOARD_RAM_LIMIT=0xc8000000", env_text)
            self.assertIn("BOARD_PAYLOAD_BASE=0x90000000", env_text)
            self.assertIn("BOARD_PAYLOAD_LIMIT=0xb0000000", env_text)
            self.assertIn("BOARD_DDR_EXCLUDE_BASE=0xb0000000", env_text)
            self.assertIn("BOARD_DDR_EXCLUDE_LIMIT=0xc0000000", env_text)
            self.assertIn("BOARD_EXT_PACK_ADDR=0xc0000000", env_text)
            self.assertIn("BOARD_EXT_PACK_MAX_BYTES=0x8000000", env_text)
            self.assertIn("BOARD_UART_BASE=0x50900000", env_text)
            self.assertIn("BLOCKED:", adapter_text)

    def test_status_prioritizes_errors_then_blockers(self) -> None:
        from onboard import Check

        self.assertEqual(status_label([Check("x", "blocked", "x")]), "blocked")
        self.assertEqual(
            status_label([Check("x", "blocked", "x"), Check("y", "error", "y")]),
            "invalid",
        )


if __name__ == "__main__":
    unittest.main()
