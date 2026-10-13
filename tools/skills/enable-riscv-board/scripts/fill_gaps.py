#!/usr/bin/env python3
"""Answer open runner requirements from the user's documents with a bounded model call.

Token-efficient by construction:
  1. Documents are converted to text locally (pdftotext) and cached by sha256.
  2. For each open requirement, local BM25 search (catalog `search` terms +
     description) picks the top-k excerpts. Only those excerpts are sent.
  3. Requirements are batched into one call each, with a fixed system prompt
     (prompt-cached), a forced tool call for structured output, and a small
     max_tokens.
  4. Code verifies every answer: the quote must appear verbatim in the cited
     excerpt and the value must appear in the quote (or be marked derived).
     Unverifiable answers are rejected, never used.
  5. Items the cheap model cannot answer may be retried once with a stronger
     model (--escalate-model) and twice the excerpts.

Outputs (in --out-dir): research reports per role in the format
resolve_requirements.py --research reads (evidence_status `partial` when the
quote holds the value, `inferred` when derived; never `confirmed`, which needs
a person or hardware), fill_gaps.md (answers, quotes, rejections, token use)
and fill_gaps.json (machine-readable).

Modes: --dry-run builds the prompts and reports retrieval and estimated tokens
without calling the API. --eval <answers.yaml> measures whether the known
answer appears in the retrieved excerpts (retrieval recall), offline.
Backends: claude-cli (default) runs one headless Claude Code turn per batch on the
user's Claude subscription (no tools, no saved session, JSON-schema output);
codex runs the logged-in Codex CLI the same way (every tool off, CODEX_BIN);
api uses ANTHROPIC_API_KEY, which is never printed or written.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.request

import yaml

SKILL_DIR = Path(__file__).resolve().parent.parent
CATALOG = SKILL_DIR / "assets" / "runner-requirements.yaml"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
ESCALATE_MODEL = "claude-sonnet-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
CHUNK_CHARS = 1600
CHUNK_OVERLAP = 200
EXCERPT_CHARS = 1200
GROUP_ROLE = {"boot": "boot_image", "identity": "boot_image", "harts": "runner_contract", "uart": "runner_contract",
              "timer": "runner_contract", "interrupt": "runner_contract", "platform": "runner_contract",
              "memory": "ddr_placement", "act": "runner_contract"}
STOP = set("the a an of and or to in for on by is are be at as with from that this it its which used use uses "
           "only if must when how what where per e g".split())

SYSTEM_PROMPT = """You extract hardware facts for a bare-metal RISC-V firmware port from document excerpts.

