# Safety and validation gates

## Evidence rules

- Treat vendor manuals, schematics, vendor BSP/device tree, and bootloader source as primary evidence.
- Treat community posts and gists as leads until corroborated.
- Attach a source to each address, clock, reserved range, boot format, and privilege claim.
- Preserve `unknown` when evidence is missing.
- Never convert an AI inference into a confirmed manifest value without source or hardware evidence.

## Workspace rules

- Inspect branch, submodule, and dirty-worktree state before edits.
- Preserve unrelated changes.
- Generate board-specific files instead of adding board conditionals throughout shared code.
- Keep secrets, UART device permissions, smart-plug keys, and API keys outside Git.

## Hardware rules

- Resolve and display an explicit target device before writing a boot medium.
- Require human approval for first-time boot-storage writes and changes to recovery firmware.
- Keep a known-good recovery image and documented recovery path.
- Lock the Jenkins hardware resource during flash, reset, and test execution.
- Bound all waits and retries. Stop repeated boot or transport failures rather than looping indefinitely.

## Required evidence by state

### offline_prepared

- Manifest validates.
- Firmware compiles.
- Linker map shows no known overlap.
- Image packaging completes.
- Host UART protocol tests pass.
- Missing hardware facts and tests are listed.

### hardware_smoke_passed

- Full clean-boot log.
- Runner `READY` contains expected board and build IDs.
- CRC-verified ELF transfer.
- Minimal ELF completes with `DONE` and structured status.
- Same result reproduced after a second clean boot.

### hypervisor_smoke_passed

- H capability evidence.
- Recorded starting and target privilege modes.
- HS/VS or Linux/KVM execution contract documented.
- At least one test has complete boot, execution, trap/result, and completion evidence.

### ci_operational

- Jenkins hardware lock and timeouts verified.
- Sanity job is repeatable.
- Portal counts and UART artifacts match Jenkins results.
- Failure and recovery procedure is documented.
