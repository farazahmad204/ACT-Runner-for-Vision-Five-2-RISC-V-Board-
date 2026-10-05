#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
script_repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
repo_root="${REPO_ROOT:-$script_repo_root}"
cd "$repo_root"

stage="${1:-}"
build_number="${BUILD_NUMBER:-manual}"
run_kind="${VF2_RUN_KIND:-weekly}"
run_id_prefix="${VF2_RUN_ID_PREFIX:-jenkins_${run_kind}}"
if [[ ! "$run_kind" =~ ^[a-z0-9][a-z0-9_-]*$ ]] ||
   [[ ! "$run_id_prefix" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
  echo "Invalid VF2_RUN_KIND or VF2_RUN_ID_PREFIX." >&2
  exit 2
fi
run_id="${run_id_prefix}_${build_number}"
state_root="$repo_root/logs/jenkins/$run_kind/$run_id"
state_file="$state_root/state.env"
test_scope="${ACT_TEST_SCOPE:-priv}"
case "$test_scope" in
  priv) test_dir="$state_root/all_priv_tests" ;;
  all) test_dir="$state_root/all_tests" ;;
  unpriv) test_dir="$state_root/unpriv_tests" ;;
  hypervisor) test_dir="$state_root/hypervisor_tests" ;;
  *) echo "ACT_TEST_SCOPE must be 'priv', 'all', 'unpriv' or 'hypervisor'." >&2; exit 2 ;;
esac
# hypervisor is the privileged suites that exercise the H extension (H*, *H,
# *H<suffix>) and the Sh* profile extensions; it is staged like priv.
hypervisor_suite_regex="${HYPERVISOR_SUITE_REGEX:-^(H|Sh)|H(F|V|Gei|Sm|Zicbo|ZicboSm)?$}"
act_build_args=()
if [[ "$test_scope" == "all" || "$test_scope" == "unpriv" ]]; then
  act_build_args+=(--act-fast)
fi
generated_test_root="$state_root/generated_tests"
missing_report="$state_root/reference_failed_no_hardware_elf.txt"
reference_status="$state_root/sail_reference_status.tsv"
priv_source_roots="$state_root/priv_source_roots.txt"
sail_bin="${SAIL_BIN:-/home/lpt-10xe/riscv-sail-0.13/bin/sail_riscv_sim}"
sail_expected_version="${SAIL_EXPECTED_VERSION:-0.13}"
act_remote_url="${ACT_REMOTE_URL:-https://github.com/Arshia2564/riscv-arch-test.git}"
act_branch="${ACT_BRANCH:-sifive_u74}"
act_revision_override="${ACT_REVISION_OVERRIDE:-}"
act_revision_file="$state_root/resolved_act_revision.txt"
act_expected_revision="$act_revision_override"

if [[ "$stage" != "preflight" && -s "$act_revision_file" ]]; then
  act_expected_revision="$(tr -d '[:space:]' < "$act_revision_file")"
fi

all_extensions="${ACT_EXTENSIONS:-all}"
act_root="$repo_root/external/riscv-arch-test"
act_config="${ACT_CONFIG_PATH:-config/cores/sifive_u74/test_config.local.yaml}"
dut_yaml_relative="${ACT_DUT_YAML_PATH:-config/cores/sifive_u74/visionfive2-rv64gc.yaml}"
sail_json_relative="${ACT_SAIL_JSON_PATH:-config/cores/sifive_u74/sail.json}"
dut_macros_relative="${ACT_DUT_MACROS_PATH:-config/cores/sifive_u74/rvmodel_macros.h}"
dut_yaml="$act_root/$dut_yaml_relative"
sail_json="$act_root/$sail_json_relative"
act_workdir="${ACT_WORKDIR_NAME:-work-vf2-jenkins-all-priv}"
dut_name="${ACT_DUT_NAME:-visionfive2-rv64gc}"
artifact_root="$act_root/$act_workdir/$dut_name/build"
if [[ "$test_scope" == "priv" || "$test_scope" == "hypervisor" ]]; then
  artifact_root="$artifact_root/priv"
fi
reference_root="$repo_root/logs/reference-model-runs/$run_id"
pack_list="$repo_root/logs/runs/$run_id/act_elfs.list"
hardware_board="${HARDWARE_BOARD:-vf2_jh7110}"
platform_label="${HARDWARE_PLATFORM_LABEL:-VF2/U74}"
requested_generator_extensions="${PRIV_GENERATOR_EXTENSIONS:-}"
include_static_priv_suites="${INCLUDE_STATIC_PRIV_SUITES:-true}"
expected_test_names="${EXPECTED_TEST_NAMES:-}"
runner_resolution_file="${RUNNER_RESOLUTION_FILE:-$repo_root/.jenkins_runner_resolution.txt}"

