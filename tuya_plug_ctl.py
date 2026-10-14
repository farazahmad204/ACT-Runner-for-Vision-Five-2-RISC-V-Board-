#!/usr/bin/env python3
import argparse
import fcntl
import os
import sys
import time

import tinytuya


def parse_args():
    p = argparse.ArgumentParser(description="Control a Tuya outlet over LAN")
    p.add_argument(
        "cmd",
        choices=["on", "off", "status", "cycle"],
        nargs="?",
        default="status",
        help="Action to run",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Cycle delay in seconds (only used for cycle)",
    )
    p.add_argument("--id", dest="device_id", default=os.getenv("TUYA_DEVICE_ID"))
    p.add_argument("--ip", dest="device_ip", default=os.getenv("TUYA_DEVICE_IP"))
    p.add_argument("--key", dest="local_key", default=os.getenv("TUYA_LOCAL_KEY"))
    p.add_argument(
        "--switch",
        type=int,
        default=int(os.getenv("TUYA_SWITCH", "1")),
        help="Outlet (DPS index) on a multi-socket strip; 1 for a single plug",
    )
    p.add_argument("--ver", type=float, default=float(os.getenv("TUYA_VERSION", "3.3")))
    p.add_argument("--retries", type=int, default=3, help="Retries per action on Tuya errors")
    p.add_argument("--retry-delay", type=float, default=2.0, help="Delay between retries in seconds")
    p.add_argument(
        "--socket-timeout",
        type=float,
        default=float(os.getenv("TUYA_SOCKET_TIMEOUT", "3.0")),
        help="Per-attempt Tuya socket timeout in seconds",
    )
    p.add_argument(
        "--socket-retries",
        type=int,
        default=int(os.getenv("TUYA_SOCKET_RETRIES", "1")),
        help="tinytuya internal socket connect retries per action",
    )
    p.add_argument(
        "--socket-retry-delay",
        type=float,
        default=float(os.getenv("TUYA_SOCKET_RETRY_DELAY", "0.5")),
        help="Delay between tinytuya internal socket retries in seconds",
    )
    return p.parse_args()


def response_ok(resp):
    return isinstance(resp, dict) and "Error" not in resp


def perform_with_retry(action_name, fn, retries, retry_delay):
    last_resp = None
    attempts = retries if retries > 0 else 1
    for attempt in range(1, attempts + 1):
        try:
            resp = fn()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            resp = {"Error": f"{type(exc).__name__}: {exc}", "Err": "EXC", "Payload": None}
        last_resp = resp
        print(resp)
        if response_ok(resp):
            return resp
        if attempt < attempts:
            print(
                f"[HOST] tuya {action_name} failed attempt {attempt}/{attempts}, "
                f"retrying in {retry_delay}s",
                file=sys.stderr,
            )
            time.sleep(retry_delay)
    raise SystemExit(f"Tuya {action_name} failed after {attempts} attempts: {last_resp}")


def main():
    args = parse_args()
    missing = []
    if not args.device_id:
        missing.append("--id/TUYA_DEVICE_ID")
    if not args.device_ip:
        missing.append("--ip/TUYA_DEVICE_IP")
    if not args.local_key:
        missing.append("--key/TUYA_LOCAL_KEY")
    if missing:
        raise SystemExit("Missing required values: " + ", ".join(missing))

    # One command at a time per device: boards on different sockets of the same strip are
    # power-cycled by concurrent jobs, and the strip handles one LAN session at a time.
    lock = open(f"/tmp/tuya-{args.device_id}.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)

    d = tinytuya.OutletDevice(args.device_id, args.device_ip, args.local_key)
    d.set_version(args.ver)
    d.set_socketTimeout(args.socket_timeout)
    d.set_socketRetryLimit(max(1, args.socket_retries))
    d.set_socketRetryDelay(max(0.0, args.socket_retry_delay))

    def turn_on():
        return d.turn_on(switch=args.switch)

    def turn_off():
        return d.turn_off(switch=args.switch)

    if args.cmd == "on":
        perform_with_retry("on", turn_on, args.retries, args.retry_delay)
    elif args.cmd == "off":
        perform_with_retry("off", turn_off, args.retries, args.retry_delay)
    elif args.cmd == "cycle":
        perform_with_retry("off", turn_off, args.retries, args.retry_delay)
        time.sleep(args.delay)
        perform_with_retry("on", turn_on, args.retries, args.retry_delay)
    else:
        resp = perform_with_retry("status", d.status, args.retries, args.retry_delay)
        dps = resp.get("dps") if isinstance(resp, dict) else None
        if isinstance(dps, dict):
            print(f"switch {args.switch}: " + {True: "on", False: "off"}.get(dps.get(str(args.switch)), "unknown"))


if __name__ == "__main__":
    main()
