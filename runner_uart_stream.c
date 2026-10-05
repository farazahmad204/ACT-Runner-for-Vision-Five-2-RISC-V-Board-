// SPDX-License-Identifier: BSD-3-Clause
// Optional one-ELF UART transport. Disabled unless RUNNER_UART_STREAM=1.

#include "runner_shared.h"

#if RUNNER_UART_STREAM

_Static_assert(UART_ELF_BUFFER_ADDR >= BOARD_RAM_BASE,
               "UART receive buffer starts below board RAM");
_Static_assert(UART_ELF_BUFFER_ADDR < BOARD_RAM_LIMIT,
               "UART receive buffer starts beyond board RAM");
_Static_assert(UART_ELF_MAX_BYTES <= BOARD_RAM_LIMIT - UART_ELF_BUFFER_ADDR,
               "UART receive buffer exceeds board RAM");
_Static_assert(BOARD_DDR_EXCLUDE_BASE >= BOARD_DDR_EXCLUDE_LIMIT ||
               UART_ELF_BUFFER_ADDR + UART_ELF_MAX_BYTES <= BOARD_DDR_EXCLUDE_BASE ||
               UART_ELF_BUFFER_ADDR >= BOARD_DDR_EXCLUDE_LIMIT,
               "UART receive buffer overlaps excluded DDR");

static char g_uart_stream_name[UART_STREAM_NAME_BYTES + 1u];
static volatile uint32_t g_uart_stream_done_emitted;

void uart_stream_emit_done(const char *name, const char *status, uint64_t tohost)
{
    if (g_uart_stream_done_emitted) return;
    g_uart_stream_done_emitted = 1u;
    asm volatile ("fence rw, rw" ::: "memory");

    uart_puts("[UART_STREAM] DONE name=");
    uart_puts(name);
    uart_puts(" status=");
    uart_puts(status);
    uart_puts(" tohost=");
    uart_put_hex(tohost);
    uart_puts("\n");
}

#if BOARD_UART_RX_DIAG
/* Diagnostic-only RX accounting. Nothing is printed while host bytes may be
   arriving (that could overflow the RX FIFO); counters are reported after
   each phase. Heartbeats are printed only while no byte has arrived. Waits
   are bounded by poll counts as well, in case mtime does not advance. */
#define UART_RX_DIAG_SAMPLE_BYTES    16u
#define UART_RX_DIAG_HEARTBEAT_POLLS (1ULL << 24)
#define UART_RX_DIAG_MAX_HEARTBEATS  24u
#define UART_RX_DIAG_SPIN_LIMIT      2000000ULL

typedef struct {
    uint64_t polls;
    uint64_t bytes;
    uint64_t start_mtime;
    uint64_t first_byte_mtime;
    uint64_t last_byte_mtime;
    uint32_t lsr_or;
    uint32_t heartbeats;
    uint8_t sample[UART_RX_DIAG_SAMPLE_BYTES];
} UartRxDiag;

static UartRxDiag g_rx_diag;

static void rx_diag_reset(void)
{
    memset_local(&g_rx_diag, 0, sizeof(g_rx_diag));
    g_rx_diag.start_mtime = *mtime_ptr();
}

static void rx_diag_put_hex8(uint8_t value)
{
    const char *digits = "0123456789abcdef";
    uart_putc(digits[value >> 4]);
    uart_putc(digits[value & 0xfu]);
}

static void rx_diag_dump_regs(const char *tag)
{
    static const struct { const char *name; uint32_t offset; } regs[] = {
        { "ier", UART_IER }, { "iir", UART_IIR }, { "lcr", UART_LCR },
        { "mcr", UART_MCR }, { "lsr", UART_LSR }, { "msr", UART_MSR },
        { "scr", UART_SCR }, { "usr", UART_DW_USR }, { "rfl", UART_DW_RFL },
    };
    uint32_t values[sizeof(regs) / sizeof(regs[0])];

    /* Sample every register before printing, since printing polls LSR. */
    for (uint32_t i = 0; i < sizeof(regs) / sizeof(regs[0]); i++) {
        values[i] = uart_reg_read(UART_BASE + regs[i].offset);
    }
    uart_log_lock();
    uart_puts("[DIAG] regs tag=");
    uart_puts(tag);
    for (uint32_t i = 0; i < sizeof(regs) / sizeof(regs[0]); i++) {
        uart_puts(" ");
        uart_puts(regs[i].name);
        uart_puts("=");
        rx_diag_put_hex8((uint8_t)(values[i] >> 8));
        rx_diag_put_hex8((uint8_t)values[i]);
    }
    uart_puts("\n");
    uart_log_unlock();
}

