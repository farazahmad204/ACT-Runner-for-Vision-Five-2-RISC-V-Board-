# RVA23 Profile Rules

Use these rules when deciding whether a failure is certification/profile,
runner/environment, ACT/Sail, or DUT.

- A runner can provide an RVA23-style execution environment, but it cannot make
  unsupported hardware extensions pass.
- Confirm the ACT profile config matches the VF2/U74 hardware capabilities
  before blaming the DUT.
- Confirm whether the test was intended to run in M, S, or U mode. A trap
  expected in S/U but executed in M-mode is a runner/profile setup issue.
- Confirm delegation policy, `medeleg`, `mideleg`, `satp`, PMP, counter access,
  and FS state match the test expectation.
- Unsupported or WARL-limited CSRs should be handled as profile/config issues
  unless the profile mandates writable behavior and the target claims support.
- For S/U tests, confirm the runner actually entered the lower-privilege path
  and established the intended page tables/PMP map.
- Certification ownership should separate profile mismatch from DUT bug. A
  legal implementation choice can still require ACT/Sail/profile adjustment.
