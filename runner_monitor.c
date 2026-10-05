#include "runner_shared.h"

static void emit_exit_u64(const char *name, uint64_t value)
{
    uart_puts(name);
    uart_puts("=");
    uart_put_hex(value);
    uart_puts("\n");
}

static void emit_exit_str(const char *name, const char *value)
{
    uart_puts(name);
    uart_puts("=");
    uart_puts(value ? value : "unknown");
    uart_puts("\n");
}

static void emit_reg_one(const char *name, uint64_t value)
{
    uart_puts(name);
    uart_puts("=");
    uart_put_hex(value);
    uart_puts("\n");
}

static uint32_t insn_csr_num(uint32_t insn)
{
    if ((insn & 0x7fu) != 0x73u) return 0xffffffffu;
    if (((insn >> 12) & 0x7u) == 0u) return 0xffffffffu;
    return (insn >> 20) & 0xfffu;
}

static const char *csr_name_for_log(uint32_t csr)
{
    switch (csr) {
        case 0x100u: return "sstatus";
        case 0x104u: return "sie";
        case 0x105u: return "stvec";
        case 0x106u: return "scounteren";
        case 0x10au: return "senvcfg";
        case 0x140u: return "sscratch";
        case 0x141u: return "sepc";
        case 0x142u: return "scause";
        case 0x143u: return "stval";
        case 0x144u: return "sip";
        case 0x180u: return "satp";
        case 0x300u: return "mstatus";
        case 0x301u: return "misa";
        case 0x302u: return "medeleg";
        case 0x303u: return "mideleg";
        case 0x304u: return "mie";
        case 0x305u: return "mtvec";
        case 0x306u: return "mcounteren";
        case 0x30au: return "menvcfg";
        case 0x340u: return "mscratch";
        case 0x341u: return "mepc";
        case 0x342u: return "mcause";
        case 0x343u: return "mtval";
        case 0x344u: return "mip";
        default: return "unknown";
    }
}

static int csr_is_machine_level(uint32_t csr)
{
    return ((csr >> 8) & 0x3u) == 3u;
}

static const char *safe_case_name(const char *name)
{
    if (!name || !is_valid_ddr_addr((uint64_t)(uintptr_t)name)) return "unknown";
    if (!is_valid_ddr_addr((uint64_t)(uintptr_t)name + 63ULL)) return "unknown";
    return name[0] ? name : "unknown";
}

static void emit_log_separator(void)
{
    uart_puts("========================================================================\n");
}

static void monitor_uart_grace_delay(void)
{
    uint64_t deadline = *mtime_ptr() + 500000ULL;
    while (*mtime_ptr() < deadline) {
        asm volatile ("nop");
    }
}

static int monitor_tohost_is_sane(void)
{
    uint64_t addr = g_runner_image.tohost_addr;
    uint64_t start = g_runner_image.loaded_region_start;
    uint64_t end = g_runner_image.loaded_region_end;

    if (!g_runner_image.tohost_ptr) return 0;
    if ((uint64_t)(uintptr_t)g_runner_image.tohost_ptr != addr) return 0;
    if (!is_valid_ddr_addr(addr) || !is_valid_ddr_addr(addr + 7ULL)) return 0;
    if (start == 0 || end <= start) return 0;
    if (addr < start || (addr + 8ULL) > end) return 0;
    return 1;
}

static int htif_tohost_is_console(uint64_t value, char *ch_out)
{
    const uint64_t htif_console_tag = 0x0101ULL;

    if ((value >> 48) != htif_console_tag) return 0;
    if (ch_out) *ch_out = (char)(value & 0xffULL);
    return 1;
}

static void monitor_clear_tohost(void)
{
    if (!g_runner_image.tohost_ptr) return;
    *g_runner_image.tohost_ptr = 0;
    asm volatile ("fence rw, rw" ::: "memory");
}

