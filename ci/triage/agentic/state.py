"""Short-term memory of one ReAct investigation (see CaseState)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CaseState:
    """Short-term memory of one agent investigation, saved to <out_dir>/agent_state.json after
    every step, so a run can be inspected or resumed and the agent cannot loop unnoticed.

    steps     every thought, tool call, arguments and (compacted) observation, in order
    findings  facts the agent established, each with the step that showed it
    cache     tool results by (tool, arguments): a repeated call is answered from here
    final     the validated final analysis, once there is one
    """

    def __init__(self, path: Path, case: str = "", inputs: dict[str, Any] | None = None):
        self.path = path
        self.data: dict[str, Any] = {"case": case, "inputs": inputs or {}, "status": "running",
                                     "steps": [], "findings": [], "cache": {}, "repeats": 0, "final": None}
        if path.is_file():
            self.data.update(json.loads(path.read_text()))

    # persistence -------------------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True, default=str))
        tmp.replace(self.path)

    # tool calls --------------------------------------------------------------
    @staticmethod
    def key(tool: str, arguments: dict[str, Any]) -> str:
        return tool + " " + json.dumps(arguments, sort_keys=True)

    def cached(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return self.data["cache"].get(self.key(tool, arguments))

    def record(self, thought: str, tool: str, arguments: dict[str, Any], result: dict[str, Any],
               observation: str, repeated: bool = False) -> int:
        step = len(self.data["steps"]) + 1
        self.data["cache"].setdefault(self.key(tool, arguments), result)
        if repeated:
            self.data["repeats"] += 1
        self.data["steps"].append({"step": step, "thought": thought[:2000], "tool": tool, "arguments": arguments,
                                   "ok": bool(result.get("ok")), "repeated": repeated,
                                   "observation": observation})
        self.save()
        return step

    def add_finding(self, text: str, step: int | None = None) -> None:
        text = text.strip()
        if text and all(f["text"] != text for f in self.data["findings"]):
            self.data["findings"].append({"text": text[:500], "step": step or len(self.data["steps"])})
            self.save()

    def finish(self, final: dict[str, Any], status: str = "completed") -> None:
        self.data.update(final=final, status=status)
        self.save()

    # what the model is shown -------------------------------------------------
    def history(self, keep_full: int = 3, short: int = 400) -> str:
        """The investigation so far: findings, then every step; only the last `keep_full`
        observations in full, older ones shortened (the cache still holds them)."""
        lines = []
        if self.data["findings"]:
            lines.append("Findings so far:")
            lines += [f"  - (step {f['step']}) {f['text']}" for f in self.data["findings"]]
        steps = self.data["steps"]
        for item in steps:
            obs = item["observation"]
            if item["step"] <= len(steps) - keep_full and len(obs) > short:
                obs = obs[:short] + " ... [shortened; call the same tool again to see it whole]"
            if item.get("thought"):
                lines.append(f"Thought {item['step']}: {item['thought'][:300]}")
            lines.append(f"Step {item['step']}: {item['tool']} {json.dumps(item['arguments'], sort_keys=True)}"
                         + (" (repeated)" if item["repeated"] else "") + f"\n  -> {obs}")
        return "\n".join(lines)
