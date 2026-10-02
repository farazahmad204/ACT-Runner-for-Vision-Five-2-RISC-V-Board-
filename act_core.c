// Core platform, debug, ELF helpers extracted from main_act.c.

#include "runner_shared.h"

static uint64_t read_csr_satp_local(void)
{
    uint64_t v;
    __asm__ volatile ("csrr %0, satp" : "=r"(v));
    return v;
}

static uint64_t read_csr_stvec_local(void)
{
    uint64_t v;
    __asm__ volatile ("csrr %0, stvec" : "=r"(v));
    return v;
}

int is_valid_ddr_addr(uint64_t addr)
{
    if (addr < BOARD_RAM_BASE || addr >= BOARD_RAM_LIMIT) return 0;
#if BOARD_DDR_EXCLUDE_BASE < BOARD_DDR_EXCLUDE_LIMIT
    if (addr >= BOARD_DDR_EXCLUDE_BASE && addr < BOARD_DDR_EXCLUDE_LIMIT) return 0;
#endif
    return 1;
}

int is_valid_ddr_range(uint64_t begin, uint64_t end)
{
    if (begin == 0 || end == 0 || end <= begin) return 0;
    if ((begin & 0x3ULL) != 0 || (end & 0x3ULL) != 0) return 0;
    if (!is_valid_ddr_addr(begin) || !is_valid_ddr_addr(end - 1)) return 0;
    if ((end - begin) > 0x200000ULL) return 0;
    return 1;
}

int is_valid_payload_addr(uint64_t addr)
{
    return addr >= BOARD_PAYLOAD_BASE && addr < BOARD_PAYLOAD_LIMIT &&
           is_valid_ddr_addr(addr);
}

int is_valid_payload_range(uint64_t begin, uint64_t end)
{
    if (begin == 0 || end == 0 || end <= begin) return 0;
    return is_valid_payload_addr(begin) && is_valid_payload_addr(end - 1);
}

static int ranges_overlap_u64(uint64_t a_begin, uint64_t a_end,
                              uint64_t b_begin, uint64_t b_end)
{
    return a_begin < b_end && b_begin < a_end;
}

void *memcpy_local(void *dst, const void *src, size_t n)
{
    uint8_t *d = (uint8_t *)dst;
    const uint8_t *s = (const uint8_t *)src;
    for (size_t i = 0; i < n; i++) d[i] = s[i];
    return dst;
}

void *memset_local(void *dst, int c, size_t n)
{
    uint8_t *d = (uint8_t *)dst;
    uint8_t v = (uint8_t)c;
    for (size_t i = 0; i < n; i++) d[i] = v;
    return dst;
}

void *memcpy(void *dst, const void *src, size_t n) { return memcpy_local(dst, src, n); }
void *memset(void *dst, int c, size_t n) { return memset_local(dst, c, n); }

static volatile uint32_t g_uart_log_lock = 0;
static volatile uint64_t g_uart_log_owner = ~0ULL;
static volatile uint32_t g_uart_log_depth = 0;

void platform_uart_init(void)
{
#if BOARD_UART_8250_INIT_ENABLE
    const uint32_t divisor =
        (BOARD_UART_INPUT_CLOCK_HZ + 8u * BOARD_UART_BAUD) /
        (16u * BOARD_UART_BAUD);

    /* Match the UART8250 initialization performed by the RockOS OpenSBI
       stage that this runner replaces. */
    uart_reg_write(UART_BASE + UART_IER, 0x00u);
    uart_reg_write(UART_BASE + UART_LCR, 0x80u);
    if (divisor != 0u) {
        uart_reg_write(UART_BASE + UART_DLL, divisor & 0xffu);
        uart_reg_write(UART_BASE + UART_DLM, (divisor >> 8) & 0xffu);
    }
    uart_reg_write(UART_BASE + UART_LCR, 0x03u);
    uart_reg_write(UART_BASE + UART_FCR, 0x01u);
    uart_reg_write(UART_BASE + UART_MCR, 0x00u);
    (void)uart_reg_read(UART_BASE + UART_LSR);
    (void)uart_reg_read(UART_BASE + UART_RBR);
    uart_reg_write(UART_BASE + UART_SCR, 0x00u);
    asm volatile ("fence iorw, iorw" ::: "memory");
#endif
}

static void uart_putc_raw(char c)
{
    while ((uart_reg_read(UART_BASE + UART_LSR) & UART_LSR_THRE) == 0) {}
    uart_reg_write(UART_BASE + UART_THR, (uint8_t)c);
}

void uart_log_lock(void)
{
    uint64_t hart = read_csr_mhartid();

    if (g_uart_log_owner == hart) {
        g_uart_log_depth++;
        return;
    }

    while (__sync_lock_test_and_set(&g_uart_log_lock, 1u) != 0u) {
        cpu_relax();
    }
    g_uart_log_owner = hart;
    g_uart_log_depth = 1u;
    asm volatile ("fence rw, rw" ::: "memory");
}

void uart_log_unlock(void)
{
    uint64_t hart = read_csr_mhartid();

    if (g_uart_log_owner != hart || g_uart_log_depth == 0u) return;
    if (--g_uart_log_depth != 0u) return;

    asm volatile ("fence rw, rw" ::: "memory");
    g_uart_log_owner = ~0ULL;
    __sync_lock_release(&g_uart_log_lock);
}

void uart_putc(char c)
{
    uart_log_lock();
    uart_putc_raw(c);
    uart_log_unlock();
}

void uart_puts(const char* s)
{
    uint32_t limit = 512u;
    uart_log_lock();
    if (!s || !is_valid_ddr_addr((uint64_t)(uintptr_t)s)) {
        const char *bad = "<badstr>";
        while (*bad) uart_putc_raw(*bad++);
        uart_log_unlock();
        return;
    }
    while (*s) {
        if (!is_valid_ddr_addr((uint64_t)(uintptr_t)s) || limit-- == 0u) break;
        if (*s == '\n') uart_putc_raw('\r');
        uart_putc_raw(*s++);
    }
    uart_log_unlock();
}

void uart_put_hex(uint64_t x)
{
    const char* h = "0123456789abcdef";
    uart_log_lock();
    uart_putc_raw('0');
    uart_putc_raw('x');
    for (int i = 15; i >= 0; --i) uart_putc_raw(h[(x >> (i * 4)) & 0xF]);
    uart_log_unlock();
}

void uart_put_dec_u64(uint64_t v)
{
    char buf[24];
    int i = 0;
    uart_log_lock();
    if (v == 0) {
        uart_putc_raw('0');
        uart_log_unlock();
        return;
    }
    while (v != 0 && i < (int)sizeof(buf)) {
        buf[i++] = (char)('0' + (v % 10));
        v /= 10;
    }
    while (i-- > 0) uart_putc_raw(buf[i]);
    uart_log_unlock();
}

void dbg_putc(char c)
{
    uart_log_lock();
    if (c == '\n') uart_putc_raw('\r');
    uart_putc_raw(c);
    uart_log_unlock();
}

void dbg_puts(const char *s)
{
    uint32_t limit = 512u;
    uart_log_lock();
    if (!s || !is_valid_ddr_addr((uint64_t)(uintptr_t)s)) {
        const char *bad = "<badstr>";
        while (*bad) uart_putc_raw(*bad++);
        uart_log_unlock();
        return;
    }
    while (*s) {
        if (!is_valid_ddr_addr((uint64_t)(uintptr_t)s) || limit-- == 0u) break;
        if (*s == '\n') uart_putc_raw('\r');
        uart_putc_raw(*s++);
    }
    uart_log_unlock();
}

void dbg_hex_u64(uint64_t v)
{
    static const char hex[] = "0123456789abcdef";
    for (int i = 15; i >= 0; --i) dbg_putc(hex[(v >> (i * 4)) & 0xF]);
}

void dbg_hex_u32(uint32_t v)
{
    static const char hex[] = "0123456789abcdef";
    for (int i = 7; i >= 0; --i) dbg_putc(hex[(v >> (i * 4)) & 0xF]);
}

void dbg_nl(void) { dbg_putc('\n'); }

void dbg_put_u32_dec(uint32_t v)
{
    char buf[12];
    int i = 0;
    if (v == 0) {
        dbg_putc('0');
        return;
    }
    while (v != 0 && i < (int)sizeof(buf)) {
        buf[i++] = (char)('0' + (v % 10u));
        v /= 10u;
    }
    while (i-- > 0) dbg_putc(buf[i]);
}

void dbg_put_reg_name(uint32_t reg)
{
    static const char *const names[32] = {
        "x0",  "x1",  "x2",  "x3",  "x4",  "x5",  "x6",  "x7",
        "x8",  "x9",  "x10", "x11", "x12", "x13", "x14", "x15",
        "x16", "x17", "x18", "x19", "x20", "x21", "x22", "x23",
        "x24", "x25", "x26", "x27", "x28", "x29", "x30", "x31"
    };
    if (reg < 32u) dbg_puts(names[reg]);
    else {
        dbg_putc('x');
        dbg_put_u32_dec(reg);
    }
}

int32_t sign_extend_32(uint32_t v, uint32_t bits)
{
    uint32_t shift = 32u - bits;
    return (int32_t)(v << shift) >> shift;
}

void dbg_put_imm_i32(int32_t v)
{
    if (v < 0) {
        dbg_putc('-');
        dbg_puts("0x");
        dbg_hex_u32((uint32_t)(-v));
    } else {
        dbg_puts("0x");
        dbg_hex_u32((uint32_t)v);
    }
}