Rules:
- Use only the excerpts in the message. Never use general knowledge, typical values or other boards.
- For every requirement call report_findings with one finding.
- found=true only when an excerpt states the value. Copy `quote` verbatim from that excerpt
  (one contiguous span, at most 300 characters, keep the original spelling and numbers) and give
  exactly one excerpt_id (the text inside the brackets, e.g. Part1.pdf#p12.0).
- Bitmask items (marked "answer as: bit list"): put the writable/implemented bit numbers in `bits`,
  quote the table rows that list them (joined with " | "), and set value to null; code computes the mask.
  A table may continue on the next excerpt: then give both ids in excerpt_id, comma-separated.
- Tables: quote whole rows exactly as printed; several rows from the same excerpt may be joined
  with " | ". Never reformat or summarize a quote.
- Underscores and spaces inside numbers are digit separators: 0x00_8000_0000 is 0x80000000.
- Ranges: "0xBASE-0xLAST". Use the "Already known" facts to tell instances apart (e.g. which UART is
  the console); they are context, not answers.
- If the value is computed from quoted numbers, put the arithmetic in `derivation` using those
  numbers, e.g. "0x02000000 + 0x4000"; otherwise leave derivation empty. Unit conversions
  (200 MHz, 64 KiB) need no derivation.
- Values: addresses and sizes as 0x-prefixed hex, frequencies in Hz as decimal integers,
  counts as decimal integers, text values as short phrases.
- found=false when the excerpts do not state it; say in `missing` what document section would.
- confidence: high only when the quote names the exact item (same device instance, e.g. UART0 not UART1)."""

TOOL = {
    "name": "report_findings",
    "description": "Report one finding per requirement.",
    "input_schema": {
        "type": "object",
        "properties": {"findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "found": {"type": "boolean"},
                "value": {"type": ["string", "integer", "null"]},
                "quote": {"type": "string"},
                "excerpt_id": {"type": "string"},
                "bits": {"type": "array", "items": {"type": "integer"}},
                "derivation": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "missing": {"type": "string"},
            },
            "required": ["id", "found"],
        }}},
        "required": ["findings"],
    },
}


# ------------------------------------------------------------------ documents

@dataclass
class Chunk:
    cid: str
    doc: str
    page: int
    text: str
    tokens: list[str] = field(default_factory=list)


def tokenize(text: str) -> list[str]:
    out = []
    for t in re.findall(r"0x[0-9a-f_]+|[a-z][a-z0-9_]*|\d+", text.lower()):
        if t in STOP:
            continue
        out.append(t)
        if "_" in t and not t.startswith("0x"):
            out += [p for p in t.split("_") if p and p not in STOP]   # lsp_uart0_intr -> uart0, intr
    return out


def doc_pages(path: Path, cache: Path) -> list[str]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cached = cache / f"{digest}.json"
    if cached.is_file():
        return json.loads(cached.read_text())
    if path.suffix.lower() == ".pdf":
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True)
        pages = out.stdout.split("\f")
    else:
        text = path.read_text(errors="replace")
        pages = [text[i:i + 6000] for i in range(0, max(len(text), 1), 6000)]
    cache.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(pages))
    return pages


def build_chunks(docs: list[Path], cache: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for d in docs:
        for pno, page in enumerate(doc_pages(d, cache), start=1):
            page = re.sub(r"[ \t]+", " ", page).strip()
            if not page:
                continue
            for start in range(0, len(page), CHUNK_CHARS - CHUNK_OVERLAP):
                text = page[start:start + CHUNK_CHARS]
                c = Chunk(f"{d.name}#p{pno}.{start // (CHUNK_CHARS - CHUNK_OVERLAP)}", d.name, pno, text)
                c.tokens = tokenize(text)
                chunks.append(c)
                if start + CHUNK_CHARS >= len(page):
                    break
    return chunks


class BM25:
    def __init__(self, chunks: list[Chunk], k1: float = 1.4, b: float = 0.75) -> None:
        self.chunks, self.k1, self.b = chunks, k1, b
        self.tf = [Counter(c.tokens) for c in chunks]
        self.avg = sum(len(c.tokens) for c in chunks) / max(len(chunks), 1)
        df: Counter = Counter()
        for tf in self.tf:
            df.update(tf.keys())
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def top(self, query: list[str], k: int, anchor: set[str] | None = None) -> list[Chunk]:
        q = Counter(query)
        scored = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            dl = len(self.chunks[i].tokens)
            for t, w in q.items():
                if t in tf:
                    f = tf[t]
                    s += w * self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
            if s > 0:
                # Facts live next to numbers: prefer chunks with hex addresses or values.
                if re.search(r"0x[0-9a-f]{4,}|\d+\s*[mk]hz", self.chunks[i].text, re.I):
                    s *= 1.15
                # The item's subject (first catalog search term, e.g. UART) must be on the page.
                if anchor and not anchor & tf.keys():
                    s *= 0.3
                scored.append((s, i))
        scored.sort(reverse=True)
        return [self.chunks[i] for _, i in scored[:k]]


def anchor_for(req: dict) -> set[str]:
    """Subject tokens from the first catalog search term; 'A | B' lists alternatives."""
    terms = req.get("search") or []
    return set(tokenize(terms[0].replace("|", " "))) if terms else set()


def top_for(index: "BM25", req: dict, k: int) -> list[Chunk]:
    if req.get("value_kind") != "bitmask":
        return index.top(query_for(req), k, anchor_for(req))
    # Bit tables run across chunk and page breaks: wider search plus each hit's continuation.
    hits = index.top(query_for(req), 2 * k, anchor_for(req))
    pos = {c.cid: i for i, c in enumerate(index.chunks)}
    out: list[Chunk] = []
    for c in hits[:k]:
        for n in (c, index.chunks[pos[c.cid] + 1] if pos[c.cid] + 1 < len(index.chunks) else None):
            if n is not None and n not in out:
                out.append(n)
    return out[:2 * k]


def query_for(req: dict) -> list[str]:
    terms = " ".join(req.get("search") or []) + " " + req["description"] + " " + req["id"].replace(".", " ")
    toks = tokenize(terms)
    return toks + tokenize(" ".join(req.get("search") or []))   # search terms count twice


# ------------------------------------------------------------------ verification

def norm(s: str) -> str:
    """Compare text the way a reader would: PDF extraction adds spaces after hyphens and varies quotes/dashes."""
    s = s.translate(str.maketrans({"\u2010": "-", "\u2011": "-", "\u2013": "-", "\u2014": "-", "\u2018": "'",
                                   "\u2019": "'", "\u201c": '"', "\u201d": '"', "\uff0c": ",", "\u00a0": " "}))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return re.sub(r"\s*-\s*", "-", s)


def numbers_in(text: str) -> set[int]:
    vals: set[int] = set()
    for m in re.finditer(r"0x[0-9a-f_]+|\b\d[\d_,]*(?:\.\d+)?\s*(?:[kmg]hz|[kmg]i?b)?\b", text.lower()):
        tok = m.group(0).replace("_", "").replace(",", "")
        try:
            if tok.startswith("0x"):
                vals.add(int(tok, 16))
                continue
            num_m = re.match(r"([\d.]+)\s*([kmg])?(hz|i?b)?", tok)
            base = float(num_m.group(1))
            scale = {"k": 1e3, "m": 1e6, "g": 1e9}.get(num_m.group(2) or "", 1)
            if num_m.group(3) and "b" in num_m.group(3):
                scale = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}.get(num_m.group(2) or "", 1)
            vals.add(int(base * scale))
            if scale == 1:
                vals.add(int(base))
        except (ValueError, AttributeError):
            continue
    return vals


def as_number(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"\s*(0x[0-9a-fA-F_]+|\d+)\s*", v):
        return int(v.replace("_", ""), 0)
    return None


def check_derivation(derivation: str, value: int, quoted: set[int]) -> bool:
    """Accept only arithmetic whose operands are quoted numbers and whose result is the value."""
    import ast
    import operator
    expr = derivation.split("=")[0]
    expr = re.sub(r"(?<=[0-9a-fA-F])_(?=[0-9a-fA-F])", "", expr)
    if not re.fullmatch(r"[\s0-9a-fA-FxX+\-*/()<]+", expr):
        return False
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.LShift: operator.lshift,
           ast.FloorDiv: operator.floordiv, ast.Div: operator.floordiv}
    operands: list[int] = []

    def ev(node: ast.AST) -> int:
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            operands.append(node.value)
            return node.value
        raise ValueError("unsupported expression")
    try:
        result = ev(ast.parse(expr.strip(), mode="eval").body)
    except (SyntaxError, ValueError, ZeroDivisionError):
        return False
    big = [o for o in operands if o > 16]          # small factors (4, 8, 1 << n) need not be quoted
    return result == value and bool(big) and all(o in quoted for o in big)


def cited(f: dict, excerpts: dict[str, Chunk]) -> list[Chunk]:
    ids = [i.strip().strip("[]").strip() for i in str(f.get("excerpt_id", "")).split(",")]
    return [excerpts[i] for i in ids if i in excerpts]


def verify(f: dict, excerpts: dict[str, Chunk], req: dict | None = None) -> tuple[str, str]:
    """Return (status, reason): status partial | inferred | rejected | not_found. May set f['value']."""
    req = req or {}
    if not f.get("found"):
        return "not_found", f.get("missing") or "not stated in the excerpts"
    chunks = cited(f, excerpts)
    if not chunks:
        return "rejected", f"unknown excerpt_id {f.get('excerpt_id')!r}"
    quote = f.get("quote") or ""
    spans = [s for s in quote.split(" | ") if s.strip()]          # several table rows may be quoted
    if not spans or any(len(norm(s)) < 4 or not any(norm(s) in norm(c.text) for c in chunks) for s in spans):
        return "rejected", "quote is not verbatim in the cited excerpt(s)"
    terms = req.get("quote_terms") or []
    if terms and not any(norm(term) in norm(quote) for term in terms):
        return "rejected", f"quote does not mention what was asked ({', '.join(terms)})"
    if req.get("value_kind") == "bitmask":
        bits = f.get("bits") or []
        quoted = numbers_in(quote)
        if not bits or any(not isinstance(b, int) or b < 0 or b > 63 or b not in quoted for b in bits):
            return "rejected", "bit list empty or has bits not in the quoted rows"
        rows = [norm(s) for s in spans]
        unproven = [b for b in bits if not any(re.match(rf"{b}\b", r) and re.search(r"\brw\b", r) for r in rows)]
        if unproven:
            return "rejected", f"bits {unproven} have no quoted row that starts with the bit and says RW"
        f["value"] = hex(sum(1 << b for b in set(bits)))
        # Every listed bit is proven; that none is missing is not (tables span pages): probe to confirm.
        return "inferred", f"mask from {len(set(bits))} quoted RW rows; completeness unverified (confirm with csr_probe.py)"
    value = f.get("value")
    n = as_number(value)
    quoted = numbers_in(quote)
    if n is not None:
        in_quote = n in quoted
    elif isinstance(value, str) and re.search(r"0x[0-9a-f]|\d", value, re.I) and numbers_in(value):
        # Ranges and lists: every number in the value must be in the quote; no derivation accepted.
        if numbers_in(value) <= quoted:
            return "partial", "every number in the value appears in the verified quote"
        return "rejected", "range/list value has numbers that are not in the quote"
    else:
        in_quote = value is not None and norm(str(value)) in norm(quote)
    if in_quote:
        return "partial", "value appears in the verified quote"
    if f.get("derivation"):
        if n is not None and check_derivation(str(f["derivation"]), n, quoted):
            return "inferred", f"derived and checked: {f['derivation']}"
        return "rejected", f"derivation does not reproduce the value from quoted numbers: {f['derivation']}"
    return "rejected", "value does not appear in the quote and no derivation was given"


# ------------------------------------------------------------------ model call

def call_anthropic(model: str, system: str, user: str, max_tokens: int) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (use --dry-run to work without it)")
    body = {
        "model": model, "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "tools": [TOOL], "tool_choice": {"type": "tool", "name": TOOL["name"]},
        "messages": [{"role": "user", "content": user}],
    }
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(2 ** attempt * 5)
                continue
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"API error {exc.code}: {detail}") from None
    raise RuntimeError("API retries exhausted")


def find_claude_cli() -> str | None:
    """Claude Code CLI: $CLAUDE_CLI, `claude` on PATH, or the newest VS Code extension binary."""
    import shutil
    if os.environ.get("CLAUDE_CLI"):
        return os.environ["CLAUDE_CLI"]
    if shutil.which("claude"):
        return shutil.which("claude")
    found = sorted(Path.home().glob(".vscode*/extensions/anthropic.claude-code-*/resources/native-binary/claude"))
    return str(found[-1]) if found else None


def call_claude_cli(model: str, system: str, user: str, max_tokens: int) -> dict:
    """One headless Claude Code turn on the user's Claude subscription; no tools, no session, schema output."""
    cli = find_claude_cli()
    if not cli:
        raise RuntimeError("Claude Code CLI not found (set CLAUDE_CLI, or use --backend api with ANTHROPIC_API_KEY)")
    cmd = [cli, "-p", "--model", model, "--effort", os.environ.get("FILL_GAPS_EFFORT", "low"),
           "--system-prompt", system, "--tools", "",
           # Extraction from short excerpts does not need extended thinking (it was ~85% of output tokens).
           "--settings", json.dumps({"alwaysThinkingEnabled": os.environ.get("FILL_GAPS_THINKING") == "1"}),
           "--json-schema", json.dumps(TOOL["input_schema"]), "--output-format", "json",
           "--no-session-persistence", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands"]
    out = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=600)
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude CLI failed (exit {out.returncode}): {(out.stderr or out.stdout)[:300]}") from None
    if data.get("is_error") or data.get("structured_output") is None:
        raise RuntimeError(f"Claude CLI returned no structured output: {str(data.get('result'))[:300]}")
    usage = data.get("usage", {})
    return {"content": [{"type": "tool_use", "name": TOOL["name"], "input": data["structured_output"]}],
            "usage": {k: usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                   "cache_creation_input_tokens")},
            "cost_usd_list_price": data.get("total_cost_usd")}


