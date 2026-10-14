#!/usr/bin/env python3
"""Parse ACT/RVCP UART case logs into structured JSON.

This script is intentionally deterministic: it extracts fields that are present
in the log and does not infer a root cause beyond a simple category label.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


HEX_RE = r"0x[0-9a-fA-F]+"


def _hex_to_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _category(data: dict[str, Any]) -> str:
    status = data.get("status")
    diagnostic = (data.get("failure") or data.get("test_info") or "").lower()
    field = (data.get("mismatching_field") or "").lower()

    if status == "TIMEOUT" or data.get("tohost_value") == "0xdead000000000001":
        return "timeout_or_watchdog"
    if "trap signature pointer offset" in diagnostic:
        return "trap_count_or_signature_offset"
    if "trap signature" in diagnostic and "m-mode" in diagnostic:
        return "wrong_trap_mode_mmode"
    if "xcause" in field or "mcause" in diagnostic:
        return "mcause_mismatch"
    if "mtval" in field or "mtval" in diagnostic:
        return "mtval_mismatch"
    if data.get("bad_value") and data.get("expected_register_value"):
        return "register_value_mismatch"
    if "mismatch" in diagnostic:
        return "semantic_or_value_mismatch"
    if status == "PASS":
        return "pass"
    if status == "FAIL":
        return "other_fail"
    if status:
        return status.lower()
    return "unknown"


def parse_text(text: str, source: str = "") -> dict[str, Any]:
    data: dict[str, Any] = {"source": source}

    patterns = [
        ("case_name", rf"\[CASE\] START name=([^\s]+)"),
        ("test_file", r'RVCP-SUMMARY: TEST \w+ - Test File "([^"]+)"'),
        ("test_info", r'RVCP: Test Info: "([^"]*)"'),
        ("failure", r'RVCP: Failure: "([^"]*)"'),
        ("trap_handler_mode", r"RVCP: Trap handler mode:\s*([^\n\r]+)"),
        ("mismatching_field", r"RVCP: Mismatching field:\s*([^\n\r]+)"),
        ("expected_value", rf"RVCP: Expected value:\s*({HEX_RE})"),
        ("actual_value", rf"RVCP: Actual value:\s*({HEX_RE})"),
        ("expected_cause", r"RVCP: Expected cause:\s*([^\n\r]+)"),
        ("actual_cause", r"RVCP: Actual cause:\s*([^\n\r]+)"),
        ("mepc", rf"RVCP: MEPC:\s*({HEX_RE})"),
        ("xepc", rf"RVCP: XEPC:\s*({HEX_RE})"),
        ("xcause", rf"RVCP: XCAUSE:\s*({HEX_RE})"),
        ("xtval", rf"RVCP: XTVAL:\s*({HEX_RE})"),
        ("xstatus", rf"RVCP: XSTATUS:\s*({HEX_RE})"),
        ("mcause", rf"RVCP: MCAUSE:\s*({HEX_RE})"),
        ("mtval", rf"RVCP: MTVAL:\s*({HEX_RE})"),
        ("mstatus", rf"RVCP: MSTATUS:\s*({HEX_RE})"),
        ("mtvec", rf"RVCP: MTVEC:\s*({HEX_RE})"),
        ("stvec", rf"RVCP: STVEC:\s*({HEX_RE})"),
        ("medeleg", rf"RVCP: MEDELEG:\s*({HEX_RE})"),
        ("mideleg", rf"RVCP: MIDELEG:\s*({HEX_RE})"),
        ("mie", rf"RVCP: MIE:\s*({HEX_RE})"),
        ("mip", rf"RVCP: MIP:\s*({HEX_RE})"),
        ("satp", rf"RVCP: SATP:\s*({HEX_RE})"),
        ("fflags", rf"RVCP: FFLAGS:\s*({HEX_RE})"),
        ("frm", rf"RVCP: FRM:\s*({HEX_RE})"),
        ("fcsr", rf"RVCP: FCSR:\s*({HEX_RE})"),
        ("act_first_valid", rf"RVCP: ACT_FIRST_VALID:\s*({HEX_RE})"),
        ("act_first_mode", rf"RVCP: ACT_FIRST_MODE:\s*({HEX_RE})"),
        ("act_first_cause", rf"RVCP: ACT_FIRST_CAUSE:\s*({HEX_RE})"),
        ("act_first_epc", rf"RVCP: ACT_FIRST_EPC:\s*({HEX_RE})"),
        ("act_first_tval", rf"RVCP: ACT_FIRST_TVAL:\s*({HEX_RE})"),
        ("act_first_status", rf"RVCP: ACT_FIRST_STATUS:\s*({HEX_RE})"),
        ("act_first_ie", rf"RVCP: ACT_FIRST_IE:\s*({HEX_RE})"),
        ("act_first_ip", rf"RVCP: ACT_FIRST_IP:\s*({HEX_RE})"),
        ("act_first_tvec", rf"RVCP: ACT_FIRST_TVEC:\s*({HEX_RE})"),
        ("act_first_satp", rf"RVCP: ACT_FIRST_SATP:\s*({HEX_RE})"),
        ("instruction", rf"RVCP: Instruction:\s*({HEX_RE})"),
        ("approximate_address", rf"RVCP: Approximate address.*:\s*({HEX_RE})"),
        ("register", r"RVCP: Register:\s*([^\n\r]+)"),
        ("bad_value", rf"RVCP: Bad Value:\s*({HEX_RE})"),
        ("expected_register_value", rf"RVCP: Expected Value:\s*({HEX_RE})"),
        ("payload_kind", r"\[PAYLOAD\] kind=([^\s]+)"),
        ("payload_entry", rf"\[PAYLOAD\].*entry=({HEX_RE})"),
        ("payload_tohost", rf"\[PAYLOAD\].*tohost=({HEX_RE})"),
        ("load_segments", rf"\[PAYLOAD\].*load_segments=({HEX_RE})"),
        ("sig_begin", rf"\[ACT\] sig_begin=({HEX_RE})"),
        ("sig_end", rf"\[ACT\] sig_end=({HEX_RE})"),
        ("tohost_addr", rf"\[MON\] tohost addr=({HEX_RE})"),
        ("tohost_value", rf"\[MON\] tohost addr={HEX_RE} value=({HEX_RE})"),
        ("case_report_status", r"\[CASE\] REPORT name=.*?status=([A-Z]+)"),
        ("ctx_reason", r"\[CTX\] reason=([A-Z_]+)"),
        ("ctx_exit_mcause", rf"\[CTX\].*exit_mcause=({HEX_RE})"),
        ("ctx_exit_mepc", rf"\[CTX\].*exit_mepc=({HEX_RE})"),
        ("ctx_exit_mtval", rf"\[CTX\].*exit_mtval=({HEX_RE})"),
        ("ctx_exit_mstatus", rf"\[CTX\].*exit_mstatus=({HEX_RE})"),
        ("actcsr_saved_mepc", rf"\[ACTCSR\].*saved_mepc=({HEX_RE})"),
        ("actcsr_saved_mcause", rf"\[ACTCSR\].*saved_mcause=({HEX_RE})"),
        ("actcsr_saved_mtval", rf"\[ACTCSR\].*saved_mtval=({HEX_RE})"),
        ("actcsr_saved_mstatus", rf"\[ACTCSR\].*saved_mstatus=({HEX_RE})"),
        ("actcsr_monitor_medeleg", rf"\[ACTCSR\].*monitor_current_medeleg=({HEX_RE})"),
        ("actcsr_monitor_mideleg", rf"\[ACTCSR\].*monitor_current_mideleg=({HEX_RE})"),
        ("env_exec_mode", r"\[ENV\]\s+exec_mode=([^\s]+)"),
        ("env_deleg_policy", r"\[ENV\]\s+deleg_policy=([^\s]+)"),
        ("env_medeleg", rf"\[ENV\].*medeleg=({HEX_RE})"),
        ("env_mideleg", rf"\[ENV\]\s+mideleg=({HEX_RE})"),
        ("env_mie", rf"\[ENV\]\s+mie=({HEX_RE})"),
        ("env_mcounteren", rf"\[ENV\]\s+mcounteren=({HEX_RE})"),
        ("env_mcountinhibit", rf"\[ENV\]\s+mcountinhibit=({HEX_RE})"),
        ("env_menvcfg", rf"\[ENV\]\s+menvcfg=({HEX_RE})"),
        ("env_scounteren", rf"\[ENV\]\s+scounteren=({HEX_RE})"),
        ("env_senvcfg", rf"\[ENV\]\s+senvcfg=({HEX_RE})"),
        ("env_satp", rf"\[ENV\]\s+satp=({HEX_RE})"),
        ("env_stvec", rf"\[ENV\]\s+stvec=({HEX_RE})"),
        ("env_sscratch", rf"\[ENV\]\s+sscratch=({HEX_RE})"),
        ("env_sie", rf"\[ENV\]\s+sie=({HEX_RE})"),
        ("failscr_begin", rf"\[FAILSCR\] begin=({HEX_RE})"),
        ("failscr_end", rf"\[FAILSCR\] begin={HEX_RE} end=({HEX_RE})"),
    ]

    for key, pattern in patterns:
        match = re.search(pattern, text, flags=re.DOTALL if key == "case_report_status" else 0)
        if match:
            data[key] = match.group(1).strip()

    case_report_statuses = re.findall(r"\bstatus=([A-Z]+)\b", text)
    if case_report_statuses:
        data["case_report_statuses"] = case_report_statuses
        data["case_report_status"] = case_report_statuses[-1]

    tohost_values = re.findall(rf"\[MON\] tohost addr={HEX_RE} value=({HEX_RE})", text)
    if tohost_values:
        data["tohost_values"] = tohost_values
        data["tohost_value"] = tohost_values[-1]

    probe_markers = re.findall(r"\[I([0-9A-Za-z])\]", text)
    if probe_markers:
        data["probe_markers"] = probe_markers
        data["last_probe_marker"] = probe_markers[-1]

    timeout_reports = len(re.findall(r"\[RST\]\s+test timeout", text))
    if timeout_reports:
        data["timeout_reports"] = timeout_reports
    watchdog_resets = len(re.findall(r"\[HOST\]\s+hard cycle reason=watchdog_reset", text))
    if watchdog_resets:
        data["watchdog_resets"] = watchdog_resets
    boot_media_failures = len(re.findall(r"\[HOST\]\s+hard cycle reason=boot_media_failure", text))
    if boot_media_failures:
        data["boot_media_failures"] = boot_media_failures

    timeout_csr_match = re.search(
        rf"\[TIMEOUTCSR\]\s+source=([^\s]+)\s+"
        rf"mstatus=({HEX_RE})\s+sstatus=({HEX_RE})\s+"
        rf"mie=({HEX_RE})\s+mip=({HEX_RE})\s+sie=({HEX_RE})\s+sip=({HEX_RE})\s+"
        rf"mideleg=({HEX_RE})\s+medeleg=({HEX_RE})\s+mtvec=({HEX_RE})\s+stvec=({HEX_RE})\s+"
        rf"satp=({HEX_RE})\s+mtime=({HEX_RE})\s+deadline=({HEX_RE})",
        text,
    )
    if timeout_csr_match:
        data["timeout_csr"] = {
            "source": timeout_csr_match.group(1),
            "mstatus": timeout_csr_match.group(2),
            "sstatus": timeout_csr_match.group(3),
            "mie": timeout_csr_match.group(4),
            "mip": timeout_csr_match.group(5),
            "sie": timeout_csr_match.group(6),
            "sip": timeout_csr_match.group(7),
            "mideleg": timeout_csr_match.group(8),
            "medeleg": timeout_csr_match.group(9),
            "mtvec": timeout_csr_match.group(10),
            "stvec": timeout_csr_match.group(11),
            "satp": timeout_csr_match.group(12),
            "mtime": timeout_csr_match.group(13),
            "deadline": timeout_csr_match.group(14),
        }

    summary = re.search(r"RVCP-SUMMARY: TEST (PASSED|FAILED|SIGRUN|TIMEOUT)", text)
    if summary:
        word = summary.group(1)
        data["status"] = "PASS" if word == "PASSED" else "FAIL" if word == "FAILED" else word
    elif data.get("case_report_status"):
        data["status"] = data["case_report_status"]

    failq: list[dict[str, str]] = []
    for addr, value in re.findall(rf"\[FAILQ\]\s+({HEX_RE})\s+:\s+({HEX_RE})", text):
        failq.append({"addr": addr, "value": value})
    if failq:
        data["failq"] = failq

    sigq: list[str] = []
    sigq_entries: list[dict[str, str]] = []
    for addr, value in re.findall(rf"\[SIGQ\]\s+({HEX_RE})\s+:\s+({HEX_RE})", text):
        normalized = f"{int(value, 16):016x}"
        sigq.append(normalized)
        sigq_entries.append({"addr": addr, "value": f"0x{int(value, 16):016x}"})
    if sigq:
        data["sigq"] = sigq
        data["sigq_entries"] = sigq_entries
        data["sigq_count"] = len(sigq)

    trap_entries: list[dict[str, str]] = []
    trap_pattern = re.compile(
        rf"\[TRAP\]\s+mcause=({HEX_RE})\s+mepc=({HEX_RE})\s+mtval=({HEX_RE})\s+mstatus=({HEX_RE})"
    )
    for mcause, mepc, mtval, mstatus in trap_pattern.findall(text):
        trap_entries.append({"mcause": mcause, "mepc": mepc, "mtval": mtval, "mstatus": mstatus})
    if trap_entries:
        data["trap_entries"] = trap_entries
        data["trap_entry"] = trap_entries[0]

    first_trap_pattern = re.compile(
        rf"\[TRAP_FIRST(?:_SUMMARY)?\]\s+valid=1\s+seq=({HEX_RE})\s+mode=([^\s]+)\s+"
        rf"mcause=({HEX_RE})\s+pc=({HEX_RE})\s+tval=({HEX_RE})\s+status=({HEX_RE})\s+"
        rf"inst16=({HEX_RE})\s+inst32=({HEX_RE})\s+mtvec=({HEX_RE})\s+stvec=({HEX_RE})\s+"
        rf"medeleg=({HEX_RE})\s+mideleg=({HEX_RE})\s+satp=({HEX_RE})"
    )
    first_traps: list[dict[str, str]] = []
    for match in first_trap_pattern.findall(text):
        first_traps.append(
            {
                "seq": match[0],
                "mode": match[1],
                "mcause": match[2],
                "pc": match[3],
                "tval": match[4],
                "status": match[5],
                "inst16": match[6],
                "inst32": match[7],
                "mtvec": match[8],
                "stvec": match[9],
                "medeleg": match[10],
                "mideleg": match[11],
                "satp": match[12],
            }
        )
    if first_traps:
        data["first_traps"] = first_traps
        data["first_trap"] = first_traps[0]

    prov_pattern = re.compile(
        rf"\[FAILSCR_PROV\]\s+first_valid=([01])\s+saved_matches_first=([01])\s+"
        rf"saved_mepc=({HEX_RE})\s+first_pc=({HEX_RE})\s+"
        rf"saved_mcause=({HEX_RE})\s+first_mcause=({HEX_RE})\s+"
        rf"saved_mtval=({HEX_RE})\s+first_tval=({HEX_RE})\s+"
        rf"failing_addr=({HEX_RE})\s+source=([^\s]+)"
    )
    prov_match = prov_pattern.search(text)
    if prov_match:
        data["failscr_provenance"] = {
            "first_valid": prov_match.group(1),
            "saved_matches_first": prov_match.group(2),
            "saved_mepc": prov_match.group(3),
            "first_pc": prov_match.group(4),
            "saved_mcause": prov_match.group(5),
            "first_mcause": prov_match.group(6),
            "saved_mtval": prov_match.group(7),
            "first_tval": prov_match.group(8),
            "failing_addr": prov_match.group(9),
            "source": prov_match.group(10),
        }

    act_mtvec_pattern = re.compile(
        rf"\[ACTMTVEC\]\s+Mtramptbl_sv=({HEX_RE})\s+act_mmode_mtvec=({HEX_RE})\s+"
        rf"base=({HEX_RE})\s+mode=({HEX_RE})\s+symbol=([^\s]+)\s+saved_runner_mtvec=({HEX_RE})"
    )
    act_mtvec_match = act_mtvec_pattern.search(text)
    if act_mtvec_match:
        data["act_mtvec"] = {
            "mtramptbl_sv": act_mtvec_match.group(1),
            "act_mmode_mtvec": act_mtvec_match.group(2),
            "base": act_mtvec_match.group(3),
            "mode": act_mtvec_match.group(4),
            "symbol": act_mtvec_match.group(5),
            "saved_runner_mtvec": act_mtvec_match.group(6),
        }

    act_htrace_match = re.search(rf"\[ACTHTRACE\]\s+Mtramptbl_sv=({HEX_RE})\s+rvmodel_sv=({HEX_RE})", text)
    if act_htrace_match:
        data["act_htrace"] = {
            "mtramptbl_sv": act_htrace_match.group(1),
            "rvmodel_sv": act_htrace_match.group(2),
        }

    act_htrace_slots: list[dict[str, str]] = []
    for slot, addr, value in re.findall(rf"\[ACTHTRACEQ\]\s+slot(\d+)_addr=({HEX_RE})\s+value=({HEX_RE})", text):
        act_htrace_slots.append({"slot": slot, "addr": addr, "value": value})
    if act_htrace_slots:
        data["act_htrace_slots"] = act_htrace_slots

    env_keys = {
        key.removeprefix("env_"): value
        for key, value in data.items()
        if key.startswith("env_")
    }
    if env_keys:
        data["env"] = env_keys

    fallback_fields = {
        "mepc": ("actcsr_saved_mepc", "ctx_exit_mepc"),
        "mcause": ("actcsr_saved_mcause", "ctx_exit_mcause"),
        "mtval": ("actcsr_saved_mtval", "ctx_exit_mtval"),
        "mstatus": ("actcsr_saved_mstatus", "ctx_exit_mstatus"),
        "medeleg": ("actcsr_monitor_medeleg", "env_medeleg"),
        "mideleg": ("actcsr_monitor_mideleg", "env_mideleg"),
        "mie": ("env_mie",),
        "satp": ("env_satp",),
        "stvec": ("env_stvec",),
    }
    if "failscr_provenance" in data:
        fallback_fields["mepc"] = ("actcsr_saved_mepc", "failscr_provenance.saved_mepc", "ctx_exit_mepc")
        fallback_fields["mcause"] = ("actcsr_saved_mcause", "failscr_provenance.saved_mcause", "ctx_exit_mcause")
        fallback_fields["mtval"] = ("actcsr_saved_mtval", "failscr_provenance.saved_mtval", "ctx_exit_mtval")

    for target, sources in fallback_fields.items():
        if data.get(target):
            continue
        for source_key in sources:
            if "." in source_key:
                parent, child = source_key.split(".", 1)
                value = (data.get(parent) or {}).get(child)
            else:
                value = data.get(source_key)
            if value and (_hex_to_int(value) != 0 or target in {"medeleg", "mideleg"}):
                data[target] = value
                break

    fp_trap_entries: list[dict[str, str]] = []
    fp_pattern = re.compile(rf"\[TRAP\]\s+fflags=({HEX_RE})\s+frm=({HEX_RE})\s+fcsr=({HEX_RE})")
    for fflags, frm, fcsr in fp_pattern.findall(text):
        fp_trap_entries.append({"fflags": fflags, "frm": frm, "fcsr": fcsr})
    if fp_trap_entries:
        data["fp_trap_entries"] = fp_trap_entries
        data["fp_trap_entry"] = fp_trap_entries[0]

    data["category"] = _category(data)

    for key in (
        "expected_value",
        "actual_value",
        "mepc",
        "mcause",
        "mtval",
        "mstatus",
        "mtvec",
        "stvec",
        "medeleg",
        "mideleg",
        "mie",
        "mip",
        "satp",
        "fflags",
        "frm",
        "fcsr",
        "act_first_valid",
        "act_first_mode",
        "act_first_cause",
        "act_first_epc",
        "act_first_tval",
        "act_first_status",
        "act_first_ie",
        "act_first_ip",
        "act_first_tvec",
        "act_first_satp",
        "instruction",
        "approximate_address",
        "bad_value",
        "expected_register_value",
    ):
        if key in data:
            data[f"{key}_int"] = _hex_to_int(data[key])

    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="events.log, case_report.txt, or UART log")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    args = parser.parse_args()

    text = args.log.read_text(errors="replace")
    data = parse_text(text, str(args.log))
    print(json.dumps(data, indent=2 if args.pretty else None, sort_keys=True))


if __name__ == "__main__":
    main()
