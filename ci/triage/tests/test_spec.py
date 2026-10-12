from __future__ import annotations

import json
import unittest

from ci.triage import spec
from ci.triage.agent import check_answer, load_knowledge
from ci.triage.ci_triage import format_ai_report

AMO = {"instruction": {"at": "0x80000714: 00f6a02f  amoadd.w zero,a5,(a3)"}}


class SpecIndexTests(unittest.TestCase):
    def test_every_board_names_versions_that_exist(self):
        for board in ("vf2_jh7110", "bpif3_k1", "milkv_megrez_eic7700x"):
            versions = load_knowledge(board)["spec_versions"]
            for role in ("priv", "unpriv", "hypervisor"):
                if role in versions:
                    self.assertTrue(spec.label(versions[role]).startswith(("Privileged", "Unprivileged",
                                                                           "Hypervisor")), versions[role])

    def test_vf2_misaligned_amo_is_judged_against_priv_1_11_with_current_text_for_comparison(self):
        terms = spec.failure_terms({"xcause": "0x6"}, {}, AMO, "ExceptionsSvZaamo_sv39_exceptions_Zaamo_Smode-00")
        self.assertIn("misaligned", terms)
        found = spec.ground(terms, load_knowledge("vf2_jh7110"))
        first = found["applicable"][0]
        self.assertEqual(first["version"], "priv-1.11")
        self.assertIn("Store/AMO address misaligned", first["text"])
        self.assertIn("Store/AMO page fault", first["text"])
        self.assertEqual(found["current"][0]["version"], "priv-current")
        self.assertIn("[priv-1.11]", spec.render(found))

    def test_megrez_hypervisor_failures_use_draft_0_6(self):
        found = spec.ground(["htinst", "mtinst", "transformed", "pseudoinstruction"],
                            load_knowledge("milkv_megrez_eic7700x"), ["H_trap_xtinst_guestpage"],
                            hypervisor=True)
        self.assertEqual(found["versions"]["hypervisor"], "h-0.6")
        self.assertEqual(found["applicable"][0]["version"], "h-0.6")
        self.assertIn("zero is not allowed", found["rules"][0]["text"])

    def test_quotes_are_checked_against_the_named_version(self):
        self.assertTrue(spec.verify_quote("priv-1.11", "The priority of any custom synchronous exceptions "
                                                       "is implementation-defined."))
        # 'Optionally' before translation only exists from privileged 1.12 on.
        self.assertFalse(spec.verify_quote("priv-1.11", "Optionally: ; | 4, 6 | Load/store/AMO address misaligned"))
        self.assertTrue(spec.verify_quote("priv-1.12", "Optionally: ; | 4, 6 | Load/store/AMO address misaligned"))
        self.assertIsNone(spec.verify_quote("priv-1.11", "short"))
        self.assertIsNone(spec.verify_quote("no-such-version", "a long enough quote to check here"))


class AnswerQuoteTests(unittest.TestCase):
    REP = {"extracted": {"xcause": "0x6", "expected_value": "0x1"}}

    def test_an_invented_quote_loses_confidence_and_is_flagged(self):
        answer = check_answer({"root_cause": "test too strict", "confidence": "high",
                               "spec_version": "priv-1.11",
                               "spec_quote": "misaligned exceptions may have lower priority than page faults"},
                              self.REP, {"priv": "priv-1.11"})
        self.assertEqual(answer["confidence"], "medium")
        self.assertEqual(answer["spec_check"], "not_found")
        self.assertIn("QUOTE NOT FOUND", format_ai_report(json.dumps(answer)))

    def test_a_hardware_bug_citing_a_version_the_board_does_not_implement_is_low(self):
        answer = check_answer({"root_cause": "this is a u74 hardware bug", "confidence": "high",
                               "spec_version": "priv-1.12",
                               "spec_quote": "Optionally: ; | 4, 6 | Load/store/AMO address misaligned"},
                              self.REP, {"priv": "priv-1.11"})
        self.assertEqual(answer["spec_check"], "verified")
        self.assertEqual(answer["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
