#ifndef RUNNER_SHARED_H
#define RUNNER_SHARED_H

#include <stdint.h>
#include <stddef.h>

#ifndef BOARD_UART_BASE
#define BOARD_UART_BASE 0x10000000UL
#endif
#ifndef BOARD_UART_SIZE
#define BOARD_UART_SIZE 0x1000UL
#endif
#ifndef BOARD_UART_REG_IO_WIDTH
#define BOARD_UART_REG_IO_WIDTH 1
#endif
#ifndef BOARD_UART_8250_INIT_ENABLE
#define BOARD_UART_8250_INIT_ENABLE 0
#endif
#ifndef BOARD_UART_INPUT_CLOCK_HZ
#define BOARD_UART_INPUT_CLOCK_HZ 0u
#endif
/* Diagnostic-only: record UART RX activity silently and report it later. */
#ifndef BOARD_UART_RX_DIAG
#define BOARD_UART_RX_DIAG 0
#endif
/* Boards without a validated PLIC/UART interrupt mapping set this to 0. */
#ifndef BOARD_EXTERNAL_IRQ_CLEANUP_ENABLE
#define BOARD_EXTERNAL_IRQ_CLEANUP_ENABLE 1
#endif
#ifndef BOARD_UART_BAUD
#define BOARD_UART_BAUD 115200u
#endif
#ifndef BOARD_PLIC_BASE
#define BOARD_PLIC_BASE 0x0c000000UL
#endif
#ifndef BOARD_UART_PLIC_SOURCE
#define BOARD_UART_PLIC_SOURCE 32u
#endif
#ifndef BOARD_RUNNER_M_PLIC_CONTEXT
#define BOARD_RUNNER_M_PLIC_CONTEXT 1u
#endif
#ifndef BOARD_RUNNER_S_PLIC_CONTEXT
#define BOARD_RUNNER_S_PLIC_CONTEXT 2u
#endif
#ifndef BOARD_CLINT_MSIP_BASE
#define BOARD_CLINT_MSIP_BASE 0x02000000UL
#endif
#ifndef BOARD_CLINT_MTIMECMP_BASE
#define BOARD_CLINT_MTIMECMP_BASE 0x02004000UL
#endif
#ifndef BOARD_CLINT_MTIME_ADDR
#define BOARD_CLINT_MTIME_ADDR 0x0200bff8UL
#endif
#ifndef BOARD_RUNNER_HART_ID
#define BOARD_RUNNER_HART_ID 1u
#endif
#ifndef BOARD_MONITOR_HART_ID
#define BOARD_MONITOR_HART_ID 2u
#endif
#ifndef BOARD_FIXED_TOHOST_ADDR
#define BOARD_FIXED_TOHOST_ADDR 0x41008000ULL
#endif
#ifndef BOARD_RAM_BASE
#define BOARD_RAM_BASE 0x40000000ULL
#endif
#ifndef BOARD_RAM_LIMIT
#define BOARD_RAM_LIMIT 0x100000000ULL
#endif
#ifndef BOARD_PAYLOAD_BASE
#define BOARD_PAYLOAD_BASE BOARD_RAM_BASE
#endif
#ifndef BOARD_PAYLOAD_LIMIT
#define BOARD_PAYLOAD_LIMIT BOARD_RAM_LIMIT
#endif
#ifndef BOARD_DDR_EXCLUDE_BASE
#define BOARD_DDR_EXCLUDE_BASE 0ULL
#endif
#ifndef BOARD_DDR_EXCLUDE_LIMIT
#define BOARD_DDR_EXCLUDE_LIMIT 0ULL
#endif
#ifndef BOARD_UART_ELF_BUFFER_ADDR
#define BOARD_UART_ELF_BUFFER_ADDR 0x88000000ULL
#endif
#ifndef BOARD_UART_ELF_MAX_BYTES
#define BOARD_UART_ELF_MAX_BYTES (128ULL * 1024ULL * 1024ULL)
#endif
#ifndef BOARD_TEST_STACK_BYTES
#define BOARD_TEST_STACK_BYTES (256u * 1024u)
#endif
#ifndef BOARD_TRAP_STACK_BYTES
#define BOARD_TRAP_STACK_BYTES 1024u
#endif
#ifndef BOARD_WDT_ENABLE
#define BOARD_WDT_ENABLE 1
#endif
#ifndef BOARD_WDT_BASE
#define BOARD_WDT_BASE 0x13070000UL
#endif
#ifndef BOARD_WDT_LOAD
#define BOARD_WDT_LOAD 0x000
#endif
#ifndef BOARD_WDT_CTRL
#define BOARD_WDT_CTRL 0x008
#endif
#ifndef BOARD_WDT_LOCK
#define BOARD_WDT_LOCK 0xc00
#endif
#ifndef BOARD_WDT_UNLOCK_KEY
#define BOARD_WDT_UNLOCK_KEY 0x1acce551u
#endif
#ifndef BOARD_K1_HART_WAKEUP_ENABLE
#define BOARD_K1_HART_WAKEUP_ENABLE 0
#endif
#define RUNNER_PLATFORM_GENERIC    0
#define RUNNER_PLATFORM_VF2_JH7110 1
#define RUNNER_PLATFORM_BPIF3_K1   2
#define RUNNER_PLATFORM_MILKV_MEGREZ_EIC7700X 3
#ifndef BOARD_PLATFORM_ID
#define BOARD_PLATFORM_ID RUNNER_PLATFORM_GENERIC
#endif
#if BOARD_PLATFORM_ID == RUNNER_PLATFORM_VF2_JH7110
#define RUNNER_PLATFORM_NAME "vf2_jh7110"
#elif BOARD_PLATFORM_ID == RUNNER_PLATFORM_BPIF3_K1
#define RUNNER_PLATFORM_NAME "bpif3_k1"
#elif BOARD_PLATFORM_ID == RUNNER_PLATFORM_MILKV_MEGREZ_EIC7700X
#define RUNNER_PLATFORM_NAME "milkv_megrez_eic7700x"
#else
#define RUNNER_PLATFORM_NAME "generic_riscv"
#endif
#ifndef RUNNER_UART_STREAM
#define RUNNER_UART_STREAM 0
#endif
#ifndef RUNNER_BUILD_ID
#define RUNNER_BUILD_ID "unknown"
#endif
#ifndef RUNNER_UART_RX_TIMEOUT_TICKS
#define RUNNER_UART_RX_TIMEOUT_TICKS 200000000ULL
#endif

