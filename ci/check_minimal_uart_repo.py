#!/usr/bin/env python3
"""Fail when legacy runner profiles or SD workflows return to this branch."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_JENKINSFILES = {
    "Jenkinsfile.uart-sanity",
    "Jenkinsfile.uart-weekly",
    "Jenkinsfile.bpif3-uart-sanity",
    "Jenkinsfile.bpif3-uart-weekly",
    "Jenkinsfile.megrez-uart-sanity",
    "Jenkinsfile.uart-single-elf",
    "Jenkinsfile.uart-triage",
}
FORBIDDEN_PATHS = {
    "runner_sd.c",
    "runner_sd_sdhci_k1.c",
    "runner_sd_sdhci_k1.h",
    "vf2_act_flash.sh",
    "bpif3_act_flash.sh",
    "write_pack_to_sd_tail.sh",
    "build_act_pack.py",
    "cert_harness/tools/build_profile.sh",
    "cert_harness/tools/run_profile.sh",
}
REQUIRED_TRIAGE_PATHS = {
    "ci/triage/ci_triage.py",
    "ci/triage/publish_triage.py",
    "ci/triage/run_ci_triage.py",
}


def main() -> int:
    errors: list[str] = []
    profiles = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "cert_harness/profiles").glob("*.env")
    )
    if profiles != ["cert_harness/profiles/UART_M_MODE.env"]:
        errors.append(f"expected only UART_M_MODE.env, found: {profiles}")

    jenkinsfiles = {path.name for path in ROOT.glob("Jenkinsfile*")}
    if jenkinsfiles != ALLOWED_JENKINSFILES:
        errors.append(
            "unexpected Jenkinsfiles: "
            + str(sorted(jenkinsfiles.symmetric_difference(ALLOWED_JENKINSFILES)))
        )

    present = sorted(path for path in FORBIDDEN_PATHS if (ROOT / path).exists())
    if present:
        errors.append(f"legacy SD/profile files are present: {present}")

    missing_triage = sorted(path for path in REQUIRED_TRIAGE_PATHS if not (ROOT / path).is_file())
    if missing_triage:
        errors.append(f"required CI triage files are missing: {missing_triage}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Minimal UART repository contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