mkdir -p "$state_root"

write_state() {
  local expected_cases="0"
  if [[ -f "$pack_list" ]]; then
    expected_cases="$(sed '/^[[:space:]]*$/d;/^[[:space:]]*#/d' "$pack_list" | wc -l | tr -d ' ')"
  fi
  {
    printf 'RUN_ID=%q\n' "$run_id"
    printf 'RUN_KIND=%q\n' "$run_kind"
    printf 'ACT_WORKDIR=%q\n' "$act_workdir"
    printf 'ACT_CONFIG=%q\n' "$act_config"
    printf 'DUT_YAML=%q\n' "$dut_yaml"
    printf 'SAIL_JSON=%q\n' "$sail_json"
    printf 'DUT_NAME=%q\n' "$dut_name"
    printf 'HARDWARE_BOARD=%q\n' "$hardware_board"
    printf 'HARDWARE_PLATFORM_LABEL=%q\n' "$platform_label"
    printf 'ARTIFACT_ROOT=%q\n' "$artifact_root"
    printf 'REFERENCE_ROOT=%q\n' "$reference_root"
    printf 'PACK_LIST=%q\n' "$pack_list"
    printf 'TEST_SCOPE=%q\n' "$test_scope"
    printf 'TEST_DIR=%q\n' "$test_dir"
    # Retained for compatibility with older report/tooling consumers.
    printf 'PRIV_TEST_DIR=%q\n' "$test_dir"
    printf 'MISSING_REPORT=%q\n' "$missing_report"
    printf 'REFERENCE_STATUS=%q\n' "$reference_status"
    printf 'PRIV_SOURCE_ROOTS=%q\n' "$priv_source_roots"
    printf 'EXPECTED_CASES=%q\n' "$expected_cases"
    printf 'EXPECTED_TEST_NAMES=%q\n' "$expected_test_names"
    printf 'ACT_REMOTE_URL=%q\n' "$act_remote_url"
    printf 'ACT_BRANCH=%q\n' "$act_branch"
    printf 'ACT_REVISION=%q\n' "$act_expected_revision"
  } > "$state_file"
}

load_state() {
  [[ -f "$state_file" ]] || { echo "Missing state file: $state_file" >&2; exit 1; }
  # shellcheck disable=SC1090
  source "$state_file"
}

