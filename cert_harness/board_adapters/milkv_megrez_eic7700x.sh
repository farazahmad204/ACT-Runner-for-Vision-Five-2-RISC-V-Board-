#!/usr/bin/env bash

# Milk-V Megrez / ESWIN EIC7700X adapter. Packaging is offline-only by
# default; persistent SPI writes require a separately reviewed procedure.

board_package_image() {
  local repo_root="$1"
  local firmware_bin="$2"
  local out_dir="$3"
  local packaging_root="${MEGREZ_NSIGN_ROOT:-}"
  local preload_dir
  local nsign
  local work_dir="$out_dir/nsign-work"
  local generated_cfg="$work_dir/bootchain.runner.cfg"
  local generated_image="$work_dir/bootloader_secboot_ddr5.bin"

  : "$repo_root"

  if [[ -z "$packaging_root" ]]; then
    echo "ERROR: set MEGREZ_NSIGN_ROOT to a pinned rockos-opensbi checkout" >&2
    return 1
  fi
  preload_dir="$packaging_root/sign/preload"
  nsign="$packaging_root/sign/nsign"

  [[ -x "$nsign" ]] || { echo "ERROR: Megrez nsign not executable: $nsign" >&2; return 1; }
  [[ -f "$preload_dir/bootchain.cfg" ]] || { echo "ERROR: missing bootchain.cfg" >&2; return 1; }
  [[ -f "$preload_dir/sys_init.bin" ]] || { echo "ERROR: missing sys_init.bin" >&2; return 1; }
  [[ -f "$preload_dir/ddr_fw.bin" ]] || { echo "ERROR: missing ddr_fw.bin" >&2; return 1; }
  [[ -f "$firmware_bin" ]] || { echo "ERROR: missing runner binary: $firmware_bin" >&2; return 1; }

  mkdir -p "$work_dir"
  cp -f "$preload_dir/sys_init.bin" "$work_dir/sys_init.bin"
  cp -f "$preload_dir/ddr_fw.bin" "$work_dir/ddr_fw.bin"
  cp -f "$firmware_bin" "$work_dir/fw_payload.bin"
  sed "s#HOLDER#$work_dir#g" "$preload_dir/bootchain.cfg" > "$generated_cfg"

  "$nsign" "$generated_cfg"
  [[ -f "$generated_image" ]] || { echo "ERROR: nsign did not produce $generated_image" >&2; return 1; }

  cp -f "$generated_image" "$out_dir/bootloader_secboot_ddr5.bin"
  cp -f "$generated_image" "$out_dir/boot_image.bin"
  sha256sum "$nsign" "$preload_dir/bootchain.cfg" \
    "$work_dir/sys_init.bin" "$work_dir/ddr_fw.bin" \
    "$work_dir/fw_payload.bin" "$generated_cfg" \
    "$out_dir/boot_image.bin" > "$out_dir/megrez_packaging_sha256sums.txt"
}

board_flash_image() {
  echo "ERROR: Megrez SPI flashing is not automated: it needs Recovery mode," >&2
  echo "the official U-Boot and explicit approval. Use cert_harness/tools/megrez_uboot_session.py" >&2
  echo "as described in cert_harness/boards/milkv_megrez_eic7700x/README.md." >&2
  return 1
}

board_write_pack() {
  echo "ERROR: Megrez UART transport does not use an SD-tail ELF pack." >&2
  return 1
}

board_capture_uart() {
  local serial_dev="$2"
  local log_path="$3"

  mkdir -p "$(dirname "$log_path")"
  picocom -b 115200 --flow n -q --logfile "$log_path" "$serial_dev"
}
