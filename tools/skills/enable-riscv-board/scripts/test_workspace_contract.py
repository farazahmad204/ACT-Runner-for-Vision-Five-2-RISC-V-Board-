#!/usr/bin/env python3
"""Tests for the canonical runner workspace contract."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from validate_workspace_contract import load_contract, normalize_git_url  # noqa: E402


class WorkspaceContractTests(unittest.TestCase):
    def test_ssh_and_https_github_urls_normalize_equally(self) -> None:
        ssh = "git@github.com:farazahmad-10xe/ACT-Runner-for-Vision-Five-2-RISC-V-Board-.git"
        https = "https://github.com/farazahmad-10xe/ACT-Runner-for-Vision-Five-2-RISC-V-Board-.git"
        self.assertEqual(normalize_git_url(ssh), normalize_git_url(https))

    def test_contract_declares_stable_build_interface(self) -> None:
        contract_path = SCRIPT_DIR.parent / "assets" / "system-contract.yaml"
        data = load_contract(contract_path)
        runner = data["repositories"]["runner"]
        self.assertEqual(runner["stable_branch"], "vf2-uart-stream-jenkins")
        self.assertEqual(runner["production_profile"], "UART_M_MODE")
        self.assertEqual(runner["build_entrypoint"], "cert_harness/tools/build_runner.sh")

    def test_contract_is_valid_yaml_mapping(self) -> None:
        contract_path = SCRIPT_DIR.parent / "assets" / "system-contract.yaml"
        data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertEqual(data["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