static int rx_diag_spin_until(uint32_t lsr_bit)
{
    for (uint64_t i = 0; i < UART_RX_DIAG_SPIN_LIMIT; i++) {
        if (uart_reg_read(UART_BASE + UART_LSR) & lsr_bit) return 0;
        cpu_relax();
    }
    return -1;
}

/* Internal 8250 loopback: proves whether this UART's receiver and our RBR
   reads work, independent of the external RX wire and of other agents. */
static void rx_diag_loopback(void)
{
    const uint32_t count = 8u;
    uint32_t mcr;
    uint32_t drained = 0;
    uint32_t got = 0;
    uint32_t match = 0;
    uint8_t seen[8] = {0};

    uart_log_lock();
    (void)rx_diag_spin_until(UART_LSR_TEMT);
    while ((uart_reg_read(UART_BASE + UART_LSR) & UART_LSR_DR) && drained < 4096u) {
        (void)uart_reg_read(UART_BASE + UART_RBR);
        drained++;
    }
    mcr = uart_reg_read(UART_BASE + UART_MCR);
    uart_reg_write(UART_BASE + UART_MCR, mcr | UART_MCR_LOOP);
    asm volatile ("fence iorw, iorw" ::: "memory");
    for (uint32_t i = 0; i < count; i++) {
        uint8_t expected = (uint8_t)(0xa0u + i);
        if (rx_diag_spin_until(UART_LSR_THRE) != 0) break;
        uart_reg_write(UART_BASE + UART_THR, expected);
        if (rx_diag_spin_until(UART_LSR_DR) != 0) continue;
        seen[got] = (uint8_t)uart_reg_read(UART_BASE + UART_RBR);
        if (seen[got] == expected) match++;
        got++;
    }
    (void)rx_diag_spin_until(UART_LSR_TEMT);
    uart_reg_write(UART_BASE + UART_MCR, mcr);
    asm volatile ("fence iorw, iorw" ::: "memory");

    uart_puts("[DIAG] loopback sent=");
    uart_put_dec_u64(count);
    uart_puts(" got=");
    uart_put_dec_u64(got);
    uart_puts(" match=");
    uart_put_dec_u64(match);
    uart_puts(" drained_before=");
    uart_put_dec_u64(drained);
    uart_puts(" mcr_before=");
    rx_diag_put_hex8((uint8_t)mcr);
    uart_puts(" seen=");
    for (uint32_t i = 0; i < got; i++) rx_diag_put_hex8(seen[i]);
    uart_puts("\n");
    uart_log_unlock();
}

static void rx_diag_heartbeat(void)
{
    g_rx_diag.heartbeats++;
    uart_log_lock();
    uart_puts("[DIAG] hb=");
    uart_put_dec_u64(g_rx_diag.heartbeats);
    uart_puts(" mtime=");
    uart_put_hex(*mtime_ptr());
    uart_puts(" polls=");
    uart_put_dec_u64(g_rx_diag.polls);
    uart_puts(" bytes=");
    uart_put_dec_u64(g_rx_diag.bytes);
    uart_puts(" lsr_or=");
    rx_diag_put_hex8((uint8_t)g_rx_diag.lsr_or);
    uart_puts("\n");
    uart_log_unlock();
}

static void rx_diag_report(const char *phase, int rc)
{
    uint64_t sampled = g_rx_diag.bytes < UART_RX_DIAG_SAMPLE_BYTES ?
                       g_rx_diag.bytes : UART_RX_DIAG_SAMPLE_BYTES;

    uart_log_lock();
    uart_puts("[DIAG] rx phase=");
    uart_puts(phase);
    uart_puts(" rc=");
    uart_put_hex((uint64_t)(int64_t)rc);
    uart_puts(" polls=");
    uart_put_dec_u64(g_rx_diag.polls);
    uart_puts(" bytes=");
    uart_put_dec_u64(g_rx_diag.bytes);
    uart_puts(" lsr_or=");
    rx_diag_put_hex8((uint8_t)g_rx_diag.lsr_or);
    uart_puts(" heartbeats=");
    uart_put_dec_u64(g_rx_diag.heartbeats);
    uart_puts("\n[DIAG] rx mtime start=");
    uart_put_hex(g_rx_diag.start_mtime);
    uart_puts(" first=");
    uart_put_hex(g_rx_diag.first_byte_mtime);
    uart_puts(" last=");
    uart_put_hex(g_rx_diag.last_byte_mtime);
    uart_puts(" now=");
    uart_put_hex(*mtime_ptr());
    uart_puts("\n[DIAG] rx sample=");
    for (uint64_t i = 0; i < sampled; i++) rx_diag_put_hex8(g_rx_diag.sample[i]);
    uart_puts("\n");
    uart_log_unlock();
    rx_diag_dump_regs(phase);
}

