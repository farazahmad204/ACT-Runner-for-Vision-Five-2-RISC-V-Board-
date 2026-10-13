# Revision and branch policy

The machine-readable source of truth is
`assets/system-contract.yaml`. This reference explains that contract; do not
maintain different branch or repository defaults here.

## Operational UART workflow

- Stable runner branch: `vf2-uart-stream-jenkins`.
- Reviewed ACT branch used by current VF2 and BPI-F3 UART jobs: `sifive_u74`.
- Runner repository: `farazahmad-10xe/ACT-Runner-for-Vision-Five-2-RISC-V-Board-`.
- ACT repository: `Arshia2564/riscv-arch-test`.
- Required Sail release for current jobs: `0.14.1`.

These are defaults, not proof of execution. Each Jenkins run resolves branches
once, checks out detached commits, and must publish the resolved SHAs. Revision
override parameters exist for reproduction.

## Installed firmware identity

An installed runner is identified by the board and `runner_build` fields in its
UART `READY` marker. Jenkins `INSTALLED_RUNNER_BUILD` must match that marker.
Changing a branch does not update a board; changing installed firmware does not
change the branch Jenkins fetches.

## Legacy workflows

Some non-UART or older Jenkinsfiles still default to `main`. Do not silently
apply their defaults to the UART sanity, weekly, or single-ELF workflows. Use
`system_inventory.py` to detect branch divergence before editing jobs.

## Change policy

- Pin and record the exact runner, ACT, Sail/UDB, compiler, and job revisions in
  result provenance.
- Change a stable default only through a reviewed patch and reinstall the
  affected Jenkins definitions.
- Rebuild, reflash, and update `INSTALLED_RUNNER_BUILD` together when runner
  firmware changes.
- Never use a developer worktree's current branch as implicit CI policy.
- Validate the runner origin, stable-base ancestry, production profile and
  build entry point before creating board implementation files.