void decode_riscv_insn(uint64_t pc, uint32_t insn)
{
    uint16_t cinsn = (uint16_t)(insn & 0xffffu);
    uint32_t cop = cinsn & 0x3u;
    uint32_t cfunct3 = (cinsn >> 13) & 0x7u;
    uint32_t opcode = insn & 0x7fu;
    uint32_t rd = (insn >> 7) & 0x1fu;
    uint32_t funct3 = (insn >> 12) & 0x7u;
    uint32_t rs1 = (insn >> 15) & 0x1fu;
    uint32_t rs2 = (insn >> 20) & 0x1fu;
    uint32_t funct7 = (insn >> 25) & 0x7fu;
    int32_t imm_i = sign_extend_32(insn >> 20, 12u);
    int32_t imm_s = sign_extend_32(((insn >> 25) << 5) | ((insn >> 7) & 0x1fu), 12u);
    int32_t imm_b = sign_extend_32(
        (((insn >> 31) & 0x1u) << 12) |
        (((insn >> 7) & 0x1u) << 11) |
        (((insn >> 25) & 0x3fu) << 5) |
        (((insn >> 8) & 0xfu) << 1), 13u);
    int32_t imm_u = (int32_t)(insn & 0xfffff000u);
    int32_t imm_j = sign_extend_32(
        (((insn >> 31) & 0x1u) << 20) |
        (((insn >> 12) & 0xffu) << 12) |
        (((insn >> 20) & 0x1u) << 11) |
        (((insn >> 21) & 0x3ffu) << 1), 21u);
    uint32_t csr = (insn >> 20) & 0xfffu;

    if (cop != 0x3u) {
        switch (cop) {
            case 0x0u:
                switch (cfunct3) {
                    case 0u: dbg_puts("c.addi4spn"); return;
                    case 2u: dbg_puts("c.lw"); return;
                    case 3u: dbg_puts("c.ld"); return;
                    case 6u: dbg_puts("c.sw"); return;
                    case 7u: dbg_puts("c.sd"); return;
                    default: break;
                }
                break;
            case 0x1u:
                switch (cfunct3) {
                    case 0u: dbg_puts("c.addi/c.nop"); return;
                    case 1u: dbg_puts("c.addiw/c.jal"); return;
                    case 2u: dbg_puts("c.li"); return;
                    case 3u: dbg_puts("c.lui/c.addi16sp"); return;
                    case 4u: dbg_puts("c.misc-alu"); return;
                    case 5u: dbg_puts("c.j"); return;
                    case 6u: dbg_puts("c.beqz"); return;
                    case 7u: dbg_puts("c.bnez"); return;
                    default: break;
                }
                break;
            case 0x2u:
                switch (cfunct3) {
                    case 0u: dbg_puts("c.slli"); return;
                    case 2u: dbg_puts("c.lwsp"); return;
                    case 3u: dbg_puts("c.ldsp"); return;
                    case 4u: dbg_puts("c.jr/c.mv/c.ebreak/c.jalr/c.add"); return;
                    case 6u: dbg_puts("c.swsp"); return;
                    case 7u: dbg_puts("c.sdsp"); return;
                    default: break;
                }
                break;
            default:
                break;
        }
        dbg_puts("c.unknown");
        return;
    }

    switch (opcode) {
        case 0x17u:
            dbg_puts("auipc ");
            dbg_put_reg_name(rd);
            dbg_puts(", ");
            dbg_put_imm_i32(imm_u >> 12);
            return;
        case 0x37u:
            dbg_puts("lui ");
            dbg_put_reg_name(rd);
            dbg_puts(", ");
            dbg_put_imm_i32(imm_u >> 12);
            return;
        case 0x6fu:
            dbg_puts("jal ");
            dbg_put_reg_name(rd);
            dbg_puts(", 0x");
            dbg_hex_u64((uint64_t)(pc + (int64_t)imm_j));
            return;
        case 0x67u:
            if (funct3 == 0u) {
                dbg_puts("jalr ");
                dbg_put_reg_name(rd);
                dbg_puts(", ");
                dbg_put_imm_i32(imm_i);
                dbg_puts("(");
                dbg_put_reg_name(rs1);
                dbg_puts(")");
                return;
            }
            break;
        case 0x63u:
            switch (funct3) {
                case 0u: dbg_puts("beq "); break;
                case 1u: dbg_puts("bne "); break;
                case 4u: dbg_puts("blt "); break;
                case 5u: dbg_puts("bge "); break;
                case 6u: dbg_puts("bltu "); break;
                case 7u: dbg_puts("bgeu "); break;
                default: dbg_puts("branch? "); break;
            }
            dbg_put_reg_name(rs1);
            dbg_puts(", ");
            dbg_put_reg_name(rs2);
            dbg_puts(", 0x");
            dbg_hex_u64((uint64_t)(pc + (int64_t)imm_b));
            return;
        case 0x03u:
            switch (funct3) {
                case 0u: dbg_puts("lb "); break;
                case 1u: dbg_puts("lh "); break;
                case 2u: dbg_puts("lw "); break;
                case 3u: dbg_puts("ld "); break;
                case 4u: dbg_puts("lbu "); break;
                case 5u: dbg_puts("lhu "); break;
                case 6u: dbg_puts("lwu "); break;
                default: dbg_puts("load? "); break;
            }
            dbg_put_reg_name(rd);
            dbg_puts(", ");
            dbg_put_imm_i32(imm_i);
            dbg_puts("(");
            dbg_put_reg_name(rs1);
            dbg_puts(")");
            return;
        case 0x23u:
            switch (funct3) {
                case 0u: dbg_puts("sb "); break;
                case 1u: dbg_puts("sh "); break;
                case 2u: dbg_puts("sw "); break;
                case 3u: dbg_puts("sd "); break;
                default: dbg_puts("store? "); break;
            }
            dbg_put_reg_name(rs2);
            dbg_puts(", ");
            dbg_put_imm_i32(imm_s);
            dbg_puts("(");
            dbg_put_reg_name(rs1);
            dbg_puts(")");
            return;
        case 0x13u:
            switch (funct3) {
                case 0u: dbg_puts("addi "); break;
                case 2u: dbg_puts("slti "); break;
                case 3u: dbg_puts("sltiu "); break;
                case 4u: dbg_puts("xori "); break;
                case 6u: dbg_puts("ori "); break;
                case 7u: dbg_puts("andi "); break;
                case 1u: dbg_puts("slli "); break;
                case 5u:
                    dbg_puts((funct7 == 0x20u) ? "srai " : "srli ");
                    break;
                default: dbg_puts("op-imm? "); break;
            }
            dbg_put_reg_name(rd);
            dbg_puts(", ");
            dbg_put_reg_name(rs1);
            dbg_puts(", ");
            dbg_put_imm_i32(imm_i);
            return;
        case 0x33u:
            if (funct7 == 0x00u && funct3 == 0u) dbg_puts("add ");
            else if (funct7 == 0x20u && funct3 == 0u) dbg_puts("sub ");
            else if (funct7 == 0x00u && funct3 == 1u) dbg_puts("sll ");
            else if (funct7 == 0x00u && funct3 == 2u) dbg_puts("slt ");
            else if (funct7 == 0x00u && funct3 == 3u) dbg_puts("sltu ");
            else if (funct7 == 0x00u && funct3 == 4u) dbg_puts("xor ");
            else if (funct7 == 0x00u && funct3 == 5u) dbg_puts("srl ");
            else if (funct7 == 0x20u && funct3 == 5u) dbg_puts("sra ");
            else if (funct7 == 0x00u && funct3 == 6u) dbg_puts("or ");
            else if (funct7 == 0x00u && funct3 == 7u) dbg_puts("and ");
            else dbg_puts("op? ");
            dbg_put_reg_name(rd);
            dbg_puts(", ");
            dbg_put_reg_name(rs1);
            dbg_puts(", ");
            dbg_put_reg_name(rs2);
            return;
        case 0x73u:
            if (insn == 0x00000073u) dbg_puts("ecall");
            else if (insn == 0x00100073u) dbg_puts("ebreak");
            else if (insn == 0x10200073u) dbg_puts("sret");
            else if (insn == 0x30200073u) dbg_puts("mret");
            else if (insn == 0x10500073u) dbg_puts("wfi");
            else if (funct3 != 0u) {
                switch (funct3) {
                    case 1u: dbg_puts("csrrw "); break;
                    case 2u: dbg_puts("csrrs "); break;
                    case 3u: dbg_puts("csrrc "); break;
                    case 5u: dbg_puts("csrrwi "); break;
                    case 6u: dbg_puts("csrrsi "); break;
                    case 7u: dbg_puts("csrrci "); break;
                    default: dbg_puts("csr? "); break;
                }
                dbg_put_reg_name(rd);
                dbg_puts(", ");
                switch (csr) {
                    case 0x001u: dbg_puts("fflags"); break;
                    case 0x002u: dbg_puts("frm"); break;
                    case 0x003u: dbg_puts("fcsr"); break;
                    case 0x100u: dbg_puts("sstatus"); break;
                    case 0x104u: dbg_puts("sie"); break;
                    case 0x105u: dbg_puts("stvec"); break;
                    case 0x106u: dbg_puts("scounteren"); break;
                    case 0x10au: dbg_puts("senvcfg"); break;
                    case 0x140u: dbg_puts("sscratch"); break;
                    case 0x141u: dbg_puts("sepc"); break;
                    case 0x142u: dbg_puts("scause"); break;
                    case 0x143u: dbg_puts("stval"); break;
                    case 0x144u: dbg_puts("sip"); break;
                    case 0x180u: dbg_puts("satp"); break;
                    case 0x300u: dbg_puts("mstatus"); break;
                    case 0x301u: dbg_puts("misa"); break;
                    case 0x302u: dbg_puts("medeleg"); break;
                    case 0x303u: dbg_puts("mideleg"); break;
                    case 0x304u: dbg_puts("mie"); break;
                    case 0x305u: dbg_puts("mtvec"); break;
                    case 0x306u: dbg_puts("mcounteren"); break;
                    case 0x30au: dbg_puts("menvcfg"); break;
                    case 0x340u: dbg_puts("mscratch"); break;
                    case 0x341u: dbg_puts("mepc"); break;
                    case 0x342u: dbg_puts("mcause"); break;
                    case 0x343u: dbg_puts("mtval"); break;
                    case 0x344u: dbg_puts("mip"); break;
                    case 0x7a0u: dbg_puts("tselect"); break;
                    case 0x7a1u: dbg_puts("tdata1"); break;
                    case 0x7a2u: dbg_puts("tdata2"); break;
                    case 0x7a3u: dbg_puts("tdata3"); break;
                    case 0xb00u: dbg_puts("mcycle"); break;
                    case 0xb02u: dbg_puts("minstret"); break;
                    case 0xc00u: dbg_puts("cycle"); break;
                    case 0xc01u: dbg_puts("time"); break;
                    case 0xc02u: dbg_puts("instret"); break;
                    case 0xf11u: dbg_puts("mvendorid"); break;
                    case 0xf12u: dbg_puts("marchid"); break;
                    case 0xf13u: dbg_puts("mimpid"); break;
                    case 0xf14u: dbg_puts("mhartid"); break;
                    default:
                        dbg_puts("csr[");
                        dbg_hex_u32(csr);
                        dbg_puts("]");
                        break;
                }
                dbg_puts(", ");
                if (funct3 >= 5u) dbg_put_u32_dec(rs1);
                else dbg_put_reg_name(rs1);
            } else dbg_puts("system");
            return;
        default:
            break;
    }

    dbg_puts("unknown");
}

