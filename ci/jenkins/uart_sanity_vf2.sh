#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
export REPO_ROOT="$repo_root"
export RUNNER_RESOLUTION_FILE="$repo_root/.jenkins_runner_resolution.txt"

export VF2_RUN_KIND="${VF2_RUN_KIND:-uart_sanity}"
export VF2_RUN_ID_PREFIX="${VF2_RUN_ID_PREFIX:-jenkins_uart_sanity}"
export ACT_WORKDIR_NAME="${ACT_WORKDIR_NAME:-work-vf2-jenkins-uart-sanity-priv}"
export PRIV_GENERATOR_EXTENSIONS="${PRIV_GENERATOR_EXTENSIONS-ExceptionsF,ExceptionsS,ExceptionsSm,ExceptionsU,ExceptionsZc}"
export INCLUDE_STATIC_PRIV_SUITES="${INCLUDE_STATIC_PRIV_SUITES:-false}"
export EXPECTED_TEST_NAMES="${EXPECTED_TEST_NAMES-ExceptionsF-00,ExceptionsS-00,ExceptionsSm-00,ExceptionsU-00,ExceptionsZc-00}"

uart_board="${UART_RUNNER_BOARD:-vf2_jh7110}"
uart_expected_board="${UART_EXPECT_BOARD:-$uart_board}"
uart_device_name="${UART_DEVICE_NAME:-}"

stage="${1:-}"
build_number="${BUILD_NUMBER:-manual}"
run_id="${VF2_RUN_ID_PREFIX}_${build_number}"
state_root="$repo_root/logs/jenkins/$VF2_RUN_KIND/$run_id"
state_file="$state_root/state.env"
run_root="$repo_root/logs/runs/$run_id"

case "$stage" in
  preflight|prepare|spike)
    exec "$script_dir/weekly_vf2.sh" "$stage"
    ;;

  verify-runner-image)
    # Look the board up by its runner id, so every board in ci/runner_inventory.json works.
    inventory_build="$(python3 -c 'import json,sys; b=[v for v in json.load(open(sys.argv[1]))["boards"].values() if v["runner_board_id"]==sys.argv[2]]; b or sys.exit("board not in runner_inventory.json: "+sys.argv[2]); print(b[0]["installed_build_id"])' "$repo_root/ci/runner_inventory.json" "$uart_board")"
    expected_build="${INSTALLED_RUNNER_BUILD:-$inventory_build}"
    if [[ ! "$expected_build" =~ ^[0-9A-Za-z._-]{1,63}$ ]]; then
      echo "Invalid runner build ID: $expected_build" >&2
      exit 2
    fi
    image="$repo_root/cert_harness/build/$uart_board/UART_M_MODE/boot_image.bin"
    PATH="/home/lpt-10xe/riscv64/bin:$PATH" \
      bash "$repo_root/cert_harness/tools/build_runner.sh" --board "$uart_board"
    test -f "$image"
    mkdir -p "$state_root"
    sha256sum "$image" > "$state_root/uart_runner_image.sha256"
    printf '%s\n' "$expected_build" > "$state_root/expected_runner_build.txt"
    ;;

  run)
    test -f "$state_file"
    # shellcheck disable=SC1090
    source "$state_file"
    mapfile -t elfs < <(sed '/^[[:space:]]*$/d;/^[[:space:]]*#/d' "$PACK_LIST")
    if [[ "${#elfs[@]}" -eq 0 ]]; then
      echo "No runnable ELFs in $PACK_LIST" >&2
      exit 1
    fi
    expected_build="$(tr -d '[:space:]' < "$state_root/expected_runner_build.txt")"
    serial_dev="${SERIAL_DEV:-/dev/ttyUSB0}"
    batch_args=(
      "${elfs[@]}"
      --serial-dev "$serial_dev"
      --run-dir "$run_root"
      --ready-timeout "${UART_READY_TIMEOUT:-180}"
      --result-timeout "${UART_RESULT_TIMEOUT:-600}"
      --transport-retries "${UART_TRANSPORT_RETRIES:-2}"
      --expect-board "$uart_expected_board"
      --expect-runner-build "$expected_build"
      --keep-going
    )
    if [[ -n "$uart_device_name" ]]; then
      batch_args+=(--device-name "$uart_device_name")
    fi
    python3 "$repo_root/cert_harness/uart_stream/run_elf_batch.py" "${batch_args[@]}"
    ;;

  finalize)
    mkdir -p "$state_root"
    runner_commit="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)"
    {
      echo "run_id=$run_id"
      echo "completed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "runner_commit=$runner_commit"
      echo "git_head=$runner_commit"
      echo "sail_version=${SAIL_EXPECTED_VERSION:-unknown}"
      echo "transport=uart_stream"
      echo "board=$uart_board"
      echo "runner_board_identity=$uart_expected_board"
      echo "installed_runner_build=$(cat "$state_root/expected_runner_build.txt" 2>/dev/null || true)"
      echo "runner_inventory=ci/runner_inventory.json"
    } > "$state_root/jenkins_manifest.txt"
    if [[ -d "$run_root" ]]; then
      if [[ -f "$run_root/summary.json" ]]; then
        python3 "$script_dir/prepare_uart_portal_results.py" --run-root "$run_root"
      fi
      cp -f "$run_root/summary.json" "$state_root/summary.json" 2>/dev/null || true
      cp -f "$run_root/junit.xml" "$state_root/junit.xml" 2>/dev/null || true
    fi
    ;;

  *)
    echo "Usage: $0 {preflight|prepare|spike|verify-runner-image|run|finalize}" >&2
    exit 2
    ;;
esac
