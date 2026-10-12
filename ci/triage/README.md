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
- `riscv-uart-triage` re-triages any retained weekly or sanity build, including
  `megrez-damo-uart-weekly`.
- `riscv-uart-single-elf` (the portal's Run one ELF) triages a failed upload the
  same way; the uploader sees the AI analysis on the submission page.

The extractor reads the RVCP diagnostics (trap-signature mismatches with
XEPC/XCAUSE/XTVAL, mismatching field and hint; register self-checks with the
bad and expected value) and ignores the `[SIGQ]` signature dump.

## How a failure is explained (cheapest first)

`run_ci_triage.py` treats each non-PASS, non-SKIPPED case like this and stops at the first
step that explains it (a matching known-issue note, step 2, outranks rule and AI answers
stored in memory; only a person's verdict outranks the note):

1. **Memory** (`memory.py`, 0 tokens). Each failure gets a *signature*: its normalized
   evidence without test-specific addresses (category, mismatching field, expected/actual
   values, trap cause, CSR of the faulting instruction, assertion text). Analyses are stored
   in SQLite (`TRIAGE_MEMORY_DB`, default `/data/ci/agent/triage-memory/triage.sqlite`) by
   board, signature and test, from three sources: **person** (a Verdict or root cause edited
   in the portal, pulled back by `publish_triage.py` from `/api/v1/triage/feedback/`),
   **rule** and **ai**. The same test is preferred, then the same signature in another
   test; a person's verdict overrides rule and AI fields. Run ELF uses a separate memory.
2. **Known issues** (`vault.py`, 0 tokens): the Obsidian vault `obsidian-vault/` holds one
   note per reported ACT/Sail issue (and Verification Agent results), with frontmatter
   saying which boards, test-name globs and evidence substrings identify it. A note marked
   `auto: true` that names the board and test explains the failure with its verdict and
   issue link; other matching notes (at most two) go into the AI question. Only a person's
   verdict in memory outranks a note. Notes are read each run, so editing one takes effect at
   once; `tools/sync_issue_notes.py` updates their GitHub state from the team's issue sheet
   and adds stubs for new issues. See `obsidian-vault/README.md`.
   The vault also holds Verification Agent results (`verifications/`), matched the same way.
3. **Board rules** (`agent.py`, 0 tokens) from `board_knowledge/<board>.yaml`, which holds
   only measured or documented facts: access to a CSR the core does not implement, a CSR
   read-back that equals the written value masked by the measured writable bits, and
   value-diff mechanisms the values alone prove (`value_diff.py`, ported from
   `tools/act_agent`, with the UDB CSR field table in `knowledge/csr_fields.json`).
4. **AI**, once per *signature cluster* (not per test), with a compact question (about
   3.6 KB): the rule-based reading, the evidence fields, the value diff, a disassembly
   window around the failing PC when the ELF is available, key log lines and up to three
   past findings of the same category. The answer is checked (an unsupported "hardware
   bug" or a spec-version claim without a hypervisor-specific field loses confidence) and
   stored.

Measured on Megrez weekly #5 (30 failures): 20 signatures; 12 explained by rules and
shared signatures without AI; 15 AI calls instead of 30, with questions about a third
smaller (55 KB sent instead of 166 KB); a second run used memory only (0 calls, 0.3 s).
`--refresh-memory` analyzes everything again.

## The case file: failing instruction and test intent (`casefile.py`)

Every weekly and sanity build keeps, for each failed test, the ELF that ran and its generated
source, plus the board's ACT config and ACT's env headers (`collect_case_files.py`, called by
`jenkins_triage.sh`). They go next to the run (`per_case/<test>/test.elf`, `test.S`, `act/`)
and into the retained artifact store, so a later `riscv-uart-triage` sees exactly what that build
ran, pinned to its ACT commit (`act/revision.txt`). Run ELF keeps the uploaded ELF the same way. damo-rv-priv-ats runs (`megrez-damo-uart-weekly`)
keep, per failing suite, the suite ELF and its C sources, plus damo's shared code, its `NORM/`
requirement tables and the damo commit (`damo/`); a damo failure's intent is the C test function
around the failed assertion (`file:line` in the log), and its `norm:` IDs resolve to damo's text.

From these, each failure gets a `casefile.json`:

- **instruction**: the failing instruction and its neighbours, disassembled from that ELF at the
  trapping PC (xepc/mepc) or the checking PC, with its source line.
- **intent**: the test code at the failing check. Generated tests label every check
  `<covergroup>_<coverpoint>_<bin>`, the same names as the log's test info, so the check is
  found exactly; the snippet runs back to the previous check (the setup) and comes with the
  test header (required extensions, defines).
- **board macros**: the board's `rvmodel_macros.h` definitions the check relies on, found by
  following the RVTEST_* macros it uses through the env headers, including `jal` into env
  routines, plus the constants they use (for example `RVMODEL_SET_SSW_INT` and `SIG_ADDRESS`).
- **sail settings**: the board's `sail.json` settings for the failure's CSRs, the extensions
  the test requires, and what decides its trap cause (misaligned priority, PMP, memory map,
  CLINT/PLIC for interrupts).

## How accurate it is (`evaluate.py`)

Run `riscv-uart-triage` with `EVALUATE=true` for a retained build. It triages the build **blind**
(no vault notes, no memory; spec text, the ACT issue index and board rules stay), so the agent
works every failure out itself, then scores the result against an answer key, per failure:

1. the Verdict a person set in the portal for that build;
2. a person's Verdict for the same test in another build of the board (reported separately);
3. a verified vault note that resolves the failure (it also gives the root-cause class).

Nothing is published to the portal. The job archives `accuracy.md` and `accuracy.json`, shows
the headline in the build description, and appends to `/data/ci/agent/triage-evals/history.jsonl`
(`tools/accuracy_history.py` prints the trend). Scores: root-cause accuracy (class where the key
has one, else verdict), verdict agreement, hardware-bug false alarms (must stay 0), missed
hardware bugs, accuracy by confidence, spec-quote checks, and every disagreement with its key.

Run it after changing the agent or its knowledge; the key grows as people set Verdicts in the
portal and as verified notes are added to the vault. A failure the vault answers is a
fair test only in a blind run: otherwise the agent would just repeat the note.

## What the reference model did (`sail_trace.py`)

The ELF that ran on the board carries the board's boot and interrupt macro code, which Sail does
not model, so the reference run uses the signature ELF ACT built for Sail (`test.sig.elf`, kept
by `collect_case_files.py`) with the board's `sail.json`. The failing PC is moved across by the
nearest code label (check labels `<cg>_<cp>_<bin>` exist in both builds) plus its offset; a
virtual PC keeps its high bits. If the instruction there is not the same one (ignoring
immediates), the builds differ at that point, typically board RVMODEL code inside an ACT
routine such as `rvtest_set_mext_int_*`, and the case file says so instead of showing an
unrelated instruction.

Sail (0.14.1, `/data/ci/toolchains/riscv-sail-0.14.1`, or `TRIAGE_SAIL`; `TRIAGE_SAIL=off`
disables it) runs once per failure with an instruction limit and a timeout, and its trace is
read as a stream. The case file gets the instruction as Sail executed it and its result (the
value read, the trap taken), earlier writes to the same CSR family and device stores (setup),
and the instructions just before it. On Megrez weekly #7 that took 16 s for 40 failures; 23 got
a trace. Examples: the interrupt priority checks read `sip = 0x2222` in Sail because the test's
`RVMODEL_SET_SSW_INT` store to `0x0C000004` raises SSIP in Sail's simulated device, which the
board does not have; `SvPMP_sv48_pmp_on_pa_Smode` takes the same access fault in both, but Sail
writes `mtval = 0x8090009014` (40 bits) where the board writes the full virtual address.

## The specification each board implements (`spec.py`)

`knowledge/spec_versions.json.gz` (built by `tools/build_spec_versions.py` from the
riscv-isa-manual, CC-BY-4.0) holds the specification as versions, each pinned to its git tag
or commit:

| Id | Text |
|---|---|
| `priv-1.11`, `unpriv-20190608` | Ratified 20190608 releases (LaTeX converted, tables kept) |
| `priv-1.12`, `h-1.0` | Privileged 20211203 with Hypervisor 1.0 |
| `h-0.6` | Hypervisor draft 0.6 (manual commit before `cc43c1fe`, 2020) |
| `priv-current`, `unpriv-current` | Current ratified release, with its normative rules (`norm:ID`) verbatim |

`spec_versions` in `board_knowledge/<board>.yaml` names what a board implements (VF2: priv 1.11;
Megrez: priv 1.11 and H draft 0.6; BPI-F3: priv 1.12, declared). For each AI question the agent
searches those versions with words from the failure's structure (the instruction and its CSR,
the differing fields, the trap causes involved, the coverpoint), shows the best passages labelled
by version, puts the current ratified text right after a passage only where it differs, and adds
the current normative rules the test names. The AI is told to judge the hardware against the
board's versions, not the current text and not ACT, Sail or Spike by default.

Each answer names the passage it relies on (`spec_version`, `spec_quote`). The agent checks the
quote is in that version: a quote that is not loses confidence and is marked "QUOTE NOT FOUND"
in the report; a hardware-bug claim citing a version the board does not implement is low
confidence.

Example: VF2's sv39 misaligned AMO failures. Privileged 1.11, which the U74 implements, lists
Store/AMO address misaligned (6) above Store/AMO page fault (15) in its fixed exception priority;
only from 1.12 is misaligned "Optionally" before translation. So reporting cause 6 is what the
board's own version requires.

## What the AI is given (`context.py`)

Each AI question is grounded, every part small and best effort:

- **Test source**: the test's own code around the failing check (addr2line on the failing
  PC, or the file:line a damo assertion prints), looked up under `TRIAGE_SOURCE_ROOTS`
  (default: the Jenkins workspace with `external/riscv-arch-test` and
  `external/damo-rv-priv-ats`; paths recorded by another workspace are remapped).
- **Requirements**: damo `norm:<ID>` tags in that code resolved to the exact spec sentence
  from the suite's `NORM/` tables.
- **Spec text**: the board's own specification versions (see above). Boards without
  `spec_versions` fall back to two passages of the current privileged spec
  (`knowledge/priv_spec_chunks.json.gz`, `tools/build_spec_index.py`).
- **Similar ACT issues**: the two riscv-arch-test issues (of all 645, open and closed, in
  `knowledge/act_issues.json.gz`; refresh with `tools/build_issue_index.py --cache DIR`) that
  name the same test or family, or share two rare identifiers (CSR, field, coverpoint names)
  with the failure. Issues already matched as vault notes are not repeated.
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