void dump_words32(uint64_t addr)
{
    const int before_words = 10;
    const int after_words = 6;
    uint64_t aligned = addr & ~0x3ULL;
    uint64_t start = aligned;
    if (aligned >= ((uint64_t)before_words * 4ULL)) {
        start = aligned - ((uint64_t)before_words * 4ULL);
    }

    dbg_puts("[MEMDUMP] around 0x"); dbg_hex_u64(addr); dbg_nl();
    for (int i = 0; i < (before_words + 1 + after_words); i++) {
        uint64_t a = start + ((uint64_t)i * 4ULL);
        if (!is_valid_ddr_addr(a) || !is_valid_ddr_addr(a + 3ULL)) break;
        uint32_t w = *(volatile const uint32_t *)(uintptr_t)a;
        dbg_puts((a == aligned) ? "=> 0x" : "   0x"); dbg_hex_u64(a);
        dbg_puts(" : 0x"); dbg_hex_u32(w);
        dbg_puts("  ");
        decode_riscv_insn(a, w);
        dbg_nl();
    }
}

static void dump_halfwords16(uint64_t addr)
{
    const int before_halfwords = 10;
    const int after_halfwords = 10;
    uint64_t aligned = addr & ~0x1ULL;
    uint64_t start = aligned;
    if (aligned >= ((uint64_t)before_halfwords * 2ULL)) {
        start = aligned - ((uint64_t)before_halfwords * 2ULL);
    }

    dbg_puts("[MEMH] around 0x"); dbg_hex_u64(addr); dbg_nl();
    for (int i = 0; i < (before_halfwords + 1 + after_halfwords); i++) {
        uint64_t a = start + ((uint64_t)i * 2ULL);
        if (!is_valid_ddr_addr(a) || !is_valid_ddr_addr(a + 1ULL)) break;
        uint16_t h = *(volatile const uint16_t *)(uintptr_t)a;
        dbg_puts((a == aligned) ? "=> 0x" : "   0x"); dbg_hex_u64(a);
        dbg_puts(" : 0x"); dbg_hex_u32((uint32_t)h);
        dbg_nl();
    }
}

void uart_put_token_str(const char *s)
{
    if (!s || !s[0]) {
        uart_puts("unknown");
        return;
    }
    while (*s) {
        char c = *s++;
        if (c == ' ' || c == '\t' || c == '\r' || c == '\n') c = '_';
        uart_putc(c);
    }
}

void emit_reg_line4(const char *n0, uint64_t v0,
                    const char *n1, uint64_t v1,
                    const char *n2, uint64_t v2,
                    const char *n3, uint64_t v3)
{
    uart_puts("[REG] ");
    uart_puts(n0); uart_puts("="); uart_put_hex(v0);
    uart_puts(" ");
    uart_puts(n1); uart_puts("="); uart_put_hex(v1);
    uart_puts(" ");
    uart_puts(n2); uart_puts("="); uart_put_hex(v2);
    uart_puts(" ");
    uart_puts(n3); uart_puts("="); uart_put_hex(v3);
    uart_puts("\n");
}

void emit_reg_line3(const char *n0, uint64_t v0,
                    const char *n1, uint64_t v1,
                    const char *n2, uint64_t v2)
{
    uart_puts("[REG] ");
    uart_puts(n0); uart_puts("="); uart_put_hex(v0);
    uart_puts(" ");
    uart_puts(n1); uart_puts("="); uart_put_hex(v1);
    uart_puts(" ");
    uart_puts(n2); uart_puts("="); uart_put_hex(v2);
    uart_puts("\n");
}

void trigger_watchdog_reset(void)
{
    if (g_runner_exec.reset_armed) return;
    g_runner_exec.reset_armed = 1;
#if RUNNER_WDT_ENABLE
    for (int t = 0; t < 3; t++) {
        mmio_write32(RUNNER_WDT_BASE + RUNNER_WDT_LOCK, RUNNER_WDT_UNLOCK_KEY);
        mmio_write32(RUNNER_WDT_BASE + RUNNER_WDT_LOAD, 1u);
        mmio_write32(RUNNER_WDT_BASE + RUNNER_WDT_CTRL, 0x3u);
        mmio_write32(RUNNER_WDT_BASE + RUNNER_WDT_LOCK, 0u);
    }
    uart_puts("[RST] watchdog armed\n");
#else
    uart_puts("[RST] watchdog unavailable for board\n");
#endif
}

void monitor_irq_enable(void)
{
    const uint64_t MSTATUS_MIE = (1ULL << 3);
    write_mie(read_mie() | MIE_MSIE | MIE_MTIE);
    write_mstatus(read_mstatus() | MSTATUS_MIE);
}

void monitor_irq_disable(void)
{
    const uint64_t MSTATUS_MIE = (1ULL << 3);
    write_mie(read_mie() & ~(MIE_MSIE | MIE_MTIE));
    write_mstatus(read_mstatus() & ~MSTATUS_MIE);
}

void enable_fpu_set_fs_only(void)
{
    const uint64_t MSTATUS_FS_MASK  = (3ULL << 13);
    const uint64_t MSTATUS_FS_DIRTY = (3ULL << 13);
    uint64_t ms = read_mstatus();
    ms &= ~MSTATUS_FS_MASK;
    ms |= MSTATUS_FS_DIRTY;
    write_mstatus(ms);
}

void enable_fpu_try_clear_fcsr(void)
{
    uint64_t ms  = read_mstatus();
    uint64_t fs  = (ms >> 13) & 0x3;
    uint64_t isa = read_misa();
    uint64_t hasF = (isa >> 5) & 1;

    if (fs == 0 || hasF == 0) return;
    write_fcsr(0);
}

int streq(const char* a, const char* b)
{
    while (*a && *b && *a == *b) { a++; b++; }
    return (*a == 0 && *b == 0);
}

static int find_symbol_by_name(const uint8_t *blob, size_t blob_size, const Elf64_Ehdr *eh,
                               const char *target, uint64_t *value_out)
{
    if (value_out) *value_out = 0;
    if (!blob || !eh || !target || !value_out) return -1;

    uint64_t sh_end = eh->e_shoff + ((uint64_t)eh->e_shnum * (uint64_t)eh->e_shentsize);
    if (eh->e_shoff == 0 || eh->e_shentsize != sizeof(Elf64_Shdr)) return -2;
    if (eh->e_shoff > blob_size || sh_end > blob_size || sh_end < eh->e_shoff) return -3;

    const Elf64_Shdr *sh = (const Elf64_Shdr *)(const void *)(blob + eh->e_shoff);
    for (uint16_t i = 0; i < eh->e_shnum; i++) {
        if (!(sh[i].sh_type == SHT_SYMTAB || sh[i].sh_type == SHT_DYNSYM)) continue;
        if (sh[i].sh_entsize != sizeof(Elf64_Sym) || sh[i].sh_entsize == 0) continue;
        if (sh[i].sh_link >= eh->e_shnum) continue;
        if (sh[i].sh_offset > blob_size || (sh[i].sh_offset + sh[i].sh_size) > blob_size) continue;

        const Elf64_Shdr *strsec = &sh[sh[i].sh_link];
        if (strsec->sh_offset > blob_size || (strsec->sh_offset + strsec->sh_size) > blob_size) continue;
        const char *strtab = (const char *)(const void *)(blob + strsec->sh_offset);

        const Elf64_Sym *sym = (const Elf64_Sym *)(const void *)(blob + sh[i].sh_offset);
        uint64_t n = sh[i].sh_size / sh[i].sh_entsize;
        for (uint64_t s = 0; s < n; s++) {
            if (sym[s].st_name >= strsec->sh_size) continue;
            if (!streq(strtab + sym[s].st_name, target)) continue;
            *value_out = sym[s].st_value;
            return 0;
        }
    }

    return -4;
}

int find_symbol_for_addr(const uint8_t *blob, size_t blob_size, uint64_t addr, SymbolInfo *out)
{
    if (!out) return -1;
    out->name = 0;
    out->value = 0;
    out->size = 0;

    if (!blob || blob_size < sizeof(Elf64_Ehdr)) return -2;

    const Elf64_Ehdr *eh = (const Elf64_Ehdr *)(const void *)blob;
    uint64_t sh_end = eh->e_shoff + ((uint64_t)eh->e_shnum * (uint64_t)eh->e_shentsize);
    if (eh->e_shoff == 0 || eh->e_shentsize != sizeof(Elf64_Shdr)) return -3;
    if (eh->e_shoff > blob_size || sh_end > blob_size || sh_end < eh->e_shoff) return -4;

    const Elf64_Shdr *sh = (const Elf64_Shdr *)(const void *)(blob + eh->e_shoff);
    uint64_t best_delta = ~0ULL;

    for (uint16_t i = 0; i < eh->e_shnum; i++) {
        if (!(sh[i].sh_type == SHT_SYMTAB || sh[i].sh_type == SHT_DYNSYM)) continue;
        if (sh[i].sh_entsize != sizeof(Elf64_Sym) || sh[i].sh_entsize == 0) continue;
        if (sh[i].sh_link >= eh->e_shnum) continue;
        if (sh[i].sh_offset > blob_size || (sh[i].sh_offset + sh[i].sh_size) > blob_size) continue;

        const Elf64_Shdr *strsec = &sh[sh[i].sh_link];
        if (strsec->sh_offset > blob_size || (strsec->sh_offset + strsec->sh_size) > blob_size) continue;

        const char *strtab = (const char *)(const void *)(blob + strsec->sh_offset);
        const Elf64_Sym *sym = (const Elf64_Sym *)(const void *)(blob + sh[i].sh_offset);
        uint64_t n = sh[i].sh_size / sh[i].sh_entsize;
        for (uint64_t s = 0; s < n; s++) {
            if (sym[s].st_name >= strsec->sh_size) continue;
            if (sym[s].st_value == 0 || sym[s].st_value > addr) continue;

            uint64_t delta = addr - sym[s].st_value;
            if (sym[s].st_size != 0 && delta >= sym[s].st_size) continue;
            if (delta > best_delta) continue;

            best_delta = delta;
            out->name = strtab + sym[s].st_name;
            out->value = sym[s].st_value;
            out->size = sym[s].st_size;
        }
    }

    return out->name ? 0 : -5;
}

