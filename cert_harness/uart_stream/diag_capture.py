#!/usr/bin/env python3
"""UART ELF capture: send_elf.py framing with long waits and
host timestamps on every target line (used to estimate the mtime rate).

Writes <out>/uart_raw.log (raw bytes), <out>/uart_timed.log (host-relative
timestamps per line) and <out>/host_events.log. Exit 0 on DONE status=PASS.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import time

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_elf import make_header  # noqa: E402

READY_RE = re.compile(rb"\[UART_STREAM\] READY [^\r\n]*board=(\S+) runner_build=(\S+)")


class Capture:
    def __init__(self, out: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        self.raw = (out / "uart_raw.log").open("wb")
        self.timed = (out / "uart_timed.log").open("w", encoding="utf-8")
        self.events = (out / "host_events.log").open("w", encoding="utf-8")
        self.start = time.monotonic()
        self.partial = b""
        self.data = b""

    def now(self) -> float:
        return time.monotonic() - self.start

    def event(self, text: str) -> None:
        line = f"t=+{self.now():9.3f} HOST {text}"
        print(line, flush=True)
        self.events.write(line + "\n")
        self.events.flush()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.raw.write(chunk)
        self.raw.flush()
        self.data += chunk
        self.partial += chunk
        while b"\n" in self.partial:
            line, self.partial = self.partial.split(b"\n", 1)
            text = line.replace(b"\r", b"").decode("latin1")
            stamped = f"t=+{self.now():9.3f} {text}"
            print(stamped, flush=True)
            self.timed.write(stamped + "\n")
            self.timed.flush()


def pump(port: serial.Serial, cap: Capture, seconds: float, until: list[bytes]) -> bytes | None:
    """Capture for up to `seconds`; return the first marker seen after now."""
    mark = len(cap.data)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cap.feed(port.read(port.in_waiting or 1))
        for marker in until:
            if marker in cap.data[mark:]:
                return marker
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("elf", type=Path)
    ap.add_argument("--serial-dev", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--expect-board", required=True)
    ap.add_argument("--expect-runner-build", required=True)
    ap.add_argument("--ready-timeout", type=float, default=900.0)
    ap.add_argument("--header-timeout", type=float, default=600.0)
    ap.add_argument("--result-timeout", type=float, default=300.0)
    ap.add_argument("--linger", type=float, default=5.0)
    args = ap.parse_args()

    payload = args.elf.read_bytes()
    name = args.elf.stem
    header, crc = make_header(name, payload)
    cap = Capture(args.out)
    cap.event(f"elf={args.elf} bytes={len(payload)} crc32=0x{crc:08x} header_bytes={len(header)}")

    with serial.Serial(args.serial_dev, 115200, timeout=0.1, write_timeout=30.0, exclusive=True) as port:
        port.reset_input_buffer()
        cap.event(f"listening on {args.serial_dev}; boot the board now")
        if pump(port, cap, args.ready_timeout, [b"[UART_STREAM] READY"]) is None:
            cap.event("RESULT no_ready")
            return 2
        pump(port, cap, 2.0, [b"\n"])  # finish the READY line
        match = READY_RE.search(cap.data)
        board = match.group(1).decode() if match else "?"
        build = match.group(2).decode() if match else "?"
        cap.event(f"ready board={board} build={build}")
        if board != args.expect_board or build != args.expect_runner_build:
            cap.event("RESULT identity_mismatch")
            return 3

        port.write(header)
        port.flush()
        cap.event(f"header_sent={len(header)}")
        seen = pump(port, cap, args.header_timeout,
                    [b"[UART_STREAM] HEADER_OK", b"[UART_STREAM] ERROR"])
        if seen != b"[UART_STREAM] HEADER_OK":
            # Keep reading so the runner's [DIAG] report after the error lands.
            pump(port, cap, 30.0, [b"[DIAG] regs tag=header"])
            pump(port, cap, args.linger, [])
            cap.event(f"RESULT header_failed marker={seen!r}")
            return 4
        pump(port, cap, 2.0, [b"\n"])

        for offset in range(0, len(payload), 4096):
            port.write(payload[offset:offset + 4096])
            cap.feed(port.read(port.in_waiting))
        port.flush()
        cap.event(f"payload_sent={len(payload)}")
        seen = pump(port, cap, args.result_timeout,
                    [b"[UART_STREAM] DONE", b"[UART_STREAM] ERROR"])
        pump(port, cap, args.linger, [])
        tail = cap.data[cap.data.rfind(b"[UART_STREAM] DONE"):] if seen else b""
        passed = seen == b"[UART_STREAM] DONE" and b"status=PASS" in tail.split(b"\n", 1)[0]
        cap.event(f"RESULT {'done_pass' if passed else 'done_not_pass'} marker={seen!r}")
        return 0 if passed else 5


if __name__ == "__main__":
    raise SystemExit(main())