#define UART_BASE       BOARD_UART_BASE
#define UART_SIZE       BOARD_UART_SIZE

#define UART_THR        0x00
#define UART_RBR        0x00
#define UART_DLL        0x00
#define UART_IER        0x04
#define UART_DLM        0x04
#define UART_FCR        0x08
#define UART_LCR        0x0c
#define UART_MCR        0x10
#define UART_LSR        0x14
#define UART_SCR        0x1c
#define UART_IIR        0x08
#define UART_MSR        0x18
#define UART_DW_USR     0x7c /* Synopsys DesignWare status register */
#define UART_DW_RFL     0x84 /* DesignWare RX FIFO level, if implemented */
#define UART_MCR_LOOP   (1u << 4)
#define UART_LSR_TEMT   (1u << 6)
#define UART_LSR_DR     (1u << 0)
#define UART_LSR_THRE   (1u << 5)
#define UART_IER_THRI   (1u << 1)

#define PLIC_BASE       BOARD_PLIC_BASE
#define UART_PLIC_SOURCE BOARD_UART_PLIC_SOURCE
#define RUNNER_M_PLIC_CONTEXT BOARD_RUNNER_M_PLIC_CONTEXT
#define RUNNER_S_PLIC_CONTEXT BOARD_RUNNER_S_PLIC_CONTEXT

#define CLINT_MSIP_BASE BOARD_CLINT_MSIP_BASE
#define CLINT_MTIMECMP_BASE BOARD_CLINT_MTIMECMP_BASE
#define CLINT_MTIME_ADDR    BOARD_CLINT_MTIME_ADDR
#define RUNNER_HART_ID  BOARD_RUNNER_HART_ID
#define MONITOR_HART_ID BOARD_MONITOR_HART_ID
#define FIXED_TOHOST_ADDR BOARD_FIXED_TOHOST_ADDR

#define PT_LOAD         1u
#define PF_X            1u
#define PF_W            2u
#define PF_R            4u
#define EM_RISCV        243u
#define EI_NIDENT       16u
#define ELFCLASS64      2u
#define ELFDATA2LSB     1u
#define SHT_SYMTAB      2u
#define SHT_DYNSYM      11u

#define UART_ELF_BUFFER_ADDR BOARD_UART_ELF_BUFFER_ADDR
#define UART_ELF_MAX_BYTES BOARD_UART_ELF_MAX_BYTES
#define TEST_STACK_BYTES BOARD_TEST_STACK_BYTES
#define TRAP_STACK_BYTES BOARD_TRAP_STACK_BYTES

#define UART_STREAM_MAGIC        0x31534655u /* little-endian "UFS1" */
#define UART_STREAM_VERSION      1u
#define UART_STREAM_NAME_BYTES   64u

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t version;
    uint64_t elf_size;
    uint32_t crc32;
    uint32_t name_len;
    char name[UART_STREAM_NAME_BYTES];
} UartStreamHeader;

_Static_assert(sizeof(UartStreamHeader) == 88u,
               "UART stream header layout changed");

#define RUNNER_WDT_ENABLE BOARD_WDT_ENABLE
#define RUNNER_WDT_BASE BOARD_WDT_BASE
#define RUNNER_WDT_LOAD BOARD_WDT_LOAD
#define RUNNER_WDT_CTRL BOARD_WDT_CTRL
#define RUNNER_WDT_LOCK BOARD_WDT_LOCK
#define RUNNER_WDT_UNLOCK_KEY BOARD_WDT_UNLOCK_KEY
#define RUNNER_K1_HART_WAKEUP_ENABLE BOARD_K1_HART_WAKEUP_ENABLE

