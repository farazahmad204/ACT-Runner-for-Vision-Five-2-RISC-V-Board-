#!/usr/bin/env python3
"""Find and download a board's official documentation for the runner port, using Codex web search.

    find_board_docs.py --board "Milk-V Megrez" --soc "ESWIN EIC7700X" --board-id milkv_megrez_eic7700x \
        [--out-dir artifacts/onboarding/<id>/docs] [--clone-repos] [--extract]

1. Ask Codex (logged-in Codex CLI, web search on, every other tool off, read-only) for the
   official documents and source repositories that answer the runner requirements in
   assets/runner-requirements.yaml: board manual, SoC TRM/datasheet (memory map, boot, UART,
   CLINT), boot/recovery guide, image packaging, vendor bootloader/OpenSBI/U-Boot sources,
   device tree. The answer is a URL list (JSON schema), not facts.
2. Check every URL by fetching it: a URL that does not resolve is rejected (models invent
   URLs). PDFs, text and source files are saved; HTML pages are saved as text; GitHub/GitLab
   blob links are fetched raw. With --clone-repos, repository links are shallow-cloned and
   their board-specific files (device trees, .its, defconfigs, platform code) gathered into
   one text file per repository.
3. Write sources.yaml (url, title, kind, publisher, official, sha256, status) and, with
   --extract, run fill_gaps.py with the Codex backend on the saved documents, which extracts
   each fact with a verbatim, code-checked quote.

The search is discovery: the facts come only from the saved documents (research-scope.md).
Downloaded files are untrusted data and are only read as text.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

SKILL = Path(__file__).resolve().parents[1]
CATALOG = SKILL / "assets" / "runner-requirements.yaml"
MAX_BYTES = 60 * 1024 * 1024
USER_AGENT = "riscv-board-onboarding/1 (documentation fetch)"
KINDS = ["board_manual", "soc_trm", "datasheet", "schematic", "boot_guide", "image_packaging",
         "bsp_source", "bootloader_source", "opensbi_source", "uboot_source", "device_tree",
         "boot_log", "other"]
# Tools Codex keeps off: it may only search the web and answer (same list as ci/triage).
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "code_mode_host", "browser_use", "browser_use_external",
    "computer_use", "in_app_browser", "apps", "plugins", "remote_plugin", "multi_agent",
    "image_generation", "hooks", "skill_mcp_dependency_install", "skill_search", "tool_suggest",
    "goals",
)
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["vendor_sites", "documents", "notes"],
    "properties": {
        "vendor_sites": {"type": "array", "items": {"type": "string"}},
        "documents": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "url", "kind", "publisher", "official", "revision", "covers", "why"],
            "properties": {
                "title": {"type": "string"},
                "url": {"type": "string"},
                "kind": {"type": "string", "enum": KINDS},
                "publisher": {"type": "string"},
                "official": {"type": "boolean"},
                "revision": {"type": "string"},
                "covers": {"type": "array", "items": {"type": "string"}},
                "why": {"type": "string"},
            },
        }},
        "notes": {"type": "string"},
    },
}
# Files in a vendor repository that hold runner facts.
REPO_FILES = re.compile(r"\.(dts|dtsi|its|its\.in|ld|lds|h|c|S|mk|cfg|conf|txt|md|rst)$|"
                        r"(^|/)(Kconfig|Makefile|objects\.mk|config\.mk|[^/]*defconfig)$")
REPO_LIMIT = 400_000  # characters gathered per repository
REPO_FILE_LIMIT = 300  # matching files fetched per repository
MAX_ASSETS = 4  # PDFs saved from one release or repository


def needs() -> str:
    """What the runner needs, by group, from the requirement catalog."""
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    by_group: dict[str, list[str]] = {}
    for req in catalog["requirements"]:
        if req["kind"] in ("fact",) and req["group"] not in ("bench", "act"):
            by_group.setdefault(req["group"], []).append(req["description"])
    lines = []
    for group, title in catalog["groups"].items():
        if group in by_group:
            lines.append(f"- {group} ({title}): " + " ".join(by_group[group]))
    return "\n".join(lines)


def prompt(board: str, soc: str, extra: str) -> str:
    return f"""Find the official documentation and source code for porting bare-metal firmware to this RISC-V board.

Board: {board}
SoC / model: {soc or "unknown (find it)"}
{extra}

The firmware is a small M-mode program that replaces the last M-mode boot stage (usually
OpenSBI) after the vendor's ROM/SPL has initialized DDR. It needs these facts:
{needs()}

