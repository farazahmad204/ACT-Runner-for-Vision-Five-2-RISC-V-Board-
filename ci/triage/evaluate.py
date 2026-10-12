#!/usr/bin/env python3
"""How often the triage agent gets the root cause right, measured against an answer key.

    evaluate.py --run-root RUN --board BOARD --job JOB --build N [--feedback FILE | --portal-url URL]
                [--output DIR]

RUN holds a triage result (triage/summary.json and per_case/<test>/report.json). For a fair
score, produce it "blind" first (run_ci_triage.py --blind): no vault notes and no memory, so the
agent works every failure out itself.

The answer key, per failed test, first match wins:
  person       the Verdict a person set in the portal workbook for this run
  person-other a person's Verdict for the same test in another run of this board
  vault        a verified vault note (issue or verification) that resolves this failure
Tests without a key are left out of the scores.

Scores (accuracy.json and accuracy.md):
  root-cause accuracy    the class matches (keys with a class: vault notes), else the Verdict
  verdict agreement      the agent's suggested Verdict equals the key's
  class accuracy         where the key has a root-cause class (vault notes)
  hardware-bug false alarms  the agent says DUT noncompliance where the key is not a hardware bug;
                         this is the number that must stay at zero
  missed hardware bugs   the key says hardware bug and the agent does not
  accuracy by confidence whether "high" really means more often right
  spec quotes            verified / not found / none, over all AI answers
plus every disagreement, so a person can see what to fix.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault  # noqa: E402

HARDWARE_BUG = "Confirmed hardware bug"


def load_reports(run_root: Path) -> list[dict[str, Any]]:
    summary = json.loads((run_root / "triage" / "summary.json").read_text(encoding="utf-8"))
    reports = []
    for item in summary.get("results", []):
        folder = run_root / str(item["report_dir"])
        report = item.get("report")
        if not report and (folder / "report.json").is_file():
            report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        evidence = {}
        if (folder / "evidence.json").is_file():
            evidence = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
        reports.append({"test": item["case"], "ai_status": item.get("ai_status", ""),
                        "report": report or {}, "evidence": evidence})
    return reports


def fetch_feedback(portal_url: str, token: str, job: str, ca_file: str | None) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"job_name": job})
    request = urllib.request.Request(portal_url.rstrip("/") + f"/api/v1/triage/feedback/?{query}",
                                     headers={"X-Portal-Token": token})
    context = ssl.create_default_context(cafile=ca_file) if ca_file else None
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        return json.loads(response.read().decode("utf-8")).get("feedback", [])


def build_key(reports: list[dict[str, Any]], board: str, run_label: str,
              feedback: list[dict[str, Any]], notes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    same_run = {f["test"]: f for f in feedback if f.get("verdict") and f.get("run") == run_label}
    other_run: dict[str, dict[str, Any]] = {}
    for item in sorted((f for f in feedback if f.get("verdict") and f.get("run") != run_label),
                       key=lambda f: f.get("updated_at", "")):
        other_run[item["test"]] = item  # the latest wins
    key: dict[str, dict[str, Any]] = {}
    for entry in reports:
        test = entry["test"]
        if test in same_run:
            key[test] = {"verdict": same_run[test]["verdict"], "source": "person",
                         "by": same_run[test].get("updated_by", "")}
        elif test in other_run:
            key[test] = {"verdict": other_run[test]["verdict"], "source": "person-other",
                         "by": f"{other_run[test].get('updated_by', '')} ({other_run[test].get('run', '')})"}
        else:
            evidence = entry["evidence"]
            known = vault.match(notes, board, test, vault.evidence_text(evidence), evidence)
            note = next((n for n in known if vault.resolves(n, board)), None)
            if note:
                answer = vault.analysis(note)
                key[test] = {"verdict": str(note.get("verdict") or answer.get("verdict") or ""),
                             "classification": answer["classification"], "source": "vault",
                             "by": note["id"]}
    return key


def score(reports: list[dict[str, Any]], key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows, by_confidence = [], {}
    counts: Counter = Counter()
    quotes: Counter = Counter()
    for entry in reports:
        report = entry["report"]
        if entry["ai_status"] == "SUCCESS":
            quotes[{"verified": "verified", "not_found": "not found"}.get(
                (report.get("spec") or {}).get("check", ""), "none")] += 1
        answer = key.get(entry["test"])
        if not answer:
            continue
        predicted = report.get("suggested_verdict", "")
        klass = report.get("classification", "")
        confidence = report.get("confidence") or "none"
        verdict_right = predicted == answer["verdict"]
        # The root cause is right when the class matches (keys that name one), else the verdict.
        right = klass == answer["classification"] if answer.get("classification") else verdict_right
        counts["keyed"] += 1
        counts["right"] += right
        counts["verdict_right"] += verdict_right
        counts[f"source:{answer['source']}"] += 1
        if answer.get("classification"):
            counts["class_keyed"] += 1
            counts["class_right"] += klass == answer["classification"]
        if klass == "DUT noncompliance" and answer["verdict"] != HARDWARE_BUG:
            counts["false_hardware_bug"] += 1
        if answer["verdict"] == HARDWARE_BUG and klass != "DUT noncompliance":
            counts["missed_hardware_bug"] += 1
        bucket = by_confidence.setdefault(confidence, {"keyed": 0, "right": 0})
        bucket["keyed"] += 1
        bucket["right"] += right
        if not right:
            rows.append({"test": entry["test"], "key_verdict": answer["verdict"],
                         "key_class": answer.get("classification", ""), "key_source": answer["source"],
                         "key_by": answer.get("by", ""), "agent_verdict": predicted,
                         "agent_class": klass, "confidence": confidence,
                         "explained_by": entry["ai_status"],
                         "agent_root_cause": str(report.get("root_cause", ""))[:400]})

    def rate(right: int, total: int) -> float | None:
        return round(100.0 * right / total, 1) if total else None

    return {
        "failures": len(reports),
        "keyed": counts["keyed"],
        "key_sources": {k.split(":", 1)[1]: v for k, v in counts.items() if k.startswith("source:")},
        "accuracy": rate(counts["right"], counts["keyed"]),
        "verdict_accuracy": rate(counts["verdict_right"], counts["keyed"]),
        "class_accuracy": rate(counts["class_right"], counts["class_keyed"]),
        "class_keyed": counts["class_keyed"],
        "false_hardware_bugs": counts["false_hardware_bug"],
        "missed_hardware_bugs": counts["missed_hardware_bug"],
        "by_confidence": {k: {**v, "accuracy": rate(v["right"], v["keyed"])}
                          for k, v in sorted(by_confidence.items())},
        "spec_quotes": dict(quotes),
        "disagreements": rows,
    }


def markdown(result: dict[str, Any], label: str) -> str:
    def pct(value):
        return "—" if value is None else f"{value}%"

    lines = [f"# Triage accuracy: {label}", "",
             f"- Failures: {result['failures']}; with an answer key: {result['keyed']} "
             f"({', '.join(f'{v} {k}' for k, v in result['key_sources'].items()) or 'none'})",
             f"- Root-cause accuracy: **{pct(result['accuracy'])}** (class where the key has one, "
             f"else verdict)",
             f"- Verdict agreement: {pct(result['verdict_accuracy'])}",
             f"- Root-cause class accuracy: {pct(result['class_accuracy'])} (of {result['class_keyed']} with a class)",
             f"- Hardware-bug false alarms: **{result['false_hardware_bugs']}**; missed hardware bugs: "
             f"{result['missed_hardware_bugs']}",
             "- Spec quotes in AI answers: " + (", ".join(f"{v} {k}" for k, v in result["spec_quotes"].items())
                                                  or "none"),
             "", "| Confidence | Keyed | Right | Accuracy |", "|---|---|---|---|"]
    for name, bucket in result["by_confidence"].items():
        lines.append(f"| {name} | {bucket['keyed']} | {bucket['right']} | {pct(bucket['accuracy'])} |")
    lines += ["", "## Disagreements", ""]
    if not result["disagreements"]:
        lines.append("None.")
    else:
        lines += ["| Test | Key | Agent | Confidence | Key from |", "|---|---|---|---|---|"]
        for row in result["disagreements"]:
            key = row["key_verdict"] + (f" / {row['key_class']}" if row["key_class"] else "")
            agent = f"{row['agent_verdict']} / {row['agent_class']}"
            lines.append(f"| `{row['test']}` | {key} | {agent} | {row['confidence']} | "
                         f"{row['key_source']} {row['key_by']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--feedback", type=Path, help="portal feedback JSON (instead of --portal-url)")
    parser.add_argument("--portal-url", default="")
    parser.add_argument("--ca-file", default=None)
    parser.add_argument("--output", type=Path, default=None, help="default: RUN/triage")
    parser.add_argument("--history", type=Path, default=None,
                        help="also append the scores to this JSON-lines file (accuracy over time)")
    args = parser.parse_args()

    feedback: list[dict[str, Any]] = []
    if args.feedback:
        feedback = json.loads(args.feedback.read_text(encoding="utf-8")).get("feedback", [])
    elif args.portal_url and os.environ.get("PORTAL_INGEST_TOKEN"):
        try:
            feedback = fetch_feedback(args.portal_url, os.environ["PORTAL_INGEST_TOKEN"], args.job, args.ca_file)
        except OSError as exc:
            print(f"Portal feedback unavailable ({exc}); scoring against vault notes only.", file=sys.stderr)
    reports = load_reports(args.run_root)
    label = f"{args.job}#{args.build}"
    key = build_key(reports, args.board, label, feedback, vault.load_notes())
    result = score(reports, key)
    result.update(run=label, board=args.board, evaluated_at=datetime.now(timezone.utc).isoformat())
    out = args.output or args.run_root / "triage"
    out.mkdir(parents=True, exist_ok=True)
    (out / "accuracy.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (out / "accuracy.md").write_text(markdown(result, label), encoding="utf-8")
    brief = (f"{'-' if result['accuracy'] is None else result['accuracy']}% of {result['keyed']} keyed, "
             f"{result['false_hardware_bugs']} false hardware bugs")
    (out / "accuracy.txt").write_text(brief + "\n", encoding="utf-8")
    if args.history:
        args.history.parent.mkdir(parents=True, exist_ok=True)
        with args.history.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({k: v for k, v in result.items() if k != "disagreements"}) + "\n")
    print(f"Triage accuracy {label}: {result['keyed']} keyed of {result['failures']} failures; "
          f"root cause {result['accuracy']}%, verdict {result['verdict_accuracy']}%, "
          f"hardware-bug false alarms {result['false_hardware_bugs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
