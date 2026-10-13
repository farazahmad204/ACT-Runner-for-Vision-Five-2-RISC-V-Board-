#!/usr/bin/env python3
"""Run the whole offline board-enablement chain from one board request.

  request.yaml -> find_docs -> extract -> resolve -> fill_gaps -> resolve -> spec
               -> runner_files -> act_config -> build -> validate -> REPORT.md

The request (see assets/board_request.example.yaml) is what a portal form
would submit: board id and name, ISA string, document quality, documents,
DTB, boot logs, user answers, an optional reviewed board spec, and the runner
and ACT checkouts to build against.

Every step is deterministic except find_docs and fill_gaps, the steps that call a
model. find_docs (request `find_docs: true`) asks Codex with web search for the board's
official documents and repositories and saves the ones that resolve
(find_board_docs.py); fill_gaps sends document excerpts to the model (`model.backend`:
claude-cli, codex or api). Both run only with --allow-model-calls (fill_gaps otherwise
as a dry run: retrieval and token estimate).

State lives in <work>/state.json. A step is skipped when its inputs are
unchanged and its outputs exist (--force reruns). The pipeline stops at the
first step that needs a person (blockers, manual steps, failed build) and says
so in REPORT.md. No hardware, flash, push or Jenkins action is ever taken:
the furthest state reachable here is `offline_prepared`.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import yaml

SCRIPTS = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS.parent
STEPS = ["find_docs", "extract", "resolve", "fill_gaps", "resolve_research", "decide", "spec", "runner_files", "act_config", "build",
         "validate", "report"]
CATALOG = SKILL_DIR / "assets" / "runner-requirements.yaml"
DEFAULT_SAIL = "/home/lpt-10xe/riscv-sail-0.14.1/bin/sail_riscv_sim"


class Stop(Exception):
    """A step needs a person; the pipeline stops here."""


RESULT_SCHEMA_VERSION = 1
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_]{1,62}$")
DOC_QUALITY = {"complete", "vague", "none"}
URL_HOSTS = set(filter(None, os.environ.get("BOARD_PIPELINE_URL_HOSTS", "192.168.100.150").split(",")))


def validate_request(req: Any) -> list[str]:
    """Shape checks for a board request (assets/schemas/board_request.md)."""
    errors: list[str] = []
    if not isinstance(req, dict):
        return ["request must be a mapping"]
    if not SAFE_ID.match(str(req.get("board_id", ""))):
        errors.append("board_id: lower-case letters, digits and '_' (2-63 chars)")
    if req.get("isa") is not None and not re.fullmatch(r"rv(32|64)[a-z][a-z0-9_]*", str(req["isa"]).lower()):
        errors.append("isa: expected an ISA string such as rv64imafdc_zicsr_zifencei")
    if req.get("docs_quality") not in (None, *DOC_QUALITY):
        errors.append(f"docs_quality: one of {sorted(DOC_QUALITY)}")
    for i, d in enumerate(req.get("documents") or []):
        if isinstance(d, dict):
            if not re.match(r"https?://", str(d.get("url", ""))) or not re.fullmatch(r"[0-9a-f]{64}", str(d.get("sha256", ""))):
                errors.append(f"documents[{i}]: {{url: http(s)://..., sha256: <64 hex>}} or a path")
        elif not isinstance(d, str):
            errors.append(f"documents[{i}]: a path or {{url, sha256}}")
    for key in ("boot_logs", "user_answers", "model", "spec_overrides"):
        if key in req and req[key] is not None and not isinstance(req[key], (dict, str) if key == "user_answers" else dict):
            errors.append(f"{key}: wrong type")
    if req.get("runner_hart") is not None and not isinstance(req["runner_hart"], int):
        errors.append("runner_hart: integer")
    for key in ("soc", "hint"):
        if req.get(key) is not None and not isinstance(req[key], str):
            errors.append(f"{key}: text")
    if (req.get("model") or {}).get("backend") not in (None, "claude-cli", "codex", "api"):
        errors.append("model.backend: claude-cli, codex or api")
    return errors


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_hash(paths: list[Path | str | None], extra: Any = None) -> str:
    h = hashlib.sha256(json.dumps(extra, sort_keys=True, default=str).encode())
    for p in paths:
        if p is None:
            continue
        p = Path(p)
        if p.is_file():
            h.update(p.read_bytes())
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    h.update(str(f.relative_to(p)).encode() + f.read_bytes())
        else:
            h.update(f"missing:{p}".encode())
    return h.hexdigest()


class Pipeline:
    def __init__(self, request_path: Path, work: Path, args: argparse.Namespace) -> None:
        self.req_path = request_path.resolve()
        self.base = self.req_path.parent
        self.req = yaml.safe_load(request_path.read_text())
        self.work = work.resolve()
        self.args = args
        self.board = self.req["board_id"]
        errors = validate_request(self.req)
        if errors:
            raise SystemExit("invalid board request:\n  " + "\n  ".join(errors))
        self.state_path = self.work / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.is_file() else {
            "board_id": self.board, "steps": {}}
        (self.work / "logs").mkdir(parents=True, exist_ok=True)
        self.fetch_documents()
        self.add_found_documents()

    def add_found_documents(self) -> None:
        """Documents find_docs saved join the request's own (for every later step and rerun)."""
        sources = self.work / "docs" / "sources.yaml"
        if not sources.is_file():
            return
        docs = self.req.setdefault("documents", [])
        for rec in (yaml.safe_load(sources.read_text()) or {}).get("documents") or []:
            path = str(self.work / "docs" / rec["file"]) if rec.get("status") == "saved" else None
            if path and path not in docs:
                docs.append(path)

    def fetch_documents(self) -> None:
        """Download {url, sha256} documents (portal uploads) into <work>/inputs and verify them."""
        import urllib.parse
        import urllib.request
        local = []
        for d in self.req.get("documents") or []:
            if not isinstance(d, dict):
                local.append(d)
                continue
            host = urllib.parse.urlparse(d["url"]).hostname or ""
            if host not in URL_HOSTS:
                raise SystemExit(f"document host {host!r} is not allowed (BOARD_PIPELINE_URL_HOSTS)")
            name = Path(urllib.parse.urlparse(d["url"]).path).name or "document"
            dest = self.work / "inputs" / f"{d['sha256'][:12]}_{name}"
            if not dest.is_file() or hashlib.sha256(dest.read_bytes()).hexdigest() != d["sha256"]:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(d["url"], timeout=120) as resp:
                    data = resp.read()
                if hashlib.sha256(data).hexdigest() != d["sha256"]:
                    raise SystemExit(f"document {d['url']} does not match its sha256")
                dest.write_bytes(data)
            local.append(str(dest))
        self.req["documents"] = local

    # ---------------------------------------------------------- helpers
    def path(self, value: str | None) -> Path | None:
        if not value:
            return None
        p = Path(os.path.expanduser(value))
        return p if p.is_absolute() else (self.base / p).resolve()

    def paths(self, values: list[str] | None) -> list[Path]:
        return [self.path(v) for v in (values or [])]

    def run_cmd(self, step: str, cmd: list[str], ok_codes: tuple[int, ...] = (0,), env: dict | None = None,
                cwd: Path | None = None, timeout: int = 1800) -> subprocess.CompletedProcess:
        log = self.work / "logs" / f"{step}.log"
        with log.open("a") as fh:
            fh.write(f"\n$ {' '.join(map(str, cmd))}\n")
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env={**os.environ, **(env or {})},
                           cwd=cwd, timeout=timeout)
        with log.open("a") as fh:
            fh.write(r.stdout + r.stderr)
        if r.returncode not in ok_codes:
            raise Stop(f"{step}: `{Path(str(cmd[1] if cmd[0] == sys.executable else cmd[0])).name}` exited "
                       f"{r.returncode}; see logs/{step}.log")
        return r

    def save(self) -> None:
        self.state["updated"] = now()
        self.state_path.write_text(json.dumps(self.state, indent=2, default=str))

    def record(self, step: str, **kw: Any) -> None:
        self.state["steps"][step] = kw          # each step records its full result once per run
        self.save()

    def fresh(self, step: str, fingerprint: str, outputs: list[Path]) -> bool:
        st = self.state["steps"].get(step, {})
        return (not self.args.force and st.get("fingerprint") == fingerprint and st.get("status") in ("ok", "warn")
                and all(o.exists() for o in outputs))

    # ---------------------------------------------------------- steps
    def step_find_docs(self) -> None:
        if not self.req.get("find_docs"):
            self.record("find_docs", status="skipped", reason="the request did not ask for a document search")
            return
        if not self.args.allow_model_calls:
            self.record("find_docs", status="skipped", reason="needs --allow-model-calls (Codex web search)")
            return
        out = self.work / "docs"
        fp = file_hash([SCRIPTS / "find_board_docs.py", CATALOG],
                       [self.req.get("board_name"), self.req.get("soc"), self.req.get("hint")])
        if not self.fresh("find_docs", fp, [out / "sources.yaml"]):
            cmd = [sys.executable, SCRIPTS / "find_board_docs.py", "--board", self.req.get("board_name") or self.board,
                   "--soc", self.req.get("soc") or "", "--board-id", self.board, "--hint", self.req.get("hint") or "",
                   "--out-dir", out, "--clone-repos", "--codex-bin", os.environ.get("CODEX_BIN", "codex")]
            self.run_cmd("find_docs", cmd, ok_codes=(0, 1), timeout=3600)
            if not (out / "sources.yaml").is_file():
                raise Stop("find_docs: the document search returned nothing; see logs/find_docs.log")
            docs = (yaml.safe_load((out / "sources.yaml").read_text()) or {}).get("documents") or []
            counts = {k: sum(1 for d in docs if d.get("status") == k) for k in ("saved", "rejected", "failed",
                                                                                 "repository")}
            self.record("find_docs", status="ok" if counts["saved"] else "warn", fingerprint=fp, **counts)
        self.add_found_documents()

    def step_extract(self) -> None:
        out = self.work / "facts.yaml"
        dtb, logs = self.path(self.req.get("dtb")), self.req.get("boot_logs") or {}
        inputs = [dtb] + [self.path(v) for v in logs.values()]
        if not any(inputs):
            self.record("extract", status="skipped", reason="no DTB or boot logs in the request")
            return
        fp = file_hash(inputs, [self.req.get("runner_hart"), str(SCRIPTS / "collect_board_facts.py")])
        if self.fresh("extract", fp, [out]):
            return
        cmd = [sys.executable, SCRIPTS / "collect_board_facts.py", "--board-id", self.board, "--output", out]
        if dtb:
            cmd += ["--dtb", dtb]
        for kind in ("opensbi", "linux", "uboot"):
            if logs.get(kind):
                cmd += [f"--{kind}-log", self.path(logs[kind])]
        if self.req.get("runner_hart") is not None:
            cmd += ["--runner-hart", str(self.req["runner_hart"])]
        self.run_cmd("extract", cmd)
        facts = yaml.safe_load(out.read_text())
        self.record("extract", status="ok", fingerprint=fp, facts=len(facts.get("facts", {})),
                    conflicts=len(facts.get("conflicts", []) or []))

    def user_answers(self) -> Path | None:
        """User answers file plus the request's own fields (the ISA string is a user answer)."""
        ua = self.req.get("user_answers")
        answers = dict(ua) if isinstance(ua, dict) else (
            yaml.safe_load(self.path(ua).read_text()) or {} if ua else {})
        if self.req.get("isa") and "identity.isa" not in answers:
            answers["identity.isa"] = {"value": self.req["isa"], "source": "board request (user)"}
        if not answers:
            return None
        p = self.work / "user_answers.yaml"
        p.write_text(yaml.safe_dump(answers, sort_keys=False, width=200))
        return p

    def resolve(self, step: str, research: list[Path]) -> None:
        out = self.work / ("requirements" if step == "resolve" else "requirements_final")
        user, facts = self.user_answers(), self.work / "facts.yaml"
        manifest = self.path(self.req.get("manifest"))
        rdir = self.work / f"research_{step}"
        if rdir.exists():
            shutil.rmtree(rdir)
        rdir.mkdir(parents=True)
        for d in research:
            for f in sorted(Path(d).glob("*.yaml")):
                shutil.copy(f, rdir / f"{Path(d).name}__{f.name}")
        fp = file_hash([user, facts, manifest, rdir, self.path(self.req.get("probe")), CATALOG,
                        SCRIPTS / "resolve_requirements.py"])
        if self.fresh(step, fp, [out / "coverage.yaml"]):
            return
        cmd = [sys.executable, SCRIPTS / "resolve_requirements.py", "--board-id", self.board, "--out", out,
               "--research", rdir]
        if user:
            cmd += ["--user", user]
        if facts.is_file():
            cmd += ["--facts", facts]
        if manifest:
            cmd += ["--manifest", manifest]
        if self.path(self.req.get("probe")):
            cmd += ["--probe", self.path(self.req["probe"])]
        r = self.run_cmd(step, cmd, ok_codes=(0, 1))
        cov = yaml.safe_load((out / "coverage.yaml").read_text())
        self.record(step, status="ok" if r.returncode == 0 else "warn", fingerprint=fp, summary=cov["summary"],
                    rule_violations=cov.get("rule_violations", []))

    def step_resolve(self) -> None:
        self.resolve("resolve", self.paths(self.req.get("research_dirs")))

    def step_fill_gaps(self) -> None:
        docs = self.paths(self.req.get("documents"))
        cov = self.work / "requirements" / "coverage.yaml"
        out = self.work / "research_fill_gaps"
        if not docs:
            self.record("fill_gaps", status="skipped", reason="no documents in the request")
            return
        model = self.req.get("model") or {}
        live = bool(self.args.allow_model_calls and model.get("enabled", True))
        fp = file_hash(docs + [cov, SCRIPTS / "fill_gaps.py", CATALOG], [live, model])
        if self.fresh("fill_gaps", fp, [out / "fill_gaps.json"]):
            return
        cmd = [sys.executable, SCRIPTS / "fill_gaps.py", "--coverage", cov, "--docs", *docs, "--out-dir", out,
               "--board-id", self.board, "--budget-tokens", str(model.get("budget_tokens", 150000))]
        if model.get("backend"):
            cmd += ["--backend", model["backend"]]
        if model.get("escalate"):
            cmd += ["--escalate-model", model["escalate"]]
        if not live:
            cmd.append("--dry-run")
        self.run_cmd("fill_gaps", cmd, timeout=3600)
        res = json.loads((out / "fill_gaps.json").read_text())
        answered = sum(1 for it in res["items"].values() if it["status"] in ("partial", "inferred"))
        self.record("fill_gaps", status="ok", fingerprint=fp, live=live, asked=sum(len(c["items"]) for c in res["calls"]),
                    answered=answered, tokens=res["tokens"],
                    note=None if live else "dry run: pass --allow-model-calls to query the model")

    def step_resolve_research(self) -> None:
        extra = self.work / "research_fill_gaps"
        dirs = self.paths(self.req.get("research_dirs"))
        if extra.is_dir() and any(extra.glob("fill_gaps_*.yaml")):
            dirs.append(extra)
        self.resolve("resolve_research", dirs)

    def step_decide(self) -> None:
        cov_in = self.work / "requirements_final" / "coverage.yaml"
        out = self.work / "requirements_final" / "coverage_decided.yaml"
        fp = file_hash([cov_in, SCRIPTS / "decide_choices.py"])
        if self.fresh("decide", fp, [out]):
            return
        r = self.run_cmd("decide", [sys.executable, SCRIPTS / "decide_choices.py", cov_in, "--out", out],
                         ok_codes=(0, 2))
        decided = [l[len("decided "):] for l in r.stdout.splitlines() if l.startswith("decided ")]
        blockers = [l[len("blocker: "):] for l in r.stdout.splitlines() if l.startswith("blocker: ")]
        notes = [l[len("note: "):] for l in r.stdout.splitlines() if l.startswith("note: ")]
        self.record("decide", status="ok" if not blockers else "warn", fingerprint=fp, decided=len(decided),
                    notes=notes, decide_blockers=blockers)

    def step_spec(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        from coverage_to_spec import bridge
        cov_path = self.work / "requirements_final" / "coverage_decided.yaml"
        base_path = self.path(self.req.get("base_spec"))
        fp = file_hash([cov_path, base_path, SCRIPTS / "coverage_to_spec.py"], self.req.get("spec_overrides"))
        out = self.work / "spec.yaml"
        if self.fresh("spec", fp, [out]):
            if self.state["steps"]["spec"].get("blockers"):
                raise Stop("spec: blockers remain (see spec_review.md)")
            return
        cov = yaml.safe_load(cov_path.read_text())
        base = yaml.safe_load(base_path.read_text()) if base_path else self.minimal_spec()
        spec, mapped, defaults, blockers, diffs = bridge(cov, base, "base" if base_path else "coverage")
        for k, v in (self.req.get("spec_overrides") or {}).items():
            cur = spec
            *parents, last = k.split(".")
            for part in parents:
                cur = cur.setdefault(part, {})
            cur[last] = v
        covered, open_blockers = [], []
        for b in blockers:
            m = re.search(r"-> `([^`]+)`", b) or re.match(r"`([^`]+)`", b)
            field = m.group(1) if m else None
            if field and self.spec_has(spec, field) and (base_path or field in (self.req.get("spec_overrides") or {})):
                covered.append(f"{b} -- covered by the reviewed spec value {self.spec_get(spec, field)!r}")
            else:
                open_blockers.append(b)
        out.write_text(f"# Board spec used by the pipeline for {self.board} ({now()})\n"
                       + yaml.safe_dump(spec, sort_keys=False, width=200))
        md = [f"# Spec review: {self.board}", "", f"Base spec: {base_path or 'none (built from the request)'}", ""]
        for title, rows in (("Open blockers", open_blockers), ("Blockers covered by the reviewed spec", covered),
                            ("Differences (reviewed spec kept)", diffs), ("Rule decisions and catalog defaults to confirm", defaults),
        ("Decision notes", self.state["steps"].get("decide", {}).get("notes", []))):
            if rows:
                md += [f"## {title}", ""] + [f"- {r}" for r in rows] + [""]
        (self.work / "spec_review.md").write_text("\n".join(md))
        self.record("spec", status="ok" if not open_blockers else "blocked", fingerprint=fp, mapped=len(mapped),
                    defaults=len(defaults), blockers=open_blockers, covered=len(covered), differences=diffs)
        if open_blockers:
            raise Stop(f"spec: {len(open_blockers)} blockers need a decision (see spec_review.md)")

    def minimal_spec(self) -> dict:
        r = self.req
        return {"dut": {"name": r.get("dut_name", r["board_id"].replace("_", "-")),
                        "vendor_dir": r.get("vendor_dir", r["board_id"]), "description": r.get("board_name", "")},
                "isa": r.get("isa"),
                "runner": {"board_id": r["board_id"], "description": r.get("board_name", r["board_id"])},
                "boot": {"image": r.get("boot_image", "boot_image.bin")}}

    @staticmethod
    def spec_get(spec: dict, field: str) -> Any:
        cur: Any = spec
        for part in field.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def spec_has(self, spec: dict, field: str) -> bool:
        return self.spec_get(spec, field) is not None

    def step_runner_files(self) -> None:
        spec = self.work / "spec.yaml"
        out = self.work / "generated" / "runner"
        runner = self.path(self.req.get("runner_checkout"))
        fp = file_hash([spec, SKILL_DIR / "assets" / "runner", SCRIPTS / "gen_board_files.py"], str(runner))
        if self.fresh("runner_files", fp, [out]):
            return
        if out.exists():
            shutil.rmtree(out)
        cmd = [sys.executable, SCRIPTS / "gen_board_files.py", spec, "--out-root", out]
        if runner:
            cmd += ["--runner-root", runner]
        r = self.run_cmd("runner_files", cmd, ok_codes=(0, 2))
        report = yaml.safe_load((out / "cert_harness" / "boards" / self.board / "runner_report.yaml").read_text())
        accepted = set(self.req.get("accepted_layout_problems") or [])
        problems = [p for p in report["problems"] if not any(a in p for a in accepted)]
        # A proposed registration patch does not block the scratch build; it is applied there and listed for review.
        blocking = [m for m in report["manual_steps"] if "registration.patch" not in m]
        self.record("runner_files", status="blocked" if problems or blocking else
                    ("warn" if report["manual_steps"] else "ok"),
                    fingerprint=fp, files=report["files"], problems=report["problems"],
                    accepted_problems=sorted(set(report["problems"]) - set(problems)),
                    manual_steps=report["manual_steps"])
        if problems or blocking:
            raise Stop("runner_files: layout problems or manual steps remain (see the report)")

    def step_act_config(self) -> None:
        spec_path = self.work / "spec.yaml"
        spec = yaml.safe_load(spec_path.read_text())
        if not spec.get("isa") or not (spec.get("csr") or {}).get("pmp"):
            self.record("act_config", status="skipped", reason="spec has no ACT section (isa/csr)")
            return
        out = self.work / "generated" / "act" / spec["dut"]["name"]
        act = self.path(self.req.get("act_checkout"))
        fp = file_hash([spec_path, SKILL_DIR / "assets" / "act", SCRIPTS / "gen_act_config.py"], str(act))
        if self.fresh("act_config", fp, [out]):
            return
        cmd = [sys.executable, SCRIPTS / "gen_act_config.py", spec_path, "--out-dir", out]
        if act:
            cmd += ["--act-root", act]
        r = self.run_cmd("act_config", cmd, ok_codes=(0, 2))
        rep = yaml.safe_load((out / "generation_report.yaml").read_text())
        suites = yaml.safe_load((out / "suites.yaml").read_text())["suites"] if (out / "suites.yaml").is_file() else {}
        self.record("act_config", status="ok" if r.returncode == 0 else "incomplete", fingerprint=fp, dir=str(out),
                    missing=rep["missing"], hypervisor=rep["hypervisor"],
                    unverified_defaults=sum(o.startswith("default") for o in rep["udb_param_origin"].values()),
                    suites={k: len(v) for k, v in suites.items()})
        # The runner does not depend on the ACT config: missing ACT inputs are reported, not a stop.

    def step_build(self) -> None:
        runner = self.path(self.req.get("runner_checkout"))
        if not runner:
            self.record("build", status="skipped", reason="no runner_checkout in the request")
            return
        gen = self.work / "generated" / "runner"
        fp = file_hash([gen], [str(runner), subprocess.run(["git", "-C", str(runner), "rev-parse", "HEAD"],
                                                          capture_output=True, text=True).stdout.strip()])
        out = self.work / "build" / "out"
        if self.fresh("build", fp, [out / self.board / "UART_M_MODE" / "firmware.bin"]):
            return
        src = self.work / "build" / "src"
        if src.exists():
            shutil.rmtree(src)
        shutil.copytree(runner, src, ignore=shutil.ignore_patterns(".git", "build", "*.o"))
        shutil.copytree(gen, src, dirs_exist_ok=True)
        patch = src / "cert_harness" / "boards" / self.board / "runner_shared.registration.patch"
        if patch.is_file():
            self.run_cmd("build", ["patch", "-p1", "-d", src, "-i", patch])   # scratch copy only
        build_id = self.req.get("runner_build_id") or f"{self.board[:40]}-pipeline"
        env = {"RUNNER_BUILD_ID_OVERRIDE": build_id}
        cmd = ["bash", "cert_harness/tools/build_runner.sh", "--board", self.board, "--out-root", out]
        if not self.req.get("package"):
            cmd.append("--no-package")
        self.run_cmd("build", cmd, env=env, cwd=src)
        art = out / self.board / "UART_M_MODE"
        size = (art / "size.txt").read_text().splitlines()[-1].split()
        sha = hashlib.sha256((art / "firmware.bin").read_bytes()).hexdigest()
        self.record("build", status="ok", fingerprint=fp, artifacts=str(art), firmware_bin_sha256=sha,
                    text_data_bss=size[:3], build_id=build_id, packaged=bool(self.req.get("package")))

    def step_validate(self) -> None:
        act = self.state["steps"].get("act_config", {})
        if act.get("status") != "ok":
            self.record("validate", status="skipped", reason="no generated ACT config")
            return
        d = Path(act["dir"])
        sail = self.req.get("sail_exe", DEFAULT_SAIL)
        results = {}
        r = subprocess.run([sail, "--config", str(d / "sail.json"), "--validate-config"], capture_output=True, text=True)
        results["sail_validate_config"] = "pass" if r.returncode == 0 else f"fail: {(r.stderr or r.stdout)[-200:]}"
        if self.args.udb_validate and self.req.get("act_checkout"):
            data = self.path(self.req["act_checkout"]) / "framework" / "src" / "act" / "data"
            udb = next(p for p in d.glob("*.yaml") if p.stem not in ("test_config", "test_config.local", "suites",
                                                                       "generation_report"))
            r = subprocess.run(["bundle", "exec", "udb", "validate", "cfg", str(udb)], capture_output=True,
                               text=True, cwd=data, timeout=900)
            results["udb_validate_cfg"] = "pass" if "is valid" in r.stdout + r.stderr else \
                f"fail: {(r.stdout + r.stderr)[-300:]}"
        ok = all(v == "pass" for v in results.values())
        self.record("validate", status="ok" if ok else "blocked", results=results)
        if not ok:
            raise Stop("validate: generated ACT config failed validation")

    def step_report(self) -> None:
        st = self.state["steps"]
        done = all(st.get(s, {}).get("status") in ("ok", "skipped", "warn", "incomplete") for s in STEPS[:-1])
        built = st.get("build", {}).get("status") == "ok"
        state = "offline_prepared" if done and built else "researching"
        self.state["completion_state"] = state
        self.record("report", status="ok")
        self.write_report(state)

    # ---------------------------------------------------------- report
    def write_report(self, state: str, stopped: str | None = None) -> None:
        st = self.state["steps"]
        L = [f"# Board pipeline: {self.req.get('board_name', self.board)} (`{self.board}`)", "",
             f"Completion state: **{state}**" + (f" (stopped: {stopped})" if stopped else ""),
             f"Request: `{self.req_path}`; documents: {self.req.get('docs_quality', 'unspecified')}; "
             f"updated {now()}.", "", "| Step | Status | Summary |", "|---|---|---|"]
        for s in STEPS:
            info = st.get(s)
            if not info:
                L.append(f"| {s} | not run | |")
                continue
            summary = {k: v for k, v in info.items() if k not in ("status", "fingerprint", "blockers", "differences",
                                                                   "problems", "manual_steps", "files")}
            L.append(f"| {s} | {info['status']} | {json.dumps(summary, default=str)[:180]} |")
        L.append("")
        todo = []
        todo += [f"Spec blocker: {b}" for b in st.get("spec", {}).get("blockers", [])]
        todo += [f"Confirm by smoke test: {n}" for n in st.get("decide", {}).get("notes", [])
                 if st.get("decide", {}).get("decided")]
        todo += [f"Runner layout: {p}" for p in st.get("runner_files", {}).get("problems", [])
                 if p not in st.get("runner_files", {}).get("accepted_problems", [])]
        todo += [f"Manual step: {m}" for m in st.get("runner_files", {}).get("manual_steps", [])]
        todo += [f"ACT input missing: {m}" for m in st.get("act_config", {}).get("missing", [])]
        if st.get("act_config", {}).get("status") == "skipped":
            todo.append("ACT config not generated: the spec has no `isa`/`csr` section (add CSR facts from the TRM, "
                        "OpenSBI log or csr_probe.py; see assets/board_specs/README.md)")
        final = self.work / "requirements_final" / "coverage.yaml"
        if final.is_file():
            for rid, rec in yaml.safe_load(final.read_text())["requirements"].items():
                if rec.get("via") == "research" and rec.get("evidence_status") == "inferred":
                    todo.append(f"Confirm `{rid}` = {rec.get('value')} (document-derived, inferred; "
                                "csr_probe.py or a reviewer): " + str(rec.get("source"))[:120])
        if st.get("fill_gaps", {}).get("live") is False:
            todo.append("fill_gaps ran as a dry run: rerun with --allow-model-calls to query documents")
        if todo:
            L += ["## Needs a person", ""] + [f"- {t}" for t in todo] + [""]
        L += ["## Next (outside this pipeline, approval required)", "",
              "- Review `spec_review.md`, `generated/` and the build; copy generated files into a clean feature "
              "branch from the stable runner ref.",
              "- Hardware smoke on a non-destructive boot path (two clean boots, `DONE PASS`), then the "
              "UNVERIFIED ACT defaults via `csr_probe.py`.",
              "- Flash install, Jenkins jobs and portal publishing each need explicit approval.", ""]
        L += ["Files: `state.json`, `spec.yaml`, `spec_review.md`, `requirements*/`, `research_fill_gaps/`, "
              "`generated/runner/`, `generated/act/`, `build/out/`, `logs/`.", ""]
        (self.work / "REPORT.md").write_text("\n".join(L))
        self.write_result(state, stopped, todo)

    def write_result(self, state: str, stopped: str | None, todo: list[str]) -> None:
        """Machine-readable summary for the portal (assets/schemas/board_result.md)."""
        st = self.state["steps"]
        build = st.get("build", {})
        act = st.get("act_config", {})
        fill = st.get("fill_gaps", {})
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "board_id": self.board,
            "board_name": self.req.get("board_name", self.board),
            "request_id": self.req.get("request_id"),
            "isa": self.req.get("isa"),
            "docs_quality": self.req.get("docs_quality"),
            "completion_state": state,
            "stopped_at": self.state.get("stopped_at") if stopped else None,
            "stop_reason": stopped,
            "generated_at": now(),
            "steps": {s: {"status": st.get(s, {}).get("status", "not_run"),
                          "seconds": st.get(s, {}).get("seconds")} for s in STEPS},
            "needs_person": todo,
            "runner": {"built": build.get("status") == "ok", "build_id": build.get("build_id"),
                       "firmware_bin_sha256": build.get("firmware_bin_sha256"),
                       "text_data_bss": build.get("text_data_bss"), "packaged": build.get("packaged")},
            "act": {"status": act.get("status", "not_run"), "hypervisor": act.get("hypervisor"),
                    "suites": act.get("suites"), "unverified_defaults": act.get("unverified_defaults"),
                    "missing": act.get("missing"),
                    "validation": st.get("validate", {}).get("results")},
            "model": {"live": fill.get("live"), "asked": fill.get("asked"), "answered": fill.get("answered"),
                      "tokens": fill.get("tokens")},
            "soc": self.req.get("soc"),
            "requirements": self.requirements(),
            "documents_found": self.documents_found(),
            "artifacts": ["REPORT.md", "state.json", "spec.yaml", "spec_review.md", "generated/", "build/out/"],
        }
        (self.work / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")

    def requirements(self) -> list[dict]:
        """Every runner requirement with its current answer, from the latest coverage."""
        for name in ("requirements_final/coverage_decided.yaml", "requirements_final/coverage.yaml",
                     "requirements/coverage.yaml"):
            path = self.work / name
            if path.is_file():
                coverage = (yaml.safe_load(path.read_text()) or {}).get("requirements") or {}
                break
        else:
            coverage = {}
        catalog = yaml.safe_load(CATALOG.read_text())
        rows = []
        for req in catalog["requirements"]:
            rec = coverage.get(req["id"]) or {}
            rows.append({"id": req["id"], "group": req["group"], "kind": req["kind"],
                         "description": req["description"], "needed_for": req.get("needed_for", []),
                         "consumer": req.get("consumer", ""), "status": rec.get("status", "open"),
                         "value": rec.get("value"), "source": rec.get("source"), "via": rec.get("via"),
                         "evidence_status": rec.get("evidence_status")})
        return rows

    def documents_found(self) -> list[dict]:
        sources = self.work / "docs" / "sources.yaml"
        if not sources.is_file():
            return []
        keep = ("title", "url", "kind", "publisher", "official", "revision", "status", "note", "file")
        return [{k: d.get(k) for k in keep}
                for d in (yaml.safe_load(sources.read_text()) or {}).get("documents") or []]

    # ---------------------------------------------------------- driver
    def run(self) -> int:
        start = STEPS.index(self.args.from_step) if self.args.from_step else 0
        stop = STEPS.index(self.args.until) + 1 if self.args.until else len(STEPS)
        for s in STEPS[start:stop]:
            t0 = time.time()
            try:
                getattr(self, f"step_{s}")()
                self.state["steps"].setdefault(s, {})["seconds"] = round(time.time() - t0, 1)
                self.save()
                print(f"[{s}] {self.state['steps'].get(s, {}).get('status')}")
            except Stop as exc:
                self.state["steps"].setdefault(s, {}).setdefault("status", "blocked")
                self.state["completion_state"] = "researching"
                self.state["stopped_at"] = s
                self.save()
                self.write_report("researching", str(exc))
                print(f"[{s}] STOP: {exc}")
                print(f"report: {self.work / 'REPORT.md'}")
                return 2
        print(f"completion: {self.state.get('completion_state')}  report: {self.work / 'REPORT.md'}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("request", type=Path, help="board request YAML")
    ap.add_argument("--work", type=Path, required=True, help="work directory (state, outputs, logs)")
    ap.add_argument("--allow-model-calls", action="store_true",
                    help="let fill_gaps send document excerpts to the model (Claude subscription or API)")
    ap.add_argument("--udb-validate", action="store_true", help="also run `udb validate cfg` (about a minute)")
    ap.add_argument("--from", dest="from_step", choices=STEPS)
    ap.add_argument("--until", choices=STEPS)
    ap.add_argument("--force", action="store_true", help="rerun steps even when their inputs are unchanged")
    args = ap.parse_args()
    return Pipeline(args.request, args.work, args).run()


if __name__ == "__main__":
    raise SystemExit(main())
