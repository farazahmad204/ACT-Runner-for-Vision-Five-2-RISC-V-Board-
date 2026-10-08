#!/usr/bin/env bash
# damo-rv-priv-ats on the Milk-V Megrez: build the selected suites for
# CONFIG=milkv-megrez-p550, run one suite ELF per power cycle through the UART
# runner, and turn the suites' [TEST]/[PASS]/[FAIL] output into per-case results.
# No reference model is involved: the suites check their own expected values.
#
#   megrez_damo.sh {prepare|run|finalize}
#
# Environment (Jenkins parameters): DAMO_REMOTE_URL, DAMO_BRANCH,
# DAMO_REVISION_OVERRIDE, DAMO_SUITES, SERIAL_DEV, UART_DEVICE_NAME,
# INSTALLED_RUNNER_BUILD, UART_READY_TIMEOUT, UART_RESULT_TIMEOUT.
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
board="milkv_megrez_eic7700x"
run_id="jenkins_megrez_damo_${BUILD_NUMBER:-manual}"
state_root="$repo_root/logs/jenkins/megrez_damo/$run_id"
run_root="$repo_root/logs/runs/$run_id"
damo_root="$repo_root/external/damo-rv-priv-ats"
elf_root="$state_root/elfs"
toolchain="${DAMO_CROSS_COMPILER:-/data/ci/toolchains/riscv64/bin/riscv64-unknown-elf-}"
default_suites="Hypervisor_CSR,Hypervisor_Exceptions,Hypervisor_Interrupts,Hypervisor_PMP,Hypervisor_Zaamo,Hypervisor_Zalrsc,Hypervisor_Zca,Hypervisor_Zicntr,Hypervisor_Zihpm,Hypervisor_Sscofpmf,Sv39x4,Sv48x4,Sv39x4_Sv39,Sv39x4_Sv48,Sv48x4_Sv39,Sv48x4_Sv48,Sha,Shcounterenw,Shgatpa,Shtvala,Shvsatpa,Shvstvala,Shvstvecd"

prepare() {
  local branch="${DAMO_BRANCH:-main}" suites suite elf built=0
  git check-ref-format --branch "$branch" >/dev/null
  rm -rf "$damo_root" "$state_root"
  mkdir -p "$damo_root" "$elf_root" "$run_root"
  git -C "$damo_root" init -q
  git -C "$damo_root" remote add origin "$DAMO_REMOTE_URL"
  timeout 300 git -C "$damo_root" fetch -q --depth 1 origin "+refs/heads/$branch:refs/remotes/origin/$branch"
  if [[ -n "${DAMO_REVISION_OVERRIDE:-}" ]]; then
    timeout 300 git -C "$damo_root" fetch -q origin "$DAMO_REVISION_OVERRIDE"
    git -C "$damo_root" checkout -q --detach FETCH_HEAD
  else
    git -C "$damo_root" checkout -q --detach "origin/$branch"
  fi
  test -d "$damo_root/config/milkv-megrez-p550" || {
    echo "$DAMO_REMOTE_URL@$branch has no config/milkv-megrez-p550" >&2
    exit 2
  }
  echo "damo-rv-priv-ats: $branch @ $(git -C "$damo_root" rev-parse HEAD)"

  IFS=',' read -r -a suites <<< "${DAMO_SUITES:-$default_suites}"
  : > "$state_root/pack_list.txt"
  : > "$state_root/build_failures.txt"
  for suite in "${suites[@]}"; do
    suite="$(echo "$suite" | tr -d '[:space:]')"
    [[ -n "$suite" ]] || continue
    if [[ ! "$suite" =~ ^[A-Za-z0-9_]+$ || ! -f "$damo_root/$suite/Makefile" ]]; then
      echo "Unknown damo suite: $suite" | tee -a "$state_root/build_failures.txt" >&2
      continue
    fi
    if (cd "$damo_root/$suite" && make -s clean >/dev/null 2>&1 &&
        make CONFIG=milkv-megrez-p550 CROSS_COMPILER="$toolchain" > "$state_root/build_$suite.log" 2>&1); then
      elf="$(ls "$damo_root/$suite"/*.elf | head -1)"
      cp "$elf" "$elf_root/$suite.elf"
      echo "$elf_root/$suite.elf" >> "$state_root/pack_list.txt"
      built=$((built + 1))
    else
      echo "Build failed: $suite (see build_$suite.log)" | tee -a "$state_root/build_failures.txt" >&2
    fi
  done
  echo "Built $built suite ELF(s) for the Megrez."
  [[ "$built" -gt 0 ]] || exit 1

  # The runner identifies itself in READY; compare it with the installed build.
  local inventory_build expected_build
  inventory_build="$(python3 -c 'import json,sys; b=[v for v in json.load(open(sys.argv[1]))["boards"].values() if v["runner_board_id"]==sys.argv[2]]; b or sys.exit("board not in runner_inventory.json: "+sys.argv[2]); print(b[0]["installed_build_id"])' "$repo_root/ci/runner_inventory.json" "$board")"
  expected_build="${INSTALLED_RUNNER_BUILD:-$inventory_build}"
  [[ "$expected_build" =~ ^[0-9A-Za-z._-]{1,63}$ ]] || { echo "Invalid runner build ID: $expected_build" >&2; exit 2; }
  printf '%s\n' "$expected_build" > "$state_root/expected_runner_build.txt"
  {
    echo "RUN_ID=$run_id"
    # The portal files these results under its Hypervisor category.
    echo "TEST_SCOPE=hypervisor"
    echo "DAMO_REMOTE_URL=$DAMO_REMOTE_URL"
    echo "DAMO_BRANCH=$branch"
    echo "DAMO_REVISION=$(git -C "$damo_root" rev-parse HEAD)"
  } > "$state_root/state.env"
}