void capture_last_trap_in_mode(const TrapFrame *tf, uint64_t mcause, uint64_t mepc,
                               uint64_t mtval, uint64_t mstatus, uint64_t trap_mode)
{
    if (tf) g_last_trap.frame = *tf;
    else memset_local(&g_last_trap.frame, 0, sizeof(g_last_trap.frame));
    g_last_trap.mcause = mcause;
    g_last_trap.mepc = mepc;
    g_last_trap.mtval = mtval;
    g_last_trap.mstatus = mstatus;
    g_last_trap.mode = trap_mode;
    g_last_trap.valid = 1u;
}

void capture_last_trap(const TrapFrame *tf, uint64_t mcause, uint64_t mepc,
                       uint64_t mtval, uint64_t mstatus)
{
    capture_last_trap_in_mode(tf, mcause, mepc, mtval, mstatus, EXEC_MODE_M);
}

static uint64_t read_insn_halfword_for_log(uint64_t pc, int *valid_out)
{
    if (valid_out) *valid_out = 0;
    if ((pc & 0x1ULL) != 0) return 0;
    if (!is_valid_ddr_addr(pc) || !is_valid_ddr_addr(pc + 1ULL)) return 0;
    if (valid_out) *valid_out = 1;
    return (uint64_t)(*(volatile const uint16_t *)(uintptr_t)pc);
}

void reset_first_trap_state(void)
{
    memset_local(&g_first_trap, 0, sizeof(g_first_trap));
}

void emit_first_trap_state(const char *tag)
{
    const char *name = tag ? tag : "[TRAP_FIRST]";

    dbg_puts(name);
    dbg_puts(" valid=");
    dbg_puts(g_first_trap.valid ? "1" : "0");
    if (!g_first_trap.valid) {
        dbg_nl();
        return;
    }
    dbg_puts(" seq=0x"); dbg_hex_u64(g_first_trap.seq);
    dbg_puts(" mode=");
    dbg_puts(exec_mode_name(g_first_trap.mode));
    dbg_puts(" mcause=0x"); dbg_hex_u64(g_first_trap.mcause);
    dbg_puts(" pc=0x"); dbg_hex_u64(g_first_trap.mepc);
    dbg_puts(" tval=0x"); dbg_hex_u64(g_first_trap.mtval);
    dbg_puts(" status=0x"); dbg_hex_u64(g_first_trap.status);
    dbg_puts(" inst16=0x"); dbg_hex_u64(g_first_trap.inst16);
    dbg_puts(" inst32=0x"); dbg_hex_u64(g_first_trap.inst32);
    dbg_puts(" mtvec=0x"); dbg_hex_u64(g_first_trap.mtvec);
    dbg_puts(" stvec=0x"); dbg_hex_u64(g_first_trap.stvec);
    dbg_puts(" medeleg=0x"); dbg_hex_u64(g_first_trap.medeleg);
    dbg_puts(" mideleg=0x"); dbg_hex_u64(g_first_trap.mideleg);
    dbg_puts(" satp=0x"); dbg_hex_u64(g_first_trap.satp);
    dbg_nl();
}

void capture_first_trap_in_mode(const TrapFrame *tf, uint64_t mcause, uint64_t pc,
                                uint64_t tval, uint64_t status, uint64_t trap_mode)
{
    int have16 = 0;
    int have32 = 0;

    if (g_first_trap.valid) return;

    if (tf) g_first_trap.frame = *tf;
    else memset_local(&g_first_trap.frame, 0, sizeof(g_first_trap.frame));
    g_first_trap.seq = 1;
    g_first_trap.mcause = mcause;
    g_first_trap.mepc = pc;
    g_first_trap.mtval = tval;
    g_first_trap.status = status;
    g_first_trap.mode = trap_mode;
    g_first_trap.inst16 = read_insn_halfword_for_log(pc, &have16);
    if (!have16) g_first_trap.inst16 = 0;
    g_first_trap.inst32 = (uint64_t)read_insn_word(pc, &have32);
    if (!have32) g_first_trap.inst32 = 0;
    g_first_trap.mtvec = read_csr_mtvec();
    g_first_trap.stvec = read_csr_stvec_local();
    g_first_trap.medeleg = read_csr_medeleg();
    g_first_trap.mideleg = read_csr_mideleg();
    g_first_trap.satp = read_csr_satp_local();
    g_first_trap.valid = 1u;

    emit_first_trap_state("[TRAP_FIRST]");
}

const char *trap_reason_name(uint64_t mcause)
{
    if (mcause & MCAUSE_INTERRUPT_BIT) {
        switch (mcause) {
            case MCAUSE_MSI: return "machine_software_interrupt";
            case MCAUSE_MTI: return "machine_timer_interrupt";
            default: return "interrupt_other";
        }
    }

    switch (mcause) {
        case MCAUSE_INST_MISALIGNED: return "instruction_misaligned";
        case MCAUSE_ILLEGAL_INSN: return "illegal_instruction";
        case MCAUSE_BREAKPOINT: return "breakpoint";
        case MCAUSE_INST_ACCESS_FAULT: return "instruction_access_fault";
        case MCAUSE_LOAD_MISALIGNED: return "load_misaligned";
        case MCAUSE_LOAD_ACCESS_FAULT: return "load_access_fault";
        case MCAUSE_STORE_MISALIGNED: return "store_misaligned";
        case MCAUSE_STORE_ACCESS_FAULT: return "store_access_fault";
        case MCAUSE_ECALL_U: return "ecall_from_umode";
        case MCAUSE_ECALL_S: return "ecall_from_smode";
        case MCAUSE_ECALL_M: return "ecall_from_mmode";
        case MCAUSE_INST_PAGE_FAULT: return "instruction_page_fault";
        case MCAUSE_LOAD_PAGE_FAULT: return "load_page_fault";
        case MCAUSE_STORE_PAGE_FAULT: return "store_page_fault";
        default: return "exception_other";
    }
}

uint32_t read_insn_word(uint64_t pc, int *valid_out)
{
    uint16_t low;
    if (valid_out) *valid_out = 0;
    if ((pc & 0x1ULL) != 0) return 0;
    if (!is_valid_ddr_addr(pc) || !is_valid_ddr_addr(pc + 1)) return 0;

    low = *(volatile const uint16_t *)(uintptr_t)pc;
    if (valid_out) *valid_out = 1;
    if ((low & 0x3u) != 0x3u) return (uint32_t)low;

    if (!is_valid_ddr_addr(pc + 3)) {
        if (valid_out) *valid_out = 0;
        return 0;
    }

    return (uint32_t)low | ((uint32_t)(*(volatile const uint16_t *)(uintptr_t)(pc + 2ULL)) << 16);
}

void find_signature_range(const uint8_t* blob, size_t blob_size, const Elf64_Ehdr* eh,
                          uint64_t *sig_begin_out, uint64_t *sig_end_out)
{
    if (sig_begin_out) *sig_begin_out = 0;
    if (sig_end_out) *sig_end_out = 0;
    if (!sig_begin_out || !sig_end_out) return;

    (void)find_symbol_by_name(blob, blob_size, eh, "begin_signature", sig_begin_out);
    (void)find_symbol_by_name(blob, blob_size, eh, "end_signature", sig_end_out);
}

void find_failure_scratch_range(const uint8_t *blob, size_t blob_size, const Elf64_Ehdr *eh,
                                uint64_t *begin_out, uint64_t *end_out)
{
    if (begin_out) *begin_out = 0;
    if (end_out) *end_out = 0;
    if (!begin_out || !end_out) return;

    (void)find_symbol_by_name(blob, blob_size, eh, "begin_failure_scratch", begin_out);
    (void)find_symbol_by_name(blob, blob_size, eh, "end_failure_scratch", end_out);
}

void dump_signature_region(uint64_t begin, uint64_t end)
{
    if (!is_valid_ddr_range(begin, end)) {
        dbg_puts("[SIG] signature range unavailable\n");
        return;
    }

    uint64_t bytes = end - begin;
    dbg_puts("[SIG] begin=0x"); dbg_hex_u64(begin);
    dbg_puts(" end=0x"); dbg_hex_u64(end);
    dbg_puts(" bytes=0x"); dbg_hex_u64(bytes); dbg_nl();

    volatile uint8_t *p = (volatile uint8_t *)(uintptr_t)begin;
    uint64_t qwords = bytes / 8;
    uint64_t tail = bytes % 8;

    for (uint64_t i = 0; i < qwords; i++) {
        uint64_t a = begin + (i * 8);
        uint64_t v = 0;
        for (uint64_t j = 0; j < 8; j++) {
            v |= ((uint64_t)p[(i * 8) + j]) << (j * 8);
        }
        dbg_puts("[SIGQ] 0x"); dbg_hex_u64(a);
        dbg_puts(" : 0x"); dbg_hex_u64(v);
        dbg_nl();
    }

    for (uint64_t i = 0; i < tail; i++) {
        uint64_t a = begin + (qwords * 8) + i;
        uint8_t b = p[(qwords * 8) + i];
        dbg_puts("[SIGB] 0x"); dbg_hex_u64(a);
        dbg_puts(" : 0x");
        dbg_putc("0123456789abcdef"[(b >> 4) & 0xf]);
        dbg_putc("0123456789abcdef"[b & 0xf]);
        dbg_nl();
    }
}