def strict_schema(schema: dict) -> dict:
    """The tool schema in the strict form Codex's --output-schema needs: every property required
    (optional ones nullable) and no extra properties."""
    out = dict(schema)
    if out.get("type") == "object":
        props = {}
        for name, sub in out.get("properties", {}).items():
            sub = strict_schema(sub)
            if name not in out.get("required", []):
                kinds = sub.get("type")
                sub["type"] = sorted(set(kinds if isinstance(kinds, list) else [kinds]) | {"null"})
                if "enum" in sub:
                    sub["enum"] = [*sub["enum"], None]
            props[name] = sub
        out.update(properties=props, required=list(props), additionalProperties=False)
    if out.get("type") == "array" or "items" in out:
        out["items"] = strict_schema(out["items"])
    return out


def call_codex(model: str, system: str, user: str, max_tokens: int) -> dict:
    """One Codex CLI turn (logged in with a ChatGPT account) with every tool off, read-only in an
    empty directory; the excerpts go in on stdin, the findings come back as schema JSON."""
    import shutil
    import tempfile
    codex = shutil.which(os.environ.get("CODEX_BIN", "codex"))
    if not codex:
        raise RuntimeError("Codex CLI not found (set CODEX_BIN)")
    disabled = ("shell_tool", "unified_exec", "code_mode_host", "browser_use", "browser_use_external",
                "computer_use", "in_app_browser", "apps", "plugins", "remote_plugin", "multi_agent",
                "image_generation", "hooks", "skill_mcp_dependency_install", "skill_search",
                "tool_suggest", "goals")
    with tempfile.TemporaryDirectory(prefix="fill-gaps-") as temporary:
        root = Path(temporary)
        (root / "work").mkdir()
        (root / "schema.json").write_text(json.dumps(strict_schema(TOOL["input_schema"])))
        cmd = [codex, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
               "--ignore-rules", "--sandbox", "read-only", "--color", "never", "-C", str(root / "work"),
               "--output-schema", str(root / "schema.json"), "-o", str(root / "answer.json"),
               "-c", 'web_search="disabled"']
        for feature in disabled:
            cmd += ["--disable", feature]
        if model and model != "default":
            cmd += ["-m", model]
        out = subprocess.run(cmd + ["-"], input=f"{system}\n\n{user}", capture_output=True, text=True,
                             timeout=900, cwd=root / "work")
        answer = root / "answer.json"
        if out.returncode != 0 or not answer.is_file():
            raise RuntimeError(f"Codex failed (exit {out.returncode}): {(out.stderr or '')[-300:]}")
        data = json.loads(answer.read_text())
    for finding in data.get("findings", []):
        for key in [k for k, v in finding.items() if v is None]:
            del finding[key]  # strict-mode nulls mean "not given"
    return {"content": [{"type": "tool_use", "name": TOOL["name"], "input": data}],
            "usage": {"input_tokens": len(system + user) // 4, "output_tokens": 0}}


BACKENDS = {"claude-cli": call_claude_cli, "api": call_anthropic, "codex": call_codex}
MODEL_DEFAULTS = {"claude-cli": ("haiku", "sonnet"), "api": (DEFAULT_MODEL, ESCALATE_MODEL),
                  "codex": ("default", "default")}


def build_user_message(items: list[dict], excerpts: dict[str, Chunk], known: dict[str, Any] | None = None) -> str:
    lines = []
    if known:
        lines.append("Already known (context):")
        lines += [f"- {k} = {v}" for k, v in known.items()]
        lines.append("")
    lines.append("Requirements:")
    for r in items:
        lines.append(f"- id: {r['id']}\n  meaning: {r['description']}\n  goes into: {r['consumer']}"
                     + ("\n  answer as: bit list" if r.get("value_kind") == "bitmask" else ""))
    lines.append("\nExcerpts:")
    for cid, c in excerpts.items():
        lines.append(f"[{cid}] ({c.doc}, page {c.page})\n{c.text[:EXCERPT_CHARS]}\n")
    return "\n".join(lines)


def findings_from(response: dict) -> list[dict]:
    for block in response.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == TOOL["name"]:
            return block["input"].get("findings", [])
    return []


# ------------------------------------------------------------------ driver

def open_items(coverage: dict, catalog: dict, only: list[str] | None) -> list[dict]:
    reqs = {r["id"]: r for r in catalog["requirements"]}
    out = []
    for rid, rec in coverage["requirements"].items():
        r = reqs.get(rid)
        if not r or not any(isinstance(s, dict) and "document" in s for s in r["resolve"]):
            continue
        if only and rid not in only:
            continue
        if only or rec.get("status") in ("open", "conflict"):
            out.append(r)
    return out


def run(args: argparse.Namespace, call: Callable[[str, str, str, int], dict] = call_anthropic) -> dict:
    catalog = yaml.safe_load(CATALOG.read_text())
    coverage = yaml.safe_load(args.coverage.read_text()) if args.coverage else {"requirements": {
        r["id"]: {"status": "open"} for r in catalog["requirements"]}}
    items = open_items(coverage, catalog, args.ids)
    chunks = build_chunks(args.docs, args.cache_dir)
    index = BM25(chunks)
    answers = yaml.safe_load(args.eval.read_text()) if args.eval else {}

    result: dict[str, Any] = {"board_id": coverage.get("board_id", args.board_id), "docs": [str(d) for d in args.docs],
                              "chunks": len(chunks), "items": {}, "calls": [], "tokens": Counter(),
                              "retrieval": {}}
    per_item_excerpts = {r["id"]: top_for(index, r, args.top_k) for r in items}

    # Retrieval recall against known answers (offline quality metric).
    for rid, ans in answers.items():
        if rid not in per_item_excerpts:
            continue
        want = as_number(ans)
        hit = any((want is not None and want in numbers_in(c.text)) or
                  (want is None and norm(str(ans)) in norm(c.text)) for c in per_item_excerpts[rid])
        result["retrieval"][rid] = hit

    def ask(batch: list[dict], model: str, k: int) -> None:
        excerpts: dict[str, Chunk] = {}
        for r in batch:
            for c in (per_item_excerpts[r["id"]] if k == args.top_k else top_for(index, r, k)):
                excerpts.setdefault(c.cid, c)
        groups = {r["group"] for r in batch}
        known = {rid: rec["value"] for rid, rec in coverage["requirements"].items()
                 if rec.get("status") == "resolved" and rid.split(".")[0] in groups
                 and rid not in {r["id"] for r in batch} and len(str(rec.get("value"))) <= 120}
        user = build_user_message(batch, excerpts, known)
        est = (len(SYSTEM_PROMPT) + len(json.dumps(TOOL)) + len(user)) // 3
        call_rec = {"model": model, "items": [r["id"] for r in batch], "excerpts": len(excerpts),
                    "estimated_input_tokens": est}
        result["calls"].append(call_rec)
        if args.dry_run:
            result["tokens"]["estimated_input"] += est
            if args.show_prompts:
                print(f"----- {model} -----\n{user}\n")
            return
        if sum(result["tokens"].values()) + est > args.budget_tokens:
            for r in batch:
                result["items"].setdefault(r["id"], {"status": "skipped", "reason": "token budget reached"})
            return
        resp = call(model, SYSTEM_PROMPT, user, 300 * len(batch) + 200)
        usage = resp.get("usage", {})
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            result["tokens"][key] += usage.get(key, 0) or 0
        call_rec["usage"] = usage
        if resp.get("cost_usd_list_price") is not None:
            result["tokens"]["cost_usd_list_price_x1e6"] += int(resp["cost_usd_list_price"] * 1e6)
        got = {f.get("id"): f for f in findings_from(resp)}
        for r in batch:
            f = got.get(r["id"], {"id": r["id"], "found": False, "missing": "no finding returned"})
            status, reason = verify(f, excerpts, r)
            chunks = cited(f, excerpts)
            chunk = chunks[0] if chunks else None
            prev = result["items"].get(r["id"])
            if prev and prev["status"] in ("partial", "inferred"):
                continue
            result["items"][r["id"]] = {
                "status": status, "reason": reason, "model": model, "value": f.get("value"),
                "quote": f.get("quote"), "confidence": f.get("confidence"),
                "source": ", ".join(f"{c.doc} page {c.page}" for c in chunks) or None, "group": r["group"],
                "description": r["description"]}

    for i in range(0, len(items), args.batch):
        ask(items[i:i + args.batch], args.model, args.top_k)
    if args.escalate_model and not args.dry_run:
        retry = [r for r in items if result["items"].get(r["id"], {}).get("status") in ("not_found", "rejected")
                 or result["items"].get(r["id"], {}).get("confidence") == "low"]
        for i in range(0, len(retry), args.batch):
            ask(retry[i:i + args.batch], args.escalate_model, args.top_k * 2)
    result["tokens"] = dict(result["tokens"])
    return result


def write_outputs(result: dict, out_dir: Path, catalog: dict) -> None:
    from validate_research_report import ROLE_FACT_IDS
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "fill_gaps.json").write_text(json.dumps(result, indent=2, default=str))
    sources = [{"name": Path(d).name, "url_or_path": d, "revision": None, "source_type": "vendor_documentation"}
               for d in result["docs"]]
    by_role: dict[str, list[dict]] = {}
    for rid, it in result["items"].items():
        if it["status"] not in ("partial", "inferred"):
            continue
        by_role.setdefault(GROUP_ROLE[it["group"]], []).append({
            "id": rid, "claim": it["description"], "value": it["value"],
            "source": f"{it['source']}: \"{it['quote']}\"", "revision": None, "evidence_status": it["status"],
            "impact": f"model-extracted by fill_gaps.py ({it['model']}); quote verified in code, review before use",
            "open_question": None})
    for role, records in by_role.items():
        have = {r["id"] for r in records}
        for rid in sorted(ROLE_FACT_IDS.get(role, set()) - have):
            records.append({"id": rid, "claim": "not researched by fill_gaps.py", "value": None,
                            "source": "fill_gaps.py", "revision": None, "evidence_status": "unknown",
                            "impact": "unchanged", "open_question": None})
        report = {"schema_version": 2, "board_id": result["board_id"], "agent_role": role,
                  "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "source_revisions": sources, "evidence": records, "blockers": [], "conflicts": [],
                  "recommendations": []}
        (out_dir / f"fill_gaps_{role}.yaml").write_text(yaml.safe_dump(report, sort_keys=False, width=200))
    md = [f"# fill_gaps: {result['board_id']}", "",
          f"Documents: {', '.join(Path(d).name for d in result['docs'])} ({result['chunks']} chunks). "
          f"Calls: {len(result['calls'])}. Tokens: {result['tokens']}.", ""]
    if result["retrieval"]:
        hits = sum(result["retrieval"].values())
        md += [f"Retrieval recall: {hits}/{len(result['retrieval'])} known answers present in the excerpts.", ""]
        md += [f"- {'hit ' if v else 'MISS'} `{k}`" for k, v in sorted(result["retrieval"].items())] + [""]
    if result["items"]:
        md += ["| Item | Status | Value | Source | Quote / reason |", "|---|---|---|---|---|"]
        for rid, it in sorted(result["items"].items()):
            q = (it.get("quote") or "").replace("|", "/").replace("\n", " ")[:120]
            md.append(f"| `{rid}` | {it['status']} | {it.get('value')} | {it.get('source') or ''} | "
                      f"{q if it['status'] in ('partial', 'inferred') else it.get('reason')} |")
    (out_dir / "fill_gaps.md").write_text("\n".join(md) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coverage", type=Path, help="coverage.yaml; its open/conflict items are researched")
    ap.add_argument("--ids", nargs="*", help="research only these requirement IDs (any status)")
    ap.add_argument("--docs", nargs="+", type=Path, required=True, help="user documents (PDF, text, DTS, logs)")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--board-id", default="unknown")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "enable-riscv-board" / "docs")
    ap.add_argument("--backend", choices=sorted(BACKENDS),
                    help="claude-cli: your Claude subscription through Claude Code (default unless "
                         "ANTHROPIC_API_KEY is set); codex: the logged-in Codex CLI; api: Anthropic API key")
    ap.add_argument("--model", help="default: haiku (claude-cli) or " + DEFAULT_MODEL + " (api)")
    ap.add_argument("--escalate-model", default=None, help="retry unanswered items once, e.g. sonnet")
    ap.add_argument("--top-k", type=int, default=4)
    ap.add_argument("--batch", type=int, default=6, help="requirements per model call")
    ap.add_argument("--budget-tokens", type=int, default=150000, help="stop calling the model above this")
    ap.add_argument("--dry-run", action="store_true", help="no API call: retrieval and token estimate only")
    ap.add_argument("--show-prompts", action="store_true")
    ap.add_argument("--eval", type=Path, help="YAML {requirement id: known value} to measure retrieval recall")
    args = ap.parse_args()
    for d in args.docs:
        if not d.is_file():
            print(f"error: no such document {d}", file=sys.stderr)
            return 1
    if not args.backend:
        args.backend = "api" if os.environ.get("ANTHROPIC_API_KEY") else "claude-cli"
    args.model = args.model or MODEL_DEFAULTS[args.backend][0]
    try:
        result = run(args, call=BACKENDS[args.backend])
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    write_outputs(result, args.out_dir, yaml.safe_load(CATALOG.read_text()))
    found = sum(1 for it in result["items"].values() if it["status"] in ("partial", "inferred"))
    print(f"{args.out_dir}: requirements={sum(len(c['items']) for c in result['calls'])} answered={found} "
          f"calls={len(result['calls'])} tokens={result['tokens']}"
          + (f" retrieval={sum(result['retrieval'].values())}/{len(result['retrieval'])}" if result["retrieval"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
