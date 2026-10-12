"""Grounding for the triage agent's AI question, each part small and best effort.

  test_source   the test's own code around the failing check (addr2line on the failing PC
                in the ELF, or the file:line a damo assertion prints)
  norms         damo-rv-priv-ats requirement IDs ("norm:H_mtval_nrz") resolved to the exact
                spec sentence from the suite's NORM/ tables
  spec_passages ratified privileged-spec text (knowledge/priv_spec_chunks.json.gz, built by
                tools/build_spec_index.py) ranked by the failure's CSRs, fields and terms
  declared_isa  the extensions the board's ACT config declares, with its DEVIATION notes

Sources are looked up under TRIAGE_SOURCE_ROOTS (colon-separated; default: the current
directory, which is the Jenkins workspace with external/riscv-arch-test and
external/damo-rv-priv-ats). A missing source just leaves its part out.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import shutil
import subprocess
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

SPEC_INDEX = Path(__file__).resolve().parent / "knowledge" / "priv_spec_chunks.json.gz"
WORD = re.compile(r"[a-z][a-z0-9_.-]{2,}")
STOP = set("""the and for with that this from into when are was were not but has have its any all
can may must will shall each which then than also only such other more most test tests failed
expected observed value values got read write written mode bit bits field fields set clear
none true false trap traps case cases zero one two""".split())


def roots() -> list[Path]:
    raw = os.environ.get("TRIAGE_SOURCE_ROOTS", "")
    return [Path(p) for p in raw.split(":") if p] or [Path.cwd()]


# ---------- test source ----------

def _addr2line(elf: Path, address: int) -> tuple[Path, int] | None:
    tool = os.environ.get("TRIAGE_ADDR2LINE") or shutil.which("riscv64-unknown-elf-addr2line")
    if not tool or not elf.is_file():
        return None
    try:
        out = subprocess.run([tool, "-e", str(elf), f"{address:#x}"], capture_output=True,
                             text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"^(.*):(\d+)", out)
    if not match or match.group(1).startswith("??"):
        return None
    return Path(match.group(1)), int(match.group(2))


def _find(path: Path, suite: str) -> Path | None:
    if path.is_absolute() and path.is_file():
        return path
    # A path recorded in another workspace (e.g. the Jenkins build that made the ELF):
    # keep the part from "external/<repo>/" on and look for it under our roots.
    parts = path.parts
    tails = []
    if "external" in parts:
        tails.append(Path(*parts[parts.index("external"):]))
    if "priv" in parts:  # ACT stages tests/priv/... as <job>_tests/priv/... in the workspace
        tails.append(Path("external", "riscv-arch-test", "tests", *parts[parts.index("priv"):]))
    for tail in tails:
        for root in roots():
            if (root / tail).is_file():
                return root / tail
    for root in roots():
        for candidate in (root / "external" / "damo-rv-priv-ats" / suite / path,
                          root / suite / path, root / path):
            if candidate.is_file():
                return candidate
    return None


def test_source(elf: Path | None, address: int | None, assert_text: str, suite: str,
                before: int = 14, after: int = 3) -> tuple[str, str]:
    """(label "file:line", numbered snippet) of the code around the failing check."""
    location = None
    hint = re.search(r"\(([^()\s]+):(\d+)\)\s*$", assert_text or "")
    if hint:
        found = _find(Path(hint.group(1)), suite)
        if found:
            location = (found, int(hint.group(2)))
    if location is None and elf and address:
        resolved = _addr2line(elf, address)
        if resolved:
            found = _find(resolved[0], suite)
            if found:
                location = (found, resolved[1])
    if location is None:
        return "", ""
    path, line = location
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start, stop = max(line - before, 1), min(line + after, len(lines))
    snippet = "\n".join(
        f"{n:5}{'>' if n == line else ' '} {lines[n - 1].rstrip()[:160]}" for n in range(start, stop + 1)
    )
    return f"{path.name}:{line}", snippet[:1600]


# ---------- damo requirement IDs ----------

@lru_cache(maxsize=4)
def _norm_table(root_key: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for root in root_key.split(":"):
        for path in (Path(root) / "external" / "damo-rv-priv-ats" / "NORM").glob("*.md"):
            for match in re.finditer(r"\|\s*`norm:([A-Za-z0-9_]+)`\s*\|\s*([^|]+?)\s*\|",
                                     path.read_text(encoding="utf-8", errors="replace")):
                table.setdefault(match.group(1), match.group(2).strip())
    return table


def norms(*texts: str, limit: int = 4) -> list[str]:
    ids = []
    for text in texts:
        for norm in re.findall(r"norm:([A-Za-z0-9_]+)", text or ""):
            if norm not in ids:
                ids.append(norm)
    table = _norm_table(":".join(str(r) for r in roots()))
    return [f"norm:{n}: {table[n][:300]}" for n in ids if n in table][:limit]


# ---------- ratified spec text ----------

@lru_cache(maxsize=1)
def _spec() -> tuple[list[dict[str, Any]], dict[str, float], str]:
    if not SPEC_INDEX.is_file():
        return [], {}, ""
    with gzip.open(SPEC_INDEX, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    chunks = data["chunks"]
    df: Counter[str] = Counter()
    for chunk in chunks:
        chunk["_words"] = Counter(WORD.findall((chunk["title"] + " " + chunk["text"]).lower()))
        df.update(chunk["_words"].keys())
    idf = {w: math.log(len(chunks) / (1 + n)) for w, n in df.items()}
    return chunks, idf, data.get("tag", "")


def query_terms(evidence: dict[str, Any], extra: list[str] = ()) -> list[str]:
    """Distinctive spec words from the failure: CSR and field names first, then the rest."""
    chunks, idf, _tag = _spec()
    ex = evidence.get("extracted", {})
    vd = evidence.get("value_diff", {})
    text = " ".join(str(v) for k, v in ex.items() if k not in {"evidence_lines", "trap_records"})
    text += " " + " ".join(str(x) for x in extra) + " " + str(vd.get("csr", ""))
    for field in (vd.get("extra_fields") or []) + (vd.get("missing_fields") or []):
        text += f" {field.get('field', '')}"
    words = [w.strip(".-_") for w in WORD.findall(text.lower().replace("_", " "))]
    scored = {w: idf.get(w, 0.0) for w in words if w not in STOP and idf.get(w, 0) > 1.0}
    return [w for w, _ in sorted(scored.items(), key=lambda kv: -kv[1])][:10]


def spec_passages(terms: list[str], limit: int = 2, width: int = 520) -> tuple[list[str], str]:
    chunks, idf, tag = _spec()
    if not chunks or not terms:
        return [], tag
    best = []
    for chunk in chunks:
        words = chunk["_words"]
        title = chunk["title"].lower()
        score = sum((1 + math.log(words[t])) * idf.get(t, 0) for t in terms if words.get(t))
        score += sum(2 * idf.get(t, 0) for t in terms if t in title)
        if score:
            best.append((score, chunk))
    best.sort(key=lambda item: -item[0])
    out = []
    for _score, chunk in best[:limit]:
        text = chunk["text"]
        lower = text.lower()
        hits = [lower.find(t) for t in terms if t in lower]
        start = max(min(hits) - 120, 0) if hits else 0
        excerpt = text[start:start + width]
        out.append(f"[{chunk['title'][:110]}] {'…' if start else ''}{excerpt}…")
    return out, tag


# ---------- the board's declared ISA ----------

def declared_isa(knowledge: dict[str, Any]) -> str:
    relative = knowledge.get("act_config")
    if not relative:
        return ""
    for root in roots():
        path = root / "external" / "riscv-arch-test" / relative
        if path.is_file():
            break
    else:
        return ""
    raw = path.read_text(encoding="utf-8", errors="replace")
    data = yaml.safe_load(raw) or {}
    implemented = data.get("implemented_extensions") or []
    names = []
    for ext in implemented:
        if isinstance(ext, dict) and ext.get("name"):
            names.append(f"{ext['name']} {str(ext.get('version', '')).replace('=', '').strip()}".strip())
    deviations = [re.sub(r"^.*#\s*", "", line).strip() for line in raw.splitlines() if "DEVIATION" in line]
    text = "Declared in the board's ACT config: " + ", ".join(names)
    if deviations:
        text += ". Deviations: " + " | ".join(dict.fromkeys(deviations))
    return text[:900]


# ---------- reported riscv-arch-test issues ----------

ISSUE_INDEX = Path(__file__).resolve().parent / "knowledge" / "act_issues.json.gz"


@lru_cache(maxsize=1)
def _issues() -> tuple[list[dict[str, Any]], dict[str, float]]:
    if not ISSUE_INDEX.is_file():
        return [], {}
    with gzip.open(ISSUE_INDEX, "rt", encoding="utf-8") as stream:
        issues = json.load(stream)["issues"]
    df: Counter[str] = Counter()
    for issue in issues:
        issue["_words"] = Counter(WORD.findall((issue["title"] + " " + issue["text"]).lower()))
        df.update(issue["_words"].keys())
    idf = {w: math.log(len(issues) / (1 + n)) for w, n in df.items()}
    return issues, idf


def _family(test: str) -> str:
    """Test family: 'Sv_sv39_canonical_Smode-00' -> 'sv_sv39_canonical'."""
    return re.sub(r"(?:_[smu]mode)?(?:-\d{2})?$", "", test, flags=re.I).lower()


def _same_family(a: str, b: str) -> bool:
    """Older runs drop the category prefix ('sv39_canonical' vs 'sv_sv39_canonical')."""
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 6 and (long_ == short or long_.endswith("_" + short))


IDENT = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+|[a-z]+[0-9][a-z0-9]*")


def issue_terms(evidence: dict[str, Any]) -> list[str]:
    """Identifiers that name what failed: CSR and field names, coverpoint and bin names
    ('cp_satp_access', 'mstatus_csrrw1'). Prose from hints matches every issue, so it is left out."""
    ex = evidence.get("extracted", {})
    vd = evidence.get("value_diff") or {}
    words = {str(vd.get("csr", "")).lower()}
    for field in (vd.get("extra_fields") or []) + (vd.get("missing_fields") or []):
        words.add(str(field.get("field", "")).lower())
    info = " ".join(str(ex.get(k, "")) for k in ("test_info", "damo_assert", "mismatching_field"))
    for token in IDENT.findall(info.lower()):
        words.add(token)
        words.update(part for part in token.split("_") if len(part) > 3)  # mstatus_csrrw1 -> mstatus
    return sorted(w for w in words if len(w) > 2 and w not in STOP)


def related_issues(test: str, terms: list[str], exclude: set[str] = frozenset(),
                   limit: int = 2, width: int = 420) -> list[str]:
    """Reported riscv-arch-test issues most like this failure: the same test or test family
    first, then shared distinctive terms. Context for the AI, never an answer by itself."""
    issues, idf = _issues()
    if not issues:
        return []
    family = _family(test)
    best = []
    for issue in issues:
        if f"ACT-{issue['n']}" in exclude:
            continue
        named = [t.lower() for t in issue["tests"]]
        score = 0.0
        same_test = True
        if test.lower() in named or any(t.endswith("_" + test.lower()) for t in named):
            score += 12
        elif family and any(_same_family(_family(t), family) for t in named):
            score += 6
        else:
            same_test = False
        words = issue["_words"]
        shared = [t for t in terms if words.get(t) and idf.get(t, 0) > 2.0]
        score += sum((1 + math.log(words[t])) * idf[t] for t in shared) / 3
        # Without the same test or family, it takes at least two rare shared identifiers.
        if score < 6 or (not same_test and len(shared) < 2):
            continue
        if issue["created"] < "2025-01-01":  # before ACT 4: different framework and tests
            score *= 0.5
        best.append((score, issue))
    best.sort(key=lambda item: -item[0])
    out = []
    for _score, issue in best[:limit]:
        status = issue["state"] + (f"/{issue['reason']}" if issue["reason"] else "")
        prs = f"; PRs {', '.join('#' + str(p) for p in issue['prs'][:4])}" if issue["prs"] else ""
        text = issue["text"]
        # The opening report and the final comment carry the most: keep both ends.
        excerpt = text if len(text) <= width else text[: width // 2] + " … " + text[-width // 2:]
        out.append(f"ACT#{issue['n']} ({status}{prs}) {issue['title'][:100]}: {excerpt}")
    return out