void dump_failure_scratch_region(uint64_t begin, uint64_t end)
{
    if (begin == 0 || end == 0 || end <= begin || !is_valid_ddr_range(begin, end)) {
        dbg_puts("[FAILSCR] unavailable\n");
        return;
    }

    uint64_t bytes = end - begin;
    dbg_puts("[FAILSCR] begin=0x"); dbg_hex_u64(begin);
    dbg_puts(" end=0x"); dbg_hex_u64(end);
    dbg_puts(" bytes=0x"); dbg_hex_u64(bytes); dbg_nl();

    volatile uint8_t *p = (volatile uint8_t *)(uintptr_t)begin;
    uint64_t qwords = bytes / 8;
    uint64_t tail = bytes % 8;

    for (uint64_t i = 0; i < qwords; i++) {
        uint64_t a = begin + (i * 8);
        uint64_t v = 0;
        for (uint64_t j = 0; j < 8; j++) {
            v |= ((uint64_t)p[(i * 8) + j]) << (j * 8);
        }
        dbg_puts("[FAILQ] 0x"); dbg_hex_u64(a);
        dbg_puts(" : 0x"); dbg_hex_u64(v);
        dbg_nl();
    }

    for (uint64_t i = 0; i < tail; i++) {
        uint64_t a = begin + (qwords * 8) + i;
        uint8_t b = p[(qwords * 8) + i];
        dbg_puts("[FAILB] 0x"); dbg_hex_u64(a);
        dbg_puts(" : 0x");
        dbg_putc("0123456789abcdef"[(b >> 4) & 0xf]);
        dbg_putc("0123456789abcdef"[b & 0xf]);
        dbg_nl();
    }
}

static int read_u32_from_addr(uint64_t addr, uint32_t *out)
{
    if (!out || !is_valid_ddr_addr(addr) || !is_valid_ddr_addr(addr + 3ULL)) return -1;
    *out = *(volatile const uint32_t *)(uintptr_t)addr;
    return 0;
}

static int read_u64_from_addr(uint64_t addr, uint64_t *out)
{
    if (!out || !is_valid_ddr_addr(addr) || !is_valid_ddr_addr(addr + 7ULL)) return -1;
    *out = *(volatile const uint64_t *)(uintptr_t)addr;
    return 0;
}

static int read_loaded_symbol_u64(const char *name, uint64_t *value_out, uint64_t *addr_out)
{
    uint64_t addr = 0;
    if (!g_runner_image.loaded_blob || g_runner_image.loaded_blob_size < sizeof(Elf64_Ehdr)) return -1;

    const Elf64_Ehdr *eh = (const Elf64_Ehdr *)(const void *)g_runner_image.loaded_blob;
    if (find_symbol_by_name(g_runner_image.loaded_blob, g_runner_image.loaded_blob_size, eh, name, &addr) != 0) return -2;
    if (addr_out) *addr_out = addr;
    if (read_u64_from_addr(addr, value_out) != 0) return -3;
    return 0;
}

static int lookup_loaded_symbol_addr(const char *name, uint64_t *addr_out)
{
    if (!addr_out) return -1;
    *addr_out = 0;
    if (!g_runner_image.loaded_blob || g_runner_image.loaded_blob_size < sizeof(Elf64_Ehdr)) return -2;

    const Elf64_Ehdr *eh = (const Elf64_Ehdr *)(const void *)g_runner_image.loaded_blob;
    return find_symbol_by_name(g_runner_image.loaded_blob, g_runner_image.loaded_blob_size, eh, name, addr_out);
}

static void dump_mtvec_symbol(uint64_t base)
{
    SymbolInfo sym;
    if (g_runner_image.loaded_blob &&
        find_symbol_for_addr(g_runner_image.loaded_blob, g_runner_image.loaded_blob_size, base, &sym) == 0 &&
        sym.name) {
        dbg_puts(" symbol=");
        dbg_puts(sym.name);
        dbg_puts("+0x");
        dbg_hex_u64(base - sym.value);
    } else {
        dbg_puts(" symbol=unknown");
    }
}

static void dump_act_mmode_mtvec(void)
{
    const uint64_t xtvec_new_off = 704ULL;
    const uint64_t xtvec_saved_off = 712ULL;
    uint64_t tbl = 0, act_mtvec = 0, saved_mtvec = 0;
    uint64_t base, mode;

    if (lookup_loaded_symbol_addr("Mtramptbl_sv", &tbl) != 0) {
        dbg_puts("[ACTMTVEC] unavailable=no_Mtramptbl_sv_symbol\n");
        return;
    }

    (void)read_u64_from_addr(tbl + xtvec_new_off, &act_mtvec);
    (void)read_u64_from_addr(tbl + xtvec_saved_off, &saved_mtvec);
    base = act_mtvec & ~3ULL;
    mode = act_mtvec & 3ULL;

    dbg_puts("[ACTMTVEC] Mtramptbl_sv=0x"); dbg_hex_u64(tbl);
    dbg_puts(" act_mmode_mtvec=0x"); dbg_hex_u64(act_mtvec);
    dbg_puts(" base=0x"); dbg_hex_u64(base);
    dbg_puts(" mode=0x"); dbg_hex_u64(mode);
    dump_mtvec_symbol(base);
    dbg_puts(" saved_runner_mtvec=0x"); dbg_hex_u64(saved_mtvec);
    dbg_nl();
}

static void dump_act_handler_trace(void)
{
    const uint64_t rvmodel_sv_off = 792ULL;
    static const char *names[8] = {
        "slot0", "slot1", "slot2", "slot3",
        "slot4", "slot5", "slot6", "slot7"
    };
    uint64_t tbl = 0;

    if (lookup_loaded_symbol_addr("Mtramptbl_sv", &tbl) != 0) {
        dbg_puts("[ACTHTRACE] unavailable=no_Mtramptbl_sv_symbol\n");
        return;
    }

    dbg_puts("[ACTHTRACE] Mtramptbl_sv=0x"); dbg_hex_u64(tbl);
    dbg_puts(" rvmodel_sv=0x"); dbg_hex_u64(tbl + rvmodel_sv_off);
    dbg_nl();

    for (uint64_t i = 0; i < 8; i++) {
        uint64_t addr = tbl + rvmodel_sv_off + (i * 8ULL);
        uint64_t value = 0;
        if (read_u64_from_addr(addr, &value) != 0) {
            dbg_puts("[ACTHTRACEQ] unavailable=read_failed index=0x");
            dbg_hex_u64(i);
            dbg_nl();
            return;
        }
        dbg_puts("[ACTHTRACEQ] ");
        dbg_puts(names[i]);
        dbg_puts("_addr=0x"); dbg_hex_u64(addr);
        dbg_puts(" value=0x"); dbg_hex_u64(value);
        dbg_nl();
    }
}

static void dump_act_reg_line(uint64_t begin, int r0, int r1, int r2, int r3)
{
    static const char *names[32] = {
        "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7",
        "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15",
        "x16", "x17", "x18", "x19", "x20", "x21", "x22", "x23",
        "x24", "x25", "x26", "x27", "x28", "x29", "x30", "x31"
    };
    uint64_t v0 = 0, v1 = 0, v2 = 0, v3 = 0;

    if (r0 != 0) (void)read_u64_from_addr(begin + ((uint64_t)r0 * 8ULL), &v0);
    if (r1 != 0) (void)read_u64_from_addr(begin + ((uint64_t)r1 * 8ULL), &v1);
    if (r2 != 0) (void)read_u64_from_addr(begin + ((uint64_t)r2 * 8ULL), &v2);
    if (r3 != 0) (void)read_u64_from_addr(begin + ((uint64_t)r3 * 8ULL), &v3);

    dbg_puts("[ACTREG] ");
    dbg_puts(names[r0]); dbg_puts("=0x"); dbg_hex_u64(v0);
    dbg_puts(" "); dbg_puts(names[r1]); dbg_puts("=0x"); dbg_hex_u64(v1);
    dbg_puts(" "); dbg_puts(names[r2]); dbg_puts("=0x"); dbg_hex_u64(v2);
    dbg_puts(" "); dbg_puts(names[r3]); dbg_puts("=0x"); dbg_hex_u64(v3);
    dbg_nl();
}

static void dump_act_named_u64(const char *tag, const char *name)
{
    uint64_t addr = 0, value = 0;
    dbg_puts(tag);
    dbg_puts(" ");
    dbg_puts(name);
    if (read_loaded_symbol_u64(name, &value, &addr) == 0) {
        dbg_puts("_addr=0x"); dbg_hex_u64(addr);
        dbg_puts(" value=0x"); dbg_hex_u64(value);
    } else {
        dbg_puts("=unavailable");
    }
    dbg_nl();
}