static int uart_getc_timeout(uint8_t *out)
{
    uint64_t deadline = *mtime_ptr() + RUNNER_UART_RX_TIMEOUT_TICKS;
    uint32_t lsr;

    while (((lsr = uart_reg_read(UART_BASE + UART_LSR)) & UART_LSR_DR) == 0u) {
        g_rx_diag.polls++;
        g_rx_diag.lsr_or |= lsr;
        if (g_rx_diag.bytes == 0u &&
            (g_rx_diag.polls % UART_RX_DIAG_HEARTBEAT_POLLS) == 0u) {
            if (g_rx_diag.heartbeats >= UART_RX_DIAG_MAX_HEARTBEATS) return -1;
            rx_diag_heartbeat();
        }
        if ((int64_t)(*mtime_ptr() - deadline) >= 0) return -1;
        cpu_relax();
    }
    g_rx_diag.lsr_or |= lsr;
    *out = (uint8_t)uart_reg_read(UART_BASE + UART_RBR);
    if (g_rx_diag.bytes < UART_RX_DIAG_SAMPLE_BYTES) {
        g_rx_diag.sample[g_rx_diag.bytes] = *out;
    }
    if (g_rx_diag.bytes == 0u) g_rx_diag.first_byte_mtime = *mtime_ptr();
    g_rx_diag.last_byte_mtime = *mtime_ptr();
    g_rx_diag.bytes++;
    return 0;
}
#else
static int uart_getc_timeout(uint8_t *out)
{
    uint64_t deadline = *mtime_ptr() + RUNNER_UART_RX_TIMEOUT_TICKS;
    while ((uart_reg_read(UART_BASE + UART_LSR) & UART_LSR_DR) == 0u) {
        if ((int64_t)(*mtime_ptr() - deadline) >= 0) return -1;
        cpu_relax();
    }
    *out = (uint8_t)uart_reg_read(UART_BASE + UART_RBR);
    return 0;
}
#endif

static int uart_receive_exact(uint8_t *dst, size_t size)
{
    for (size_t i = 0; i < size; i++) {
        if (uart_getc_timeout(&dst[i]) != 0) return -1;
    }
    return 0;
}

static uint32_t crc32_update_byte(uint32_t crc, uint8_t byte)
{
    crc ^= byte;
    for (uint32_t bit = 0; bit < 8u; bit++) {
        uint32_t mask = 0u - (crc & 1u);
        crc = (crc >> 1) ^ (0xedb88320u & mask);
    }
    return crc;
}

static int receive_header(UartStreamHeader *header)
{
    if (uart_receive_exact((uint8_t *)(void *)header, sizeof(*header)) != 0) {
        uart_puts("[UART_STREAM] ERROR reason=rx_timeout phase=header\n");
        return -5;
    }

    if (header->magic != UART_STREAM_MAGIC) {
        uart_puts("[UART_STREAM] ERROR reason=bad_magic value=");
        uart_put_hex(header->magic);
        uart_puts("\n");
        return -1;
    }
    if (header->version != UART_STREAM_VERSION) {
        uart_puts("[UART_STREAM] ERROR reason=bad_version value=");
        uart_put_hex(header->version);
        uart_puts("\n");
        return -2;
    }
    if (header->elf_size < sizeof(Elf64_Ehdr) || header->elf_size > UART_ELF_MAX_BYTES) {
        uart_puts("[UART_STREAM] ERROR reason=bad_size value=");
        uart_put_hex(header->elf_size);
        uart_puts(" max=");
        uart_put_hex(UART_ELF_MAX_BYTES);
        uart_puts("\n");
        return -3;
    }
    if (header->name_len == 0u || header->name_len > UART_STREAM_NAME_BYTES) {
        uart_puts("[UART_STREAM] ERROR reason=bad_name_len value=");
        uart_put_hex(header->name_len);
        uart_puts("\n");
        return -4;
    }

    for (uint32_t i = 0; i < header->name_len; i++) {
        char c = header->name[i];
        g_uart_stream_name[i] = (c >= 0x20 && c <= 0x7e) ? c : '_';
    }
    g_uart_stream_name[header->name_len] = '\0';
    return 0;
}

