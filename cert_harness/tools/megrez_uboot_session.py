#!/usr/bin/env python3
"""Drive the Megrez U-Boot console over the onboard debug UART.

Steps run in order:
  --interrupt          wait for "Autoboot in" and stop it (tries several keys)
  --cmd TEXT           run a U-Boot command (repeatable)
  --loady FILE         YMODEM FILE to --addr with `loady`, then verify with
                       `crc32` against the host CRC32
  --flash-sha256 HEX   after a verified --loady, run `es_burn write <addr> flash`
                       ONLY if HEX equals the sha256 of the loaded FILE

Everything is logged to <out>/uart_raw.log and <out>/session.log.
Exit 0 on success, non-zero on the first failed step.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time
import zlib

import serial

# Line-anchored: crc32 output contains "==> ", which ends in "=> ".
PROMPT = b"\n=> "
STOP_KEYS = [b"s", b"\x03", b" ", b"\r", b"q"]


class Console:
    def __init__(self, dev: str, out: Path, open_now: bool = True) -> None:
        self.dev = dev
        out.mkdir(parents=True, exist_ok=True)
        self.raw = (out / "uart_raw.log").open("ab")
        self.log = (out / "session.log").open("a", encoding="utf-8")
        self.start = time.monotonic()
        self.data = bytearray()
        self.port: serial.Serial | None = None
        if open_now:
            self.open()

    def open(self, dev: str | None = None) -> None:
        # Keep DTR/RTS de-asserted: a boot with them asserted on the onboard
        # CH340 stalled between OpenSBI and U-Boot (observed on the Megrez onboard CH340).
        port = serial.Serial()
        port.port = dev or self.dev
        port.baudrate = 115200
        port.timeout = 0.05
        port.exclusive = True
        port.dtr = False
        port.rts = False
        port.open()
        self.port = port

    def close(self) -> None:
        if self.port:
            self.port.close()
            self.port = None

    def note(self, text: str) -> None:
        line = f"t=+{time.monotonic() - self.start:9.3f} HOST {text}"
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    def pump(self, seconds: float, marker: bytes | None = None, since: int | None = None) -> bool:
        since = len(self.data) if since is None else since
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            chunk = self.port.read(self.port.in_waiting or 1)
            if chunk:
                self.raw.write(chunk)
                self.raw.flush()
                self.data.extend(chunk)
                print(chunk.decode("latin1"), end="", flush=True)
            if marker is not None and marker in self.data[since:]:
                return True
        return False

    def run(self, command: str, timeout: float = 30.0) -> bytes:
        mark = len(self.data)
        self.port.write(command.encode() + b"\r")
        self.port.flush()
        if not self.pump(timeout, PROMPT, mark + len(command)):
            raise RuntimeError(f"no prompt after {command!r} within {timeout}s")
        return bytes(self.data[mark:])


def interrupt(con: Console, timeout: float, watch_dev: str | None = None) -> None:
    if watch_dev:
        # Boot with the console port closed; watch on another receiver and
        # open the console only once U-Boot is counting down.
        con.open(watch_dev)
        con.note(f"watching {watch_dev} up to {timeout:.0f}s for the U-Boot countdown")
        seen = con.pump(timeout, b"Autoboot in", 0)
        con.close()
        if not seen:
            raise RuntimeError("U-Boot countdown not seen on the watch port")
        con.open()
        con.note(f"countdown seen; console {con.dev} opened")
    else:
        con.note(f"waiting up to {timeout:.0f}s for the U-Boot countdown")
        if not con.pump(timeout, b"Autoboot in", 0):
            raise RuntimeError("U-Boot countdown not seen")
    mark = len(con.data)
    for attempt in range(200):
        key = STOP_KEYS[attempt % len(STOP_KEYS)]
        con.port.write(key)
        if con.pump(0.05, PROMPT, mark):
            con.note(f"autoboot stopped; prompt seen (last key {key!r})")
            con.pump(0.5)
            return
    raise RuntimeError("could not stop autoboot")


def loady(con: Console, path: Path, addr: str) -> None:
    payload = path.read_bytes()
    host_crc = zlib.crc32(payload) & 0xFFFFFFFF
    con.note(f"loady {path.name} bytes={len(payload)} host_crc32={host_crc:08x} addr={addr}")
    mark = len(con.data)
    con.port.write(f"loady {addr}\r".encode())
    # Read one byte at a time and stop at the end of the "Ready for binary"
    # line, so the receiver's first YMODEM 'C' stays buffered for sz.
    deadline = time.monotonic() + 15
    ready = False
    while time.monotonic() < deadline:
        byte = con.port.read(1)
        if not byte:
            continue
        con.raw.write(byte)
        con.data.extend(byte)
        if b"Ready for binary" in con.data[mark:]:
            ready = True
            if byte == b"\n":
                break
    con.raw.flush()
    if not ready:
        raise RuntimeError("U-Boot did not start YMODEM receive")
    # Hand sz the already-open port (raw 115200, DTR/RTS low) instead of
    # reopening the tty, which would assert DTR/RTS again. pyserial opens it
    # non-blocking; sz needs blocking I/O for the transfer.
    fd = con.port.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    # 115200 baud is about 11.5 KB/s; allow 3x plus a minute of handshake.
    limit = int(len(payload) / 11520 * 3) + 60
    try:
        result = subprocess.run(["sz", "--ymodem", "-b", str(path)], stdin=fd, stdout=fd,
                                stderr=subprocess.PIPE, timeout=limit)
        sz_status = f"exit={result.returncode} {result.stderr.decode(errors='replace').strip()[-200:]}"
    except subprocess.TimeoutExpired:
        # lrzsz sz can wait forever for the end-of-batch ACK even though the
        # file arrived. Ctrl-C aborts loady if it is still receiving; the CRC
        # check below is the real verification either way.
        result = None
        sz_status = f"no exit within {limit}s (end-of-batch wait or failed transfer)"
    fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    con.note(f"sz {sz_status}")
    con.port.write(b"\x03\r" if result is None else b"\r")
    if not con.pump(30, PROMPT):
        raise RuntimeError("no prompt after YMODEM transfer")
    out = con.run(f"crc32 {addr} {len(payload):x}")
    match = re.search(rb"==>\s*([0-9a-fA-F]{8})", out)
    if not match or int(match.group(1), 16) != host_crc:
        raise RuntimeError(f"CRC mismatch: target={match.group(1) if match else None} host={host_crc:08x}")
    con.note(f"VERIFIED {path.name} in RAM at {addr}: crc32={host_crc:08x}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial-dev", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--interrupt", action="store_true")
    ap.add_argument("--boot-timeout", type=float, default=900.0)
    ap.add_argument("--watch-dev", help="receive-only port to watch the boot on; --serial-dev opens at the countdown")
    ap.add_argument("--cmd", action="append", default=[])
    ap.add_argument("--loady", type=Path)
    ap.add_argument("--addr", default="0x90000000")
    ap.add_argument("--flash-sha256")
    args = ap.parse_args()

    if args.flash_sha256:
        if not args.loady:
            ap.error("--flash-sha256 requires --loady in the same session")
        digest = hashlib.sha256(args.loady.read_bytes()).hexdigest()
        if digest != args.flash_sha256.lower():
            ap.error(f"flash refused before touching the target: sha256 {digest} != approved {args.flash_sha256}")
    if args.watch_dev and not args.interrupt:
        ap.error("--watch-dev requires --interrupt")
    con = Console(args.serial_dev, args.out, open_now=not args.watch_dev)
    try:
        if args.interrupt:
            interrupt(con, args.boot_timeout, args.watch_dev)
        else:
            con.port.write(b"\r")
            if not con.pump(5, PROMPT):
                raise RuntimeError("no U-Boot prompt on the console")
        for command in args.cmd:
            con.note(f"run: {command}")
            con.run(command)
        if args.loady:
            loady(con, args.loady, args.addr)
        if args.flash_sha256:
            digest = hashlib.sha256(args.loady.read_bytes()).hexdigest()
            con.note(f"FLASH WRITE es_burn {args.loady.name} sha256={digest}")
            out = con.run(f"es_burn write {args.addr} flash", timeout=600)
            if b"error" in out.lower() or b"fail" in out.lower():
                raise RuntimeError("es_burn reported an error; see uart_raw.log")
            con.note("es_burn completed without an error message")
        con.note("RESULT ok")
        return 0
    except Exception as exc:  # noqa: BLE001 - report every failure the same way
        con.note(f"RESULT failed: {exc}")
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
