#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

export VF2_RUN_KIND="megrez_uart_weekly"
export VF2_RUN_ID_PREFIX="jenkins_megrez_uart_weekly"
case "${TEST_SCOPE:-all}" in
  all|priv|unpriv|hypervisor) export ACT_TEST_SCOPE="${TEST_SCOPE:-all}" ;;
  *) echo "TEST_SCOPE must be 'all', 'priv', 'unpriv' or 'hypervisor'." >&2; exit 2 ;;
esac
export ACT_WORKDIR_NAME="work-megrez-jenkins-uart-weekly-${ACT_TEST_SCOPE}"
export PRIV_GENERATOR_EXTENSIONS=""
export INCLUDE_STATIC_PRIV_SUITES="true"
export EXPECTED_TEST_NAMES=""

exec "$script_dir/uart_sanity_megrez.sh" "$@"