#define MCAUSE_INTERRUPT_BIT (1ULL << 63)
#define MCAUSE_SSI      (MCAUSE_INTERRUPT_BIT | 1ULL)
#define MCAUSE_MSI      (MCAUSE_INTERRUPT_BIT | 3ULL)
#define MCAUSE_STI      (MCAUSE_INTERRUPT_BIT | 5ULL)
#define MCAUSE_MTI      (MCAUSE_INTERRUPT_BIT | 7ULL)
#define MCAUSE_SEI      (MCAUSE_INTERRUPT_BIT | 9ULL)
#define MCAUSE_LCOFI    (MCAUSE_INTERRUPT_BIT | 13ULL)
#define MCAUSE_ECALL_U  8ULL
#define MCAUSE_ECALL_S  9ULL
#define MCAUSE_ECALL_M  11ULL
#define MCAUSE_INST_MISALIGNED 0ULL
#define MCAUSE_INST_ACCESS_FAULT 1ULL
#define MCAUSE_ILLEGAL_INSN 2ULL
#define MCAUSE_BREAKPOINT 3ULL
#define MCAUSE_LOAD_MISALIGNED 4ULL
#define MCAUSE_LOAD_ACCESS_FAULT 5ULL
#define MCAUSE_STORE_MISALIGNED 6ULL
#define MCAUSE_STORE_ACCESS_FAULT 7ULL
#define MCAUSE_INST_PAGE_FAULT 12ULL
#define MCAUSE_LOAD_PAGE_FAULT 13ULL
#define MCAUSE_STORE_PAGE_FAULT 15ULL
#define TOHOST_TIMEOUT  0xdead000000000001ULL
#define TOHOST_TRAP     0xdead000000000002ULL
#ifndef RUNNER_TEST_TIMEOUT_TICKS
#define RUNNER_TEST_TIMEOUT_TICKS 50000000ULL
#endif
#ifndef RUNNER_RESET_DELAY_TICKS
#define RUNNER_RESET_DELAY_TICKS 20000000ULL
#endif
#ifndef RUNNER_TIMEOUT_FAST_RESET
#define RUNNER_TIMEOUT_FAST_RESET 0
#endif
#ifndef RUNNER_DISABLE_TEST_TIMEOUT
#define RUNNER_DISABLE_TEST_TIMEOUT 1
#endif
#define TEST_TIMEOUT_TICKS RUNNER_TEST_TIMEOUT_TICKS
#define RESET_DELAY_TICKS RUNNER_RESET_DELAY_TICKS
#define SATP_MODE_SV39  8ULL
#define MIE_MSIE        (1ULL << 3)
#define MIE_MTIE        (1ULL << 7)
#define SIE_SSIE        (1ULL << 1)
#define SIE_STIE        (1ULL << 5)
#define SIE_SEIE        (1ULL << 9)
#define SIE_LCOFIE      (1ULL << 13)
#define MCOUNTEREN_CY   (1ULL << 0)
#define MCOUNTEREN_TM   (1ULL << 1)
#define MCOUNTEREN_IR   (1ULL << 2)
#define COUNTEREN_BASE  (MCOUNTEREN_CY | MCOUNTEREN_TM | MCOUNTEREN_IR)
#define COUNTEREN_ALL   0xffffffffULL
#define SV39_L0_TABLE_COUNT 64u
#define MAX_RUNNER_LOAD_SEGMENTS 16u
#define RUNNER_TEST_SBI_EXT 0x56524632ULL
#define RUNNER_TEST_OP_ECALL_TEST 0ULL
#define RUNNER_TEST_OP_GOTO_M_MODE 1ULL
#define RUNNER_TEST_OP_GOTO_S_MODE 2ULL
#define RUNNER_TEST_OP_ACCESS_CSR 5ULL
#define RUNNER_TEST_OP_RETURN_TO_M 0x8000000000000000ULL
#define TSBI_ECALL_TEST 0x00000073ULL
#define TSBI_GOTO_MMODE 0xf0001001ULL
#define TSBI_GOTO_SMODE 0xf0001002ULL
#define TSBI_GOTO_UMODE 0xf0001003ULL
#define TSBI_GOTO_VSMODE 0xf0001004ULL
#define TSBI_GOTO_VUMODE 0xf0001005ULL

#ifndef RUNNER_ENABLE_TSBI
#define RUNNER_ENABLE_TSBI 1
#endif

#ifndef RUNNER_ENABLE_PRIVATE_SBI
#define RUNNER_ENABLE_PRIVATE_SBI 1
#endif

#ifndef RUNNER_ENABLE_LOWER_MODES
#define RUNNER_ENABLE_LOWER_MODES 1
#endif

#ifndef RUNNER_VERBOSE_FLOW
#define RUNNER_VERBOSE_FLOW 0
#endif

#ifndef RUNNER_SMODE_CSR_POLICY
#define RUNNER_SMODE_CSR_POLICY RUNNER_SMODE_CSR_POLICY_STRICT
#endif

extern uint8_t __text_start[];
extern uint8_t __text_end[];
extern uint8_t __rodata_start[];
extern uint8_t __rodata_end[];
extern uint8_t __data_start[];
extern uint8_t __data_end[];
extern uint8_t __stack_bottom[];
extern uint8_t __stack_top[];
extern uint8_t __bss_end[];
extern volatile uint64_t g_boot_sync;

