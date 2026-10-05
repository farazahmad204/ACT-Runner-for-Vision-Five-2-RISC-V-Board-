import os, sys, time, threading, subprocess, select, zlib, tempfile, glob, termios, tty
tool, img, out = sys.argv[1], sys.argv[2], sys.argv[3]; sha = sys.argv[4] if len(sys.argv) > 4 else None
a, b = f"{out}.ptyA", f"{out}.ptyB"
soc = subprocess.Popen(["socat", f"pty,raw,echo=0,link={a}", f"pty,raw,echo=0,link={b}"])
for _ in range(50):
    if os.path.exists(a) and os.path.exists(b): break
    time.sleep(0.1)
fd = os.open(b, os.O_RDWR | os.O_NOCTTY); tty.setraw(fd)
recv = tempfile.mkdtemp(); st = {"file": None, "burned": None}
def w(s): os.write(fd, s)
def target():
    time.sleep(1); w(b"U-Boot 2024.01-gdbb5f9e3\r\nAutoboot in 5 seconds\r\n")
    buf = b""; stopped = False
    while True:
        r, _, _ = select.select([fd], [], [], 600)
        if not r: return
        buf += os.read(fd, 256)
        if not stopped:
            if b"s" in buf: stopped = True; buf = b""; w(b"\r\n=> ")
            continue
        while b"\r" in buf:
            line, buf = buf.split(b"\r", 1); line = line.strip(); w(line + b"\r\n")
            if line.startswith(b"loady"):
                w(b"## Ready for binary (ymodem) download to 0x90000000 at 115200 bps...\r\n")
                subprocess.run(["rz", "--ymodem", "-y"], stdin=fd, stdout=fd, cwd=recv, stderr=subprocess.DEVNULL, timeout=120)
                st["file"] = open(glob.glob(recv + "/*")[0], "rb").read(); buf = b""
                w(b"## Total Size = 0x%08x\r\n" % len(st["file"]))
            elif line.startswith(b"crc32"):
                n = int(line.split()[2], 16); c = zlib.crc32(st["file"][:n]) & 0xffffffff
                w(b"crc32 for 90000000 ... %08x ==> " % (0x90000000 + n - 1)); time.sleep(0.05); w(b"%08x\r\n" % c)
            elif line.startswith(b"es_burn"):
                st["burned"] = len(st["file"]); w(b"es_burn: write ok\r\n")
            w(b"=> ")
threading.Thread(target=target, daemon=True).start()
args = [sys.executable, tool, "--serial-dev", a, "--out", out, "--interrupt", "--boot-timeout", "10", "--cmd", "version", "--loady", img]
if sha: args += ["--flash-sha256", sha]
try:
    r = subprocess.run(args, capture_output=True, text=True, timeout=420)
    print("rc=", r.returncode, "burned_bytes=", st["burned"]); print("\n".join(l for l in r.stdout.splitlines() if "HOST" in l))
finally:
    soc.terminate()
