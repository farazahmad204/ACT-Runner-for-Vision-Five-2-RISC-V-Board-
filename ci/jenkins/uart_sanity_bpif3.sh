#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

export VF2_RUN_KIND="${VF2_RUN_KIND:-bpif3_uart_sanity}"
export VF2_RUN_ID_PREFIX="${VF2_RUN_ID_PREFIX:-jenkins_bpif3_uart_sanity}"
export ACT_CONFIG_PATH="config/cores/bpif3/bpif3-rva22s64/test_config.yaml"
export ACT_DUT_YAML_PATH="config/cores/bpif3/bpif3-rva22s64/bpif3-rva22s64.yaml"
export ACT_SAIL_JSON_PATH="config/cores/bpif3/bpif3-rva22s64/sail.json"
export ACT_DUT_MACROS_PATH="config/cores/bpif3/bpif3-rva22s64/rvmodel_macros.h"
export ACT_DUT_NAME="bpif3-rva22s64"
export ACT_WORKDIR_NAME="${ACT_WORKDIR_NAME:-work-bpif3-jenkins-uart-sanity-priv}"
export ACT_TEST_SCOPE="${ACT_TEST_SCOPE:-priv}"
export HARDWARE_BOARD="bpif3_k1"
export HARDWARE_PROFILE="UART_M_MODE"
export HARDWARE_PLATFORM_LABEL="BPI-F3/K1"
export UART_RUNNER_BOARD="bpif3_k1"
export UART_EXPECT_BOARD="bpif3_k1"
export UART_DEVICE_NAME="${UART_DEVICE_NAME:-MELECTRO:3}"
export PRIV_GENERATOR_EXTENSIONS="${PRIV_GENERATOR_EXTENSIONS-ExceptionsF,ExceptionsS,ExceptionsSm,ExceptionsU,ExceptionsZc}"
export INCLUDE_STATIC_PRIV_SUITES="${INCLUDE_STATIC_PRIV_SUITES:-false}"
export EXPECTED_TEST_NAMES="${EXPECTED_TEST_NAMES-ExceptionsF-00,ExceptionsS-00,ExceptionsSm-00,ExceptionsSm_medeleg_m-00,ExceptionsSm_medeleg_s-00,ExceptionsSm_medeleg_u-00,ExceptionsU-00,ExceptionsZc-00}"

exec "$script_dir/uart_sanity_vf2.sh" "$@"
