from __future__ import annotations

import tempfile
import unittest
import zlib
from pathlib import Path

from ci.triage import validate_context
from ci.triage.ci_triage import stale

ELF = b"\x7fELF" + b"\x00" * 60


def run_dir(root: Path) -> Path:
    (root / "per_case" / "Sm-00").mkdir(parents=True)
    (root / "per_case" / "Sm-00" / "test.elf").write_bytes(ELF)
    (root / "act").mkdir()
    (root / "act" / "revision.txt").write_text("act_commit=abc123\nconfig_dir=config/x\nsail_version=0.15\n")
    return root


def log(size: int, crc: int, board: str = "vendor_x1") -> str:
    return (f"[UART_STREAM] READY version=1 board={board} runner_build=x1-v2\n"
            f"[UART_STREAM] HEADER_OK name=Sm-00 size={size} crc32=0x{crc:016x}\n")


class ValidateContextTests(unittest.TestCase):
    def test_matching_elf_and_board(self):
        with tempfile.TemporaryDirectory() as d:
            found = validate_context.check(run_dir(Path(d)), "Sm-00", "vendor_x1",
                                           log(len(ELF), zlib.crc32(ELF)), {"spec_versions": {"priv": "priv-1.11"}})
        self.assertTrue(found["checks"]["elf_matches"])
        self.assertTrue(found["checks"]["board_matches"])
        self.assertEqual(found["revisions"]["act_commit"], "abc123")
        self.assertEqual(found["revisions"]["runner_build"], "x1-v2")
        self.assertEqual(found["revisions"]["spec_versions"], "priv=priv-1.11")
        self.assertTrue(found["items"][0].startswith("OK: the kept test.elf"))

    def test_a_different_elf_or_board_is_a_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            found = validate_context.check(run_dir(Path(d)), "Sm-00", "vendor_x1", log(len(ELF), 1, "other_board"))
        self.assertFalse(found["checks"]["elf_matches"])
        self.assertFalse(found["checks"]["board_matches"])
        self.assertTrue(any(i.startswith("MISMATCH: the kept test.elf") for i in found["items"]))

    def test_ai_answers_are_reused_only_for_the_same_revisions(self):
        current = {"act_commit": "abc", "sail_version": "0.15", "spec_versions": "priv=priv-1.11"}
        self.assertFalse(stale({"act_commit": "abc"}, current))
        self.assertTrue(stale({"act_commit": "old"}, current))
        self.assertFalse(stale({}, current))  # stored before revisions were recorded


if __name__ == "__main__":
    unittest.main()