case "$stage" in
  preflight)
    test -f Jenkinsfile.uart-sanity
    python3 ci/check_minimal_uart_repo.py
    git check-ref-format --branch "$act_branch" >/dev/null
    if ! git -C "$act_root" diff --quiet --ignore-submodules -- ||
       ! git -C "$act_root" diff --cached --quiet --ignore-submodules --; then
      echo "Tracked ACT files differ from the current checkout." >&2
      echo "Commit/revert those changes before Jenkins resolves $act_branch." >&2
      git -C "$act_root" status --short --untracked-files=no >&2
      exit 1
    fi

    jenkins_act_ref="refs/remotes/jenkins/$act_branch"
    fetch_ok=false
    for attempt in 1 2 3 4; do
      echo "ACT fetch attempt ${attempt}/4"
      if timeout 180 git -C "$act_root" \
        -c http.lowSpeedLimit=1 -c http.lowSpeedTime=30 \
        fetch --force "$act_remote_url" \
        "+refs/heads/$act_branch:$jenkins_act_ref"; then
        fetch_ok=true
        break
      fi
      if [[ "$attempt" -lt 4 ]]; then
        sleep "$((attempt * 15))"
      fi
    done
    [[ "$fetch_ok" == true ]] || {
      echo "Unable to fetch ACT branch after 4 attempts: $act_branch" >&2
      exit 1
    }

    fetched_act_revision="$(
      git -C "$act_root" rev-parse --verify "${jenkins_act_ref}^{commit}"
    )"
    if [[ -n "$act_revision_override" ]]; then
      if ! resolved_act_revision="$(
        git -C "$act_root" rev-parse --verify "${act_revision_override}^{commit}" 2>/dev/null
      )"; then
        echo "ACT_REVISION_OVERRIDE is not available after fetching $act_branch: $act_revision_override" >&2
        exit 1
      fi
      act_expected_revision="$resolved_act_revision"
      act_source="override"
    else
      resolved_act_revision="$fetched_act_revision"
      act_expected_revision="$resolved_act_revision"
      act_source="latest-branch-head"
    fi

    git -C "$act_root" checkout --detach "$resolved_act_revision"
    actual_act_revision="$(git -C "$act_root" rev-parse HEAD)"
    if git -C "$act_root" symbolic-ref -q HEAD >/dev/null; then
      echo "ACT checkout is still attached to a branch; refusing the run." >&2
      exit 1
    fi
    if [[ "$actual_act_revision" != "$resolved_act_revision" ]]; then
      echo "ACT checkout does not match the resolved revision." >&2
      echo "Resolved: $resolved_act_revision" >&2
      echo "HEAD:     $actual_act_revision" >&2
      exit 1
    fi
    printf '%s\n' "$resolved_act_revision" > "$act_revision_file"
    {
      echo "ACT repository:       $act_remote_url"
      echo "ACT branch:           $act_branch"
      echo "Fetched branch SHA:   $fetched_act_revision"
      echo "Selected source:      $act_source"
      echo "Selected ACT SHA:     $resolved_act_revision"
      echo "Checkout mode:        detached"
    } | tee "$state_root/act_resolution.txt"

    test -f ci/jenkins/stage_priv_tests.py
    test -f "$act_root/$act_config"
    test -f "$dut_yaml"
    test -f "$sail_json"
    test -x tools/act_agent/run_vf2_pack.py
    test -f tools/act_agent/run_reference_elf.py
    test -x cert_harness/tools/build_runner.sh
    command -v uv
    command -v make
    command -v mkimage
    command -v zip
    command -v curl
    command -v riscv64-unknown-elf-gcc
    test -x "$sail_bin"
    resolved_sail="$(command -v sail_riscv_sim)"
    if [[ "$(readlink -f "$resolved_sail")" != "$(readlink -f "$sail_bin")" ]]; then
      echo "Jenkins PATH resolves Sail to $resolved_sail, expected $sail_bin" >&2
      exit 1
    fi
    sail_version="$("$sail_bin" --version | head -n 1 | tr -d '\r')"
    if [[ "$sail_version" != "$sail_expected_version" ]]; then
      echo "Sail version mismatch: found '$sail_version', expected '$sail_expected_version'" >&2
      exit 1
    fi
    act_required_sail="$(
      sed -n 's/^REQUIRED_SAIL_VERSION = "\([^"]*\)"/\1/p' \
        "$act_root/framework/src/act/config.py"
    )"
    if [[ "$act_required_sail" != "$sail_expected_version" ]]; then
      echo "ACT framework/Sail mismatch: this checkout requires Sail $act_required_sail, but Jenkins is configured for $sail_expected_version." >&2
      echo "Update riscv-arch-test to a Sail-$sail_expected_version-compatible revision before starting the job." >&2
      exit 1
    fi
    configured_sail_entry="$(awk -F': *' '/^ref_model_exe:/ {print $2; exit}' "$act_root/$act_config" | awk '{print $1}')"
    if [[ "$configured_sail_entry" == */* ]]; then
      if [[ "$configured_sail_entry" == /* ]]; then
        configured_sail="$configured_sail_entry"
      else
        configured_sail="$act_root/$configured_sail_entry"
      fi
    else
      configured_sail="$(command -v "$configured_sail_entry" || true)"
    fi
    if [[ -z "$configured_sail" ]] ||
       [[ "$(readlink -f "$configured_sail")" != "$(readlink -f "$sail_bin")" ]]; then
      echo "ACT config uses $configured_sail_entry (resolved: ${configured_sail:-unavailable}), expected $sail_bin" >&2
      exit 1
    fi
    command -v spike
    free_kb="$(df -Pk "$repo_root" | awk 'NR == 2 { print $4 }')"
    if (( free_kb < 6 * 1024 * 1024 )); then
      echo "At least 6 GiB of free workspace space is required; found $((free_kb / 1024)) MiB." >&2
      exit 1
    fi
    python3 -m py_compile tools/act_agent/run_vf2_pack.py tools/act_agent/run_reference_elf.py
    if [[ -n "$runner_resolution_file" && -f "$runner_resolution_file" ]]; then
      cp -f "$runner_resolution_file" "$state_root/runner_resolution.txt"
    fi
    echo "Preflight passed for $run_id"
    ;;

  prepare)
    # This workdir is Jenkins-owned generated output. Recreate it so removed or
    # renamed tests can never be packed from stale artifacts.
    if [[ -d "$act_root/$act_workdir" ]]; then
      find "$act_root/$act_workdir" -depth -delete
    fi

    generator_exclude=""
    if [[ "$test_scope" == "all" ]]; then
      generator_extensions="all"
    else
      registered_priv_generator_extensions="$(
        cd "$act_root"
        uv run python -c \
          'import testgen.priv as p; get_suites = getattr(p, "get_priv_test_suites", None) or p.get_priv_test_extensions; print(",".join(sorted(get_suites())))'
      )"
      if [[ "$test_scope" == "hypervisor" ]]; then
        registered_priv_generator_extensions="$(
          printf '%s\n' "$registered_priv_generator_extensions" | tr ',' '\n' |
            { grep -E "$hypervisor_suite_regex" || true; } | paste -sd, -
        )"
        if [[ -z "$registered_priv_generator_extensions" ]]; then
          echo "ACT revision registers no hypervisor generators (regex: $hypervisor_suite_regex)." >&2
          exit 1
        fi
      fi
      if [[ "$test_scope" == "unpriv" ]]; then
        generator_extensions="all"
        generator_exclude="$registered_priv_generator_extensions"
      elif [[ -n "$requested_generator_extensions" ]]; then
        generator_extensions="$requested_generator_extensions"
        IFS=',' read -r -a requested_extensions <<< "$generator_extensions"
        for extension in "${requested_extensions[@]}"; do
          if [[ -z "$extension" ]] ||
             [[ ",$registered_priv_generator_extensions," != *",$extension,"* ]]; then
            echo "Requested privileged generator is unavailable: '$extension'" >&2
            exit 1
          fi
        done
      else
        generator_extensions="$registered_priv_generator_extensions"
      fi
    fi
    if [[ "${REGENERATE_TESTS:-true}" == "true" ]]; then
      if [[ -d "$generated_test_root" ]]; then
        find "$generated_test_root" -depth -delete
      fi
      (
        cd "$act_root"
        uv run testgen testplans -o "$generated_test_root" --jobs 0 \
          --extensions "$generator_extensions" --exclude "$generator_exclude"
      )
    elif [[ -d "$generated_test_root" ]]; then
      find "$generated_test_root" -depth -delete
    fi

    static_priv_suites=""
    if [[ "$test_scope" == "priv" && "$include_static_priv_suites" == "true" ]]; then
      static_priv_suites="$(
        git -C "$act_root" ls-tree -d --name-only HEAD:tests/priv | paste -sd, -
      )"
    elif [[ "$test_scope" == "hypervisor" && "$include_static_priv_suites" == "true" ]]; then
      static_priv_suites="$(
        git -C "$act_root" ls-tree -d --name-only HEAD:tests/priv |
          { grep -E "$hypervisor_suite_regex" || true; } | paste -sd, -
      )"
    fi
    stage_scope="$test_scope"
    if [[ "$test_scope" == "hypervisor" ]]; then
      stage_scope="priv"
    fi
    if [[ "$test_scope" == "all" || "$test_scope" == "unpriv" ]]; then
      printf '%s\n' "$test_scope" > "$priv_source_roots"
    else
      {
        printf '%s\n' "$generator_extensions" | tr ',' '\n'
        if [[ -n "$static_priv_suites" ]]; then
          printf '%s\n' "$static_priv_suites" | tr ',' '\n'
        fi
      } > "$priv_source_roots"
    fi

    python3 ci/jenkins/stage_priv_tests.py \
      --source "$act_root/tests" \
      --repository-root "$act_root" \
      --generated-source "$generated_test_root" \
      --destination "$test_dir" \
      --scope "$stage_scope" \
      --include-top-level "$static_priv_suites" \
      --include-generated-top-level "$generator_extensions"

    python3 tools/act_agent/run_vf2_pack.py \
      --run-id "$run_id" \
      --act-config "$act_config" \
      --act-workdir "$act_workdir" \
      --dut-name "$dut_name" \
      --artifact-root "$artifact_root" \
      --extensions "$all_extensions" \
      --test-dir "$test_dir" \
      --build-act-artifacts \
      --no-act-debug \
      "${act_build_args[@]}" \
      --skip-build \
      --skip-sd-write \
      --skip-serial-run \
      --skip-triage \
      --skip-final-snapshot \
      --expected-cases 0

    # UART streams individual self-checking ELFs.  Generate its input list
    # directly instead of invoking the removed SD/embedded act_pack.bin path.
    python3 - "$artifact_root" "$pack_list" <<'PY'
from pathlib import Path
import sys

artifact_root = Path(sys.argv[1]).resolve()
pack_list = Path(sys.argv[2]).resolve()
parts = artifact_root.parts
if "build" not in parts:
    raise SystemExit(f"ACT artifact root has no build component: {artifact_root}")
index = parts.index("build")
elf_root = Path(*parts[:index], "elfs", *parts[index + 1 :])
candidates = sorted(
    path.resolve()
    for path in elf_root.rglob("*.elf")
    if not path.name.endswith(".sig.elf")
)
if not candidates:
    raise SystemExit(f"No UART hardware ELFs found under {elf_root}")

# Work directories are recreated for every run, but de-duplicate by test name
# defensively so a test can never be executed twice through two nested paths.
by_name = {}
for path in candidates:
    by_name.setdefault(path.stem, path)
selected = [by_name[name] for name in sorted(by_name)]
pack_list.parent.mkdir(parents=True, exist_ok=True)
pack_list.write_text("".join(f"{path}\n" for path in selected))
print(f"UART ELF list: {len(selected)} files from {elf_root}")
PY

    if [[ -n "$expected_test_names" ]]; then
      python3 - "$pack_list" "$expected_test_names" <<'PY'
from pathlib import Path
import sys

pack_list = Path(sys.argv[1])
expected = sorted(name.strip() for name in sys.argv[2].split(",") if name.strip())
actual = sorted(
    Path(line.strip()).stem
    for line in pack_list.read_text().splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
if actual != expected:
    print("Sanity pack does not contain the exact requested test set.", file=sys.stderr)
    print(f"Expected ({len(expected)}): {expected}", file=sys.stderr)
    print(f"Actual   ({len(actual)}): {actual}", file=sys.stderr)
    raise SystemExit(1)
print(f"Validated exact hardware pack: {len(actual)} tests: {', '.join(actual)}")
PY
    fi

    python3 - "$artifact_root" "$pack_list" "$missing_report" "$reference_status" <<'PY'
from pathlib import Path
import sys

artifact_root = Path(sys.argv[1])
referenced = {
    path.name[:-8]
    for path in artifact_root.rglob("*.sig.elf")
}
packed = {
    path.name[:-4]
    for raw in Path(sys.argv[2]).read_text().splitlines()
    if raw.strip() and not raw.lstrip().startswith("#")
    for path in [Path(raw.strip())]
}
missing = sorted(referenced - packed)
Path(sys.argv[3]).write_text("".join(f"{name}\n" for name in missing))
rows = ["test_name\tsail_status\thardware_elf\n"]
rows.extend(f"{name}\tPASS\tyes\n" for name in sorted(packed))
rows.extend(f"{name}\tFAIL_OR_BLOCKED\tno\n" for name in missing)
Path(sys.argv[4]).write_text("".join(rows))
print(f"Sail eligibility: reference_attempted={len(referenced)} runnable={len(packed)} reference_failed={len(missing)}")
if missing:
    print("Sail did not produce self-checking hardware ELFs for:")
    for name in missing:
        print(f"  {name}")
PY

    write_state
    ;;

  spike)
    load_state
    mkdir -p "$REFERENCE_ROOT"
    total=0
    failed=0
    : > "$state_root/spike_status.tsv"
    while IFS= read -r elf; do
      [[ -n "$elf" && "${elf#\#}" == "$elf" ]] || continue
      name="$(basename "$elf" .elf)"
      out="$REFERENCE_ROOT/$name/spike"
      total=$((total + 1))
      if python3 tools/act_agent/run_reference_elf.py "$elf" \
          --model spike --execute --out-dir "$out" --spike-timeout 120; then
        printf '%s\tPASS\n' "$name" >> "$state_root/spike_status.tsv"
      else
        printf '%s\tFAIL\n' "$name" >> "$state_root/spike_status.tsv"
        failed=$((failed + 1))
      fi
    done < "$PACK_LIST"
    printf 'total=%s\nfailed=%s\n' "$total" "$failed" > "$state_root/spike_counts.txt"
    [[ "$failed" -eq 0 ]]
    ;;

  *)
    echo "Usage: $0 {preflight|prepare|spike}" >&2
    exit 2
    ;;
esac
