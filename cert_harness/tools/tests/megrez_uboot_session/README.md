# megrez_uboot_session.py offline tests

Fake U-Boot on a socat pty pair, running a real `rz` YMODEM receiver and real
CRC32. Usage: `python3 <fake>.py <megrez_uboot_session.py> <image> <out-prefix> [flash-sha256]`.

| Harness | Checks |
|---|---|
| `fake_uboot.py` | interrupt, `loady`, CRC verify; with sha argument reaches `es_burn` |
| `fake_uboot_watch.py` | same, using `--watch-dev` |
| `fake_uboot_corrupt.py` | flips byte 1000 after transfer; tool must refuse (CRC mismatch, no `es_burn`) |

Wrong-sha interlock: `megrez_uboot_session.py --serial-dev /dev/null --out X --loady IMG
--flash-sha256 0000` must exit 2 without creating X.

Known harness artifact: lrzsz `rz` exits without ACKing the YMODEM
end-of-batch block, so `sz` can hang; the tool handles this and gates on CRC.
Results 2026-10-03: 6/6 verified (3 with sz hang), corrupt refused, wrong-sha refused.
