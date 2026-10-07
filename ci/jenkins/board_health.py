#!/usr/bin/env python3
"""Report whether each board can take a Run ELF job, and post it to the portal.

A board is online when its USB-UART device is present and usable by this agent
and its smart plug answers a read-only status query. Nothing is powered,
switched or locked: a board that is busy running a job is still online.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import stat
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Portal slug -> USB-UART device and smart-plug name in devices.json (None: no plug yet).
BOARDS = {
    "visionfive2": {"serial": "/dev/ttyCI-vf2", "plug": None},
    "bananapi-f3": {"serial": "/dev/ttyCI-bpif3", "plug": None},
    "milkv-megrez": {"serial": "/dev/ttyCI-megrez", "plug": "SCW1050"},
}


def check_serial(path: str) -> tuple[bool, str]:
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return False, f"USB-UART {path} not connected"
    if not stat.S_ISCHR(mode):
        return False, f"{path} is not a serial device"
    if not os.access(path, os.R_OK | os.W_OK):
        return False, f"{path} is not accessible to the CI agent"
    return True, "present"


def check_plug(devices: list[dict], name: str, timeout: float) -> tuple[bool, str, str]:
    """Return (reachable, reason, power) using a read-only status query."""
    matches = [d for d in devices if d.get("name") == name]
    if len(matches) != 1:
        return False, f"smart plug {name} is not configured", "unknown"
    device = matches[0]
    try:
        import tinytuya

        plug = tinytuya.OutletDevice(device.get("id"), device.get("ip"), device.get("key"))
        plug.set_version(float(device.get("version", 3.4)))
        plug.set_socketTimeout(timeout)
        plug.set_socketRetryLimit(1)
        response = plug.status()
    except Exception as exc:  # network errors and tinytuya internals alike
        return False, f"smart plug {name} unreachable ({type(exc).__name__})", "unknown"
    dps = response.get("dps") if isinstance(response, dict) else None
    if not isinstance(dps, dict):
        error = response.get("Error", "no reply") if isinstance(response, dict) else "no reply"
        return False, f"smart plug {name} unreachable ({error})", "unknown"
    power = {True: "on", False: "off"}.get(dps.get("1"), "unknown")
    return True, "reachable", power


def collect(devices: list[dict], plug_timeout: float) -> list[dict]:
    reports = []
    for slug, config in BOARDS.items():
        serial_ok, serial_note = check_serial(config["serial"])
        checks = {"serial": serial_note}
        reasons = [] if serial_ok else [serial_note]
        if config["plug"] is None:
            checks["plug"] = "none"
            reasons.append("no smart plug")
        else:
            plug_ok, plug_note, power = check_plug(devices, config["plug"], plug_timeout)
            checks["plug"] = plug_note
            checks["power"] = power
            if not plug_ok:
                reasons.append(plug_note)
        reports.append({"slug": slug, "online": not reasons, "reason": "; ".join(reasons),
                        "checks": checks})
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", type=Path, default=Path("/data/ci/devices.json"))
    parser.add_argument("--portal-url", default="https://apollo/portal/")
    parser.add_argument("--ca-file", type=Path, default=Path("/data/ci/agent/jenkins-internal-ca.crt"))
    parser.add_argument("--plug-timeout", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true", help="print the report; do not post it")
    args = parser.parse_args()

    try:
        devices = json.loads(args.devices.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        devices = []
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "boards": collect(devices if isinstance(devices, list) else [], args.plug_timeout),
    }
    for board in payload["boards"]:
        state = "online" if board["online"] else "offline"
        print(f"[BOARD_HEALTH] {board['slug']}: {state} {board['reason']}".rstrip())
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    token = os.environ.get("PORTAL_INGEST_TOKEN", "")
    if not token:
        print("PORTAL_INGEST_TOKEN is not set", file=sys.stderr)
        return 2
    request = urllib.request.Request(
        args.portal_url.rstrip("/") + "/api/v1/boards/health/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Portal-Token": token},
        method="POST",
    )
    context = ssl.create_default_context(cafile=str(args.ca_file))
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        print(f"[BOARD_HEALTH] portal updated: {json.loads(response.read()).get('updated')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
