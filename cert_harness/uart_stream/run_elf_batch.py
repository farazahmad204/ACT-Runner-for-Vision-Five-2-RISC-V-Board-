#!/usr/bin/env python3
"""Run multiple ELFs through the one-ELF UART runner with power isolation."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


DONE_RE = re.compile(
    rb"\[UART_STREAM\] DONE name=([^\r\n ]+) "
    rb"status=(PASS|FAIL|TIMEOUT|ERROR) tohost=(0x[0-9a-fA-F]+)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Power-cycle a board and send one UART ELF per boot."
    )
    parser.add_argument("elfs", nargs="+", type=Path)
    parser.add_argument("--serial-dev", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--tuya-config", type=Path, default=Path("devices.json"))
    parser.add_argument("--device-name",
                        help="Select a named devices.json entry; <name>:<socket> for one outlet of a strip")
    parser.add_argument(
        "--expect-board",
        help="Require every boot to report this board identity",
    )
    parser.add_argument("--cycle-delay", type=float, default=3.0)
    parser.add_argument("--ready-timeout", type=float, default=180.0)
    parser.add_argument("--result-timeout", type=float, default=300.0)
    parser.add_argument(
        "--transport-retries",
        type=int,
        default=2,
        help="Additional power-cycle/send attempts after a transport failure",
    )
    parser.add_argument(
        "--expect-runner-build",
        help="Require every boot to report this runner build ID",
    )
    parser.add_argument(
        "--no-power-cycle",
        action="store_true",
        help="Prompt for a manual reset before each ELF",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue after a transport error",
    )
    parser.add_argument(
        "--leave-power-on",
        action="store_true",
        help="Do not turn the smart outlet off when the batch exits",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print actions without UART or power changes",
    )
    return parser.parse_args()


def safe_name(path: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem).strip("._")
    return name or "elf"


def load_tuya_env(config: Path, device_name: str | None) -> dict[str, str]:
    """Tuya settings for tuya_plug_ctl.py. device_name is a devices.json name, optionally with
    the outlet of a multi-socket strip: "MELECTRO:1" is socket 1 of the entry "MELECTRO"."""
    env = os.environ.copy()
    if device_name and ":" in device_name:
        device_name, switch = device_name.rsplit(":", 1)
        if not switch.isdigit() or int(switch) < 1:
            raise ValueError(f"bad outlet {switch!r} in device name; use <name>:<socket number>")
        env["TUYA_SWITCH"] = switch
    required = ("TUYA_DEVICE_ID", "TUYA_DEVICE_IP", "TUYA_LOCAL_KEY")
    if all(env.get(key) for key in required):
        return env

    devices = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(devices, list) or not devices:
        raise ValueError(f"{config} must contain a non-empty device list")
    if device_name:
        matches = [item for item in devices if item.get("name") == device_name]
        if len(matches) != 1:
            raise ValueError(
                f"expected one device named {device_name!r}, found {len(matches)}"
            )
        device = matches[0]
    else:
        device = devices[0]

    mapping = {
        "TUYA_DEVICE_ID": device.get("id"),
        "TUYA_DEVICE_IP": device.get("ip"),
        "TUYA_LOCAL_KEY": device.get("key"),
        "TUYA_VERSION": str(device.get("version", "3.4")),
    }
    for key, value in mapping.items():
        if value and not env.get(key):
            env[key] = value
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise ValueError(f"missing Tuya fields: {', '.join(missing)}")
    return env


def payload_started(log_path: Path) -> bool:
    """The runner received the ELF and began executing it (so a missing result is a timeout of
    the program, not a transport failure)."""
    if not log_path.exists():
        return False
    text = log_path.read_bytes()
    return b"[UART_STREAM] RX_OK" in text and b"[CASE] START" in text


def parse_done(log_path: Path) -> tuple[str, str, str] | None:
    matches = list(DONE_RE.finditer(log_path.read_bytes()))
    if not matches:
        return None
    match = matches[-1]
    return tuple(part.decode("ascii", errors="replace") for part in match.groups())


def write_junit(
    path: Path,
    results: list[dict[str, object]],
    elapsed: float,
    board_id: str,
) -> None:
    failures = sum(result["status"] != "PASS" for result in results)
    suite = ET.Element(
        "testsuite",
        name=f"{board_id}-uart-stream",
        tests=str(len(results)),
        failures=str(failures),
        errors="0",
        time=f"{elapsed:.3f}",
    )
    for result in results:
        case = ET.SubElement(
            suite,
            "testcase",
            classname=f"{board_id}.uart_stream",
            name=str(result["name"]),
            time=f'{float(result["elapsed_seconds"]):.3f}',
        )
        if result["status"] != "PASS":
            failure = ET.SubElement(
                case,
                "failure",
                message=f'target status {result["status"]}',
                type="UARTTargetFailure",
            )
            failure.text = f'UART log: {result["uart_log"]}'
        ET.SubElement(case, "system-out").text = f'UART log: {result["uart_log"]}'
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    args = parse_args()
    if args.transport_retries < 0:
        raise SystemExit("--transport-retries must be non-negative")
    repo_root = Path(__file__).resolve().parents[2]
    sender = repo_root / "cert_harness/uart_stream/send_elf.py"
    power_ctl = repo_root / "tuya_plug_ctl.py"
    elfs = [path.resolve() for path in args.elfs]

    for elf in elfs:
        data = elf.read_bytes()
        if len(data) < 64 or data[:4] != b"\x7fELF":
            raise SystemExit(f"not an ELF file: {elf}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = (args.run_dir or Path(f"logs/runs/uart_stream_batch_{stamp}")).resolve()
    print(f"[UART_BATCH] cases={len(elfs)} run_dir={run_dir}")
    for index, elf in enumerate(elfs, 1):
        print(f"[UART_BATCH] plan index={index} elf={elf}")
    if args.dry_run:
        print("[UART_BATCH] dry-run complete; no power or UART action performed")
        return 0

    run_dir.mkdir(parents=True, exist_ok=True)
    power_env = None
    power_off_registered = False
    if not args.no_power_cycle:
        power_env = load_tuya_env(args.tuya_config, args.device_name)
        if not args.leave_power_on:
            def power_off() -> None:
                subprocess.run(
                    [sys.executable, str(power_ctl), "off"],
                    cwd=repo_root,
                    env=power_env,
                    check=False,
                )

            atexit.register(power_off)
            power_off_registered = True

    results: list[dict[str, object]] = []
    batch_started = time.monotonic()
    for index, elf in enumerate(elfs, 1):
        case_name = safe_name(elf)
        print(f"\n[UART_BATCH] START index={index}/{len(elfs)} name={case_name}")
        case_started = time.monotonic()
        max_attempts = args.transport_retries + 1
        done = None
        process_returncode = 1
        log_path = run_dir / f"{index:04d}_{case_name}.uart.log"
        attempt = 0

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                log_path = run_dir / f"{index:04d}_{case_name}.attempt{attempt}.uart.log"
            print(
                f"[UART_BATCH] ATTEMPT index={index} attempt={attempt}/{max_attempts}"
            )
            if args.no_power_cycle:
                input("Power-cycle/reset the board, then press Enter to continue: ")
            else:
                print(f"[UART_BATCH] power_cycle delay={args.cycle_delay}s")
                subprocess.run(
                    [
                        sys.executable,
                        str(power_ctl),
                        "cycle",
                        "--delay",
                        str(args.cycle_delay),
                    ],
                    cwd=repo_root,
                    env=power_env,
                    check=True,
                )

            command = [
                sys.executable,
                str(sender),
                str(elf),
                "--serial-dev",
                args.serial_dev,
                "--baud",
                str(args.baud),
                "--log",
                str(log_path),
                "--ready-timeout",
                str(args.ready_timeout),
                "--result-timeout",
                str(args.result_timeout),
            ]
            if args.expect_runner_build:
                command.extend(["--expect-runner-build", args.expect_runner_build])
            if args.expect_board:
                command.extend(["--expect-board", args.expect_board])
            process = subprocess.run(command, cwd=repo_root, check=False)
            process_returncode = process.returncode
            done = parse_done(log_path) if log_path.exists() else None
            if done is not None:
                break
            if payload_started(log_path):
                # The runner received the ELF and started it, but it never finished within the
                # result timeout: the program hangs (no tohost write). Retrying cannot change
                # that and holds the board for another timeout, so report TIMEOUT now.
                done = (case_name, "TIMEOUT", "")
                print(f"[UART_BATCH] NO_RETRY index={index} name={case_name} reason=payload_started_no_result")
                break
            if attempt < max_attempts:
                print(
                    f"[UART_BATCH] RETRY index={index} name={case_name} "
                    "reason=transport_error",
                    file=sys.stderr,
                )

        elapsed = round(time.monotonic() - case_started, 3)
        if done:
            target_name, status, tohost = done
        else:
            target_name, status, tohost = case_name, "TRANSPORT_ERROR", ""
        result = {
            "index": index,
            "elf": str(elf),
            "name": target_name,
            "status": status,
            "tohost": tohost,
            "sender_returncode": process_returncode,
            "attempts": attempt,
            "elapsed_seconds": elapsed,
            "uart_log": str(log_path),
        }
        results.append(result)
        print(
            f"[UART_BATCH] RESULT index={index} name={target_name} "
            f"status={status} tohost={tohost or 'unavailable'} elapsed={elapsed}s"
        )
        if done is None and not args.keep_going:
            print("[UART_BATCH] stopping after transport error", file=sys.stderr)
            break

    counts: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1
    summary = {
        "board": args.expect_board or "unspecified",
        "planned": len(elfs),
        "completed": len(results),
        "counts": counts,
        "results": results,
    }
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    junit_path = run_dir / "junit.xml"
    write_junit(
        junit_path,
        results,
        time.monotonic() - batch_started,
        args.expect_board or "riscv",
    )
    print(f"\n[UART_BATCH] SUMMARY {json.dumps(counts, sort_keys=True)}")
    print(f"[UART_BATCH] summary_file={summary_path}")
    print(f"[UART_BATCH] junit_file={junit_path}")
    if power_off_registered:
        power_off()
        atexit.unregister(power_off)
    return 0 if len(results) == len(elfs) and counts == {"PASS": len(elfs)} else 1


if __name__ == "__main__":
    raise SystemExit(main())