static void emit_hart1_csr_snapshot(void)
{
    if (!g_runner_exec.hart1_csr_snapshot_valid) {
        uart_puts("[HART1CSR] unavailable=no_hart1_snapshot\n");
        return;
    }

    uart_puts("[HART1CSR] valid=1 reason=");
    uart_put_hex(g_runner_exec.hart1_csr_snapshot_reason);
    uart_puts(" hartid=");
    uart_put_hex(g_runner_exec.hart1_mhartid);
    uart_puts(" mstatus=");
    uart_put_hex(g_runner_exec.hart1_mstatus);
    uart_puts(" mepc=");
    uart_put_hex(g_runner_exec.hart1_mepc);
    uart_puts(" mcause=");
    uart_put_hex(g_runner_exec.hart1_mcause);
    uart_puts(" mtval=");
    uart_put_hex(g_runner_exec.hart1_mtval);
    uart_puts(" medeleg=");
    uart_put_hex(g_runner_exec.hart1_medeleg);
    uart_puts(" mideleg=");
    uart_put_hex(g_runner_exec.hart1_mideleg);
    uart_puts(" satp=");
    uart_put_hex(g_runner_exec.hart1_satp);
    uart_puts(" pmpcfg0=");
    uart_put_hex(g_runner_exec.hart1_pmpcfg0);
    uart_puts("\n");

    for (uint32_t i = 0; i < 8; i++) {
        uart_puts("[HART1CSR] pmpaddr");
        uart_put_dec_u64(i);
        uart_puts("=");
        uart_put_hex(g_runner_exec.hart1_pmpaddr[i]);
        uart_puts("\n");
    }
}

static void wait_for_hart1_csr_snapshot(void)
{
    for (uint32_t i = 0; i < 1000000u; i++) {
        asm volatile ("fence r, r" ::: "memory");
        if (g_runner_exec.hart1_csr_snapshot_valid) return;
        cpu_relax();
    }
}

static void emit_mode_value(uint64_t mode)
{
    switch (mode) {
        case EXEC_MODE_M: uart_puts("M"); break;
        case EXEC_MODE_S: uart_puts("S"); break;
        case EXEC_MODE_U: uart_puts("U"); break;
        default: uart_puts("UNKNOWN"); break;
    }
}

static void emit_exit_mode_value(const char *name, uint64_t mode)
{
    uart_puts(name);
    uart_puts("=");
    emit_mode_value(mode);
    uart_puts("\n");
}

