#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
state_root="logs/jenkins/single_elf/${BUILD_NUMBER:?BUILD_NUMBER is required}"
run_root="logs/runs/jenkins_single_elf_${BUILD_NUMBER}"
payload="$state_root/payload.elf"
ca_file="${JENKINS_INTERNAL_CA_FILE:-/data/ci/agent/jenkins-internal-ca.crt}"

board_settings() {
  case "${TARGET_BOARD:-}" in
    visionfive2)
      board_id="vf2_jh7110"
      serial_default=/dev/ttyCI-vf2
      power_device="${POWER_DEVICE_NAME:-SCW1050}"
      ready_timeout_default=240
      ;;
    bananapi-f3)
      board_id="bpif3_k1"
      serial_default=/dev/ttyCI-bpif3
      power_device="${POWER_DEVICE_NAME:-SCW1050}"
      ready_timeout_default=240
      ;;
    milkv-megrez)
      # SPI boot + DDR training reaches READY in about 157 s.
      board_id="milkv_megrez_eic7700x"
      serial_default=/dev/ttyCI-megrez
      power_device="${POWER_DEVICE_NAME:-SCW1050}"
      ready_timeout_default=300
      ;;
    *)
      echo "Unsupported TARGET_BOARD: ${TARGET_BOARD:-unset}" >&2
      return 2
      ;;
  esac
  runner_build="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["boards"][sys.argv[2]]["installed_build_id"])' ci/runner_inventory.json "$TARGET_BOARD")"
}

prepare() {
  board_settings
  [[ "${SUBMISSION_ID:-}" =~ ^[0-9a-fA-F-]{36}$ ]] || { echo 'Invalid SUBMISSION_ID' >&2; return 2; }
  [[ "${ELF_SHA256:-}" =~ ^[0-9a-fA-F]{64}$ ]] || { echo 'Invalid ELF_SHA256' >&2; return 2; }
  case "${ELF_DOWNLOAD_URL:-}" in
    https://apollo/portal/api/v1/elf/*/download/ | https://192.168.100.150/portal/api/v1/elf/*/download/) ;;
    *) echo 'ELF_DOWNLOAD_URL is outside the approved portal endpoint' >&2; return 2 ;;
  esac
  [[ "${ELF_DOWNLOAD_URL}" == *"/${SUBMISSION_ID}/download/" ]] || {
    echo 'ELF download URL does not match SUBMISSION_ID' >&2
    return 2
  }
  [[ -n "${ELF_DOWNLOAD_TOKEN:-}" ]] || { echo 'ELF_DOWNLOAD_TOKEN is required' >&2; return 2; }

  mkdir -p "$state_root" "$run_root"
  set +x
  curl --fail --silent --show-error --location \
    --cacert "$ca_file" \
    --header "X-ELF-Download-Token: ${ELF_DOWNLOAD_TOKEN}" \
    --output "$payload" \
    "$ELF_DOWNLOAD_URL"
  set -x
  actual_sha="$(sha256sum "$payload" | awk '{print $1}')"
  [[ "$actual_sha" == "${ELF_SHA256,,}" ]] || {
    echo "ELF SHA-256 mismatch: expected ${ELF_SHA256,,}, got $actual_sha" >&2
    return 2
  }
  python3 - "$payload" <<'PY'
import pathlib
import sys

data = pathlib.Path(sys.argv[1]).read_bytes()[:20]
if len(data) < 20 or data[:4] != b"\x7fELF":
    raise SystemExit("download is not an ELF file")
if data[4] != 2 or data[5] != 1:
    raise SystemExit("download is not a little-endian ELF64 file")
if int.from_bytes(data[18:20], "little") != 243:
    raise SystemExit("download is not a RISC-V ELF")
PY
  chmod 0400 "$payload"
  {
    echo "SUBMISSION_ID=$SUBMISSION_ID"
    echo "TARGET_BOARD=$TARGET_BOARD"
    echo "BOARD_ID=$board_id"
    echo "RUNNER_BUILD=$runner_build"
    echo "ELF_SHA256=${ELF_SHA256,,}"
    printf 'ELF_NAME=%q\n' "${ELF_NAME:-payload.elf}"
  } > "$state_root/state.env"
  echo "[SINGLE_ELF] verified submission=$SUBMISSION_ID board=$TARGET_BOARD sha256=$actual_sha"
}

run() {
  board_settings
  [[ -r "$payload" ]] || { echo "Prepared ELF is missing: $payload" >&2; return 2; }
  python3 cert_harness/uart_stream/run_elf_batch.py "$payload" \
    --serial-dev "${SERIAL_DEV:-$serial_default}" \
    --baud 115200 \
    --run-dir "$run_root" \
    --tuya-config devices.json \
    --device-name "$power_device" \
    --expect-board "$board_id" \
    --expect-runner-build "$runner_build" \
    --ready-timeout "${UART_READY_TIMEOUT:-$ready_timeout_default}" \
    --result-timeout "${UART_RESULT_TIMEOUT:-600}"
}

# Triage a failed run (AI analysis when it can run). The portal shows the uploaded ELF under
# its original name, so the triage case uses ELF_NAME too.
triage() {
  board_settings
  if [[ ! -f "$run_root/summary.json" ]]; then
    echo "Triage skipped: no UART result in $run_root"
    return 0
  fi
  python3 - "$run_root" "${ELF_NAME:-payload.elf}" <<'PY'
import json
import shutil
import sys
from pathlib import Path

run_root, name = Path(sys.argv[1]), sys.argv[2]
results = json.loads((run_root / "summary.json").read_text()).get("results") or [{}]
result = results[0]
status = str(result.get("status", "ERROR")).upper()
source_log = Path(str(result.get("uart_log", "")))
log = Path("per_case") / "elf" / "uart.log"
if source_log.is_file():
    (run_root / log).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_log, run_root / log)
case = {
    "test_name": name,
    "status": status,
    "tohost": str(result.get("tohost", "")),
    "root_cause": "" if status == "PASS" else str(result.get("error", status)),
    "uart_log": str(log) if (run_root / log).is_file() else "",
}
(run_root / "cases.json").write_text(json.dumps([case], indent=2) + "\n")
PY
  # Run ELF logs come from users' own ELFs: keep their analyses out of the memory the
  # ACT/damo jobs reuse, so a crafted log cannot plant an "analysis" for real failures.
  export TRIAGE_MEMORY_DB="${TRIAGE_MEMORY_DB:-/data/ci/agent/triage-memory/run-elf.sqlite}"
  ci/triage/jenkins_triage.sh "$run_root" "$state_root" "$board_id"
}

case "$action" in
  prepare) prepare ;;
  run) run ;;
  triage) triage ;;
  *) echo "Usage: $0 {prepare|run|triage}" >&2; exit 2 ;;
esac