run() {
  local elfs expected_build batch_args
  mapfile -t elfs < <(sed '/^[[:space:]]*$/d' "$state_root/pack_list.txt")
  [[ "${#elfs[@]}" -gt 0 ]] || { echo "No damo ELFs to run" >&2; exit 1; }
  expected_build="$(tr -d '[:space:]' < "$state_root/expected_runner_build.txt")"
  batch_args=(
    "${elfs[@]}"
    --serial-dev "${SERIAL_DEV:-/dev/ttyCI-megrez}"
    --run-dir "$run_root"
    --ready-timeout "${UART_READY_TIMEOUT:-300}"
    --result-timeout "${UART_RESULT_TIMEOUT:-900}"
    --transport-retries "${UART_TRANSPORT_RETRIES:-2}"
    --expect-board "$board"
    --expect-runner-build "$expected_build"
    --keep-going
  )
  [[ -z "${UART_DEVICE_NAME:-}" ]] || batch_args+=(--device-name "$UART_DEVICE_NAME")
  python3 "$repo_root/cert_harness/uart_stream/run_elf_batch.py" "${batch_args[@]}"
}

finalize() {
  mkdir -p "$state_root"
  {
    echo "run_id=$run_id"
    echo "completed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "runner_commit=$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"
    echo "test_source=damo-rv-priv-ats $(git -C "$damo_root" rev-parse HEAD 2>/dev/null || true)"
    echo "transport=uart_stream"
    echo "board=$board"
    echo "installed_runner_build=$(cat "$state_root/expected_runner_build.txt" 2>/dev/null || true)"
  } > "$state_root/jenkins_manifest.txt"
  if [[ -f "$run_root/summary.json" ]]; then
    python3 "$script_dir/damo_cases.py" --run-root "$run_root"
    cp -f "$run_root/summary.json" "$state_root/summary.json"
    cp -f "$run_root/junit.xml" "$state_root/junit.xml" 2>/dev/null || true
  fi
}

case "${1:-}" in
  prepare) prepare ;;
  run) run ;;
  finalize) finalize ;;
  *) echo "Usage: $0 {prepare|run|finalize}" >&2; exit 2 ;;
esac