void emit_execution_context(const char *reason)
{
    SymbolInfo sym;
    int have_sym = 0;
    int have_insn = 0;
    uint32_t insn = 0;
    uint64_t reported_exec_mode = EXEC_MODE_M;

    if (g_last_trap.valid) {
        have_sym = (find_symbol_for_addr(g_runner_image.loaded_blob, g_runner_image.loaded_blob_size, g_last_trap.mepc, &sym) == 0);
        insn = read_insn_word(g_last_trap.mepc, &have_insn);
        reported_exec_mode = g_last_trap.mode;
    } else {
        reported_exec_mode = g_lower_state.requested_exec_mode;
    }

    uart_puts("[CTX] reason=");
    uart_puts(reason ? reason : "unknown");
    uart_puts(" exec_mode=");
    emit_mode_value(reported_exec_mode);
    uart_puts(" uflow_marker=");
    uart_put_hex(g_lower_state.uflow_marker);
    uart_puts(" trap_valid=");
    uart_puts(g_last_trap.valid ? "1" : "0");
    uart_puts(" exit_trap_mode=");
    if (g_last_trap.valid) emit_mode_value(g_last_trap.mode);
    else uart_puts("none");
    uart_puts(" exit_mcause="); uart_put_hex(g_last_trap.mcause);
    uart_puts(" exit_reason=");
    uart_puts(g_last_trap.valid ? trap_reason_name(g_last_trap.mcause) : "no_trap");
    uart_puts(" exit_mepc="); uart_put_hex(g_last_trap.mepc);
    uart_puts(" exit_insn=");
    uart_put_hex((uint64_t)insn);
    uart_puts(" exit_insn_valid=");
    uart_puts(have_insn ? "1" : "0");
    uart_puts(" exit_mtval="); uart_put_hex(g_last_trap.mtval);
    uart_puts(" exit_mstatus="); uart_put_hex(g_last_trap.mstatus);
    uart_puts(" exit_symbol=");
    if (have_sym) uart_put_token_str(sym.name);
    else uart_puts("unknown");
    uart_puts(" exit_symbol_addr=");
    if (have_sym) uart_put_hex(sym.value);
    else uart_put_hex(0);
    uart_puts("\n");

    if (reason && streq(reason, "PASS") && g_last_trap.valid) {
        uart_puts("[HANDLED] trap_was_handled=1 final_status=PASS handled_trap_mode=");
        emit_mode_value(g_last_trap.mode);
        uart_puts(" handled_mcause=");
        uart_put_hex(g_last_trap.mcause);
        uart_puts(" handled_mepc=");
        uart_put_hex(g_last_trap.mepc);
        uart_puts(" handled_insn=");
        uart_put_hex((uint64_t)insn);
        uart_puts(" sbi_direct_request_count=");
        uart_put_hex(g_runner_sbi.direct_request_count);
        uart_puts(" sbi_compat_forward_count=");
        uart_put_hex(g_runner_sbi.compat_forward_count);
        uart_puts("\n");
        uart_puts("[REG] unavailable=pass_after_handled_trap\n");
        return;
    }

    if (!g_last_trap.valid) {
        uart_puts("[REG] unavailable=no_trap_frame\n");
        return;
    }

    uart_puts("[EXIT] BEGIN\n");
    emit_exit_mode_value("exit_trap_mode", g_last_trap.mode);
    emit_exit_u64("exit_mcause", g_last_trap.mcause);
    emit_exit_str("exit_reason", trap_reason_name(g_last_trap.mcause));
    emit_exit_u64("exit_mepc", g_last_trap.mepc);
    emit_exit_u64("exit_insn", (uint64_t)insn);
    emit_exit_str("exit_insn_valid", have_insn ? "1" : "0");
    uart_puts("exit_insn_decode=");
    if (have_insn) decode_riscv_insn(g_last_trap.mepc, insn);
    else uart_puts("unknown");
    uart_puts("\n");
    emit_exit_u64("exit_mtval", g_last_trap.mtval);
    emit_exit_u64("exit_mstatus", g_last_trap.mstatus);
    if (g_last_trap.mcause == MCAUSE_ILLEGAL_INSN && have_insn) {
        uint32_t csr = insn_csr_num(insn);
        if (csr != 0xffffffffu) {
            emit_exit_u64("exit_illegal_csr", (uint64_t)csr);
            emit_exit_str("exit_illegal_csr_name", csr_name_for_log(csr));
            if (g_last_trap.mode == EXEC_MODE_S && csr_is_machine_level(csr)) {
                emit_exit_str("exit_diagnosis", "illegal_machine_csr_access_from_s_mode");
            } else {
                emit_exit_str("exit_diagnosis", "illegal_csr_or_system_instruction");
            }
        } else {
            emit_exit_str("exit_diagnosis", "illegal_instruction");
        }
    }
    uart_puts("exit_symbol=");
    if (have_sym) uart_put_token_str(sym.name);
    else uart_puts("unknown");
    uart_puts("\n");
    emit_exit_u64("exit_symbol_addr", have_sym ? sym.value : 0);
    emit_exit_str("ctx_reason", reason ? reason : "unknown");
    emit_exit_mode_value("ctx_exec_mode", reported_exec_mode);
    emit_exit_u64("ctx_uflow_marker", g_lower_state.uflow_marker);
    emit_exit_u64("sbi_direct_request_count", g_runner_sbi.direct_request_count);
    emit_exit_u64("sbi_compat_forward_count", g_runner_sbi.compat_forward_count);
    emit_exit_u64("sbi_last_error", g_runner_sbi.last_error);
    uart_puts("[EXIT] END\n");

    uart_puts("[REGS] BEGIN\n");
    emit_reg_one("ra", g_last_trap.frame.ra);
    emit_reg_one("sp", g_last_trap.frame.sp);
    emit_reg_one("gp", g_last_trap.frame.gp);
    emit_reg_one("tp", g_last_trap.frame.tp);
    emit_reg_one("t0", g_last_trap.frame.t0);
    emit_reg_one("t1", g_last_trap.frame.t1);
    emit_reg_one("t2", g_last_trap.frame.t2);
    emit_reg_one("s0", g_last_trap.frame.s0);
    emit_reg_one("s1", g_last_trap.frame.s1);
    emit_reg_one("a0", g_last_trap.frame.a0);
    emit_reg_one("a1", g_last_trap.frame.a1);
    emit_reg_one("a2", g_last_trap.frame.a2);
    emit_reg_one("a3", g_last_trap.frame.a3);
    emit_reg_one("a4", g_last_trap.frame.a4);
    emit_reg_one("a5", g_last_trap.frame.a5);
    emit_reg_one("a6", g_last_trap.frame.a6);
    emit_reg_one("a7", g_last_trap.frame.a7);
    emit_reg_one("s2", g_last_trap.frame.s2);
    emit_reg_one("s3", g_last_trap.frame.s3);
    emit_reg_one("s4", g_last_trap.frame.s4);
    emit_reg_one("s5", g_last_trap.frame.s5);
    emit_reg_one("s6", g_last_trap.frame.s6);
    emit_reg_one("s7", g_last_trap.frame.s7);
    emit_reg_one("s8", g_last_trap.frame.s8);
    emit_reg_one("s9", g_last_trap.frame.s9);
    emit_reg_one("s10", g_last_trap.frame.s10);
    emit_reg_one("s11", g_last_trap.frame.s11);
    emit_reg_one("t3", g_last_trap.frame.t3);
    emit_reg_one("t4", g_last_trap.frame.t4);
    emit_reg_one("t5", g_last_trap.frame.t5);
    emit_reg_one("t6", g_last_trap.frame.t6);
    uart_puts("[REGS] END\n");

    emit_reg_line4("ra", g_last_trap.frame.ra, "sp", g_last_trap.frame.sp,
                   "gp", g_last_trap.frame.gp, "tp", g_last_trap.frame.tp);
    emit_reg_line4("t0", g_last_trap.frame.t0, "t1", g_last_trap.frame.t1,
                   "t2", g_last_trap.frame.t2, "t3", g_last_trap.frame.t3);
    emit_reg_line4("t4", g_last_trap.frame.t4, "t5", g_last_trap.frame.t5,
                   "t6", g_last_trap.frame.t6, "s0", g_last_trap.frame.s0);
    emit_reg_line4("s1", g_last_trap.frame.s1, "s2", g_last_trap.frame.s2,
                   "s3", g_last_trap.frame.s3, "s4", g_last_trap.frame.s4);
    emit_reg_line4("s5", g_last_trap.frame.s5, "s6", g_last_trap.frame.s6,
                   "s7", g_last_trap.frame.s7, "s8", g_last_trap.frame.s8);
    emit_reg_line4("s9", g_last_trap.frame.s9, "s10", g_last_trap.frame.s10,
                   "s11", g_last_trap.frame.s11, "a0", g_last_trap.frame.a0);
    emit_reg_line4("a1", g_last_trap.frame.a1, "a2", g_last_trap.frame.a2,
                   "a3", g_last_trap.frame.a3, "a4", g_last_trap.frame.a4);
    emit_reg_line3("a5", g_last_trap.frame.a5, "a6", g_last_trap.frame.a6,
                   "a7", g_last_trap.frame.a7);
    uart_puts("[CSR] mepc="); uart_put_hex(g_last_trap.mepc);
    uart_puts(" mcause="); uart_put_hex(g_last_trap.mcause);
    uart_puts(" mtval="); uart_put_hex(g_last_trap.mtval);
    uart_puts(" mstatus="); uart_put_hex(g_last_trap.mstatus);
    uart_puts(" trap_mode="); emit_mode_value(g_last_trap.mode);
    uart_puts("\n");

    if (is_valid_ddr_addr(g_last_trap.mepc)) dump_words32(g_last_trap.mepc);
}

