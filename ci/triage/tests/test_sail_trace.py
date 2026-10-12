from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci.triage import sail_trace

TRACE = """\
mem[X,0x0000000090000160] -> 0x0713
[5] [HS]: 0x0000000090000160 (0xFFF70713) addi a4, a4, -0x1     rvtest_code_begin+96
a4 <- 0x0000000000000000
CSR mip (0x344) <- 0x0000000000000222
mem[W,0x000000000C000004] <- 0x80000002
mem[W,0x0000000090008000] <- 0x1
[6] [HS]: 0x0000000090000164 (0xFE071EE3) bne a4, zero, -0x4    rvtest_code_begin+100
[7] [HS]: 0x0000000090000168 (0x144026F3) csrrs a3, sip, zero   Check_cg_cp_x_bin+0
CSR sip (0x144) -> 0x0000000000002222
a3 <- 0x0000000000002222
[8] [HS]: 0x000000009000016C (0x00001737) lui a4, 0x1           Check_cg_cp_x_bin+4
"""


class SailTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.board, self.sig, self.config = root / "test.elf", root / "test.sig.elf", root / "sail.json"
        for path in (self.board, self.sig, self.config):
            path.write_text("x")
        sail_trace._symbols.cache_clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _symbols(self, elf):
        if elf == str(self.board):
            return [(0x90000000, "rvtest_entry_point"), (0x90000168, "Check_cg_cp_x_bin")]
        return [(0x90000000, "rvtest_entry_point"), (0x90000168, "Check_cg_cp_x_bin")]

    def _trace(self, sig_elf, config, work):
        path = work / "trace.txt"
        path.write_text(TRACE)
        return path

    def test_the_reference_result_and_its_setup_at_the_failing_instruction(self):
        with mock.patch.object(sail_trace, "_symbols", side_effect=self._symbols), \
                mock.patch.object(sail_trace, "_run_sail", side_effect=self._trace):
            found = sail_trace.reference(self.board, self.sig, self.config, 0x90000168, ["sip"])
        self.assertTrue(found["executed"])
        self.assertEqual(found["label"], "Check_cg_cp_x_bin+0x0")
        self.assertEqual(found["result"], ["CSR sip (0x144) -> 0x0000000000002222",
                                           "a3 <- 0x0000000000002222"])
        # mip is sip's alias; 0x0C000004 is a device store; the RAM store is not setup.
        self.assertEqual(found["setup"], ["CSR mip (0x344) <- 0x0000000000000222",
                                          "mem[W,0x000000000C000004] <- 0x80000002"])
        self.assertFalse(any(line.startswith("mem[X,") for line in found["window"]))
        self.assertIn("Sail's result: CSR sip (0x144) -> 0x0000000000002222", sail_trace.render(found))

    def test_a_virtual_pc_keeps_its_high_bits(self):
        with mock.patch.object(sail_trace, "_symbols", side_effect=lambda elf: [
                (0x90000000, "a"), (0x90000300, "store_check")] if elf == str(self.board)
                else [(0x80000000, "a"), (0x80000400, "store_check")]):
            self.assertEqual(sail_trace.map_pc(self.board, self.sig, 0x0000030090000398),
                             (0x0000030080000498, "store_check+0x98"))

    def test_an_instruction_sail_never_runs_is_reported_as_such(self):
        with mock.patch.object(sail_trace, "_symbols", side_effect=self._symbols), \
                mock.patch.object(sail_trace, "_run_sail", side_effect=self._trace):
            found = sail_trace.reference(self.board, self.sig, self.config, 0x90000170)
        self.assertFalse(found["executed"])
        self.assertIn("never executed", sail_trace.render(found))

    def test_without_sail_nothing_is_added(self):
        with mock.patch.dict("os.environ", {"TRIAGE_SAIL": "off"}):
            self.assertIsNone(sail_trace.sail_binary())


if __name__ == "__main__":
    unittest.main()
