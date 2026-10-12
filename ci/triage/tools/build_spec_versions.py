#!/usr/bin/env python3
"""Build the triage agent's version-aware RISC-V specification index.

    git clone --filter=blob:none https://github.com/riscv/riscv-isa-manual.git isa
    python3 ci/triage/tools/build_spec_versions.py isa

Writes ci/triage/knowledge/spec_versions.json.gz with one entry per specification version a
board can claim (see VERSIONS): titled sections of plain text (at most ~1400 characters each)
and, for releases that tag them, the normative rules (`[#norm:ID]#text#`) with their exact text.
Older releases are LaTeX and are converted to plain text here. The text is the RISC-V ISA
manual (CC-BY-4.0, RISC-V International); every version records its git tag or commit.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "knowledge" / "spec_versions.json.gz"
MAX_CHARS = 1400
CURRENT = "riscv-isa-release-ff6bb3c-2026-04-08"
PRIV_TEX = ["priv-intro", "priv-csrs", "machine", "supervisor", "priv-insns", "plic"]
UNPRIV_TEX = ["intro", "rv32", "rv64", "zifencei", "csr", "counters", "m", "a", "f", "d", "c",
              "rvwmo", "extensions"]

# id -> (label, git ref, format, files). Files are paths under src/ (globs allowed for adoc).
VERSIONS = {
    "priv-1.11": ("Privileged ISA 1.11 (Ratified, 20190608)", "Ratified-IMFDQC-and-Priv-v1.11",
                  "tex", PRIV_TEX),
    "unpriv-20190608": ("Unprivileged ISA 20190608 (Ratified)", "Ratified-IMFDQC-and-Priv-v1.11",
                        "tex", UNPRIV_TEX),
    "priv-1.12": ("Privileged ISA 1.12 (Ratified, 20211203)", "Priv-v1.12", "tex", PRIV_TEX),
    "h-1.0": ("Hypervisor Extension 1.0 (Ratified, in Privileged 20211203)", "Priv-v1.12", "tex",
              ["hypervisor"]),
    # Filled in by --h06-ref: the manual commit whose hypervisor chapter is draft 0.6.
    "h-0.6": ("Hypervisor Extension 0.6 (draft)", None, "tex", ["hypervisor"]),
    "priv-current": (f"Privileged ISA, current ratified ({CURRENT})", CURRENT, "adoc", ["priv/*.adoc"]),
    "unpriv-current": (f"Unprivileged ISA, current ratified ({CURRENT})", CURRENT, "adoc",
                       ["*.adoc", "unpriv/*.adoc"]),
}
SKIP_ADOC = {"index", "bibliography", "contributors", "license", "preface", "riscv-spec",
             "unpriv", "symbols", "colophon", "mm-formal", "mm-appendix"}


def git(manual: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(manual), *args], capture_output=True, text=True,
                          check=True).stdout


def files_at(manual: Path, ref: str, patterns: list[str], fmt: str) -> list[str]:
    names = git(manual, "ls-tree", "-r", "--name-only", ref, "src/").split()
    out = []
    for pattern in patterns:
        if fmt == "tex":
            wanted = f"src/{pattern}.tex"
            out += [n for n in names if n == wanted]
        else:
            regex = re.compile("^src/" + re.escape(pattern).replace(r"\*", "[^/]*") + "$")
            out += [n for n in names if regex.match(n) and Path(n).stem not in SKIP_ADOC]
    return out


# ---------- LaTeX ----------

# Tables stay: the cause-code and exception-priority tables are among the most cited text.
DROP_ENVS = ("figure", "bytefield", "register", "tikzpicture", "wavedrom", "verbatim")
KEEP_ARG = ("textbf", "textit", "texttt", "emph", "em", "tt", "mbox", "wordwrap", "underline",
            "textsc", "textrm", "textsf", "small", "hyperref", "nolinkurl", "url", "csr", "insn")
HEADING = re.compile(r"\\(chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\{")


def _balanced(text: str, start: int) -> tuple[str, int]:
    """Content of the {...} group opening at text[start] and the index after it."""
    depth, index = 0, start
    while index < len(text):
        char = text[index]
        if char == "{" and text[index - 1] != "\\":
            depth += 1
        elif char == "}" and text[index - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
        index += 1
    return text[start + 1:], len(text)


def tex_plain(text: str) -> str:
    for env in DROP_ENVS:
        text = re.sub(rf"\\begin\{{{env}\*?\}}.*?\\end\{{{env}\*?\}}", " ", text, flags=re.S)
    text = re.sub(r"\\(label|index|vspace|hspace|cite|setlength|addtolength)\*?(\[[^\]]*\])?\{[^{}]*\}", "", text)
    text = re.sub(r"\\ref\{[^{}]*\}", "§", text)
    text = re.sub(r"\\multicolumn\{[^{}]*\}\{[^{}]*\}\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(hline|cline\{[^{}]*\}|toprule|midrule|bottomrule)", "", text)
    text = text.replace("&", " | ")
    text = re.sub(r"\\footnote\{", "(", text)
    for _ in range(4):
        text = re.sub(r"\\(" + "|".join(KEEP_ARG) + r")\s*\{([^{}]*)\}", r"\2", text)
    text = re.sub(r"\{\\(tt|em|bf|it|sc|small)\s+([^{}]*)\}", r"\2", text)
    text = re.sub(r"\\item(\[[^\]]*\])?", "\n- ", text)
    text = re.sub(r"\\(begin|end)\{[^{}]*\}(\{[^{}]*\})*", "\n", text)
    text = text.replace("\\\\", " ;\n").replace("~", " ").replace("``", '"').replace("''", '"')
    text = re.sub(r"\\([_%$#{}])", r"\1", text).replace("\\ | ", "&")
    text = re.sub(r"\\(ldots|dots)\b", "...", text)
    text = re.sub(r"\$([^$]*)\$", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+\*?", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"[ \t]+", " ", text)


def tex_sections(source: str):
    source = "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in source.splitlines())
    titles: list[str] = []
    levels = {"chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4, "paragraph": 5}
    position = 0
    pending = ""
    for match in HEADING.finditer(source):
        body = source[position:match.start()]
        yield " > ".join(titles), pending + body
        title, end = _balanced(source, match.end() - 1)
        level = levels[match.group(1)]
        titles = titles[: level - 1] + [tex_plain(title).strip()]
        position, pending = end, ""
    yield " > ".join(titles), source[position:]


# ---------- AsciiDoc ----------

NORM = re.compile(r"\[#norm:([A-Za-z0-9_]+)\]#(.+?)#(?!#)", re.S)
# A rule anchored on its own line ([[norm:ID]] or [#norm:ID]) covers the paragraph after it.
NORM_LINE = re.compile(r"^\[(?:\[|#)norm:([A-Za-z0-9_]+)\]\]?[ \t]*\n(.+?)(?:\n[ \t]*\n|\Z)", re.S | re.M)


def adoc_plain(text: str) -> str:
    # Table and block layout markup: attribute lines and cell specifiers (.>, ^.>, 2+, a|).
    text = re.sub(r"(?m)^\[(?!\[?#?norm:)[^\]\n]*\]\s*$", "", text)
    text = re.sub(r"(?m)^\.(?=[A-Z])", "", text)  # block titles ".Synchronous exception ..."
    text = re.sub(r"(?<![^\s|])(?:\d+\+|[\^<>]?\.[<>^]|[<>^]\.[<>^]?|[a-z](?=\|))(?=[\s|]|$)", " ", text)
    text = re.sub(r"(?<![^\s|])\+(?=\s|$)", " ", text)
    text = re.sub(r"<<[^,>]+,([^>]+)>>", r"\1", text)
    text = re.sub(r"<<([^>]+)>>", r"\1", text)
    text = re.sub(r"\b(?:link|xref|image|footnote|kbd|pass):[^\[\s]*\[([^\]]*)\]", r"\1", text)
    text = re.sub(r"\[\[[^\]]*\]\]|\[#[^\]]*\]#?", "", text)
    text = re.sub(r"(?<![A-Za-z0-9])[`*_+]{1,2}([^`*_+\n]+?)[`*_+]{1,2}(?![A-Za-z0-9])", r"\1", text)
    text = text.replace("#", "").replace("|", " ").replace("{", "").replace("}", "")
    return re.sub(r"[ \t]+", " ", text)


def adoc_sections(source: str):
    titles: list[str] = []
    buffer: list[str] = []
    in_block = False
    for raw in source.splitlines():
        if raw.strip() in ("----", "....", "++++"):
            in_block = not in_block
            continue
        if raw.strip() == "|===":  # tables stay as text rows (cause codes, priorities, encodings)
            continue
        if in_block:
            continue
        heading = re.match(r"^(=+)\s+(.*)$", raw)
        if heading:
            if buffer:
                yield " > ".join(titles), "\n".join(buffer)
                buffer = []
            level = len(heading.group(1))
            titles = titles[: max(level - 1, 0)] + [adoc_plain(heading.group(2)).strip()]
            continue
        if raw.startswith(("include::", "ifdef::", "endif::", "ifndef::", "//", "image::", ":")):
            continue
        buffer.append(raw)
    if buffer:
        yield " > ".join(titles), "\n".join(buffer)


# ---------- both ----------

def chunks(text: str):
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    current = ""
    for paragraph in (p for p in paragraphs if p):
        if current and len(current) + len(paragraph) > MAX_CHARS:
            yield current
            current = ""
        current = f"{current} {paragraph}".strip()
        while len(current) > MAX_CHARS:
            cut = current.rfind(". ", 0, MAX_CHARS)
            cut = cut + 1 if cut > MAX_CHARS // 2 else MAX_CHARS
            yield current[:cut]
            current = current[cut:].strip()
    if current:
        yield current


def build_version(manual: Path, ref: str, fmt: str, patterns: list[str]) -> dict:
    sections, rules = [], {}
    for path in files_at(manual, ref, patterns, fmt):
        source = git(manual, "show", f"{ref}:{path}")
        parts = tex_sections(source) if fmt == "tex" else adoc_sections(source)
        for title, body in parts:
            if fmt == "adoc":
                for pattern in (NORM, NORM_LINE):
                    for rule, text in pattern.findall(body):
                        rules.setdefault(rule, [len(sections), re.sub(r"\s+", " ", adoc_plain(text)).strip()[:900]])
                plain = adoc_plain(NORM.sub(lambda m: m.group(2), body))
            else:
                plain = tex_plain(body)
            title = title or Path(path).stem
            for piece in chunks(plain):
                if len(piece) > 80:
                    sections.append({"f": Path(path).stem, "t": title, "x": piece})
            # Rules point at the first chunk of their section; good enough to show context.
    commit = git(manual, "rev-parse", ref).strip()
    return {"ref": ref, "commit": commit, "sections": sections, "rules": rules}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("manual", type=Path, help="riscv-isa-manual checkout with tags")
    parser.add_argument("--h06-ref", required=True,
                        help="manual commit whose src/hypervisor.tex is draft 0.6")
    args = parser.parse_args()
    out = {"source": "https://github.com/riscv/riscv-isa-manual",
           "license": "CC-BY-4.0, RISC-V International", "versions": {}}
    for vid, (label, ref, fmt, patterns) in VERSIONS.items():
        ref = ref or args.h06_ref
        entry = build_version(args.manual, ref, fmt, patterns)
        entry["label"] = label
        out["versions"][vid] = entry
        size = sum(len(s["x"]) for s in entry["sections"])
        print(f"{vid}: {len(entry['sections'])} sections, {len(entry['rules'])} rules, {size // 1000} K chars")
    with gzip.open(OUT, "wt", encoding="utf-8") as stream:
        json.dump(out, stream, separators=(",", ":"))
    print(f"-> {OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