uint64_t get_case_exit_pc(void)
{
    if (g_last_trap.valid) return g_last_trap.mepc;
    return 0;
}

static void emit_rvcp_status_line(const char *tag, const char *name, int passed)
{
    uart_puts(tag);
    uart_puts(": Test File \"");
    uart_puts(name);
    uart_puts(".S\": ");
    uart_puts(passed ? "PASSED" : "FAILED");
    uart_puts("\n");
}

void emit_trap_failure_report(uint64_t mcause, uint64_t mepc)
{
    const char *name = safe_case_name((const char *)g_runner_exec.active_case_name);

    uart_puts("[CASE] REPORT name=");
    uart_puts(name);
    uart_puts(" status=FAIL");
    uart_puts(" rc="); uart_put_hex((uint64_t)(int64_t)-1);
    uart_puts(" tohost_addr="); uart_put_hex(g_runner_image.tohost_addr);
    uart_puts(" tohost_value="); uart_put_hex(TOHOST_TRAP);
    uart_puts(" trap_mcause="); uart_put_hex(mcause);
    uart_puts(" trap_mepc="); uart_put_hex(mepc);
    uart_puts(" exit_pc="); uart_put_hex(get_case_exit_pc());
    uart_puts("\n");

    emit_rvcp_status_line("RVCP-RESULT", name, 0);
    emit_rvcp_status_line("RVCP-SUMMARY", name, 0);
    emit_execution_context("TRAP");

    dump_failure_scratch_region(g_runner_image.fail_begin, g_runner_image.fail_end);
    dump_act_failure_context();
}