typedef struct RunnerLoadSegment {
    uint64_t start;
    uint64_t end;
    uint32_t flags;
} RunnerLoadSegment;

typedef struct RunnerImageState {
    volatile uint64_t *tohost_ptr;
    volatile uint64_t tohost_addr;
    volatile uint64_t sig_begin;
    volatile uint64_t sig_end;
    volatile uint64_t fail_begin;
    volatile uint64_t fail_end;
    const uint8_t *loaded_blob;
    size_t loaded_blob_size;
    uint64_t loaded_region_start;
    uint64_t loaded_region_end;
    uint32_t load_segment_count;
    RunnerLoadSegment load_segments[MAX_RUNNER_LOAD_SEGMENTS];
} RunnerImageState;

typedef struct RunnerExecState {
    volatile uint64_t runner_active;
    volatile uint64_t monitor_seen_tohost;
    volatile uint64_t test_done;
    volatile uint64_t test_tohost_value;
    volatile uint64_t test_resume_pc;
    volatile uint64_t runner_saved_sp;
    volatile uint64_t runner_saved_gp;
    volatile uint64_t reset_armed;
    volatile uint64_t test_deadline_mtime;
    volatile uint64_t reset_request_mtime;
    volatile uint64_t sig_dump_in_progress;
    volatile uint64_t case_report_ready;
    volatile uint64_t monitor_report_done;
    volatile uint64_t fault_reported;
    volatile const char *active_case_name;
    volatile uint64_t hart1_csr_snapshot_valid;
    volatile uint64_t hart1_csr_snapshot_reason;
    volatile uint64_t hart1_mhartid;
    volatile uint64_t hart1_mstatus;
    volatile uint64_t hart1_mepc;
    volatile uint64_t hart1_mcause;
    volatile uint64_t hart1_mtval;
    volatile uint64_t hart1_medeleg;
    volatile uint64_t hart1_mideleg;
    volatile uint64_t hart1_satp;
    volatile uint64_t hart1_pmpcfg0;
    volatile uint64_t hart1_pmpaddr[8];
} RunnerExecState;

typedef struct LowerModeState {
    volatile uint64_t requested_exec_mode;
    volatile uint64_t active_exec_mode;
    volatile uint64_t smode_trap_bridge_to_m;
    volatile uint64_t lower_entry_pc;
    volatile uint64_t lower_user_sp;
    volatile uint64_t uflow_marker;
} LowerModeState;

extern RunnerImageState g_runner_image;
extern RunnerExecState g_runner_exec;
extern LowerModeState g_lower_state;
extern struct MachineEnvConfig g_machine_env_config;
extern uint64_t g_sv39_root_page_table[512];
extern uint64_t g_sv39_l1_page_tables[4][512];
extern uint64_t g_sv39_l0_page_tables[SV39_L0_TABLE_COUNT][512];
extern uint8_t g_test_stack[TEST_STACK_BYTES];
extern uint8_t g_trap_stack[TRAP_STACK_BYTES];
typedef struct {
    uint64_t ra;
    uint64_t gp;
    uint64_t tp;
    uint64_t t0;
    uint64_t t1;
    uint64_t t2;
    uint64_t s0;
    uint64_t s1;
    uint64_t a0;
    uint64_t a1;
    uint64_t a2;
    uint64_t a3;
    uint64_t a4;
    uint64_t a5;
    uint64_t a6;
    uint64_t a7;
    uint64_t s2;
    uint64_t s3;
    uint64_t s4;
    uint64_t s5;
    uint64_t s6;
    uint64_t s7;
    uint64_t s8;
    uint64_t s9;
    uint64_t s10;
    uint64_t s11;
    uint64_t t3;
    uint64_t t4;
    uint64_t t5;
    uint64_t t6;
    uint64_t sp;
} TrapFrame;

typedef struct LastTrapState {
    TrapFrame frame;
    uint64_t mcause;
    uint64_t mepc;
    uint64_t mtval;
    uint64_t mstatus;
    uint64_t mode;
    uint8_t valid;
} LastTrapState;

typedef struct FirstTrapState {
    TrapFrame frame;
    uint64_t seq;
    uint64_t mcause;
    uint64_t mepc;
    uint64_t mtval;
    uint64_t status;
    uint64_t mode;
    uint64_t inst16;
    uint64_t inst32;
    uint64_t mtvec;
    uint64_t stvec;
    uint64_t medeleg;
    uint64_t mideleg;
    uint64_t satp;
    uint8_t valid;
} FirstTrapState;

typedef struct RunnerSbiState {
    volatile uint64_t pending_kind;
    TrapFrame *lower_tf;
    uint64_t lower_sepc;
    uint32_t lower_insn;
    uint64_t lower_src_value;
    uint64_t lower_saved_a0;
    uint64_t lower_saved_a1;
    uint64_t lower_saved_a2;
    uint64_t lower_saved_a7;
    volatile uint64_t compat_forward_count;
    volatile uint64_t direct_request_count;
    volatile uint64_t last_error;
} RunnerSbiState;