int run_uart_stream_once(uint64_t *total, uint64_t *pass, uint64_t *fail)
{
    UartStreamHeader header;
    uint8_t *dst = (uint8_t *)(uintptr_t)UART_ELF_BUFFER_ADDR;
    uint32_t crc = 0xffffffffu;
    int rc;
    TestResult tr;

    g_uart_stream_done_emitted = 0u;

#if BOARD_UART_RX_DIAG
    rx_diag_dump_regs("pre_ready");
    rx_diag_loopback();
    rx_diag_dump_regs("post_loopback");
    uart_puts("[DIAG] mtime_at_ready=");
    uart_put_hex(*mtime_ptr());
    uart_puts(" rx_timeout_ticks=");
    uart_put_dec_u64(RUNNER_UART_RX_TIMEOUT_TICKS);
    uart_puts("\n");
#endif

    uart_puts("[UART_STREAM] READY version=");
    uart_put_dec_u64(UART_STREAM_VERSION);
    uart_puts(" header_bytes=");
    uart_put_dec_u64(sizeof(UartStreamHeader));
    uart_puts(" max_elf_bytes=");
    uart_put_dec_u64(UART_ELF_MAX_BYTES);
    uart_puts(" buffer=");
    uart_put_hex(UART_ELF_BUFFER_ADDR);
    uart_puts(" board=");
    uart_puts(RUNNER_PLATFORM_NAME);
    uart_puts(" runner_build=");
    uart_puts(RUNNER_BUILD_ID);
    uart_puts("\n");

#if BOARD_UART_RX_DIAG
    rx_diag_reset();
#endif
    rc = receive_header(&header);
#if BOARD_UART_RX_DIAG
    /* Printed after the 88-byte header phase; the host waits for HEADER_OK. */
    rx_diag_report("header", rc);
#endif
    if (rc != 0) return rc;

    uart_puts("[UART_STREAM] HEADER_OK name=");
    uart_puts(g_uart_stream_name);
    uart_puts(" size=");
    uart_put_dec_u64(header.elf_size);
    uart_puts(" crc32=");
    uart_put_hex(header.crc32);
    uart_puts("\n");

#if BOARD_UART_RX_DIAG
    rx_diag_reset();
#endif
    for (uint64_t i = 0; i < header.elf_size; i++) {
        uint8_t byte;
        if (uart_getc_timeout(&byte) != 0) {
            uart_puts("[UART_STREAM] ERROR reason=rx_timeout phase=payload offset=");
            uart_put_dec_u64(i);
            uart_puts("\n");
#if BOARD_UART_RX_DIAG
            rx_diag_report("payload", -6);
#endif
            return -6;
        }
        dst[i] = byte;
        crc = crc32_update_byte(crc, byte);
    }
    crc ^= 0xffffffffu;
    asm volatile ("fence rw, rw" ::: "memory");
#if BOARD_UART_RX_DIAG
    rx_diag_report("payload", crc == header.crc32 ? 0 : -5);
#endif

    if (crc != header.crc32) {
        uart_puts("[UART_STREAM] ERROR reason=crc_mismatch expected=");
        uart_put_hex(header.crc32);
        uart_puts(" actual=");
        uart_put_hex(crc);
        uart_puts("\n");
        return -5;
    }

    uart_puts("[UART_STREAM] RX_OK name=");
    uart_puts(g_uart_stream_name);
    uart_puts(" size=");
    uart_put_dec_u64(header.elf_size);
    uart_puts(" crc32=");
    uart_put_hex(crc);
    uart_puts("\n");

    (void)run_one_blob(g_uart_stream_name, dst, (size_t)header.elf_size, &tr);
    *total += 1u;
    if (case_is_pass(&tr)) *pass += 1u;
    else *fail += 1u;

    if (tr.status == CASE_STATUS_PASS) {
        uart_stream_emit_done(g_uart_stream_name, "PASS", tr.tohost);
    } else if (tr.status == CASE_STATUS_TIMEOUT) {
        uart_stream_emit_done(g_uart_stream_name, "TIMEOUT", tr.tohost);
    } else if (tr.status == CASE_STATUS_FAIL) {
        uart_stream_emit_done(g_uart_stream_name, "FAIL", tr.tohost);
    } else {
        uart_stream_emit_done(g_uart_stream_name, "ERROR", tr.tohost);
    }
    return 0;
}

#else

int run_uart_stream_once(uint64_t *total, uint64_t *pass, uint64_t *fail)
{
    (void)total;
    (void)pass;
    (void)fail;
    return -1;
}

void uart_stream_emit_done(const char *name, const char *status, uint64_t tohost)
{
    (void)name;
    (void)status;
    (void)tohost;
}

#endif