void handle_fatal_test_trap(const TrapFrame *tf, uint64_t mcause, uint64_t mepc,
                            uint64_t mtval, uint64_t mstatus)
{
    capture_last_trap(tf, mcause, mepc, mtval, mstatus);
    g_runner_exec.test_tohost_value = TOHOST_TRAP;
    g_runner_exec.test_done = 1;
    g_runner_exec.runner_active = 0;
    g_runner_exec.test_deadline_mtime = 0;

    emit_trap_failure_report(mcause, mepc);
    uart_stream_emit_done(safe_case_name((const char *)g_runner_exec.active_case_name),
                          "FAIL", TOHOST_TRAP);

    g_runner_exec.case_report_ready = 1;
    g_runner_exec.monitor_report_done = 1;
    request_deferred_reset();

    while (1) { wfi(); }
}

void request_deferred_reset(void)
{
    g_runner_exec.reset_request_mtime = *mtime_ptr() + RESET_DELAY_TICKS;
    asm volatile ("fence rw, rw" ::: "memory");
}

int wait_for_monitor_report(void)
{
    for (;;) {
        if (g_runner_exec.monitor_report_done) return 0;
        /* The monitor hart exclusively owns reports and reset recovery. */
        cpu_relax();
    }
}

/*
 * Interrupt tests use UART0 THRE as a deterministic real PLIC source.  Keep
 * it quiescent outside the payload and mask every inherited PLIC source for
 * the payload hart's M/S contexts.  ACT interrupt tests intentionally write
 * mie=-1; leaving boot-firmware device enables in the PLIC would allow an
 * unrelated MEIP/SEIP to preempt the interrupt that the test just injected.
 */
void platform_external_irq_cleanup(void)
{
#if !BOARD_EXTERNAL_IRQ_CLEANUP_ENABLE
    /* No validated PLIC context/source mapping on this board. */
    return;
#else
    const uint32_t source = UART_PLIC_SOURCE;
    const uint32_t contexts[2] = {
        RUNNER_M_PLIC_CONTEXT,
        RUNNER_S_PLIC_CONTEXT,
    };
    uint8_t ier = mmio_read8(UART_BASE + UART_IER);

    mmio_write8(UART_BASE + UART_IER, (uint8_t)(ier & (uint8_t)~UART_IER_THRI));
    __asm__ volatile ("fence iorw, iorw" ::: "memory");

    for (uint32_t i = 0; i < 2u; ++i) {
        uintptr_t claim_addr = PLIC_BASE + 0x200004u + ((uintptr_t)contexts[i] * 0x1000u);
        uint32_t claim = mmio_read32(claim_addr);

        if (claim != 0u) mmio_write32(claim_addr, claim);
        for (uint32_t word = 0; word < 5u; ++word) {
            uintptr_t enable_addr = PLIC_BASE + 0x2000u +
                                    ((uintptr_t)contexts[i] * 0x80u) +
                                    ((uintptr_t)word * sizeof(uint32_t));
            mmio_write32(enable_addr, 0u);
        }
    }

    mmio_write32(PLIC_BASE + ((uintptr_t)source * sizeof(uint32_t)), 0u);
    __asm__ volatile ("fence iorw, iorw" ::: "memory");
#endif
}

