# ACT CI triage agent

The CI triage agent reads the normalized `cases.json` and bounded per-case UART
logs produced by the weekly UART jobs. Deterministic evidence is generated for
every non-PASS case. An optional local Ollama model can add one advisory report
per failure without an API key or per-request charge.

The agent never changes ACT, Sail, Spike, hardware, JUnit, or portal verdicts.
Local-model output is an investigation aid and is archived separately from
certification results.

## Jenkins parameters

- `RUN_TRIAGE`: deterministic reports, enabled by default.
- `RUN_LOCAL_AI_TRIAGE`: optional local Ollama reports, disabled by default.
- `LOCAL_AI_MODEL`: installed Ollama model, default `qwen2.5:1.5b`.
- `LOCAL_AI_ENDPOINT`: default `http://127.0.0.1:11434/api/generate`.
- `LOCAL_AI_MAX_FAILURES`: optional cap; `0` analyzes every failure.

No Jenkins AI credential is required. Ollama must be running on the Jenkins
agent and the selected model must already be pulled.

## Agent provisioning

For the current 4-thread, 11 GiB, CPU-only agent, start with the approximately
1 GB quantized model:

```bash
ollama pull qwen2.5:1.5b
ollama list
curl -sS http://127.0.0.1:11434/api/tags
```

Do not start with a 7B or larger model on this machine. The local model should
be considered a report-writing assistant, not an architecture authority.

## Manual use

Deterministic reports only:

```bash
python3 ci/triage/run_ci_triage.py \
  --run-root logs/runs/jenkins_uart_weekly_42 \
  --state-root logs/jenkins/uart_weekly/jenkins_uart_weekly_42 \
  --board vf2_jh7110
```

Add local analysis:

```bash
python3 ci/triage/run_ci_triage.py \
  --run-root logs/runs/jenkins_uart_weekly_42 \
  --state-root logs/jenkins/uart_weekly/jenkins_uart_weekly_42 \
  --board vf2_jh7110 \
  --local-ai \
  --provider ollama \
  --model qwen2.5:1.5b \
  --endpoint http://127.0.0.1:11434/api/generate \
  --max-ai-failures 1
```

Use one failure for the first performance test. Set `--max-ai-failures 0` only
after measuring CPU temperature, memory pressure, and report quality.

Outputs are written below `<run-root>/triage/`:

- `summary.json` and `summary.md`
- `per_case/<case>/evidence.json`
- `per_case/<case>/deterministic.md`
- `per_case/<case>/ai_analysis.md` when local analysis succeeds
- `per_case/<case>/ai_error.txt` when the local runtime is unavailable

UART text is explicitly treated as untrusted data and input is bounded before
submission to the local model.

## Re-analyze an existing weekly build

Install the `riscv-uart-triage` Jenkins job and select the source weekly job and
build number. It copies retained `cases.json` and per-case UART logs from
`/home/lpt-10xe/jenkins-agent/results-artifacts`, then performs deterministic
triage with optional local analysis. It does not reserve, reset, or execute a
board.

When `PUBLISH_TO_PORTAL` is enabled, only advisory triage fields are updated on
the existing portal run. Hardware and reference-model verdicts are unchanged.
