# Jenkins UART jobs

The repository defines five UART-only jobs:

- `vf2-uart-sanity`
- `vf2-uart-weekly`
- `bpif3-uart-sanity`
- `bpif3-uart-weekly`
- `riscv-uart-single-elf`

All jobs fetch the same runner branch, validate the selected board identity and
installed runner build, serialize access with a board-specific lock, power
cycle through the configured smart outlet, and publish results to the portal.
No Jenkins job writes boot media or builds an SD payload pack.

`megrez-damo-uart-weekly` runs the
[damo-rv-priv-ats](https://github.com/farazahmad204/damo-rv-priv-ats) privileged and
hypervisor suites on the Milk-V Megrez (`ci/jenkins/megrez_damo.sh`): it builds each suite
in `DAMO_SUITES` with `CONFIG=milkv-megrez-p550`, runs one suite ELF per power cycle, and
`ci/jenkins/damo_cases.py` turns the suites' `[TEST]`/`[PASS]`/`[FAIL]` output into one
portal row per test case (`<Suite>-<case>`). No reference model is involved.

`riscv-board-health` runs every 10 minutes and tells the portal's Run ELF page
which boards are online: a board is online when its `/dev/ttyCI-<board>`
USB-UART is present and its smart plug answers a read-only status query
(`ci/jenkins/board_health.py`; boards without a plug report offline). It takes
no board lock and never switches a plug. Preview without posting:
`python3 ci/jenkins/board_health.py --dry-run`.

Installed firmware revisions are tracked in `ci/runner_inventory.json`.
Override `RUNNER_REVISION_OVERRIDE` only for an intentional reproducible build;
override `INSTALLED_RUNNER_BUILD` only while diagnosing inventory drift.

Install or update a job with:

```bash
python3 ci/jenkins/install_apollo_uart_job.py --user admin --job <job-name>
```
