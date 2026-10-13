# Autonomous board-onboarding state machine

The persistent controller belongs in Jenkins or another durable workflow
service. The AI model is a worker invoked for bounded reasoning tasks; it is
not the state store and does not remain alive between steps.

## States

```text
requested
  -> contract_validated
  -> researching
  -> evidence_validated
  -> workspace_ready
  -> implemented
  -> offline_validated
  -> review_ready
  -> awaiting_external_approval
  -> hardware_smoke_1
  -> hardware_smoke_2
  -> awaiting_ci_approval
  -> ci_operational
```

Any state may transition to `blocked`. Store the exact blocker, evidence and
safe resume state in `state.json`. Every action must be idempotent: rerunning a
completed step verifies its artifacts rather than repeating a destructive
operation.

## Automatic authority

The controller may perform these actions without another prompt inside an
isolated feature workspace:

- Read repositories and public documentation.
- Run restricted research agents and validators.
- Create a local feature branch from the contract-pinned stable base.
- Edit board-scoped and approved shared files.
- Compile, package, inspect maps/disassembly, hash artifacts and run tests.
- Produce review patches and a proposed pull-request description.

## Approval gates

Require a short-lived, recorded approval for each external or hardware action:

- Push a branch or create/update a pull request.
- Write SD/eMMC/SPI or recovery storage.
- Power-cycle or reset physical hardware.
- Install/update Jenkins jobs or portal production configuration.
- Merge a pull request or promote a board to `ci_operational`.

Do not grant one permanent “full authority” token. Bind approval to the board,
action, exact artifact SHA-256, target device/resource and expiry time.

## Durable state and evidence

Store per-board state under an external Jenkins artifact/work directory, not
only in an AI conversation. Record:

- Contract version and canonical repository URLs.
- Stable base and feature commit SHAs.
- Board manifest and research-report hashes.
- Build command, profile, toolchain and image hashes.
- Human approvals and their scope.
- Hardware lock, target device, UART identity and raw logs.
- Current state, retry count, blocker and next safe action.

## Failure behavior

- Retry only bounded, explicitly transient operations.
- Never automatically retry a storage write after an ambiguous failure.
- Stop before hardware mutation if target identity or recovery evidence differs.
- Stop when source evidence conflicts; do not let the model select a convenient
  value without primary evidence.
- Preserve logs and make the next invocation resume from the last verified
  state.

## Controller commands

Initialize once using a manifest and the three validated research reports:

```bash
python3 tools/skills/enable-riscv-board/scripts/onboarding_controller.py init \
  --repo-root /path/to/clean/feature/workspace \
  --manifest /path/to/board-manifest.yaml \
  --research-dir /path/to/research-reports \
  --run-dir /durable/state/<board>/<run-id>/controller \
  --run-id <run-id>
```

Resume safe local work:

```bash
python3 tools/skills/enable-riscv-board/scripts/onboarding_controller.py resume \
  --run-dir /durable/state/<board>/<run-id>/controller \
  --execute-build
```

Inspect, retry a resolved blocker, or record the review approval:

```bash
python3 tools/skills/enable-riscv-board/scripts/onboarding_controller.py status \
  --run-dir <controller-dir>

python3 tools/skills/enable-riscv-board/scripts/onboarding_controller.py retry \
  --run-dir <controller-dir>

python3 tools/skills/enable-riscv-board/scripts/onboarding_controller.py approve \
  --run-dir <controller-dir> \
  --gate push_or_open_pull_request \
  --actor <reviewer> \
  --artifact-sha256 <review-patch-sha256>
```

The `riscv-board-onboarding` Jenkins job wraps these commands and keeps the
feature workspace and controller state outside the disposable Jenkins build
workspace. At initialization it also snapshots the controller, validators and
system contract outside the editable feature checkout. Resumed runs therefore
cannot silently replace their own control policy, and the controller rejects a
different feature branch or a HEAD that no longer descends from the stable
commit recorded at initialization.
