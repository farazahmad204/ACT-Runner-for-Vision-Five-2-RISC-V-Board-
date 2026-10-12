#!/usr/bin/env bash
# Triage a run's failures from a Jenkins job: AI analysis when it can run, otherwise
# deterministic triage only. Advisory: never changes a verdict.
#   jenkins_triage.sh RUN_ROOT STATE_ROOT BOARD_ID
# Environment (the jobs' parameters): RUN_AI_TRIAGE (true/false), AI_TRIAGE_PROVIDER
# (codex | openai), AI_TRIAGE_MODEL (blank = provider default), AI_TRIAGE_MAX_FAILURES
# (0 = all), REFRESH_MEMORY (true: redo rule/AI answers; people's verdicts are kept),
# OPENAI_API_KEY (openai only), CODEX_BIN.
set -euo pipefail
run_root="$1"
state_root="$2"
board="$3"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$run_root/cases.json" ]]; then
  echo "Triage skipped: $run_root/cases.json is unavailable"
  exit 0
fi
# Keep each failed test's ELF and source and the board's ACT config next to the run, and in
# the retained artifact store, so this and any later re-triage can read the failing
# instruction and the test. A re-triage job reads the store copy and has nothing to add.
store_args=()
store_root="${TRIAGE_ARTIFACT_STORE:-/data/ci/agent/results-artifacts}"
if [[ -n "${JOB_NAME:-}" && -n "${BUILD_NUMBER:-}" && "$JOB_NAME" != riscv-uart-triage \
      && -d "$store_root" ]]; then
  store_args=(--store-dir "$store_root/${JOB_NAME//[^A-Za-z0-9_.-]/_}/$BUILD_NUMBER")
fi
python3 "$here/collect_case_files.py" --run-root "$run_root" --state-root "$state_root" \
  --board "$board" "${store_args[@]}" || true

args=(--run-root "$run_root" --state-root "$state_root" --board "$board"
      --max-ai-failures "${AI_TRIAGE_MAX_FAILURES:-0}")
if [[ "${REFRESH_MEMORY:-false}" == true ]]; then
  args+=(--refresh-memory)
  echo "Triage memory: refresh (earlier rule and AI answers are not reused)"
fi
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
