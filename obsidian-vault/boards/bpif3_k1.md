---
board: bpif3_k1
core: SpacemiT K1 (X60)
---

# Banana Pi BPI-F3 (bpif3_k1)

No reported issue names this board. Issues that apply to its failing tests:
[[ACT-1854]] PMPSm_cfg_A_tor_zero uses address 0 (the K1 has RAM there; map it in sail.json),
[[ACT-2206]] ExceptionsZalrsc sc.w fault assumption, [[ACT-2028]] misaligned AMO/LR-SC
priority, [[ACT-1502]] mcounteren writable bits.