extern LastTrapState g_last_trap;
extern FirstTrapState g_first_trap;
extern RunnerSbiState g_runner_sbi;

static inline void mmio_write8(uintptr_t addr, uint8_t v) { *(volatile uint8_t*)addr = v; }
static inline uint8_t mmio_read8(uintptr_t addr) { return *(volatile uint8_t*)addr; }
static inline void mmio_write16(uintptr_t addr, uint16_t v) { *(volatile uint16_t*)addr = v; }
static inline uint16_t mmio_read16(uintptr_t addr) { return *(volatile uint16_t*)addr; }
static inline void mmio_write32(uintptr_t addr, uint32_t v) { *(volatile uint32_t*)addr = v; }
static inline uint32_t mmio_read32(uintptr_t addr) { return *(volatile uint32_t*)addr; }

static inline uint32_t uart_reg_read(uintptr_t addr)
{
#if BOARD_UART_REG_IO_WIDTH == 4
    return mmio_read32(addr);
#elif BOARD_UART_REG_IO_WIDTH == 2
    return mmio_read16(addr);
#elif BOARD_UART_REG_IO_WIDTH == 1
    return mmio_read8(addr);
#else
#error "Unsupported BOARD_UART_REG_IO_WIDTH"
#endif
}

static inline void uart_reg_write(uintptr_t addr, uint32_t value)
{
#if BOARD_UART_REG_IO_WIDTH == 4
    mmio_write32(addr, value);
#elif BOARD_UART_REG_IO_WIDTH == 2
    mmio_write16(addr, (uint16_t)value);
#elif BOARD_UART_REG_IO_WIDTH == 1
    mmio_write8(addr, (uint8_t)value);
#else
#error "Unsupported BOARD_UART_REG_IO_WIDTH"
#endif
}

