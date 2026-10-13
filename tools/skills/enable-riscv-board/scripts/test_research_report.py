#!/usr/bin/env python3
"""Unit tests for independent evidence reports."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from validate_research_report import RECOMMENDED_ROLE_FACT_IDS, ROLE_FACT_IDS, validate  # noqa: E402


class ResearchReportTests(unittest.TestCase):
    def test_valid_report(self) -> None:
        report = {
            "schema_version": 2,
            "board_id": "example",
            "agent_role": "boot_image",
            "source_revisions": [{
                "name": "BSP",
                "url_or_path": "/tmp/bsp",
                "revision": "abc",
                "source_type": "vendor_source",
            }],
            "evidence": [
                {
                    "id": fact_id,
                    "claim": fact_id,
                    "value": "documented-value",
                    "source": "BSP/config",
                    "revision": "abc",
                    "evidence_status": "confirmed",
                    "impact": "Board port input",
                    "open_question": None,
                }
                for fact_id in sorted(ROLE_FACT_IDS["boot_image"])
            ],
            "blockers": [],
            "conflicts": [],
            "recommendations": [],
        }
        self.assertEqual(validate(report), [])

    def test_missing_source_is_rejected(self) -> None:
        report = {
            "schema_version": 2,
            "board_id": "example",
            "agent_role": "ddr_placement",
            "source_revisions": [],
            "evidence": [],
            "blockers": [],
            "conflicts": [],
            "recommendations": [],
        }
        errors = validate(report)
        self.assertTrue(any("source_revisions" in error for error in errors))
        self.assertTrue(any("evidence" in error for error in errors))

    def test_out_of_scope_role_and_fact_are_rejected(self) -> None:
        report = {
            "schema_version": 2,
            "board_id": "example",
            "agent_role": "ci_integration",
            "source_revisions": [{
                "name": "Jenkins",
                "url_or_path": "ci/Jenkinsfile",
                "revision": "abc",
                "source_type": "vendor_source",
            }],
            "evidence": [{
                "id": "ci.job",
                "claim": "Jenkins job",
                "value": "example-weekly",
                "source": "ci/Jenkinsfile",
                "revision": "abc",
                "evidence_status": "confirmed",
                "impact": "CI",
                "open_question": None,
            }],
            "blockers": [],
            "conflicts": [],
            "recommendations": [],
        }
        errors = validate(report)
        self.assertTrue(any("agent_role" in error for error in errors))

    def test_fact_from_another_allowed_role_is_rejected(self) -> None:
        report = {
            "schema_version": 2,
            "board_id": "example",
            "agent_role": "boot_image",
            "source_revisions": [{
                "name": "TRM",
                "url_or_path": "https://example.invalid/trm",
                "revision": "1.0",
                "source_type": "vendor_documentation",
            }],
            "evidence": [{
                "id": "uart.base",
                "claim": "UART base",
                "value": "0x10000000",
                "source": "TRM chapter 4",
                "revision": "1.0",
                "evidence_status": "confirmed",
                "impact": "UART",
                "open_question": None,
            }],
            "blockers": [],
            "conflicts": [],
            "recommendations": [],
        }
        errors = validate(report)
        self.assertTrue(any("outside" in error for error in errors))

    def test_incomplete_role_report_is_rejected(self) -> None:
        report = {
            "schema_version": 2,
            "board_id": "example",
            "agent_role": "runner_contract",
            "source_revisions": [{
                "name": "TRM",
                "url_or_path": "https://example.invalid/trm",
                "revision": "1.0",
                "source_type": "vendor_documentation",
            }],
            "evidence": [{
                "id": "uart.base",
                "claim": "UART base",
                "value": "0x10000000",
                "source": "TRM chapter 4",
                "revision": "1.0",
                "evidence_status": "confirmed",
                "impact": "UART access",
                "open_question": None,
            }],
            "blockers": [],
            "conflicts": [],
            "recommendations": [],
        }
        errors = validate(report)
        self.assertTrue(any("missing required" in error for error in errors))

    def test_structured_recommendation_is_rejected(self) -> None:
        report = {
            "schema_version": 2,
            "board_id": "example",
            "agent_role": "boot_image",
            "source_revisions": [{
                "name": "BSP",
                "url_or_path": "/tmp/bsp",
                "revision": "abc",
                "source_type": "vendor_source",
            }],
            "evidence": [
                {
                    "id": fact_id,
                    "claim": fact_id,
                    "value": None,
                    "source": "/tmp/bsp",
                    "revision": "abc",
                    "evidence_status": "unknown",
                    "impact": "Board port input",
                    "open_question": "Not documented",
                }
                for fact_id in sorted(ROLE_FACT_IDS["boot_image"])
            ],
            "blockers": [],
            "conflicts": [],
            "recommendations": [{"gate": "test"}],
        }
        errors = validate(report)
        self.assertTrue(any("recommendations[0]" in error for error in errors))

    def test_recommended_fact_is_accepted_only_for_its_role(self) -> None:
        def report(extra_id):
            ids = sorted(ROLE_FACT_IDS["runner_contract"]) + [extra_id]
            return {
                "schema_version": 2, "board_id": "example", "agent_role": "runner_contract",
                "source_revisions": [{"name": "DTB", "url_or_path": "/tmp/b.dtb", "revision": "r1",
                                      "source_type": "vendor_source"}],
                "evidence": [{"id": i, "claim": i, "value": "v", "source": "DTB", "revision": "r1",
                              "evidence_status": "confirmed", "impact": "port input", "open_question": None}
                             for i in ids],
                "blockers": [], "conflicts": [], "recommendations": [],
            }
        self.assertIn("uart.host_bridge", RECOMMENDED_ROLE_FACT_IDS["runner_contract"])
        self.assertEqual(validate(report("uart.host_bridge")), [])
        self.assertEqual(validate(report("uart.input_clock_hz")), [])  # catalog ID owned by this role
        self.assertTrue(any("outside" in e for e in validate(report("boot.load_address"))))  # boot_image owns it
        errors = validate(report("memory.firmware_window"))
        self.assertTrue(any("outside the 'runner_contract'" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
