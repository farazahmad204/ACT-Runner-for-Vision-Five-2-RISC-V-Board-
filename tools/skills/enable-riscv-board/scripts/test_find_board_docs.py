#!/usr/bin/env python3
"""Tests for find_board_docs.py and fill_gaps.py's Codex backend, with a fake Codex and fetcher."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error

import fill_gaps as fg
import find_board_docs as fbd

FOUND = {
    "vendor_sites": ["https://www.vendor.example"],
    "notes": "",
    "documents": [
        {"title": "EIC9999 TRM", "url": "https://vendor.example/trm.pdf", "kind": "soc_trm",
         "publisher": "Vendor", "official": True, "revision": "v1.2", "covers": ["memory"], "why": "map"},
        {"title": "Board dts", "url": "https://github.com/v/linux/blob/main/arch/riscv/x.dts",
         "kind": "device_tree", "publisher": "Vendor", "official": True, "revision": "main",
         "covers": ["uart"], "why": "nodes"},
        {"title": "Guide", "url": "https://vendor.example/guide", "kind": "boot_guide",
         "publisher": "Vendor", "official": True, "revision": "", "covers": ["boot"], "why": "boot"},
        {"title": "Invented", "url": "https://vendor.example/missing.pdf", "kind": "datasheet",
         "publisher": "Vendor", "official": True, "revision": "", "covers": [], "why": "?"},
        {"title": "OpenSBI fork", "url": "https://github.com/v/opensbi/tree/board-1", "kind": "opensbi_source",
         "publisher": "Vendor", "official": True, "revision": "board-1", "covers": ["boot"], "why": "slot"},
    ],
}
PAGES = {
    "https://vendor.example/trm.pdf": (b"%PDF-1.7 fake", "application/pdf"),
    "https://raw.githubusercontent.com/v/linux/main/arch/riscv/x.dts": (b"/dts-v1/; serial@50900000 {};",
                                                                        "text/plain"),
    "https://vendor.example/guide": (b"<html><body><nav>menu</nav><h1>Boot</h1><p>"
                                     + b"Set the boot switch to SPI. " * 20 + b"</p></body></html>",
                                     "text/html; charset=utf-8"),
}


def fake_fetch(url: str) -> tuple[bytes, str]:
    if url not in PAGES:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return PAGES[url]


def fake_codex(folder: Path, answer: dict) -> Path:
    """A codex stand-in that records its argv and stdin and writes `answer` to -o."""
    script = folder / "codex"
    script.write_text(
        "#!/usr/bin/env python3\nimport json, sys\nargs = sys.argv[1:]\n"
        f"open({str(folder / 'argv.json')!r}, 'w').write(json.dumps({{'args': args, 'stdin': sys.stdin.read()}}))\n"
        f"open(args[args.index('-o') + 1], 'w').write({json.dumps(json.dumps(answer))})\n")
    script.chmod(0o755)
    return script


class FindBoardDocsTests(unittest.TestCase):
    def args(self, **kw) -> argparse.Namespace:
        base = dict(board="Vendor Board", soc="EIC9999", board_id="vendor_eic9999", max_docs=25,
                    clone_repos=False)
        return argparse.Namespace(**{**base, **kw})

    def test_links_are_checked_and_saved_by_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            records = fbd.collect(FOUND, out, self.args(), get=fake_fetch)
            by_title = {r["title"]: r for r in records}
            self.assertEqual(by_title["EIC9999 TRM"]["file"], "01_EIC9999_TRM.pdf")
            self.assertEqual(by_title["Board dts"]["file"], "02_Board_dts.dts")  # fetched raw
            guide = (out / by_title["Guide"]["file"]).read_text()
            self.assertIn("Set the boot switch to SPI.", guide)
            self.assertNotIn("menu", guide)
            self.assertEqual(by_title["Invented"]["status"], "rejected")
            self.assertEqual(by_title["OpenSBI fork"]["status"], "repository")
            self.assertEqual(len(by_title["EIC9999 TRM"]["sha256"]), 64)

    def test_urls(self) -> None:
        self.assertEqual(fbd.raw_url("https://github.com/a/b/blob/v1/x/y.its"),
                         "https://raw.githubusercontent.com/a/b/v1/x/y.its")
        self.assertEqual(fbd.repo_url("https://github.com/a/opensbi/tree/dev-1"),
                         ("https://github.com/a/opensbi.git", "dev-1"))
        self.assertIsNone(fbd.repo_url("https://github.com/a/b/blob/v1/x.c"))
        with self.assertRaises(ValueError):
            fbd.fetch("file:///etc/passwd")

    def test_release_pages_give_their_pdf_assets(self) -> None:
        api = json.dumps([{"assets": [{"name": "notes.txt", "browser_download_url": "https://x/notes.txt"}]},
                          {"assets": [{"name": "TRM_Part1.pdf", "browser_download_url": "https://x/TRM_Part1.pdf"},
                                      {"name": "TRM_Part2.pdf", "browser_download_url": "https://x/TRM_Part2.pdf"}]}])
        pages = {"https://api.github.com/repos/v/trm/releases": (api.encode(), "application/json"),
                 "https://x/TRM_Part1.pdf": (b"%PDF-1", "application/pdf"),
                 "https://x/TRM_Part2.pdf": (b"%PDF-2", "application/pdf")}
        with tempfile.TemporaryDirectory() as temporary:
            record = fbd.save_document(1, {"url": "https://github.com/v/trm/releases", "title": "TRM"},
                                       Path(temporary), lambda url: pages[url])
        self.assertEqual(record["file"], "01_1_TRM_Part1.pdf")
        self.assertEqual(record["extra_files"], ["01_2_TRM_Part2.pdf"])
        self.assertIn("eic7700", fbd.keywords_for("Milk-V Megrez", "ESWIN EIC7700X", "milkv_megrez"))

    def test_search_runs_codex_with_web_search_and_no_other_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            found = fbd.call_codex(fbd.prompt("Vendor Board", "EIC9999", ""), str(fake_codex(folder, FOUND)))
            seen = json.loads((folder / "argv.json").read_text())
        self.assertEqual(found["documents"][0]["kind"], "soc_trm")
        args = seen["args"]
        self.assertIn('web_search="live"', args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        disabled = {args[i + 1] for i, a in enumerate(args) if a == "--disable"}
        self.assertTrue({"shell_tool", "unified_exec", "browser_use"} <= disabled)
        self.assertIn("Never construct or guess a URL", seen["stdin"])
        self.assertIn("Console UART", seen["stdin"])  # the needs come from the catalog


class FillGapsCodexTests(unittest.TestCase):
    def test_strict_schema_requires_everything(self) -> None:
        item = fg.strict_schema(fg.TOOL["input_schema"])["properties"]["findings"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(set(item["required"]), set(item["properties"]))
        self.assertIn("null", item["properties"]["quote"]["type"])
        self.assertEqual(item["properties"]["found"]["type"], "boolean")

    def test_codex_backend_returns_findings_without_nulls(self) -> None:
        answer = {"findings": [{"id": "uart.base", "found": True, "value": "0x50900000",
                                "quote": "UART0 base address 0x5090_0000", "excerpt_id": "d#p1.0",
                                "bits": None, "derivation": None, "confidence": "high", "missing": None}]}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            os.environ["CODEX_BIN"] = str(fake_codex(folder, answer))
            try:
                response = fg.call_codex("default", "SYSTEM", "USER", 500)
            finally:
                del os.environ["CODEX_BIN"]
            seen = json.loads((folder / "argv.json").read_text())
        finding = fg.findings_from(response)[0]
        self.assertEqual(finding["value"], "0x50900000")
        self.assertNotIn("derivation", finding)
        self.assertIn('web_search="disabled"', seen["args"])
        self.assertNotIn("-m", seen["args"])
        self.assertTrue(seen["stdin"].startswith("SYSTEM"))


if __name__ == "__main__":
    unittest.main()