static inline uint64_t read_csr_mhartid(void) { uint64_t v; __asm__ volatile ("csrr %0, mhartid" : "=r"(v)); return v; }
static inline void write_csr_mtvec(uint64_t v) { __asm__ volatile ("csrw mtvec, %0" :: "r"(v)); }
static inline void write_csr_mscratch(uint64_t v) { __asm__ volatile ("csrw mscratch, %0" :: "r"(v)); }
static inline void write_csr_mepc(uint64_t v) { __asm__ volatile ("csrw mepc, %0" :: "r"(v)); }
static inline void write_csr_sepc(uint64_t v) { __asm__ volatile ("csrw sepc, %0" :: "r"(v)); }
static inline void write_csr_stvec(uint64_t v) { __asm__ volatile ("csrw stvec, %0" :: "r"(v)); }
static inline void write_csr_sscratch(uint64_t v) { __asm__ volatile ("csrw sscratch, %0" :: "r"(v)); }
static inline void write_csr_satp(uint64_t v) { __asm__ volatile ("csrw satp, %0" :: "r"(v)); }
static inline void write_csr_medeleg(uint64_t v) { __asm__ volatile ("csrw medeleg, %0" :: "r"(v)); }
static inline void write_csr_mideleg(uint64_t v) { __asm__ volatile ("csrw mideleg, %0" :: "r"(v)); }
static inline void write_csr_mcounteren(uint64_t v) { __asm__ volatile ("csrw mcounteren, %0" :: "r"(v)); }
static inline void write_csr_mcountinhibit(uint64_t v) { __asm__ volatile ("csrw mcountinhibit, %0" :: "r"(v)); }
static inline void write_csr_menvcfg(uint64_t v) { __asm__ volatile ("csrw menvcfg, %0" :: "r"(v)); }
static inline void write_csr_scounteren(uint64_t v) { __asm__ volatile ("csrw scounteren, %0" :: "r"(v)); }
static inline void write_csr_senvcfg(uint64_t v) { __asm__ volatile ("csrw senvcfg, %0" :: "r"(v)); }
static inline void write_csr_sie(uint64_t v) { __asm__ volatile ("csrw sie, %0" :: "r"(v)); }
static inline void write_sstatus(uint64_t x){ asm volatile("csrw sstatus, %0"::"r"(x)); }
static inline void write_csr_pmpaddr0(uint64_t v) { __asm__ volatile ("csrw pmpaddr0, %0" :: "r"(v)); }
static inline void write_csr_pmpaddr1(uint64_t v) { __asm__ volatile ("csrw pmpaddr1, %0" :: "r"(v)); }
static inline void write_csr_pmpaddr2(uint64_t v) { __asm__ volatile ("csrw pmpaddr2, %0" :: "r"(v)); }
static inline void write_csr_pmpaddr3(uint64_t v) { __asm__ volatile ("csrw pmpaddr3, %0" :: "r"(v)); }
static inline void write_csr_pmpaddr4(uint64_t v) { __asm__ volatile ("csrw pmpaddr4, %0" :: "r"(v)); }
static inline void write_csr_pmpaddr5(uint64_t v) { __asm__ volatile ("csrw pmpaddr5, %0" :: "r"(v)); }
static inline void write_csr_pmpaddr6(uint64_t v) { __asm__ volatile ("csrw pmpaddr6, %0" :: "r"(v)); }
static inline void write_csr_pmpaddr7(uint64_t v) { __asm__ volatile ("csrw pmpaddr7, %0" :: "r"(v)); }
static inline void write_csr_pmpcfg0(uint64_t v) { __asm__ volatile ("csrw pmpcfg0, %0" :: "r"(v)); }
static inline uint64_t read_csr_pmpaddr0(void) { uint64_t v; __asm__ volatile ("csrr %0, pmpaddr0" : "=r"(v)); return v; }
static inline uint64_t read_csr_pmpaddr1(void) { uint64_t v; __asm__ volatile ("csrr %0, pmpaddr1" : "=r"(v)); return v; }
static inline uint64_t read_csr_pmpaddr2(void) { uint64_t v; __asm__ volatile ("csrr %0, pmpaddr2" : "=r"(v)); return v; }
static inline uint64_t read_csr_pmpaddr3(void) { uint64_t v; __asm__ volatile ("csrr %0, pmpaddr3" : "=r"(v)); return v; }
static inline uint64_t read_csr_pmpaddr4(void) { uint64_t v; __asm__ volatile ("csrr %0, pmpaddr4" : "=r"(v)); return v; }
static inline uint64_t read_csr_pmpaddr5(void) { uint64_t v; __asm__ volatile ("csrr %0, pmpaddr5" : "=r"(v)); return v; }
static inline uint64_t read_csr_pmpaddr6(void) { uint64_t v; __asm__ volatile ("csrr %0, pmpaddr6" : "=r"(v)); return v; }
static inline uint64_t read_csr_pmpaddr7(void) { uint64_t v; __asm__ volatile ("csrr %0, pmpaddr7" : "=r"(v)); return v; }
static inline uint64_t read_csr_pmpcfg0(void) { uint64_t v; __asm__ volatile ("csrr %0, pmpcfg0" : "=r"(v)); return v; }
static inline void wfi(void) { __asm__ volatile ("wfi"); }
static inline void cpu_relax(void) { __asm__ volatile ("nop" ::: "memory"); }
static inline void sfence_vma_all(void) { __asm__ volatile ("sfence.vma x0, x0" ::: "memory"); }
static inline uint64_t read_mstatus(void){ uint64_t x; asm volatile("csrr %0, mstatus":"=r"(x)); return x; }
static inline uint64_t read_sstatus(void){ uint64_t x; asm volatile("csrr %0, sstatus":"=r"(x)); return x; }
static inline uint64_t read_csr_mtvec(void){ uint64_t x; asm volatile("csrr %0, mtvec":"=r"(x)); return x; }
static inline uint64_t read_csr_stvec(void){ uint64_t x; asm volatile("csrr %0, stvec":"=r"(x)); return x; }
static inline uint64_t read_csr_medeleg(void){ uint64_t x; asm volatile("csrr %0, medeleg":"=r"(x)); return x; }
static inline uint64_t read_csr_mideleg(void){ uint64_t x; asm volatile("csrr %0, mideleg":"=r"(x)); return x; }
static inline uint64_t read_csr_satp(void){ uint64_t x; asm volatile("csrr %0, satp":"=r"(x)); return x; }
static inline uint64_t read_misa(void){ uint64_t x; asm volatile("csrr %0, misa":"=r"(x)); return x; }
static inline uint64_t read_mvendorid(void){ uint64_t x; asm volatile("csrr %0, mvendorid":"=r"(x)); return x; }
static inline uint64_t read_marchid(void){ uint64_t x; asm volatile("csrr %0, marchid":"=r"(x)); return x; }
static inline uint64_t read_mimpid(void){ uint64_t x; asm volatile("csrr %0, mimpid":"=r"(x)); return x; }
static inline void write_mstatus(uint64_t x){ asm volatile("csrw mstatus, %0"::"r"(x)); }
static inline uint64_t read_mie(void){ uint64_t x; asm volatile("csrr %0, mie":"=r"(x)); return x; }
static inline uint64_t read_csr_mip(void){ uint64_t x; asm volatile("csrr %0, mip":"=r"(x)); return x; }
static inline uint64_t read_csr_sie(void){ uint64_t x; asm volatile("csrr %0, sie":"=r"(x)); return x; }
static inline uint64_t read_csr_sip(void){ uint64_t x; asm volatile("csrr %0, sip":"=r"(x)); return x; }
static inline void write_mie(uint64_t x){ asm volatile("csrw mie, %0"::"r"(x)); }
static inline uint64_t read_fflags(void){ uint64_t x; asm volatile("csrr %0, fflags":"=r"(x)); return x; }
static inline uint64_t read_frm(void){ uint64_t x; asm volatile("csrr %0, frm":"=r"(x)); return x; }
static inline uint64_t read_fcsr(void){ uint64_t x; asm volatile("csrr %0, fcsr":"=r"(x)); return x; }
static inline void write_fcsr(uint64_t x){ asm volatile("csrw fcsr, %0"::"r"(x)); }
static inline volatile uint32_t *msip_ptr(uint32_t hartid)
{
    return (volatile uint32_t *)(uintptr_t)(CLINT_MSIP_BASE + (hartid * 4u));
}
static inline volatile uint64_t *mtimecmp_ptr(uint32_t hartid)
{
    return (volatile uint64_t *)(uintptr_t)(CLINT_MTIMECMP_BASE + ((uint64_t)hartid * 8ULL));
}
static inline volatile uint64_t *mtime_ptr(void)
{
    return (volatile uint64_t *)(uintptr_t)CLINT_MTIME_ADDR;
}
static inline void sync_icache(void) { asm volatile ("fence.i" ::: "memory"); }

