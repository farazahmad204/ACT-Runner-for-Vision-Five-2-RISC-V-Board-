/* Generic RISC-V CSR probe (M-mode, run by the UART runner on its runner hart).
 *
 * Each probe stores two dwords in the signature region:
 *   value   - CSR/memory value read (0 if the access trapped)
 *   trapped - 0 if no trap, else mcause + 1 of the last trap
 * Every CSR that is written is restored. The probe ends with tohost = 3
 * (FAIL) on purpose: the runner dumps the signature region only for
 * non-passing cases. Build and decode with scripts/csr_probe.py.
 */
    .option norvc            /* all instructions 4 bytes: the trap handler skips +4 */

#define SLOT_STORE(reg)  sd reg, 0(s0); sd s11, 8(s0); addi s0, s0, 16

/* Read a CSR by number. */
#define READ(csr)               \
    li s11, 0;                  \
    li t0, 0;                   \
    csrr t0, csr;               \
    SLOT_STORE(t0)

/* Write a value, read it back, restore the original. Stores the read-back. */
#define WARL(csr, val)          \
    li s11, 0;                  \
    li t0, 0;                   \
    li t1, 0;                   \
    csrr t1, csr;               \
    li t2, val;                 \
    csrw csr, t2;               \
    csrr t0, csr;               \
    csrw csr, t1;               \
    SLOT_STORE(t0)

    .section .text.init, "ax", @progbits
    .globl _start
_start:
    la t0, probe_trap
    csrw mtvec, t0
    csrci mstatus, 8          /* MIE = 0: pending bits cannot fire */
    la s0, begin_signature

    /* 0-8: H and interrupt state at entry (before anything is written). */
    READ(0x645)   /* hvip */
    READ(0x644)   /* hip */
    READ(0x604)   /* hie */
    READ(0x603)   /* hideleg */
    READ(0x602)   /* hedeleg */
    READ(0x600)   /* hstatus */
    READ(0x680)   /* hgatp */
    READ(0x280)   /* vsatp */
    READ(0x344)   /* mip */

    /* 9-14: identity and delegation at entry. */
    READ(0x301)   /* misa */
    READ(0xF11)   /* mvendorid */
    READ(0xF12)   /* marchid */
    READ(0xF13)   /* mimpid */
    READ(0x303)   /* mideleg */
    READ(0x302)   /* medeleg */

    /* 15-23: writable masks (write all-ones, read back, restore). */
    WARL(0x302, -1)                   /* medeleg */
    WARL(0x303, -1)                   /* mideleg */
    WARL(0x602, -1)                   /* hedeleg */
    WARL(0x603, -1)                   /* hideleg */
    WARL(0x645, -1)                   /* hvip */
    WARL(0x607, -1)                   /* hgeie: GEILEN mask */
    WARL(0x680, 0x03FFF00000000000)   /* hgatp: MODE=Bare, VMID all ones */
    WARL(0xB03, -1)                   /* mhpmcounter3: counter width */
    WARL(0x105, 0xFFFFFFFFFFFFFFFC)   /* stvec: BASE all ones, MODE=Direct */

    /* 24-25: stvec sign-extension probes (Sstvecd-00 bit 50 case). */
    WARL(0x105, 0x0004000000000000)
    WARL(0x105, 0x0000800000000000)

    /* 26-35: existence (expect illegal-instruction trap if absent). */
    READ(0xC01)   /* time */
    READ(0x30A)   /* menvcfg (priv 1.12) */
    READ(0x60A)   /* henvcfg (H 1.0 / priv 1.12) */
    READ(0xF15)   /* mconfigptr (priv 1.12) */
    READ(0x34A)   /* mtinst */
    READ(0x34B)   /* mtval2 */
    READ(0x64A)   /* htinst */
    READ(0x643)   /* htval */
    READ(0x14D)   /* stimecmp (Sstc) */
    READ(0x320)   /* mcountinhibit */

    /* 36: load from the candidate RVMODEL_ACCESS_FAULT_ADDRESS (PROBE_FAULT_ADDR, default 0). */
#ifndef PROBE_FAULT_ADDR
#define PROBE_FAULT_ADDR 0
#endif
    li s11, 0
    li t0, 0
    li t3, PROBE_FAULT_ADDR
    ld t0, 0(t3)
    SLOT_STORE(t0)

    /* 37: misaligned load inside the payload window. */
    li s11, 0
    li t0, 0
    la t3, misaligned_target
    addi t3, t3, 1
    lw t0, 0(t3)
    SLOT_STORE(t0)

    /* 38-52: CSRs the ACT H boot code and trap handlers touch. */
    READ(0x605)   /* htimedelta */
    READ(0x606)   /* hcounteren */
    WARL(0x606, -1)                   /* hcounteren */
    READ(0x200)   /* vsstatus */
    READ(0x204)   /* vsie */
    READ(0x205)   /* vstvec */
    READ(0x240)   /* vsscratch */
    READ(0x241)   /* vsepc */
    READ(0x242)   /* vscause */
    READ(0x243)   /* vstval */
    READ(0x244)   /* vsip */
    READ(0xE12)   /* hgeip */
    READ(0x10A)   /* senvcfg */
    READ(0x106)   /* scounteren */
    READ(0x306)   /* mcounteren */

    /* Report FAIL on purpose so the runner dumps the signature region. */
    fence rw, rw
    li t0, 3
    la t1, tohost
    sd t0, 0(t1)
1:  j 1b

    .balign 256
probe_trap:
    csrr s11, mcause
    addi s11, s11, 1
    csrr t6, mepc
    addi t6, t6, 4
    csrw mepc, t6
    mret

    .section .data
    .balign 64
misaligned_target:
    .dword 0x0807060504030201, 0x100F0E0D0C0B0A09
    .balign 64
    .globl begin_signature
begin_signature:
    .zero 1024                /* 64 slots of 16 bytes */
    .globl end_signature
end_signature:

    .section .tohost, "aw", @progbits
    .balign 64
    .globl tohost
tohost:   .dword 0
    .globl fromhost
fromhost: .dword 0
