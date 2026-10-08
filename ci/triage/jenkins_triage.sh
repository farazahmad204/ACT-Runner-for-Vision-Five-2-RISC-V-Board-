#!/usr/bin/env bash
# Triage a run's failures from a Jenkins job: AI analysis when it can run, otherwise
# deterministic triage only. Advisory: never changes a verdict.
#   jenkins_triage.sh RUN_ROOT STATE_ROOT BOARD_ID
# Environment (the jobs' parameters): RUN_AI_TRIAGE (true/false), AI_TRIAGE_PROVIDER
# (codex | openai), AI_TRIAGE_MODEL (blank = provider default), AI_TRIAGE_MAX_FAILURES
# (0 = all), OPENAI_API_KEY (openai only), CODEX_BIN.
set -euo pipefail
run_root="$1"
state_root="$2"
board="$3"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$run_root/cases.json" ]]; then
  echo "Triage skipped: $run_root/cases.json is unavailable"
  exit 0
fi
args=(--run-root "$run_root" --state-root "$state_root" --board "$board"
      --max-ai-failures "${AI_TRIAGE_MAX_FAILURES:-0}")
if [[ "${RUN_AI_TRIAGE:-false}" == true ]]; then
  provider="${AI_TRIAGE_PROVIDER:-codex}"
  codex="${CODEX_BIN:-codex}"
  reason=""
  case "$provider" in
    codex)
      if ! command -v "$codex" >/dev/null; then
        reason="the Codex CLI ($codex) is not installed on this agent"
      elif ! "$codex" login status >/dev/null 2>&1; then
        reason="Codex is not logged in for user $(id -un) (run: codex login --device-auth)"
      fi
      ;;
    openai) [[ -n "${OPENAI_API_KEY:-}" ]] || reason="no OpenAI API key credential" ;;
    *) reason="unknown AI_TRIAGE_PROVIDER '$provider'" ;;
  esac
  if [[ -z "$reason" ]]; then
    args+=(--ai --provider "$provider" --codex-bin "$codex")
    [[ -z "${AI_TRIAGE_MODEL:-}" ]] || args+=(--model "$AI_TRIAGE_MODEL")
    echo "AI triage: provider=$provider model=${AI_TRIAGE_MODEL:-default}"
  else
    echo "AI triage skipped: $reason. Running deterministic triage only."
  fi
fi
python3 "$here/run_ci_triage.py" "${args[@]}"
