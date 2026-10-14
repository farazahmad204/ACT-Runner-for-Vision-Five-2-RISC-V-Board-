"""A scripted stand-in for the ReAct agent's model, for tests of the CI integration."""

from __future__ import annotations

import json

from ci.triage.agentic import react

FINAL = {"analysis_status": "COMPLETED", "failure_classification": "Test_Suite_Error",
         "symptom_isolation": "x", "identified_root_cause": "trap delegation differs",
         "reproduction_steps_or_fix": "z"}


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
        backend = Scripted([*self.turns, react.Turn("conclude", [react.ToolCall("submit_analysis", self.final)])])
        self.backends.append(backend)
        return backend

    @property
    def calls(self):
        return len(self.backends)


def dumps(value):
    return json.dumps(value)
