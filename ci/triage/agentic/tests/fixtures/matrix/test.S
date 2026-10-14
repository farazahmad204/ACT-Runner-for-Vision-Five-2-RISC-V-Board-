// MExt/MExt_mul-00.S  (triage-agent matrix fixture)
##### START_TEST_CONFIG #####
# REQUIRED_EXTENSIONS: ['I', 'M']
# MARCH: rv${XLEN}im
##### END_TEST_CONFIG #####
#define BOOT_TO_MMODE
// cp_mul_basic
.globl _start
_start:
  li a0, 6
  li a1, 7
MExt_cg_cp_mul_basic_b0:
  mul a2, a0, a1
  li a3, 42
  bne a2, a3, fail
  ret
fail:
  j fail
