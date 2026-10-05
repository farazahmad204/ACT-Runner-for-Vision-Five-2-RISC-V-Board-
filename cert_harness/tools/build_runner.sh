#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: build_runner.sh --board <board> [options]

Options:
  --out-root <dir>  Artifact root (default: cert_harness/build)
  --no-package      Skip board boot-image packaging
  -h, --help        Show this help
EOF
}

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
board=""
out_root="cert_harness/build"
package_image=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --board) board="${2:-}"; shift 2 ;;
    --out-root) out_root="${2:-}"; shift 2 ;;
    --no-package) package_image=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$board" ]] || { usage >&2; exit 2; }
board_file="$repo_root/cert_harness/boards/${board}.env"
profile_file="$repo_root/cert_harness/profiles/UART_M_MODE.env"
[[ -f "$board_file" ]] || { echo "Unsupported board: $board" >&2; exit 2; }

# shellcheck disable=SC1090
source "$board_file"
# shellcheck disable=SC1090
source "$profile_file"

runner_revision="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || printf unknown)"
runner_build_id="${RUNNER_BUILD_ID_OVERRIDE:-${runner_revision:0:12}}"
[[ "$runner_build_id" =~ ^[0-9A-Za-z._-]{1,63}$ ]] || {
  echo "Invalid RUNNER_BUILD_ID_OVERRIDE: $runner_build_id" >&2
  exit 2
}

tool_prefix="${TOOLCHAIN_DIR:-}/${TOOLCHAIN_TRIPLE:-riscv64-unknown-elf}"
if [[ -x "${tool_prefix}-gcc" ]]; then
  cc="${tool_prefix}-gcc"
  objcopy="${tool_prefix}-objcopy"
  objdump="${tool_prefix}-objdump"
  size_bin="${tool_prefix}-size"
else
  cc="${TOOLCHAIN_TRIPLE:-riscv64-unknown-elf}-gcc"
  objcopy="${TOOLCHAIN_TRIPLE:-riscv64-unknown-elf}-objcopy"
  objdump="${TOOLCHAIN_TRIPLE:-riscv64-unknown-elf}-objdump"
  size_bin="${TOOLCHAIN_TRIPLE:-riscv64-unknown-elf}-size"
fi

board_cflags=()
for var in \
  BOARD_PLATFORM_ID BOARD_UART_BASE BOARD_UART_SIZE BOARD_UART_REG_IO_WIDTH \
  BOARD_UART_8250_INIT_ENABLE BOARD_UART_INPUT_CLOCK_HZ BOARD_UART_BAUD BOARD_H_STATE_RESET \
  BOARD_UART_RX_DIAG BOARD_EXTERNAL_IRQ_CLEANUP_ENABLE \
  BOARD_PLIC_BASE BOARD_UART_PLIC_SOURCE BOARD_RUNNER_M_PLIC_CONTEXT \
  BOARD_RUNNER_S_PLIC_CONTEXT BOARD_CLINT_MSIP_BASE BOARD_CLINT_MTIMECMP_BASE \
  BOARD_CLINT_MTIME_ADDR BOARD_RUNNER_HART_ID BOARD_MONITOR_HART_ID \
  BOARD_FIXED_TOHOST_ADDR BOARD_RAM_BASE BOARD_RAM_LIMIT \
  BOARD_PAYLOAD_BASE BOARD_PAYLOAD_LIMIT \
  BOARD_DDR_EXCLUDE_BASE BOARD_DDR_EXCLUDE_LIMIT BOARD_UART_ELF_BUFFER_ADDR \
  BOARD_UART_ELF_MAX_BYTES BOARD_TEST_STACK_BYTES BOARD_TRAP_STACK_BYTES \
  BOARD_WDT_ENABLE BOARD_WDT_BASE BOARD_WDT_LOAD BOARD_WDT_CTRL BOARD_WDT_LOCK \
  BOARD_WDT_UNLOCK_KEY BOARD_K1_HART_WAKEUP_ENABLE
do
  [[ -z "${!var+x}" ]] || board_cflags+=("-D$var=${!var}")
done

board_ldflags=("-Wl,--defsym=BOARD_FW_LOAD_ADDR=$BOARD_FW_LOAD_ADDR")
board_ldflags+=("-Wl,--defsym=BOARD_FW_STACK_BYTES=$BOARD_FW_STACK_BYTES")

if [[ "$out_root" = /* ]]; then
  out_dir="$out_root/$board/UART_M_MODE"
else
  out_dir="$repo_root/$out_root/$board/UART_M_MODE"
fi
mkdir -p "$out_dir"

cd "$repo_root"
make -f Makefile.act clean
make -f Makefile.act \
  CC="$cc" OBJCOPY="$objcopy" OBJDUMP="$objdump" \
  LINKER_SCRIPT="${BOARD_LINKER_SCRIPT:-link.ld}" \
  RUNNER_BUILD_ID="$runner_build_id" \
  "BOARD_CFLAGS=${board_cflags[*]}" \
  "BOARD_LDFLAGS=${board_ldflags[*]}"

cp -f firmware.elf firmware.bin firmware.dis firmware.map "$out_dir/"
"$size_bin" "$out_dir/firmware.elf" > "$out_dir/size.txt"

if [[ "$package_image" -eq 1 && "${BOARD_PACKAGE_MODE:-}" == "adapter" ]]; then
  adapter_file="$repo_root/cert_harness/board_adapters/${BOARD_ADAPTER:-$board}.sh"
  [[ -f "$adapter_file" ]] || { echo "Missing board adapter: $adapter_file" >&2; exit 1; }
  # shellcheck disable=SC1090
  source "$adapter_file"
  declare -F board_package_image >/dev/null || {
    echo "Adapter does not define board_package_image: $adapter_file" >&2
    exit 1
  }
  board_package_image "$repo_root" "$out_dir/firmware.bin" "$out_dir"
elif [[ "$package_image" -eq 1 ]]; then
  mkimage -f "$BOARD_FIT_SOURCE" "$out_dir/boot_image.bin"
  if [[ -n "${BOARD_BOOT_IMAGE_PAD_BYTES:-}" ]]; then
    truncate -s "$BOARD_BOOT_IMAGE_PAD_BYTES" "$out_dir/boot_image.bin"
  fi
fi

(
  cd "$out_dir"
  sha256sum firmware.elf firmware.bin firmware.dis firmware.map size.txt > sha256sums.txt
  [[ ! -f boot_image.bin ]] || sha256sum boot_image.bin >> sha256sums.txt
)

cat > "$out_dir/manifest.json" <<EOF
{
  "schema_version": 1,
  "board": "$board",
  "profile": "UART_M_MODE",
  "transport": "uart_stream",
  "execution_mode": "M",
  "runner_revision": "$runner_revision",
  "runner_build_id": "$runner_build_id",
  "toolchain": "$($cc --version | head -n 1)",
  "artifact_dir": "$out_dir"
}
EOF

echo "Built $board UART runner at $out_dir"
echo "Runner revision: $runner_revision"
echo "Runner build ID: $runner_build_id"
