from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci.triage.memory import TriageMemory


class TriageMemoryTests(unittest.TestCase):
    def test_person_verdict_overrides_ai_and_is_shared_by_signature(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = TriageMemory(Path(temporary) / "m.sqlite")
            memory.remember("megrez", "sig1", "H_trap-00", "ai", "trap_cause_mismatch",
                            {"root_cause": "draft H 0.6 htinst", "confidence": "medium"})
            memory.remember("megrez", "sig1", "H_trap-00", "person", "trap_cause_mismatch",
                            {"verdict": "Known deviation", "confirmed_by": "faraz"})
            same = memory.lookup("megrez", "sig1", "H_trap-00")
            other = memory.lookup("megrez", "sig1", "HSm_trap-00")
            memory.close()
        self.assertEqual(same["source"], "person")
        self.assertEqual(same["verdict"], "Known deviation")
        self.assertEqual(same["root_cause"], "draft H 0.6 htinst")  # AI text kept under the verdict
        self.assertEqual(other["match"], "same_signature")
        self.assertEqual(other["matched_test"], "H_trap-00")

    def test_boards_do_not_share_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = TriageMemory(Path(temporary) / "m.sqlite")
            memory.remember("megrez", "sig1", "t", "ai", "c", {"root_cause": "x"})
            self.assertIsNone(memory.lookup("vf2", "sig1", "t"))
            memory.close()


if __name__ == "__main__":
    unittest.main()
