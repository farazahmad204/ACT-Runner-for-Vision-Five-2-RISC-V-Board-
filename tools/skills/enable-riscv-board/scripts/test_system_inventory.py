#!/usr/bin/env python3
"""Tests for the system inventory."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[3]
sys.path.insert(0, str(SCRIPT_DIR))

from system_inventory import DEFAULT_CONTRACT, build_inventory, load_policy  # noqa: E402


def write_contract(directory: Path, contract: dict) -> Path:
    path = directory / "system-contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return path


class SystemInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = yaml.safe_load(DEFAULT_CONTRACT.read_text(encoding="utf-8"))
        cls.data = build_inventory(REPO_ROOT)

    def test_existing_boards_are_discovered(self) -> None:
        boards = {board["id"]: board for board in self.data["boards"]}
        self.assertIn("vf2_jh7110", boards)
        self.assertIn("bpif3_k1", boards)
        self.assertTrue(boards["vf2_jh7110"]["packaging_ready"])
        self.assertTrue(boards["bpif3_k1"]["packaging_ready"])

    def test_uart_jobs_follow_stable_runner_policy(self) -> None:
        stable = self.contract["repositories"]["runner"]["stable_branch"]
        jobs = {pipeline["job"]: pipeline for pipeline in self.data["uart_pipelines"]}
        for name in (
            "vf2-uart-sanity",
            "vf2-uart-weekly",
            "bpif3-uart-sanity",
            "bpif3-uart-weekly",
            "riscv-uart-single-elf",
        ):
            self.assertEqual(jobs[name]["runner_branch"], stable)

    def test_policy_comes_from_system_contract(self) -> None:
        policy = self.data["policy"]
        self.assertEqual(policy["contract"], str(DEFAULT_CONTRACT))
        self.assertEqual(
            policy["stable_runner_branch"],
            self.contract["repositories"]["runner"]["stable_branch"],
        )
        self.assertEqual(
            policy["stable_act_branch"], self.contract["repositories"]["act"]["stable_branch"]
        )
        self.assertEqual(
            policy["required_sail_version"],
            self.contract["toolchain"]["required_sail_version"],
        )
        self.assertEqual(
            self.data["external_components"]["portal_repository"],
            self.contract["repositories"]["portal"]["canonical_url"].removesuffix(".git"),
        )

    def test_changed_contract_drives_policy_warnings(self) -> None:
        contract = yaml.safe_load(DEFAULT_CONTRACT.read_text(encoding="utf-8"))
        contract["repositories"]["runner"]["stable_branch"] = "other-runner-branch"
        contract["repositories"]["act"]["stable_branch"] = "other-act-branch"
        contract["toolchain"]["required_sail_version"] = "9.9.9"
        with tempfile.TemporaryDirectory() as directory:
            path = write_contract(Path(directory), contract)
            data = build_inventory(REPO_ROOT, load_policy(path))
        self.assertEqual(data["policy"]["contract"], str(path))
        self.assertEqual(data["policy"]["stable_runner_branch"], "other-runner-branch")
        warnings = "\n".join(data["warnings"])
        self.assertIn("vf2-uart-sanity runner default", warnings)
        self.assertIn("'other-runner-branch'", warnings)
        self.assertIn("'other-act-branch'", warnings)
        self.assertIn("requires '9.9.9'", warnings)

    def test_missing_contract_value_is_rejected(self) -> None:
        contract = yaml.safe_load(DEFAULT_CONTRACT.read_text(encoding="utf-8"))
        del contract["toolchain"]["required_sail_version"]
        with tempfile.TemporaryDirectory() as directory:
            path = write_contract(Path(directory), contract)
            with self.assertRaisesRegex(ValueError, "toolchain.required_sail_version"):
                load_policy(path)

    def test_unquoted_numeric_version_is_rejected(self) -> None:
        contract = yaml.safe_load(DEFAULT_CONTRACT.read_text(encoding="utf-8"))
        contract["toolchain"]["required_sail_version"] = "SAIL_VERSION_SENTINEL"
        with tempfile.TemporaryDirectory() as directory:
            path = write_contract(Path(directory), contract)
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("SAIL_VERSION_SENTINEL", "0.10"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "quote version numbers"):
                load_policy(path)

    def test_cli_reports_invalid_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_contract(Path(directory), {"schema_version": 2})
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "system_inventory.py"),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--contract",
                    str(path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("system_inventory=ERROR", result.stderr)

    def test_board_facts_come_from_presets(self) -> None:
        boards = {board["id"]: board for board in self.data["boards"]}
        self.assertEqual(boards["vf2_jh7110"]["uart_base"], "0x10000000")
        self.assertEqual(boards["bpif3_k1"]["uart_base"], "0xD4017000")


if __name__ == "__main__":
    unittest.main()