void dump_act_irq_section_trace(void)
{
    const uint64_t record_qwords = 8ULL;
    const uint64_t record_bytes = record_qwords * sizeof(uint64_t);
    const uint64_t max_records = 128ULL;
    uint64_t count = 0;
    uint64_t trace_addr = 0;

    if (read_loaded_symbol_u64("irqdbg_trace_count", &count, 0) != 0 ||
        lookup_loaded_symbol_addr("irqdbg_trace", &trace_addr) != 0) {
        return;
    }

    if (count > max_records) count = max_records;
    dbg_puts("[IRQSEC] records=0x"); dbg_hex_u64(count);
    dbg_puts(" base=0x"); dbg_hex_u64(trace_addr);
    dbg_nl();

    for (uint64_t i = 0; i < count; ++i) {
        uint64_t record[8] = {0, 0, 0, 0, 0, 0, 0, 0};
        uint64_t addr = trace_addr + (i * record_bytes);
        SymbolInfo sym;
        int valid = 1;

        for (uint64_t word = 0; word < record_qwords; ++word) {
            if (read_u64_from_addr(addr + (word * sizeof(uint64_t)), &record[word]) != 0) {
                valid = 0;
                break;
            }
        }

        dbg_puts("[IRQSEC] index=0x"); dbg_hex_u64(i);
        if (!valid) {
            dbg_puts(" valid=0\n");
            continue;
        }

        dbg_puts(" stage=0x"); dbg_hex_u64(record[0]);
        dbg_puts(" phase=");
        if ((record[0] & 0xfULL) == 0) dbg_puts("configured");
        else if ((record[0] & 0xfULL) == 1) dbg_puts("asserted");
        else if ((record[0] & 0xfULL) == 2) dbg_puts("returned");
        else dbg_puts("unknown");
        dbg_puts(" case_pc=0x"); dbg_hex_u64(record[1]);
        if (g_runner_image.loaded_blob &&
            find_symbol_for_addr(g_runner_image.loaded_blob,
                                 g_runner_image.loaded_blob_size,
                                 record[1], &sym) == 0 &&
            sym.name) {
            dbg_puts(" case="); dbg_puts(sym.name);
        }
        dbg_puts(" mideleg=0x"); dbg_hex_u64(record[2]);
        dbg_puts(" mie=0x"); dbg_hex_u64(record[3]);
        dbg_puts(" mip=0x"); dbg_hex_u64(record[4]);
        dbg_puts(" mstatus=0x"); dbg_hex_u64(record[5]);
        dbg_puts(" sip=0x"); dbg_hex_u64(record[6]);
        dbg_puts(" sie=0x"); dbg_hex_u64(record[7]);
        dbg_nl();
    }
}

void dump_act_irq_timeout_context(void)
{
    dbg_puts("[IRQTIMEOUT] live payload snapshot\n");
    dump_act_named_u64("[IRQTIMEOUT]", "rvmodel_boot_hartid");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_stage");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_pre_mideleg");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_pre_mie");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_pre_mip");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_pre_mstatus");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_pre_sip");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_pre_sie");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_pre_stvec");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_pre_s_trap_count");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_s_count");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_s_scause");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_s_sepc");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_s_stvec");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_s_sstatus");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_s_sscratch");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_s_sp");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_uart_ier");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_plic_pending");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_plic_m_enable");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_plic_s_enable");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_valid");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_mode");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_xepc");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_xcause");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_xtval");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_xstatus");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_mideleg");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_mip");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_mie");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_sip");
    dump_act_named_u64("[IRQTIMEOUT]", "irqdbg_first_sie");
    dump_act_named_u64("[IRQTIMEOUT]", "rvmodel_last_plic_claim");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_count");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_pre_sp");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_pre_mscratch");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_pre_saved_sp");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_pre_mepc");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_pre_mcause");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_post_sp");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_post_mscratch");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_post_saved_sp");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_post_mepc");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_mext_post_mcause");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_gotom_count");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_gotom_entry_sp");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_gotom_entry_mscratch");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_gotom_entry_mepc");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_gotom_entry_mstatus");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_gotom_final_sp");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_gotom_final_mscratch");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_gotom_final_t2");
    dump_act_named_u64("[IRQTIMEOUT]", "irqret_gotom_final_t4");
    dump_act_irq_section_trace();
    dump_act_handler_trace();
}

static void dump_act_hart_csr_snapshot(void)
{
    static const char *const names[] = {
        "saved_mhartid",
        "saved_mie",
        "saved_mip",
        "saved_mtvec",
        "saved_medeleg",
        "saved_mideleg",
        "saved_sstatus",
        "saved_stvec",
        "saved_scause",
        "saved_sepc",
        "saved_stval",
        "saved_satp",
        "saved_pmpcfg0",
        "saved_pmpaddr0",
        "saved_pmpaddr1",
        "saved_pmpaddr2",
        "saved_pmpaddr3",
        "saved_pmpaddr4",
        "saved_pmpaddr5",
        "saved_pmpaddr6",
        "saved_pmpaddr7",
    };

    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        dump_act_named_u64("[ACTHARTCSR]", names[i]);
    }
}

static void dump_act_trap_sig_window(uint64_t current_ptr)
{
    uint64_t trap_sigptr = 0;
    uint64_t begin = 0;
    uint64_t end = 0;

    if (current_ptr == 0 || current_ptr < 0x40ULL) {
        dbg_puts("[ACTTSIG] unavailable=no_current_pointer\n");
        return;
    }

    begin = current_ptr - 0x40ULL;
    end = current_ptr + 0x20ULL;
    if (!is_valid_ddr_range(begin, end)) {
        dbg_puts("[ACTTSIG] unavailable=invalid_range ptr=0x");
        dbg_hex_u64(current_ptr);
        dbg_nl();
        return;
    }

    (void)lookup_loaded_symbol_addr("trap_sigptr", &trap_sigptr);
    dbg_puts("[ACTTSIG] trap_sigptr=0x"); dbg_hex_u64(trap_sigptr);
    dbg_puts(" current_ptr=0x"); dbg_hex_u64(current_ptr);
    if (trap_sigptr != 0 && current_ptr >= trap_sigptr) {
        dbg_puts(" current_off=0x"); dbg_hex_u64(current_ptr - trap_sigptr);
    }
    dbg_puts(" window_begin=0x"); dbg_hex_u64(begin);
    dbg_puts(" window_end=0x"); dbg_hex_u64(end);
    dbg_nl();

    for (uint64_t a = begin; a < end; a += 8ULL) {
        uint64_t v = 0;
        if (read_u64_from_addr(a, &v) != 0) break;
        dbg_puts("[ACTTSIGQ] 0x"); dbg_hex_u64(a);
        if (trap_sigptr != 0 && a >= trap_sigptr) {
            dbg_puts(" off=0x"); dbg_hex_u64(a - trap_sigptr);
        }
        dbg_puts(" : 0x"); dbg_hex_u64(v);
        dbg_nl();
    }
}

static void dump_act_signature_flow(uint64_t current_ptr,
                                    uint64_t saved_mepc,
                                    uint64_t saved_mcause,
                                    uint64_t saved_mtval,
                                    uint64_t saved_mstatus)
{
    uint64_t trap_sigptr = 0;
    uint64_t compare_slot = current_ptr;
    uint64_t prev_exception_slot = 0;
    uint64_t compare_slot_word_index = ~0ULL;
    uint64_t prev_exception_word_index = ~0ULL;
    uint64_t expected_signature_word = 0;
    uint64_t actual_signature_word = 0;
    uint64_t trap_diag_expected = 0;
    uint64_t trap_diag_actual = 0;
    uint64_t scratch_expected = 0;
    uint64_t scratch_actual = 0;
    int expected_valid = 0;
    int actual_valid = 0;

    (void)lookup_loaded_symbol_addr("trap_sigptr", &trap_sigptr);

    if (current_ptr >= 4ULL * sizeof(uint64_t)) {
        prev_exception_slot = current_ptr - (4ULL * sizeof(uint64_t));
    }

    if (trap_sigptr != 0 && current_ptr >= trap_sigptr) {
        compare_slot_word_index = (current_ptr - trap_sigptr) / 8ULL;
    }
    if (trap_sigptr != 0 && prev_exception_slot >= trap_sigptr) {
        prev_exception_word_index = (prev_exception_slot - trap_sigptr) / 8ULL;
    }

    if (compare_slot != 0 &&
        is_valid_ddr_addr(compare_slot + sizeof(uint64_t)) &&
        read_u64_from_addr(compare_slot + sizeof(uint64_t), &actual_signature_word) == 0) {
        actual_valid = 1;
    } else if (read_loaded_symbol_u64("failing_value", &scratch_actual, 0) == 0) {
        actual_signature_word = scratch_actual;
        actual_valid = 1;
    } else if (read_loaded_symbol_u64("trap_diag_actual_value", &trap_diag_actual, 0) == 0) {
        actual_signature_word = trap_diag_actual;
        actual_valid = 1;
    }

    if (read_loaded_symbol_u64("expected_value", &scratch_expected, 0) == 0) {
        expected_signature_word = scratch_expected;
        expected_valid = 1;
    } else if (read_loaded_symbol_u64("trap_diag_expected_value", &trap_diag_expected, 0) == 0) {
        expected_signature_word = trap_diag_expected;
        expected_valid = 1;
    }

    dbg_puts("[ACTFLOW] trap_sigptr_base=0x"); dbg_hex_u64(trap_sigptr);
    dbg_puts(" trap_sigptr_before=0x"); dbg_hex_u64(prev_exception_slot);
    dbg_puts(" trap_sigptr_after=0x"); dbg_hex_u64(compare_slot);
    dbg_puts(" compare_slot=0x"); dbg_hex_u64(compare_slot);
    dbg_puts(" prev_exception_slot=0x"); dbg_hex_u64(prev_exception_slot);
    dbg_puts(" saved_mepc=0x"); dbg_hex_u64(saved_mepc);
    dbg_puts(" saved_mcause=0x"); dbg_hex_u64(saved_mcause);
    dbg_puts(" saved_mtval=0x"); dbg_hex_u64(saved_mtval);
    dbg_puts(" saved_mstatus=0x"); dbg_hex_u64(saved_mstatus);
    dbg_puts(" compare_slot_word_index=");
    if (compare_slot_word_index == ~0ULL) {
        dbg_puts("unavailable");
    } else {
        dbg_puts("0x"); dbg_hex_u64(compare_slot_word_index);
    }
    dbg_puts(" signature_word_index=");
    if (compare_slot_word_index == ~0ULL) {
        dbg_puts("unavailable");
    } else {
        dbg_puts("0x"); dbg_hex_u64(compare_slot_word_index);
    }
    dbg_puts(" prev_exception_word_index=");
    if (prev_exception_word_index == ~0ULL) {
        dbg_puts("unavailable");
    } else {
        dbg_puts("0x"); dbg_hex_u64(prev_exception_word_index);
    }
    dbg_puts(" expected_signature_word=");
    if (expected_valid) {
        dbg_puts("0x"); dbg_hex_u64(expected_signature_word);
    } else {
        dbg_puts("unavailable");
    }
    dbg_puts(" actual_signature_word=");
    if (actual_valid) {
        dbg_puts("0x"); dbg_hex_u64(actual_signature_word);
    } else {
        dbg_puts("unavailable");
    }
    dbg_nl();

    for (unsigned slot = 0; slot < 2; ++slot) {
        uint64_t addr = slot == 0 ? compare_slot : prev_exception_slot;
        uint64_t word[4] = {0, 0, 0, 0};
        int valid = 1;

        if (addr == 0 || !is_valid_ddr_range(addr, addr + 4ULL * sizeof(uint64_t))) {
            valid = 0;
        } else {
            for (unsigned i = 0; i < 4; ++i) {
                if (read_u64_from_addr(addr + (uint64_t)i * sizeof(uint64_t), &word[i]) != 0) {
                    valid = 0;
                    break;
                }
            }
        }

        dbg_puts(slot == 0 ? "[ACTFLOW_SLOT] kind=compare" : "[ACTFLOW_SLOT] kind=prev_exception");
        dbg_puts(" addr=0x"); dbg_hex_u64(addr);
        if (trap_sigptr != 0 && addr >= trap_sigptr) {
            dbg_puts(" off=0x"); dbg_hex_u64(addr - trap_sigptr);
        }
        if (!valid) {
            dbg_puts(" valid=0\n");
            continue;
        }
        dbg_puts(" valid=1 word0=0x"); dbg_hex_u64(word[0]);
        dbg_puts(" xcause=0x"); dbg_hex_u64(word[1]);
        dbg_puts(" xepc=0x"); dbg_hex_u64(word[2]);
        dbg_puts(" xtval=0x"); dbg_hex_u64(word[3]);
        dbg_nl();
    }
}

