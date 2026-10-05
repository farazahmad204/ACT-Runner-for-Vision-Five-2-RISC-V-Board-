/* start.S - minimal reset entry, hart roles set by BOARD_RUNNER_HART_ID/BOARD_MONITOR_HART_ID */

#ifndef BOARD_RUNNER_HART_ID
#define BOARD_RUNNER_HART_ID 1
#endif
#ifndef BOARD_MONITOR_HART_ID
#define BOARD_MONITOR_HART_ID 2
#endif

.section .text
.globl _start
.type _start, @function
.globl g_boot_sync

.section .data
g_boot_sync:
    .dword 0

.section .text

_start:
    /* ---- Use hart1 as runner, hart2 as monitor; park others ---- */
    csrr t0, mhartid
    li   t1, BOARD_RUNNER_HART_ID
    beq  t0, t1, .Lhart1
    li   t1, BOARD_MONITOR_HART_ID
    beq  t0, t1, .Lhart2
    j    .Lpark

.Lhart1:
#if defined(BOARD_K1_HART_WAKEUP_ENABLE) && BOARD_K1_HART_WAKEUP_ENABLE
    /* CSR_ML2SETUP (0x7F0, undocumented) - per-hart L1 snoop-enable bit,
       required for intra-cluster cache coherency (CCI-500 only covers
       cross-cluster/L2, irrelevant here since hart0/hart1 share a cluster).
       Must be set by each hart on itself - CSRs have no cross-hart write
       path - matching every real call site in vendor OpenSBI
       (spacemit_cold_boot_allowed(), psci_enable_core_snoop()), all of which
       derive the bit from the executing hart's own mhartid rather than any
       compile-time hart-role constant, so this does too. Must happen before
       this hart's first shared-memory access below (g_boot_sync). */
    csrr t4, mhartid
    andi t4, t4, 3
    li   t5, 1
    sll  t5, t5, t4
    csrs 0x7f0, t5
#endif
    /* ---- Hart1 stack ---- */
    la   sp, __stack_top
    li   t2, 4096
    sub  sp, sp, t2

    /* ---- Clear .bss ---- */
    la   t0, __bss_start
    la   t1, __bss_end
.Lbss:
    bgeu t0, t1, .Lbss_done
    sd   zero, 0(t0)
    addi t0, t0, 8
    j    .Lbss
.Lbss_done:
#if defined(BOARD_UART_8250_INIT_ENABLE) && BOARD_UART_8250_INIT_ENABLE
    /* Initialize the console before releasing the monitor hart so it cannot
       race divisor/FIFO programming with runner output. */
    call platform_uart_init
#endif
    /* ---- Release hart2 after BSS init is complete ---- */
    la   t2, g_boot_sync
    li   t3, 1
    sd   t3, 0(t2)
    fence rw, rw

    /* ---- Call C entry ---- */
    call main
    j    .Lhang

.Lhart2:
    /* ---- Raw, lock-free liveness probe: literal first instructions on the
       monitor hart's path, before any C runtime or shared state, so a wake
       via reset lands here and prints regardless of anything else working. */
#if defined(BOARD_K1_HART_WAKEUP_ENABLE) && BOARD_K1_HART_WAKEUP_ENABLE
    /* li+slli+srli zero-extends: BOARD_UART_BASE has bit 31 set on K1, and a
       bare li on RV64 would sign-extend that to garbage (see LOAD_ADDR32 in
       the ACT rvmodel_macros.h for the same fix applied to test-side code). */
    li   t0, BOARD_UART_BASE
    slli t0, t0, 32
    srli t0, t0, 32
    la   t1, k1_probe_str
.Lprobe_loop:
    lb   t2, 0(t1)
    beqz t2, .Lprobe_done
.Lprobe_wait_thre:
    lb   t3, 0x14(t0)
    andi t3, t3, 0x20
    beqz t3, .Lprobe_wait_thre
    sb   t2, 0(t0)
    addi t1, t1, 1
    j    .Lprobe_loop
.Lprobe_done:

    /* CSR_ML2SETUP self-enable, same as hart1's above - must happen before
       the g_boot_sync read below. Re-read mhartid fresh rather than relying
       on t0 from before the probe loop, since the probe loop above just
       clobbered it with BOARD_UART_BASE. */
    csrr t4, mhartid
    andi t4, t4, 3
    li   t5, 1
    sll  t5, t5, t4
    csrs 0x7f0, t5
#endif

    /* ---- Hart2 stack ---- */
    la   sp, __stack_top
    li   t2, 8192
    sub  sp, sp, t2

    /* ---- Wait until hart1 finished BSS clear ---- */
.Lwait_boot:
    la   t2, g_boot_sync
    ld   t3, 0(t2)
    beqz t3, .Lwait_boot

    /* ---- Call C entry ---- */
    call main

    /* If main returns, hang */
.Lhang:
    wfi
    j .Lhang

/* ---- Other harts park here forever ---- */
.Lpark:
    wfi
    j .Lpark
.size _start, . - _start

#if defined(BOARD_K1_HART_WAKEUP_ENABLE) && BOARD_K1_HART_WAKEUP_ENABLE
.section .rodata
k1_probe_str:
    .asciz "\n[K1PROBE] hart2 alive at reset vector\n"
#endif