typedef struct {
    uint8_t  e_ident[EI_NIDENT];
    uint16_t e_type;
    uint16_t e_machine;
    uint32_t e_version;
    uint64_t e_entry;
    uint64_t e_phoff;
    uint64_t e_shoff;
    uint32_t e_flags;
    uint16_t e_ehsize;
    uint16_t e_phentsize;
    uint16_t e_phnum;
    uint16_t e_shentsize;
    uint16_t e_shnum;
    uint16_t e_shstrndx;
} Elf64_Ehdr;

typedef struct {
    uint32_t p_type;
    uint32_t p_flags;
    uint64_t p_offset;
    uint64_t p_vaddr;
    uint64_t p_paddr;
    uint64_t p_filesz;
    uint64_t p_memsz;
    uint64_t p_align;
} Elf64_Phdr;

typedef struct {
    uint32_t sh_name;
    uint32_t sh_type;
    uint64_t sh_flags;
    uint64_t sh_addr;
    uint64_t sh_offset;
    uint64_t sh_size;
    uint32_t sh_link;
    uint32_t sh_info;
    uint64_t sh_addralign;
    uint64_t sh_entsize;
} Elf64_Shdr;

typedef struct {
    uint32_t st_name;
    uint8_t  st_info;
    uint8_t  st_other;
    uint16_t st_shndx;
    uint64_t st_value;
    uint64_t st_size;
} Elf64_Sym;

typedef struct {
    const char *name;
    uint64_t value;
    uint64_t size;
} SymbolInfo;

typedef struct {
    const char *name;
    int rc;
    uint64_t tohost;
    uint64_t tohost_addr;
    uint64_t sig_begin;
    uint64_t sig_end;
    int status;
} TestResult;

typedef struct MachineEnvConfig {
    uint64_t mode;
    uint64_t delegation_policy;
    uint64_t medeleg;
    uint64_t mideleg;
    uint64_t mie;
    uint64_t mcounteren;
    uint64_t mcountinhibit;
    uint64_t menvcfg;
    uint64_t scounteren;
    uint64_t senvcfg;
    uint64_t satp;
    uint64_t stvec;
    uint64_t sscratch;
    uint64_t sie;
    uint64_t pmpcfg0;
    uint64_t pmpaddr[8];
} MachineEnvConfig;

enum {
    EXEC_MODE_M = 0,
    EXEC_MODE_S = 1,
    EXEC_MODE_U = 2,
};

enum {
    DELEGATION_POLICY_NONE = 0,
    DELEGATION_POLICY_S_BASE = 1,
};

enum {
    RUNNER_SMODE_BRIDGE_NONE = 0,
    RUNNER_SMODE_BRIDGE_RETURN_TO_M = 1,
    RUNNER_SMODE_BRIDGE_REQUEST = 2,
};

enum {
    RUNNER_SMODE_CSR_POLICY_STRICT = 0,
    RUNNER_SMODE_CSR_POLICY_COMPAT = 1,
};

enum {
    RUNNER_SBI_PENDING_NONE = 0,
    RUNNER_SBI_PENDING_COMPAT_CSR = 1,
    RUNNER_SBI_PENDING_FORWARDED_ECALL = 2,
    RUNNER_SBI_PENDING_TSBI = 3,
};

enum {
    CASE_STATUS_PASS = 0,
    CASE_STATUS_FAIL = 1,
    CASE_STATUS_TIMEOUT = 2,
    CASE_STATUS_ERROR = 3,
};

int is_valid_ddr_addr(uint64_t addr);
int is_valid_ddr_range(uint64_t begin, uint64_t end);
int is_valid_payload_addr(uint64_t addr);
int is_valid_payload_range(uint64_t begin, uint64_t end);

void *memcpy_local(void *dst, const void *src, size_t n);
void *memset_local(void *dst, int c, size_t n);
void *memcpy(void *dst, const void *src, size_t n);
void *memset(void *dst, int c, size_t n);

void uart_putc(char c);
void platform_uart_init(void);
void uart_puts(const char* s);
void uart_put_hex(uint64_t x);
void uart_put_dec_u64(uint64_t v);
void uart_log_lock(void);
void uart_log_unlock(void);

void dbg_putc(char c);
void dbg_puts(const char *s);
void dbg_hex_u64(uint64_t v);
void dbg_hex_u32(uint32_t v);
void dbg_nl(void);
void dbg_put_u32_dec(uint32_t v);
void dbg_put_reg_name(uint32_t reg);
int32_t sign_extend_32(uint32_t v, uint32_t bits);
void dbg_put_imm_i32(int32_t v);
void decode_riscv_insn(uint64_t pc, uint32_t insn);
void dump_words32(uint64_t addr);
void uart_put_token_str(const char *s);
void emit_reg_line4(const char *n0, uint64_t v0,
                    const char *n1, uint64_t v1,
                    const char *n2, uint64_t v2,
                    const char *n3, uint64_t v3);
