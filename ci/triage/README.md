# ACT CI triage agent

The CI triage agent reads the normalized `cases.json` and bounded per-case UART
logs produced by the weekly UART jobs. It creates deterministic evidence for
every non-PASS case and can optionally request one advisory AI analysis per
failure.

The agent never changes ACT, Sail, Spike, hardware, JUnit, or portal verdicts.
AI output is an investigation aid and is archived separately from certification
results.

## When it runs

- Every weekly build (`vf2-`, `bpif3-`, `megrez-uart-weekly`, parameter `RUN_TRIAGE`)
  and every sanity build triages its non-PASS cases after the run.
- After the run is published, `publish_triage.py` sends the result to the portal,
  which fills the Root cause, Category, Owner and Evidence columns of the run's
  workbook and marks each failure's Verdict "Needs investigation". Cells a person
  has edited are never overwritten.
- `riscv-uart-triage` re-triages any retained weekly or sanity build.
- `riscv-uart-single-elf` (the portal's Run one ELF) triages a failed upload the
  same way; the uploader sees the AI analysis on the submission page.

The extractor reads the RVCP diagnostics (trap-signature mismatches with
XEPC/XCAUSE/XTVAL, mismatching field and hint; register self-checks with the
bad and expected value) and ignores the `[SIGQ]` signature dump.

## How a failure is explained (cheapest first)

`run_ci_triage.py` treats each non-PASS, non-SKIPPED case like this and stops at the first
step that explains it:

1. **Memory** (`memory.py`, 0 tokens). Each failure gets a *signature*: its normalized
   evidence without test-specific addresses (category, mismatching field, expected/actual
   values, trap cause, CSR of the faulting instruction, assertion text). Analyses are stored
   in SQLite (`TRIAGE_MEMORY_DB`, default `/data/ci/agent/triage-memory/triage.sqlite`) by
   board, signature and test, from three sources: **person** (a Verdict or root cause edited
   in the portal, pulled back by `publish_triage.py` from `/api/v1/triage/feedback/`),
   **rule** and **ai**. The same test is preferred, then the same signature in another
   test; a person's verdict overrides rule and AI fields. Run ELF uses a separate memory.
2. **Board rules** (`agent.py`, 0 tokens) from `board_knowledge/<board>.yaml`, which holds
   only measured or documented facts: access to a CSR the core does not implement, a CSR
   read-back that equals the written value masked by the measured writable bits, and
   value-diff mechanisms the values alone prove (`value_diff.py`, ported from
   `tools/act_agent`, with the UDB CSR field table in `knowledge/csr_fields.json`).
3. **AI**, once per *signature cluster* (not per test), with a compact question (about
   3.6 KB): the rule-based reading, the evidence fields, the value diff, a disassembly
   window around the failing PC when the ELF is available, key log lines and up to three
   past findings of the same category. The answer is checked (an unsupported "hardware
   bug" or a spec-version claim without a hypervisor-specific field loses confidence) and
   stored.

Measured on Megrez weekly #5 (30 failures): 20 signatures; 12 explained by rules and
shared signatures without AI; 15 AI calls instead of 30, with questions about a third
smaller (55 KB sent instead of 166 KB); a second run used memory only (0 calls, 0.3 s).
`--refresh-memory` analyzes everything again.

## What the AI is given (`context.py`)

Each AI question is grounded, every part small and best effort:

- **Test source**: the test's own code around the failing check (addr2line on the failing
  PC, or the file:line a damo assertion prints), looked up under `TRIAGE_SOURCE_ROOTS`
  (default: the Jenkins workspace with `external/riscv-arch-test` and
  `external/damo-rv-priv-ats`; paths recorded by another workspace are remapped).
- **Requirements**: damo `norm:<ID>` tags in that code resolved to the exact spec sentence
  from the suite's `NORM/` tables.
- **Spec text**: the two most relevant passages of the ratified privileged spec, from
  `knowledge/priv_spec_chunks.json.gz` (riscv-isa-manual, CC-BY-4.0; rebuild with
  `tools/build_spec_index.py`), ranked by the failure's CSRs, fields and terms.
- **Declared ISA**: the extensions the board's ACT config declares (`act_config` in
  `board_knowledge/<board>.yaml`) with its DEVIATION notes.

On Megrez weekly #5 every AI question carried source, spec and ISA (about 5.7 KB each,
15 calls); the AI then cited the H 1.0 htinst/mtinst rule and asked for the missing
htval/mtval2 value instead of guessing.

## AI analysis

With `RUN_AI_TRIAGE` (default on) every failed case also gets an AI analysis: a
failure reason, root cause, confidence and next step, shown in the portal
workbook's AI analysis column. It is advisory and never changes a verdict.

- `AI_TRIAGE_PROVIDER=codex` (default) runs the Codex CLI on the agent, logged in
  with a ChatGPT account. Install `codex` in `/data/ci/bin` and log in once as the
  agent user: `sudo -u ci-agent -H /data/ci/bin/codex login --device-auth`.
  Codex runs with all of its tools disabled (no shell, code execution, browser or
  plugins), so it can only read the prompt and answer: the read-only sandbox alone
  would still let commands read files on the agent, and Run ELF logs come from
  users' own ELFs. Usage counts against that ChatGPT plan.
- `AI_TRIAGE_PROVIDER=openai` uses the OpenAI Responses API with the Jenkins
  Secret Text credential `AI_TRIAGE_CREDENTIAL_ID` (default `openai-api-key`).
- `AI_TRIAGE_MODEL`: blank uses the provider's default model.
- `AI_TRIAGE_MAX_FAILURES`: cap on AI calls; `0` analyzes every failure.

If the AI cannot run (no `codex`, not logged in, no key), the job says why and
runs deterministic triage only. `ci/triage/jenkins_triage.sh` holds that logic
for every job.

## Manual use

```bash
python3 ci/triage/run_ci_triage.py \
  --run-root logs/runs/jenkins_uart_weekly_42 \
  --state-root logs/jenkins/uart_weekly/jenkins_uart_weekly_42 \
  --board vf2_jh7110
```

With Codex logged in, add `--ai`. With an OpenAI API key instead:

```bash
OPENAI_API_KEY=... python3 ci/triage/run_ci_triage.py \
  --run-root logs/runs/jenkins_uart_weekly_42 \
  --state-root logs/jenkins/uart_weekly/jenkins_uart_weekly_42 \
  --board vf2_jh7110 \
  --ai \
  --provider openai \
  --model gpt-5 \
  --max-ai-failures 0
```

Outputs are written below `<run-root>/triage/`:

- `summary.json` and `summary.md`
- `per_case/<case>/evidence.json`
- `per_case/<case>/deterministic.md`
- `per_case/<case>/ai_analysis.md` when AI succeeds
- `per_case/<case>/ai_error.txt` when an AI request fails

The model receives only the evidence JSON produced by this tool. UART text is
explicitly treated as untrusted data, and input is bounded before submission.

## Re-analyze an existing weekly build

Install the `riscv-uart-triage` Jenkins job and select the source weekly job and
build number. It copies the retained `cases.json` and per-case UART logs from
`/data/ci/agent/results-artifacts`, then performs deterministic
triage with optional AI. It does not reserve, reset, or execute either board.
When `PUBLISH_TO_PORTAL` is enabled, the job updates only the advisory triage
fields on the existing portal run; hardware and reference-model verdicts are
not modified.