Search the web and return the documents and repositories that state these facts:
- board user manual / hardware guide (boot switches, boot media, debug UART);
- SoC technical reference manual or datasheet (memory/address map, boot flow, UART, CLINT/ACLINT, PLIC, harts);
- boot, recovery and flashing guides; image packaging/signing tools;
- vendor source: bootloader/SPL, OpenSBI and U-Boot forks or platform code, image recipes
  (FIT .its, partition tables), and the board's device tree;
- stock boot logs if the vendor publishes them.

Rules:
- Only URLs you found in search results. Never construct or guess a URL. Prefer direct file links
  (PDF, raw source files) and the vendor's own sites and repositories; mark third-party
  pages official=false.
- One entry per document; for a repository give its URL with the branch or tag in `revision`.
- `covers`: which of the groups above it answers (boot, harts, uart, timer, interrupt, memory,
  platform, identity). `why`: one sentence on what it contains.
- Up to 25 documents, most useful first. Put anything you could not find in `notes`."""


def call_codex(text: str, codex_bin: str, model: str = "", timeout: int = 900) -> dict[str, Any]:
    """One Codex turn with live web search and no other tool; returns the schema answer."""
    codex = shutil.which(codex_bin)
    if not codex:
        raise RuntimeError(f"Codex CLI not found: {codex_bin}")
    with tempfile.TemporaryDirectory(prefix="board-docs-") as temporary:
        root = Path(temporary)
        work = root / "work"
        work.mkdir()
        schema = root / "schema.json"
        schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
        answer = root / "answer.json"
        command = [codex, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                   "--ignore-rules", "--sandbox", "read-only", "--color", "never", "-C", str(work),
                   "--output-schema", str(schema), "-o", str(answer), "-c", 'web_search="live"']
        for feature in CODEX_DISABLED_FEATURES:
            command += ["--disable", feature]
        if model:
            command += ["-m", model]
        command.append("-")
        process = subprocess.run(command, input=text, text=True, capture_output=True,
                                 timeout=timeout, cwd=work)
        body = answer.read_text(encoding="utf-8").strip() if answer.is_file() else ""
        if process.returncode != 0 or not body:
            raise RuntimeError(f"Codex failed (rc={process.returncode}): {(process.stderr or '')[-400:]}")
    return json.loads(body)


def raw_url(url: str) -> str:
    """GitHub/GitLab/Gitee file pages -> the raw file."""
    m = re.match(r"^https://github\.com/([^/]+/[^/]+)/blob/(.+)$", url)
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}"
    m = re.match(r"^(https://(?:gitlab\.com|gitee\.com)/.+?)/(?:-/)?blob/(.+)$", url)
    if m:
        return f"{m.group(1)}/raw/{m.group(2)}"
    return url


def repo_url(url: str) -> tuple[str, str] | None:
    """(clone URL, branch or '') when the link is a repository or a tree in one."""
    m = re.match(r"^https://(github\.com|gitlab\.com|gitee\.com)/([^/]+/[^/#?]+?)(?:\.git)?/?"
                 r"(?:(?:-/)?tree/([^?#]+))?$", url)
    if not m:
        return None
    branch = (m.group(3) or "").split("/")[0]
    return f"https://{m.group(1)}/{m.group(2)}.git", branch


def release_assets(url: str, get: Callable[[str], tuple[bytes, str]]) -> list[str]:
    """PDF assets of a GitHub releases page (they are not in its HTML), newest release first."""
    m = re.match(r"^https://github\.com/([^/]+/[^/]+)/releases(?:/tag/([^/?#]+)|/latest)?/?$", url)
    if not m:
        return []
    api = f"https://api.github.com/repos/{m.group(1)}/releases" + (f"/tags/{m.group(2)}" if m.group(2) else "")
    body, _kind = get(api)
    data = json.loads(body)
    releases = data if isinstance(data, list) else [data]
    for release in releases:
        pdfs = [a["browser_download_url"] for a in release.get("assets") or []
                if str(a.get("name", "")).lower().endswith(".pdf")]
        if pdfs:
            return pdfs[:MAX_ASSETS]
    return []


def fetch(url: str) -> tuple[bytes, str]:
    """(body, content type) of an http(s) URL, bounded."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs are fetched")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read(MAX_BYTES + 1)
        kind = response.headers.get("Content-Type", "")
    if len(body) > MAX_BYTES:
        raise ValueError(f"larger than {MAX_BYTES // (1024 * 1024)} MB")
    return body, kind


def html_text(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|pre)>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:60] or "document"