void emit_reg_line3(const char *n0, uint64_t v0,
                    const char *n1, uint64_t v1,
                    const char *n2, uint64_t v2);

void trigger_watchdog_reset(void);
void k1_cci_init_own_cluster(void);
void k1_wakeup_monitor_hart(void);
void monitor_irq_enable(void);
void monitor_irq_disable(void);
void platform_external_irq_cleanup(void);
void enable_fpu_set_fs_only(void);
void enable_fpu_try_clear_fcsr(void);
const char *exec_mode_name(uint64_t mode);
const char *payload_kind_name(void);
const char *delegation_policy_name(uint64_t policy);
const char *smode_csr_policy_name(uint64_t policy);
uint64_t select_default_exec_mode(void);
uint64_t select_default_delegation_policy(uint64_t mode);
void build_machine_env_config(uint64_t mode, MachineEnvConfig *cfg);
void apply_machine_env_config(const MachineEnvConfig *cfg);
void emit_machine_env_config(const MachineEnvConfig *cfg);
void platform_prepare_exec_env(uint64_t mode);
void reset_lower_mode_state(void);
void run_test_in_requested_mode(uint64_t entry, uintptr_t test_sp);
void runner_reset_sbi_state(void);
void runner_prepare_smode_return_bridge(TrapFrame *tf);
int runner_prepare_smode_request_bridge(uint64_t sepc, TrapFrame *tf);
int runner_prepare_smode_illegal_csr_bridge(uint64_t scause, uint64_t sepc, TrapFrame *tf);
int runner_handle_test_sbi(TrapFrame *tf, uint64_t trap_pc, uint64_t trap_mode, uint64_t *next_pc_out);
int runner_prepare_smode_tsbi_bridge(uint64_t sepc, TrapFrame *tf);
int runner_handle_tsbi(TrapFrame *tf, uint64_t trap_pc, uint64_t trap_mode, uint64_t *next_pc_out);

int streq(const char* a, const char* b);
int find_symbol_for_addr(const uint8_t *blob, size_t blob_size, uint64_t addr, SymbolInfo *out);
void capture_last_trap_in_mode(const TrapFrame *tf, uint64_t mcause, uint64_t mepc,
                               uint64_t mtval, uint64_t mstatus, uint64_t trap_mode);
void capture_last_trap(const TrapFrame *tf, uint64_t mcause, uint64_t mepc,
                       uint64_t mtval, uint64_t mstatus);
void reset_first_trap_state(void);
void capture_first_trap_in_mode(const TrapFrame *tf, uint64_t mcause, uint64_t pc,
                                uint64_t tval, uint64_t status, uint64_t trap_mode);
void emit_first_trap_state(const char *tag);
const char *trap_reason_name(uint64_t mcause);
uint32_t read_insn_word(uint64_t pc, int *valid_out);
void find_signature_range(const uint8_t *blob, size_t blob_size, const Elf64_Ehdr *eh,
                          uint64_t *sig_begin_out, uint64_t *sig_end_out);
void find_failure_scratch_range(const uint8_t *blob, size_t blob_size, const Elf64_Ehdr *eh,
                                uint64_t *begin_out, uint64_t *end_out);
void dump_signature_region(uint64_t begin, uint64_t end);
void dump_failure_scratch_region(uint64_t begin, uint64_t end);
void dump_act_failure_context(void);
void dump_act_irq_timeout_context(void);
void dump_act_irq_section_trace(void);
int load_elf_blob(const uint8_t *blob, size_t blob_size, uint64_t *entry_out);
void emit_execution_context(const char *reason);
uint64_t get_case_exit_pc(void);
void emit_trap_failure_report(uint64_t mcause, uint64_t mepc);
void handle_fatal_test_trap(const TrapFrame *tf, uint64_t mcause, uint64_t mepc,
                            uint64_t mtval, uint64_t mstatus);
void request_deferred_reset(void);
int wait_for_monitor_report(void);
uint64_t trap_c(uint64_t mcause, uint64_t mepc, uint64_t mtval, uint64_t mstatus,
                TrapFrame *tf);
void trap_entry(void);
uint64_t s_trap_c(uint64_t scause, uint64_t sepc, uint64_t stval, uint64_t sstatus,
                  TrapFrame *tf);
void s_trap_entry(void);
void monitor_hart_loop(void);
const char *case_status_name(int status);
int case_is_pass(const TestResult *tr);
void emit_case_report(const TestResult *tr);
uint64_t run_loaded_entry(uint64_t entry);
int run_one_blob(const char *name, const uint8_t *blob, size_t blob_size, TestResult *out);
int run_uart_stream_once(uint64_t *total, uint64_t *pass, uint64_t *fail);
void uart_stream_emit_done(const char *name, const char *status, uint64_t tohost);

#endif