void monitor_hart_loop(void)
{
    uint64_t monitor_deadline_mtime = 0;
    uint64_t monitor_saw_active = 0;
    uint64_t timeout_disabled_notice = 0;

    uart_log_lock();
    uart_puts("[MON] hart2 online\n");
    uart_log_unlock();
    while (1) {
        if (g_runner_exec.runner_active && monitor_saw_active == 0) {
            monitor_saw_active = 1;
            monitor_deadline_mtime = *mtime_ptr() + TEST_TIMEOUT_TICKS + (TEST_TIMEOUT_TICKS / 4ULL);
        } else if (!g_runner_exec.runner_active) {
            monitor_saw_active = 0;
            monitor_deadline_mtime = 0;
        }

        if (!g_runner_exec.reset_armed && g_runner_exec.reset_request_mtime != 0 && g_runner_exec.sig_dump_in_progress == 0) {
            uint64_t now = *mtime_ptr();
            if (now >= g_runner_exec.reset_request_mtime) {
                uart_log_lock();
                uart_puts("[RST] trigger watchdog reset\n");
                uart_log_unlock();
                trigger_watchdog_reset();
            }
        }

        if (g_runner_exec.runner_active && !g_runner_exec.reset_armed && g_runner_exec.reset_request_mtime == 0) {
            uint64_t now = *mtime_ptr();
            uint64_t shared_deadline = g_runner_exec.test_deadline_mtime;
            int shared_expired = (shared_deadline != 0 && now >= shared_deadline);
            int monitor_expired = (monitor_deadline_mtime != 0 && now >= monitor_deadline_mtime);
            if (shared_expired || monitor_expired) {
#if RUNNER_DISABLE_TEST_TIMEOUT
                if (timeout_disabled_notice == 0) {
                    uart_log_lock();
                    uart_puts("[RST] test timeout disabled; monitor timeout observed");
                    uart_puts(" shared_expired=");
                    uart_put_hex((uint64_t)shared_expired);
                    uart_puts(" monitor_expired=");
                    uart_put_hex((uint64_t)monitor_expired);
                    uart_puts(" mtime=");
                    uart_put_hex(now);
                    uart_puts(" shared_deadline=");
                    uart_put_hex(shared_deadline);
                    uart_puts("\n");
                    dump_act_irq_timeout_context();
                    uart_log_unlock();
                    timeout_disabled_notice = 1;
                }
                monitor_deadline_mtime = 0;
                continue;
#else
                const char *name = safe_case_name((const char *)g_runner_exec.active_case_name);
                g_runner_exec.test_tohost_value = TOHOST_TIMEOUT;
                g_runner_exec.test_done = 1;
                *msip_ptr(RUNNER_HART_ID) = 1;
                *mtimecmp_ptr(RUNNER_HART_ID) = now;
                asm volatile ("fence rw, rw" ::: "memory");

#if RUNNER_TIMEOUT_FAST_RESET && !RUNNER_UART_STREAM
                g_runner_exec.runner_active = 0;
                g_runner_exec.case_report_ready = 1;
                g_runner_exec.monitor_report_done = 1;
                trigger_watchdog_reset();
#endif

                uart_log_lock();
                uart_puts("[RST] test timeout\n");
                if (monitor_expired && !shared_expired) {
                    uart_puts("[RST] monitor-local deadline expired\n");
                }
                uart_puts("[TIMEOUTCSR] source=monitor_hart");
                uart_puts(" mstatus="); uart_put_hex(read_mstatus());
                uart_puts(" sstatus="); uart_put_hex(read_sstatus());
                uart_puts(" mie="); uart_put_hex(read_mie());
                uart_puts(" mip="); uart_put_hex(read_csr_mip());
                uart_puts(" sie="); uart_put_hex(read_csr_sie());
                uart_puts(" sip="); uart_put_hex(read_csr_sip());
                uart_puts(" mideleg="); uart_put_hex(read_csr_mideleg());
                uart_puts(" medeleg="); uart_put_hex(read_csr_medeleg());
                uart_puts(" mtvec="); uart_put_hex(read_csr_mtvec());
                uart_puts(" stvec="); uart_put_hex(read_csr_stvec());
                uart_puts(" satp="); uart_put_hex(read_csr_satp());
                uart_puts(" mtime="); uart_put_hex(*mtime_ptr());
                uart_puts(" deadline="); uart_put_hex(shared_deadline);
                uart_puts("\n");

                uart_puts("[CASE] REPORT name=");
                uart_puts(name);
                uart_puts(" exec_mode=");
                emit_mode_value(g_lower_state.requested_exec_mode);
                uart_puts(" status=TIMEOUT rc=0x0000000000000000");
                uart_puts(" tohost_addr=");
                uart_put_hex(g_runner_image.tohost_addr);
                uart_puts(" tohost_value=");
                uart_put_hex(TOHOST_TIMEOUT);
                uart_puts(" sig_begin=");
                uart_put_hex(g_runner_image.sig_begin);
                uart_puts(" sig_end=");
                uart_put_hex(g_runner_image.sig_end);
                uart_puts(" sig_bytes=");
                if (g_runner_image.sig_end > g_runner_image.sig_begin) {
                    uart_put_hex(g_runner_image.sig_end - g_runner_image.sig_begin);
                } else {
                    uart_put_hex(0);
                }
                uart_puts(" exit_pc=");
                uart_put_hex(get_case_exit_pc());
                uart_puts("\n");
                emit_execution_context("TIMEOUT");

                g_runner_exec.sig_dump_in_progress = 1;
                asm volatile ("fence rw, rw" ::: "memory");
                dump_failure_scratch_region(g_runner_image.fail_begin, g_runner_image.fail_end);
                dump_act_failure_context();
                asm volatile ("fence rw, rw" ::: "memory");
                g_runner_exec.sig_dump_in_progress = 0;

                uart_stream_emit_done(name, "TIMEOUT", TOHOST_TIMEOUT);
                g_runner_exec.case_report_ready = 1;
                g_runner_exec.monitor_report_done = 1;
#if RUNNER_TIMEOUT_FAST_RESET && !RUNNER_UART_STREAM
                uart_puts("[RST] timeout fast reset\n");
                uart_puts("[RST] trigger watchdog reset\n");
                uart_log_unlock();
                continue;
#else
                g_runner_exec.reset_request_mtime = now + (RESET_DELAY_TICKS * 10ULL);
                uart_log_unlock();
#endif
#endif
            }
        }

        if (g_runner_exec.runner_active && monitor_tohost_is_sane() && g_runner_exec.monitor_seen_tohost == 0) {
            uint64_t v = *g_runner_image.tohost_ptr;
            if (v != 0) {
                char console_ch = 0;
                if (htif_tohost_is_console(v, &console_ch)) {
                    uart_log_lock();
                    uart_putc(console_ch);
                    uart_log_unlock();
                    monitor_clear_tohost();
                    continue;
                }
                uint64_t sig_bytes = 0;
                const char *st = "FAIL";
                const char *name = safe_case_name((const char *)g_runner_exec.active_case_name);
                if (v == 1) st = "PASS";
                else if (v == TOHOST_TIMEOUT) st = "TIMEOUT";
                if (g_runner_image.sig_end > g_runner_image.sig_begin) sig_bytes = g_runner_image.sig_end - g_runner_image.sig_begin;

                g_runner_exec.test_tohost_value = v;
                asm volatile ("fence rw, rw" ::: "memory");
                g_runner_exec.monitor_seen_tohost = 1;
                *msip_ptr(RUNNER_HART_ID) = 1;
                asm volatile ("fence rw, rw" ::: "memory");
                wait_for_hart1_csr_snapshot();
                monitor_uart_grace_delay();

                uart_log_lock();
                uart_puts("\n");
                emit_log_separator();
                uart_puts("[MON] tohost addr="); uart_put_hex(g_runner_image.tohost_addr);
                uart_puts(" value="); uart_put_hex(v); uart_puts("\n");
                emit_log_separator();
                uart_puts("[MON] sig_begin="); uart_put_hex(g_runner_image.sig_begin);
                uart_puts("\n");
                uart_puts("      sig_end="); uart_put_hex(g_runner_image.sig_end);
                uart_puts("\n");
                uart_puts("      sig_bytes="); uart_put_hex(sig_bytes); uart_puts("\n");
                uart_puts("[CASE] REPORT name=");
                uart_puts(name);
                uart_puts("\n");
                uart_puts("              exec_mode="); emit_mode_value(g_lower_state.requested_exec_mode);
                uart_puts(" status="); uart_puts(st);
                uart_puts("\n");
                uart_puts("              rc=0x0000000000000000\n");
                uart_puts("              tohost_addr="); uart_put_hex(g_runner_image.tohost_addr);
                uart_puts("\n");
                uart_puts("              tohost_value="); uart_put_hex(v);
                uart_puts("\n");
                uart_puts("              sig_begin="); uart_put_hex(g_runner_image.sig_begin);
                uart_puts("\n");
                uart_puts("              sig_end="); uart_put_hex(g_runner_image.sig_end);
                uart_puts("\n");
                uart_puts("              sig_bytes="); uart_put_hex(sig_bytes);
                uart_puts("\n");
                uart_puts("              exit_pc="); uart_put_hex(get_case_exit_pc());
                uart_puts("\n");

                if (v == 1) emit_execution_context("PASS");
                else if (v == TOHOST_TIMEOUT) emit_execution_context("TIMEOUT");
                else emit_execution_context("FAIL");
                emit_hart1_csr_snapshot();

                if (v == 1) dump_act_irq_section_trace();
#if RUNNER_FAST_FAIL_RESET
                if (v != 1 && v != TOHOST_TIMEOUT) {
                    g_runner_exec.case_report_ready = 1;
                    g_runner_exec.monitor_report_done = 1;
                    uart_puts("[RST] fast fail reset\n");
                    uart_puts("[RST] trigger watchdog reset\n");
                    uart_log_unlock();
                    trigger_watchdog_reset();
                    continue;
                }
#endif
                if (v != 1) {
                    g_runner_exec.sig_dump_in_progress = 1;
                    asm volatile ("fence rw, rw" ::: "memory");
                    dump_failure_scratch_region(g_runner_image.fail_begin, g_runner_image.fail_end);
                    dump_act_failure_context();
                    dump_signature_region(g_runner_image.sig_begin, g_runner_image.sig_end);
                    asm volatile ("fence rw, rw" ::: "memory");
                    g_runner_exec.sig_dump_in_progress = 0;
                }
                uart_stream_emit_done(name, st, v);
                g_runner_exec.case_report_ready = 1;
                g_runner_exec.monitor_report_done = 1;
                g_runner_exec.reset_request_mtime = *mtime_ptr() + RESET_DELAY_TICKS;
                uart_log_unlock();
                *msip_ptr(RUNNER_HART_ID) = 1;
                asm volatile ("fence rw, rw" ::: "memory");
            }
        }
        cpu_relax();
    }
}

