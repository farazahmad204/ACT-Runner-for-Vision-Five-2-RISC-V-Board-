# CI system architecture

## Components

```text
Runner Git + ACT Git
        -> Apollo Jenkins controller (HTTPS)
        -> riscv-hw-agent on the lab PC
        -> per-board hardware lock
        -> smart-plug/reset + USB-UART + installed M-mode runner
        -> JSON/JUnit/raw UART/provenance artifacts
        -> portal ingestion API
        -> board/job/run and single-ELF result pages
```

Jenkins schedules and records work; the agent PC owns toolchains and physical
connections. The board runs only the persistent minimal runner and the streamed
ELF. The portal displays results and can submit a user ELF; it does not directly
control UART hardware.

Portal application source is maintained separately at
`https://github.com/farazahmad204/RISC-V-CI-Django-Web-Portal`. When that checkout
is unavailable, this skill knows the runner-side ingestion contract but must not
claim knowledge of current Django models, permissions, migrations or deployment
units. Inspect the exact deployed portal revision before changing internals.

## Current UART jobs

| Job | Pipeline | Lock | Purpose |
|---|---|---|---|
| `vf2-uart-sanity` | `Jenkinsfile.uart-sanity` | `vf2-hardware` | Focused VF2 validation |
| `vf2-uart-weekly` | `Jenkinsfile.uart-weekly` | `vf2-hardware` | Scheduled VF2 ACT run |
| `bpif3-uart-sanity` | `Jenkinsfile.bpif3-uart-sanity` | `bpif3-hardware` | Focused BPI-F3 validation |
| `bpif3-uart-weekly` | `Jenkinsfile.bpif3-uart-weekly` | `bpif3-hardware` | Scheduled BPI-F3 ACT run |
| `riscv-uart-single-elf` | `Jenkinsfile.uart-single-elf` | selected board lock | Portal-uploaded ELF |
| `riscv-board-onboarding` | `Jenkinsfile.board-onboarding` | no hardware lock before smoke phase | Durable offline board onboarding and approval workflow |

Definitions are installed by `ci/jenkins/install_apollo_uart_job.py`. Its mapping
of job names to Jenkinsfiles and XML templates is authoritative.

The onboarding job stores each run below
`/home/lpt-10xe/jenkins-agent/onboarding-state/<board>/<run-id>`. It reuses the
same feature workspace and `state.json` on `RESUME`, so a Jenkins restart or
bounded command failure does not restart completed stages. The job automatically
performs only local research handoffs, validation, editing handoffs, builds and
review packaging. Push, hardware and production-promotion actions remain
separate approval-gated phases.

## Execution contract

1. Fetch runner and ACT branches and resolve exact commits.
2. Perform ACT/toolchain/Sail preflight and generate expected artifacts.
3. Build the expected UART runner and compare its build identity with installed
   firmware.
4. Acquire the board lock.
5. Power-cycle, wait for `READY`, stream each ELF with CRC, and wait for `DONE`.
6. Normalize JSON and JUnit, archive UART/provenance artifacts.
7. Publish through the portal API and attach the returned portal URL to the
   Jenkins build description.

## Current deployment identifiers

- Jenkins node label: `riscv-hw-agent`.
- Jenkins/portal base host in current pipelines: `https://192.168.100.150/`.
- Portal path: `/portal/`.
- Portal ingest secret-text credential ID: `riscv-portal-ingest-token`.
- Internal CA on the agent: `/home/lpt-10xe/jenkins-agent/jenkins-internal-ca.crt`.
- Artifact store: `/home/lpt-10xe/jenkins-agent/results-artifacts`.

Record credential IDs only. Never read, print, copy, or commit secret values.
Deployment paths are environment-specific and must be verified before changes.

## Adding a board to CI

After two repeatable hardware smoke boots:

1. Allocate a unique Jenkins hardware lock and stable serial-by-id path.
2. Configure a validated power/reset backend outside Git secrets.
3. Add sanity and weekly pipeline parameters, scripts and job XML.
4. Extend the installer's job mapping.
5. Add board selection to the single-ELF pipeline only after compatible ELF
   validation and runner identity checks exist.
6. Add portal board slug/name/core metadata and authorize ingestion.
7. Test failure, timeout, transport-error, recovery, counts, artifact links and
   user permissions before scheduling weekly runs.
