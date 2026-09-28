# ACT CI triage agent

The CI triage agent reads the normalized `cases.json` and bounded per-case UART
logs produced by the weekly UART jobs. It creates deterministic evidence for
every non-PASS case and can optionally request one advisory AI analysis per
failure.

The agent never changes ACT, Sail, Spike, hardware, JUnit, or portal verdicts.
AI output is an investigation aid and is archived separately from certification
results.

## Jenkins parameters

- `RUN_TRIAGE`: generate deterministic reports (default: enabled).
- `RUN_AI_TRIAGE`: add AI reports (default: disabled).
- `AI_TRIAGE_MODEL`: Responses API model (default: `gpt-5`).
- `AI_TRIAGE_CREDENTIAL_ID`: Jenkins Secret Text credential containing the API
  key (default: `openai-api-key`).
- `AI_TRIAGE_MAX_FAILURES`: optional AI-call cap; `0` analyzes every failure
  (default: `0`).

The credential is exposed only inside `withCredentials` and is never written to
the prompt, reports, console, or archived response metadata.

## Manual use

```bash
python3 ci/triage/run_ci_triage.py \
  --run-root logs/runs/jenkins_uart_weekly_42 \
  --state-root logs/jenkins/uart_weekly/jenkins_uart_weekly_42 \
  --board vf2_jh7110
```

Add AI analysis only when `OPENAI_API_KEY` is set:

```bash
OPENAI_API_KEY=... python3 ci/triage/run_ci_triage.py \
  --run-root logs/runs/jenkins_uart_weekly_42 \
  --state-root logs/jenkins/uart_weekly/jenkins_uart_weekly_42 \
  --board vf2_jh7110 \
  --ai \
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
`/home/lpt-10xe/jenkins-agent/results-artifacts`, then performs deterministic
triage with optional AI. It does not reserve, reset, or execute either board.
When `PUBLISH_TO_PORTAL` is enabled, the job updates only the advisory triage
fields on the existing portal run; hardware and reference-model verdicts are
not modified.
