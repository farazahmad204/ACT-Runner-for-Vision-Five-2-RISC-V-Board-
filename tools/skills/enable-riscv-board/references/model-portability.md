# Model portability

The AI model is the reasoning engine, not the project memory. Durable knowledge
must remain in this repository as instructions, references, manifests, scripts,
tests, source code and evidence.

## What survives a model change

- Board manifests and evidence status.
- Runner/board/CI architecture references.
- Deterministic inventory, validation, build and test scripts.
- Safety gates and completion definitions.
- Build manifests, logs, hashes and exact revisions.

Any capable coding model with repository and shell access can follow these.

## What may change

- Quality of hardware and RISC-V reasoning.
- Ability to search and synthesize vendor documentation.
- Tool-call syntax and skill auto-discovery.
- Context length, consistency, speed and cost.
- Willingness to stop at an evidence gap rather than guess.

## Required adapter for another agent platform

Ensure the platform loads `AGENTS.md` or explicitly provide `SKILL.md`, permits
read-only repository inspection, supports controlled file edits and command
execution, and enforces approval for destructive/external operations. If it
does not support Codex skills, use `SKILL.md` as the system/workflow prompt; the
Markdown references and Python scripts are vendor-neutral.

Run the same inventory and tests when comparing models. Do not accept a model
because it produces plausible code; require the same linker-map, packaging,
hardware-log and CI evidence gates.
