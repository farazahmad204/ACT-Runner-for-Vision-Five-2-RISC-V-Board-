#!/usr/bin/env python3
"""Build the triage agent's privileged-spec index from riscv-isa-manual sources.

    git clone --depth 1 --branch <tag> https://github.com/riscv/riscv-isa-manual.git isa
    python3 ci/triage/tools/build_spec_index.py isa --tag <tag>

Splits src/priv/*.adoc into titled sections of at most ~1200 characters of plain text and
writes ci/triage/knowledge/priv_spec_chunks.json.gz. The spec is CC-BY-4.0 (RISC-V
International); the index records the source tag and commit.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "knowledge" / "priv_spec_chunks.json.gz"
MAX_CHARS = 1200


def plain(line: str) -> str:
    line = re.sub(r"<<[^,>]+,([^>]+)>>", r"\1", line)        # <<ref,text>>
    line = re.sub(r"<<([^>]+)>>", r"\1", line)               # <<ref>>
    line = re.sub(r"\b(?:link|xref|image|footnote):[^\[]*\[([^\]]*)\]", r"\1", line)
    line = re.sub(r"\[\[[^\]]*\]\]|\[#[^\]]*\]", "", line)   # anchors
    line = re.sub(r"[`*_]{1,2}([^`*_]+)[`*_]{1,2}", r"\1", line)
    line = line.replace("|", " ").replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", line).strip()


def sections(path: Path):
    titles: list[str] = []
    buffer: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        heading = re.match(r"^(=+)\s+(.*)$", raw)
        if heading:
            if buffer:
                yield " > ".join(titles), " ".join(buffer)
                buffer = []
            level = len(heading.group(1))
            titles = titles[: max(level - 2, 0)] + [plain(heading.group(2))]
            continue
        if raw.startswith(("include::", "ifdef::", "endif::", "ifndef::", ":", "//", "[", "----",
                           "....", "====", "|===", "image::")) or not raw.strip():
            if not raw.strip() and buffer and not buffer[-1].endswith("\n"):
                buffer[-1] += "\n"
            continue
        text = plain(raw)
        if text:
            buffer.append(text)
    if buffer:
        yield " > ".join(titles), " ".join(buffer)


def chunks(text: str):
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) > MAX_CHARS:
            yield current
            current = ""
        current = f"{current} {paragraph}".strip()
        while len(current) > MAX_CHARS:
            yield current[:MAX_CHARS]
            current = current[MAX_CHARS:]
    if current:
        yield current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manual", type=Path, help="riscv-isa-manual checkout")
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    commit = subprocess.run(["git", "-C", str(args.manual), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    entries = []
    for path in sorted((args.manual / "src" / "priv").glob("*.adoc")):
        for title, text in sections(path):
            for index, piece in enumerate(chunks(text)):
                if len(piece) > 80:
                    entries.append({"file": path.name, "title": title or path.stem,
                                    "part": index, "text": piece})
    payload = {"source": "riscv-isa-manual privileged volume", "tag": args.tag, "commit": commit,
               "license": "CC-BY-4.0, RISC-V International", "chunks": entries}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
    print(f"{len(entries)} chunks -> {OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