const char *case_status_name(int status)
{
    switch (status) {
        case CASE_STATUS_PASS: return "PASS";
        case CASE_STATUS_FAIL: return "FAIL";
        case CASE_STATUS_TIMEOUT: return "TIMEOUT";
        default: return "ERROR";
    }
}

int case_is_pass(const TestResult *tr)
{
    return tr->status == CASE_STATUS_PASS;
}

void emit_case_report(const TestResult *tr)
{
    uint64_t sig_bytes = 0;
    if (tr->sig_end > tr->sig_begin) sig_bytes = tr->sig_end - tr->sig_begin;

    uart_puts("[CASE] REPORT name="); uart_puts(tr->name);
    uart_puts("\n");
    uart_puts("              exec_mode="); emit_mode_value(g_lower_state.requested_exec_mode);
    uart_puts(" status="); uart_puts(case_status_name(tr->status));
    uart_puts("\n");
    uart_puts("              rc="); uart_put_hex((uint64_t)(int64_t)tr->rc);
    uart_puts("\n");
    uart_puts("              tohost_addr="); uart_put_hex(tr->tohost_addr);
    uart_puts("\n");
    uart_puts("              tohost_value="); uart_put_hex(tr->tohost);
    uart_puts("\n");
    uart_puts("              sig_begin="); uart_put_hex(tr->sig_begin);
    uart_puts("\n");
    uart_puts("              sig_end="); uart_put_hex(tr->sig_end);
    uart_puts("\n");
    uart_puts("              sig_bytes="); uart_put_hex(sig_bytes);
    uart_puts("\n");
    uart_puts("              exit_pc="); uart_put_hex(get_case_exit_pc());
    uart_puts("\n");

    emit_rvcp_status_line("RVCP-RESULT", tr->name, tr->status == CASE_STATUS_PASS);
    emit_rvcp_status_line("RVCP-SUMMARY", tr->name, tr->status == CASE_STATUS_PASS);
}
