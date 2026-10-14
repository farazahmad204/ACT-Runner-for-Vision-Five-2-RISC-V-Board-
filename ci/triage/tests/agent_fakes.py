"""A scripted stand-in for the ReAct agent's model, for tests of the CI integration."""

from __future__ import annotations

import json

from ci.triage.agentic import react

FINAL = {"triage_status": "SUCCESS", "target_board": "vf2_jh7110", "failure_classification": "Test_Suite_Error",
         "tier_1_root_cause": {"failing_pc": "0x80000a3c", "failing_instruction": "lw a0,1(zero)",
                               "architectural_symptom": "trap delegation differs", "divergence_cycle": "not traceable"},
         "tier_2_board_specific_remediation": {"remediation_type": "CONFIG_REMAP",
                                               "target_repository_or_file": "tests/priv/x.S",
                                               "actionable_fix_directive": "Relax the check."}}


class Scripted:
    def __init__(self, turns):
        self.turns, self.observations, self.user = list(turns), [], ""

    def start(self, system, user):
        self.system, self.user = system, user

    def next(self, force_final):
        if self.turns:
            return self.turns.pop(0)
        return react.Turn("done", [react.ToolCall("submit_analysis", FINAL)])

    def observe(self, call, text):
        self.observations.append((call.name, text))


class Factory:
    """agent_backend for run_triage: a fresh scripted model per failure signature."""

    def __init__(self, final=None, turns=()):
        self.final, self.turns, self.backends = final or FINAL, list(turns), []

    def __call__(self, state):
        # The report names the board being triaged (the agent reads it from the profile).
        board = (state.data.get("inputs") or {}).get("board") or self.final["target_board"]
        final = {**self.final, "target_board": board}
        backend = Scripted([*self.turns, react.Turn("conclude", [react.ToolCall("submit_analysis", final)])])
        self.backends.append(backend)
        return backend

    @property
    def calls(self):
        return len(self.backends)


def dumps(value):
    return json.dumps(value)