void dump_act_failure_context(void)
{
    uint64_t begin = g_runner_image.fail_begin;
    uint64_t end = g_runner_image.fail_end;
    uint64_t saved_mepc = 0, saved_mcause = 0, saved_mtval = 0, saved_mstatus = 0;
    uint64_t failing_addr = 0;
    uint64_t monitor_current_hartid = read_csr_mhartid();
    uint64_t monitor_current_mtvec = read_csr_mtvec();
    uint64_t monitor_current_stvec = read_csr_stvec_local();
    uint64_t monitor_current_medeleg = read_csr_medeleg();
    uint64_t monitor_current_mideleg = read_csr_mideleg();
    uint64_t monitor_current_satp = read_csr_satp_local();
    uint64_t monitor_current_pmpcfg0 = read_csr_pmpcfg0();
    uint64_t monitor_current_pmpaddr0 = read_csr_pmpaddr0();
    uint64_t monitor_current_pmpaddr1 = read_csr_pmpaddr1();
    uint64_t act_x6_trap_sig_ptr = 0;
    uint32_t failure_type = 0;

    if (begin == 0 || end == 0 || end <= begin || !is_valid_ddr_range(begin, end)) {
        dbg_puts("[ACTDBG] failure_context=unavailable\n");
        return;
    }

    dbg_puts("[ACTDBG] decoded_failure_scratch begin=0x"); dbg_hex_u64(begin);
    dbg_puts(" end=0x"); dbg_hex_u64(end); dbg_nl();

    if (read_u32_from_addr(begin, &failure_type) == 0) {
        dbg_puts("[ACTDBG] failure_type=0x"); dbg_hex_u32(failure_type);
        dbg_puts(" note=x0_slot_reused_by_ACT\n");
    }

    for (int r = 0; r < 32; r += 4) {
        dump_act_reg_line(begin, r, r + 1, r + 2, r + 3);
    }
    (void)read_u64_from_addr(begin + (6ULL * 8ULL), &act_x6_trap_sig_ptr);

    dump_act_named_u64("[ACTFAIL]", "failing_instruction");
    dump_act_named_u64("[ACTFAIL]", "failing_addr");
    dump_act_named_u64("[ACTFAIL]", "failing_value");
    dump_act_named_u64("[ACTFAIL]", "expected_value");
    dump_act_named_u64("[ACTFAIL]", "failure_string_ptr");
    dump_act_named_u64("[ACTTRAP]", "trap_diag_actual_value");
    dump_act_named_u64("[ACTTRAP]", "trap_diag_expected_value");
    dump_act_named_u64("[ACTCMP]", "trap_compare_link_reg");
    dump_act_named_u64("[ACTCMP]", "trap_compare_sigptr");
    dump_act_named_u64("[ACTCMP]", "trap_compare_actual_reg");
    dump_act_named_u64("[ACTCMP]", "trap_compare_expected_reg");
    dump_act_named_u64("[ACTCMP]", "trap_compare_live_mepc");
    dump_act_named_u64("[ACTCMP]", "trap_compare_live_mcause");
    dump_act_named_u64("[ACTCMP]", "trap_compare_live_mtval");
    dump_act_named_u64("[ACTCMP]", "trap_compare_live_mstatus");
    dump_act_named_u64("[ACTSLOT]", "dbg_trap_slot_t1");
    dump_act_named_u64("[ACTSLOT]", "dbg_trap_slot_t4");
    dump_act_named_u64("[ACTSLOT]", "dbg_trap_entry_size");
    dump_act_named_u64("[ACTSLOT]", "dbg_trap_xepc");
    dump_act_named_u64("[ACTSLOT]", "dbg_trap_xcause");
    dump_act_named_u64("[ACTSLOT]", "dbg_trap_xtval");
    dump_act_named_u64("[ACTSLOT]", "rvmodel_last_plic_claim");
    dump_act_named_u64("[ACTSLOT]", "dbg_mcause_sigptr");
    dump_act_named_u64("[ACTSLOT]", "dbg_mcause_actual");
    dump_act_named_u64("[ACTSLOT]", "dbg_mcause_expected");
    dump_act_named_u64("[ACTSLOT]", "dbg_mcause_live_xepc");
    dump_act_named_u64("[ACTSLOT]", "dbg_mcause_live_xcause");
    dump_act_named_u64("[ACTSLOT]", "dbg_mcause_live_xtval");
    dump_act_named_u64("[ACTSLOT]", "dbg_mepc_sigptr");
    dump_act_named_u64("[ACTSLOT]", "dbg_mepc_actual");
    dump_act_named_u64("[ACTSLOT]", "dbg_mepc_expected");
    dump_act_named_u64("[ACTSLOT]", "dbg_mepc_live_xepc");
    dump_act_named_u64("[ACTSLOT]", "dbg_mepc_live_xcause");
    dump_act_named_u64("[IRQDBG]", "irqdbg_stage");
    dump_act_named_u64("[IRQDBG]", "irqdbg_pre_mideleg");
    dump_act_named_u64("[IRQDBG]", "irqdbg_pre_mie");
    dump_act_named_u64("[IRQDBG]", "irqdbg_pre_mip");
    dump_act_named_u64("[IRQDBG]", "irqdbg_pre_mstatus");
    dump_act_named_u64("[IRQDBG]", "irqdbg_pre_sip");
    dump_act_named_u64("[IRQDBG]", "irqdbg_pre_sie");
    dump_act_named_u64("[IRQDBG]", "irqdbg_pre_stvec");
    dump_act_named_u64("[IRQDBG]", "irqdbg_pre_s_trap_count");
    dump_act_named_u64("[IRQDBG]", "irqret_s_count");
    dump_act_named_u64("[IRQDBG]", "irqret_s_scause");
    dump_act_named_u64("[IRQDBG]", "irqret_s_sepc");
    dump_act_named_u64("[IRQDBG]", "irqret_s_stvec");
    dump_act_named_u64("[IRQDBG]", "irqret_s_sstatus");
    dump_act_named_u64("[IRQDBG]", "irqdbg_uart_ier");
    dump_act_named_u64("[IRQDBG]", "irqdbg_plic_pending");
    dump_act_named_u64("[IRQDBG]", "irqdbg_plic_m_enable");
    dump_act_named_u64("[IRQDBG]", "irqdbg_plic_s_enable");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_valid");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_mode");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_xepc");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_xcause");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_xtval");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_xstatus");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_mideleg");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_mip");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_mie");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_sip");
    dump_act_named_u64("[IRQDBG]", "irqdbg_first_sie");
    dump_act_named_u64("[IRQRET]", "rvmodel_boot_hartid");
    dump_act_named_u64("[IRQRET]", "irqret_mext_count");
    dump_act_named_u64("[IRQRET]", "irqret_mext_pre_sp");
    dump_act_named_u64("[IRQRET]", "irqret_mext_pre_mscratch");
    dump_act_named_u64("[IRQRET]", "irqret_mext_pre_saved_sp");
    dump_act_named_u64("[IRQRET]", "irqret_mext_pre_mepc");
    dump_act_named_u64("[IRQRET]", "irqret_mext_pre_mcause");
    dump_act_named_u64("[IRQRET]", "irqret_mext_post_sp");
    dump_act_named_u64("[IRQRET]", "irqret_mext_post_mscratch");
    dump_act_named_u64("[IRQRET]", "irqret_mext_post_saved_sp");
    dump_act_named_u64("[IRQRET]", "irqret_mext_post_mepc");
    dump_act_named_u64("[IRQRET]", "irqret_mext_post_mcause");
    dump_act_named_u64("[IRQRET]", "irqret_gotom_count");
    dump_act_named_u64("[IRQRET]", "irqret_gotom_entry_sp");
    dump_act_named_u64("[IRQRET]", "irqret_gotom_entry_mscratch");
    dump_act_named_u64("[IRQRET]", "irqret_gotom_entry_mepc");
    dump_act_named_u64("[IRQRET]", "irqret_gotom_entry_mstatus");
    dump_act_named_u64("[IRQRET]", "irqret_gotom_final_sp");
    dump_act_named_u64("[IRQRET]", "irqret_gotom_final_mscratch");
    dump_act_named_u64("[IRQRET]", "irqret_gotom_final_t2");
    dump_act_named_u64("[IRQRET]", "irqret_gotom_final_t4");
    dump_act_irq_section_trace();
    dump_act_trap_sig_window(act_x6_trap_sig_ptr);
    dump_act_handler_trace();
    dump_act_mmode_mtvec();
    dump_act_hart_csr_snapshot();

    (void)read_loaded_symbol_u64("saved_mepc", &saved_mepc, 0);
    (void)read_loaded_symbol_u64("saved_mcause", &saved_mcause, 0);
    (void)read_loaded_symbol_u64("saved_mtval", &saved_mtval, 0);
    (void)read_loaded_symbol_u64("saved_mstatus", &saved_mstatus, 0);
    (void)read_loaded_symbol_u64("failing_addr", &failing_addr, 0);
    uint64_t saved_mstatus_mpp = (saved_mstatus >> 11) & 3ULL;

    dump_act_signature_flow(act_x6_trap_sig_ptr, saved_mepc, saved_mcause, saved_mtval, saved_mstatus);

    dbg_puts("[ACTCSR] monitor_current_hartid=0x"); dbg_hex_u64(monitor_current_hartid);
    dbg_puts(" saved_mepc=0x"); dbg_hex_u64(saved_mepc);
    dbg_puts(" saved_mcause=0x"); dbg_hex_u64(saved_mcause);
    dbg_puts(" saved_mtval=0x"); dbg_hex_u64(saved_mtval);
    dbg_puts(" saved_mstatus=0x"); dbg_hex_u64(saved_mstatus);
    dbg_puts(" saved_mstatus_mpp=0x"); dbg_hex_u64(saved_mstatus_mpp);
    dbg_puts(" monitor_current_mtvec=0x"); dbg_hex_u64(monitor_current_mtvec);
    dbg_puts(" monitor_current_stvec=0x"); dbg_hex_u64(monitor_current_stvec);
    dbg_puts(" monitor_current_medeleg=0x"); dbg_hex_u64(monitor_current_medeleg);
    dbg_puts(" monitor_current_mideleg=0x"); dbg_hex_u64(monitor_current_mideleg);
    dbg_puts(" monitor_current_satp=0x"); dbg_hex_u64(monitor_current_satp);
    dbg_puts(" monitor_current_pmpcfg0=0x"); dbg_hex_u64(monitor_current_pmpcfg0);
    dbg_puts(" monitor_current_pmpaddr0=0x"); dbg_hex_u64(monitor_current_pmpaddr0);
    dbg_puts(" monitor_current_pmpaddr1=0x"); dbg_hex_u64(monitor_current_pmpaddr1);
    if ((saved_mcause & (1ULL << 63)) == 0 && saved_mcause < 64ULL) {
        dbg_puts(" monitor_medeleg_saved_mcause_bit=0x");
        dbg_hex_u64((monitor_current_medeleg >> saved_mcause) & 1ULL);
    } else {
        dbg_puts(" monitor_medeleg_saved_mcause_bit=unavailable");
    }
    dbg_nl();

    emit_first_trap_state("[TRAP_FIRST_SUMMARY]");
    dbg_puts("[FAILSCR_PROV] first_valid=");
    dbg_puts(g_first_trap.valid ? "1" : "0");
    dbg_puts(" saved_matches_first=");
    if (g_first_trap.valid &&
        saved_mepc == g_first_trap.mepc &&
        saved_mcause == g_first_trap.mcause &&
        saved_mtval == g_first_trap.mtval) {
        dbg_puts("1");
    } else {
        dbg_puts("0");
    }
    dbg_puts(" saved_mepc=0x"); dbg_hex_u64(saved_mepc);
    dbg_puts(" first_pc=0x"); dbg_hex_u64(g_first_trap.mepc);
    dbg_puts(" saved_mcause=0x"); dbg_hex_u64(saved_mcause);
    dbg_puts(" first_mcause=0x"); dbg_hex_u64(g_first_trap.mcause);
    dbg_puts(" saved_mtval=0x"); dbg_hex_u64(saved_mtval);
    dbg_puts(" first_tval=0x"); dbg_hex_u64(g_first_trap.mtval);
    dbg_puts(" failing_addr=0x"); dbg_hex_u64(failing_addr);
    dbg_puts(" source=");
    if (!g_first_trap.valid) {
        dbg_puts("unknown_no_first_trap");
    } else if (saved_mepc == g_first_trap.mepc &&
               saved_mcause == g_first_trap.mcause &&
               saved_mtval == g_first_trap.mtval) {
        dbg_puts("first_trap");
    } else if (failing_addr == g_first_trap.mepc) {
        dbg_puts("first_trap_pc_only");
    } else {
        dbg_puts("not_first_trap_or_mixed");
    }
    dbg_nl();

    if (saved_mepc != 0 && is_valid_ddr_addr(saved_mepc)) {
        dbg_puts("[ACTDBG] instructions_around_saved_mepc\n");
        dump_words32(saved_mepc);
        dump_halfwords16(saved_mepc);
    } else {
        dbg_puts("[ACTDBG] instructions_around_saved_mepc=unavailable\n");
    }
}