def save_document(index: int, doc: dict[str, Any], out: Path,
                  get: Callable[[str], tuple[bytes, str]]) -> dict[str, Any]:
    assets = release_assets(doc["url"].strip(), get)
    if assets:
        files = []
        for number, asset in enumerate(assets, start=1):
            body, _kind = get(asset)
            path = out / f"{index:02d}_{number}_{slug(Path(urllib.parse.urlparse(asset).path).stem)}.pdf"
            path.write_bytes(body)
            files.append(path.name)
        first = out / files[0]
        return {"file": files[0], "extra_files": files[1:], "assets": assets,
                "sha256": hashlib.sha256(first.read_bytes()).hexdigest(), "bytes": first.stat().st_size}
    url = raw_url(doc["url"].strip())
    body, kind = get(url)
    name = f"{index:02d}_{slug(doc.get('title') or url)}"
    if body[:5] == b"%PDF-" or "pdf" in kind:
        path, data = out / f"{name}.pdf", body
    elif "html" in kind or body.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        text = html_text(body)
        if len(text) < 200:
            raise ValueError("page has no readable text (needs a browser or login)")
        path, data = out / f"{name}.txt", (f"Source: {doc['url']}\n\n" + text).encode()
    else:
        suffix = Path(urllib.parse.urlparse(url).path).suffix
        if not re.match(r"^\.[A-Za-z0-9]{1,6}$", suffix):
            suffix = ".txt"
        path, data = out / f"{name}{suffix}", body
    path.write_bytes(data)
    return {"file": path.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def clone_repo(index: int, doc: dict[str, Any], out: Path, keywords: list[str]) -> dict[str, Any]:
    """Gather a repository's board-specific files into one text file. Only the file list is
    cloned (shallow, no file contents); then just the matching files are fetched, so a kernel
    or Buildroot tree costs megabytes, not gigabytes."""
    clone, branch = repo_url(doc["url"].strip()) or ("", "")
    branch = branch or doc.get("revision", "").strip()
    with tempfile.TemporaryDirectory(prefix="board-repo-") as temporary:
        target = Path(temporary) / "repo"
        env = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin", "HOME": temporary}

        def git(*args: str, timeout: int = 300) -> str:
            return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                                  timeout=timeout, env=env).stdout

        command = ["clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "--quiet"]
        if branch and re.match(r"^[\w./-]+$", branch):
            command += ["--branch", branch]
        git(*command, clone, str(target))
        commit = git("-C", str(target), "rev-parse", "HEAD").strip()
        tree = git("-C", str(target), "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        names = [n for n in tree if REPO_FILES.search(n) and any(k in n.lower() for k in keywords)][:REPO_FILE_LIMIT]
        # Binary documents: the repository's PDFs (a TRM repository) and the board's device trees.
        useful = re.compile(r"schemat|manual|trm|reference|user.?guide|hardware|datasheet", re.I)
        pdfs = sorted((n for n in tree if n.lower().endswith(".pdf")),
                      key=lambda n: (not any(k in n.lower() for k in keywords), not useful.search(n), n))
        binaries = (pdfs[:MAX_ASSETS]
                    + [n for n in tree if n.lower().endswith(".dtb") and any(k in n.lower() for k in keywords)][:2])
        if not names and not binaries:
            raise ValueError(f"no files naming {', '.join(keywords)}")
        fetched = []
        for start in range(0, len(names) + len(binaries), 50):  # only these blobs are downloaded
            batch = (names + binaries)[start:start + 50]
            try:
                git("-C", str(target), "checkout", "HEAD", "--", *batch)
                fetched += batch
            except subprocess.CalledProcessError:
                continue  # an unreadable path costs its batch, not the repository
        extra = []
        for name in (b for b in binaries if b in fetched):
            data = (target / name).read_bytes()
            if len(data) <= MAX_BYTES:
                dest = out / f"{index:02d}_{slug(Path(name).stem)}{Path(name).suffix.lower()}"
                dest.write_bytes(data)
                extra.append(dest.name)
        parts, size = [], 0
        for name in (n for n in names if n in fetched):
            text = (target / name).read_text(encoding="utf-8", errors="replace")
            block = f"\n===== {clone} @ {commit[:12]} : {name} =====\n{text}\n"
            if size + len(block) > REPO_LIMIT:
                break
            parts.append(block)
            size += len(block)
    if not parts and not extra:
        raise ValueError(f"no readable files naming {', '.join(keywords)}")
    if not parts:
        first = out / extra[0]
        return {"file": extra[0], "extra_files": extra[1:], "commit": commit, "files": 0,
                "sha256": hashlib.sha256(first.read_bytes()).hexdigest(), "bytes": first.stat().st_size}
    path = out / f"{index:02d}_{slug(doc.get('title') or clone)}_repo.txt"
    path.write_text(f"Source: {clone} @ {commit}\n" + "".join(parts), encoding="utf-8")
    return {"file": path.name, "extra_files": extra, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size, "commit": commit, "files": len(parts)}


def keywords_for(board: str, soc: str, board_id: str) -> list[str]:
    words = set(re.findall(r"[a-z0-9]{3,}", f"{board} {soc} {board_id}".lower()))
    # A part number's family: eic7700x -> eic7700 (files are often named after the family).
    words |= {m.group(1) for w in words if (m := re.match(r"^([a-z]+\d{3,})[a-z]{1,2}$", w))}
    return sorted(w for w in words if w not in {"the", "board", "soc", "risc", "and"})


def collect(found: dict[str, Any], out: Path, args: argparse.Namespace,
            get: Callable[[str], tuple[bytes, str]] = fetch) -> list[dict[str, Any]]:
    out.mkdir(parents=True, exist_ok=True)
    keywords = keywords_for(args.board, args.soc, args.board_id)
    records, seen = [], set()
    for index, doc in enumerate(found.get("documents", [])[: args.max_docs], start=1):
        url = str(doc.get("url", "")).strip()
        record = {k: doc.get(k) for k in ("title", "url", "kind", "publisher", "official", "revision",
                                          "covers", "why")}
        if not url or url in seen:
            continue
        seen.add(url)
        print(f"  {index:2d}. {doc.get('kind')}: {url}", flush=True)
        try:
            if repo_url(url):
                if not args.clone_repos:
                    record.update(status="repository", note="rerun with --clone-repos to gather its files")
                else:
                    record.update(status="saved", **clone_repo(index, doc, out, keywords))
            else:
                record.update(status="saved", **save_document(index, doc, out, get))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            record.update(status="rejected", note=f"does not resolve: {exc}")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            record.update(status="failed", note=str(exc)[:300])
        records.append(record)
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", required=True, help='board name, e.g. "Milk-V Megrez"')
    ap.add_argument("--soc", default="", help='SoC or model, e.g. "ESWIN EIC7700X"')
    ap.add_argument("--board-id", required=True)
    ap.add_argument("--hint", default="", help="extra context for the search (vendor site, revision)")
    ap.add_argument("--out-dir", type=Path, help="default artifacts/onboarding/<board-id>/docs")
    ap.add_argument("--codex-bin", default="codex")
    ap.add_argument("--model", default="")
    ap.add_argument("--max-docs", type=int, default=25)
    ap.add_argument("--clone-repos", action="store_true", help="shallow-clone repository links")
    ap.add_argument("--from-json", type=Path, help="reuse a saved search answer (no Codex call)")
    ap.add_argument("--extract", action="store_true",
                    help="then run fill_gaps.py --backend codex on the saved documents")
    ap.add_argument("--coverage", type=Path, help="coverage.yaml for --extract (its open items)")
    args = ap.parse_args()
    out = args.out_dir or Path("artifacts/onboarding") / args.board_id / "docs"
    out.mkdir(parents=True, exist_ok=True)
    if not args.from_json:
        print("Searching with Codex (a few minutes)...", flush=True)
    try:
        found = (json.loads(args.from_json.read_text(encoding="utf-8")) if args.from_json
                 else call_codex(prompt(args.board, args.soc, args.hint), args.codex_bin, args.model))
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    (out / "search_answer.json").write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8")
    print(f"{len(found.get('documents', []))} links found (saved in search_answer.json; "
          "rerun with --from-json to skip the search). Fetching:", flush=True)
    records = collect(found, out, args)
    sources = {"board": args.board, "soc": args.soc, "board_id": args.board_id,
               "searched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "vendor_sites": found.get("vendor_sites", []), "notes": found.get("notes", ""),
               "documents": records}
    (out / "sources.yaml").write_text(yaml.safe_dump(sources, sort_keys=False, allow_unicode=True),
                                      encoding="utf-8")
    saved = [out / r["file"] for r in records if r.get("status") == "saved"]
    counts = {s: sum(1 for r in records if r["status"] == s) for s in ("saved", "repository", "rejected", "failed")}
    print(f"{out}: " + " ".join(f"{k}={v}" for k, v in counts.items()))
    for r in records:
        print(f"  [{r['status']}] {r.get('kind')}: {r.get('title')} <{r.get('url')}>"
              + (f" ({r['note']})" if r.get("note") else ""))
    if not saved:
        return 1
    command = [sys.executable, str(Path(__file__).with_name("fill_gaps.py")), "--backend", "codex",
               "--board-id", args.board_id, "--out-dir", str(out.parent / "fill_gaps"), "--docs",
               *map(str, saved)] + (["--coverage", str(args.coverage)] if args.coverage else [])
    if not args.extract:
        print("Next, extract the facts:\n  " + " ".join(command))
        return 0
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