int load_elf_blob(const uint8_t *blob, size_t blob_size, uint64_t *entry_out)
{
    if (blob_size < sizeof(Elf64_Ehdr)) return -1;

    const Elf64_Ehdr *eh = (const Elf64_Ehdr *)(const void *)blob;
    if (eh->e_ident[0] != 0x7f || eh->e_ident[1] != 'E' || eh->e_ident[2] != 'L' || eh->e_ident[3] != 'F') return -2;
    if (eh->e_ident[4] != ELFCLASS64 || eh->e_ident[5] != ELFDATA2LSB) return -3;
    if (eh->e_machine != EM_RISCV) return -4;
    if (eh->e_phentsize != sizeof(Elf64_Phdr)) return -5;

    uint64_t ph_end = eh->e_phoff + ((uint64_t)eh->e_phnum * (uint64_t)eh->e_phentsize);
    if (eh->e_phoff > blob_size || ph_end > blob_size || ph_end < eh->e_phoff) return -6;

    g_runner_image.loaded_blob = blob;
    g_runner_image.loaded_blob_size = blob_size;
    g_runner_image.tohost_addr = 0;
    g_runner_image.loaded_region_start = 0;
    g_runner_image.loaded_region_end = 0;
    g_runner_image.load_segment_count = 0;
    if (find_symbol_by_name(blob, blob_size, eh, "tohost", (uint64_t *)&g_runner_image.tohost_addr) != 0) {
        g_runner_image.tohost_addr = FIXED_TOHOST_ADDR;
    }
    find_signature_range(blob, blob_size, eh, (uint64_t *)&g_runner_image.sig_begin, (uint64_t *)&g_runner_image.sig_end);
    find_failure_scratch_range(blob, blob_size, eh, (uint64_t *)&g_runner_image.fail_begin, (uint64_t *)&g_runner_image.fail_end);

    const Elf64_Phdr *ph = (const Elf64_Phdr *)(const void *)(blob + eh->e_phoff);
    for (uint16_t i = 0; i < eh->e_phnum; i++) {
        if (ph[i].p_type != PT_LOAD) continue;
        if (ph[i].p_filesz == 0 && ph[i].p_memsz == 0) continue;

        uint64_t seg_end = ph[i].p_offset + ph[i].p_filesz;
        if (ph[i].p_offset > blob_size || seg_end > blob_size || seg_end < ph[i].p_offset) return -7;
        if (ph[i].p_memsz < ph[i].p_filesz) return -8;

        uint64_t dst_addr = (ph[i].p_paddr ? ph[i].p_paddr : ph[i].p_vaddr);
        uint64_t dst_end;
        if (dst_addr == 0) return -9;
        if (!is_valid_payload_addr(dst_addr)) return -10;
        if (ph[i].p_memsz > UINT64_MAX - dst_addr) return -11;
        dst_end = dst_addr + ph[i].p_memsz;
        if (ph[i].p_memsz > 0 && !is_valid_payload_addr(dst_end - 1)) return -11;
        if (ranges_overlap_u64(dst_addr, dst_end,
                               (uint64_t)(uintptr_t)__text_start,
                               (uint64_t)(uintptr_t)__stack_top)) return -13;
        if (ranges_overlap_u64(dst_addr, dst_end,
                               UART_ELF_BUFFER_ADDR,
                               UART_ELF_BUFFER_ADDR + UART_ELF_MAX_BYTES)) return -14;
        if (g_runner_image.load_segment_count >= MAX_RUNNER_LOAD_SEGMENTS) return -12;

        if (g_runner_image.loaded_region_start == 0 || dst_addr < g_runner_image.loaded_region_start) {
            g_runner_image.loaded_region_start = dst_addr;
        }
        if (dst_end > g_runner_image.loaded_region_end) {
            g_runner_image.loaded_region_end = dst_end;
        }
        g_runner_image.load_segments[g_runner_image.load_segment_count].start = dst_addr;
        g_runner_image.load_segments[g_runner_image.load_segment_count].end = dst_end;
        g_runner_image.load_segments[g_runner_image.load_segment_count].flags = ph[i].p_flags;
        g_runner_image.load_segment_count++;

        void *dst = (void *)(uintptr_t)dst_addr;
        const void *src = (const void *)(blob + ph[i].p_offset);

        memcpy_local(dst, src, (size_t)ph[i].p_filesz);
        if (ph[i].p_memsz > ph[i].p_filesz) {
            memset_local((uint8_t *)dst + ph[i].p_filesz, 0, (size_t)(ph[i].p_memsz - ph[i].p_filesz));
        }
    }

    if (!is_valid_payload_addr(eh->e_entry)) return -15;

    if (g_runner_image.tohost_addr && is_valid_payload_addr(g_runner_image.tohost_addr)) {
        g_runner_image.tohost_ptr = (volatile uint64_t *)(uintptr_t)g_runner_image.tohost_addr;
    } else {
        g_runner_image.tohost_ptr = 0;
    }

    sync_icache();
    *entry_out = eh->e_entry;

    asm volatile ("fence rw, rw" ::: "memory");
    asm volatile ("fence.i" ::: "memory");
    return 0;
}
