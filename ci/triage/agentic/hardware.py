#!/usr/bin/env python3
"""Target hardware profiles for the triage agent: one schema for every board and core.

load_hardware_profile(path) reads any of the board descriptions this project already keeps
and maps it to the standard profile; get_hardware_context_string(profile) renders it for the
agent's context.

Accepted inputs (detected by content, pointers followed):
  UDB architecture config   ACT config/cores/<vendor>/<name>.yaml: MXLEN, implemented_extensions
  ACT test_config.yaml      run configs: follows `udb_config`
  onboarding board spec     tools/skills/enable-riscv-board/assets/board_specs/*.yaml: isa,
                            privilege_modes, virtual_memory, priv_version, dut.description
  CI board knowledge        ci/triage/board_knowledge/*.yaml: verified facts, spec_versions;
                            follows `act_config` into the ACT checkout
  platform markdown         knowledge/*_platform.md: "Platform: board / SoC / core." plus any
                            ISA, MMU or privilege statements; a markdown file names the board
                            but rarely its ISA, so it is completed from a structured source for
                            the same core when one is found

Nothing is guessed: a field no source states stays None and is listed in `missing`, and
`sources` says where each field came from.

    hardware_registry.py PATH [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[3]
ACT_ROOTS = [REPO / "external" / "riscv-arch-test", Path.home() / "riscv-arch-test"]
STRUCTURED_DIRS = [REPO / "tools" / "skills" / "enable-riscv-board" / "assets" / "board_specs",
                   REPO / "ci" / "triage" / "board_knowledge"]
SINGLE_LETTERS = "IEMAFDQCBVH"  # canonical order of single-letter extensions in an ISA string
MMU_ORDER = ["Sv32", "Sv39", "Sv48", "Sv57"]
# Extensions whose absence often explains a failure; the context lists the missing ones.
NOTABLE = ["H", "V", "Sv48", "Sv57", "Svpbmt", "Svnapot", "Svadu", "Svinval", "Sstc", "Sscofpmf",
           "Smaia", "Ssaia", "Zicbom", "Zicboz", "Zicfilp", "Zicfiss"]
CORE = re.compile(r"\b(SiFive [UPSEX]\d+\w*|SpacemiT X\d+|T-Head C\d+\w*|XuanTie C\d+\w*|lowRISC Ibex|Ibex|"
                  r"CVA6|CV32E\d+\w*|Rocket|BOOM|Andes \w+|Nuclei \w+|[A-Z]\w+ [A-Z]\d{2,}\w*)\b")


@dataclass
class HardwareProfile:
    board_name: str | None = None
    core_name: str | None = None
    xlen: int | None = None
    implemented_isa: str | None = None
    mmu_type: str | None = None
    privilege_modes: list[str] | None = None
    # Beyond the core schema; filled when a source states them.
    soc: str | None = None
    extensions: list[str] = field(default_factory=list)
    mmu_modes: list[str] = field(default_factory=list)
    priv_version: str | None = None
    # hard_macro: fixed silicon (fixes are firmware/bootloader/compiler workarounds or errata);
    # soft_core: open RTL on an FPGA (rtl_source says where the HDL is); unknown: treat as hard.
    core_type: str | None = None
    rtl_source: str | None = None
    spec_versions: dict[str, str] = field(default_factory=dict)
    facts: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    CORE_FIELDS = ("board_name", "core_name", "xlen", "implemented_isa", "mmu_type", "privilege_modes")

    def set(self, name: str, value: Any, source: str) -> None:
        """Set a field once: the first source that states it wins."""
        if value in (None, "", [], {}) or getattr(self, name) not in (None, "", [], {}):
            return
        setattr(self, name, value)
        self.sources[name] = source

    def merge(self, other: "HardwareProfile") -> None:
        for name in ("board_name", "core_name", "xlen", "implemented_isa", "mmu_type", "privilege_modes",
                     "soc", "extensions", "mmu_modes", "priv_version", "spec_versions", "core_type", "rtl_source"):
            self.set(name, getattr(other, name), other.sources.get(name, "merged"))
        self.facts += [f for f in other.facts if f not in self.facts]


# ---------- ISA strings ----------

def parse_isa(isa: str) -> tuple[int | None, list[str]]:
    """'rv64imafdc_zicsr_zba' -> (64, ['I','M','A','F','D','C','Zicsr','Zba'])."""
    match = re.match(r"^\s*rv(32|64|128)([a-z]*)((?:_[a-z0-9]+)*)", isa.lower())
    if not match:
        return None, []
    letters = match.group(2).replace("g", "imafd")  # G = IMAFD + Zicsr + Zifencei
    exts = [ch.upper() for ch in dict.fromkeys(letters)]
    if "g" in match.group(2):
        exts += ["Zicsr", "Zifencei"]
    exts += [part[:1].upper() + part[1:] for part in match.group(3).split("_") if part]
    return int(match.group(1)), list(dict.fromkeys(exts))


def isa_string(xlen: int | None, extensions: list[str]) -> str | None:
    """Canonical ISA string: single letters in order (IMAFD + Zicsr + Zifencei -> G), then the
    Z and X extensions. Privileged S-names (Sv39, Sstc...) are reported separately."""
    if not xlen or not extensions:
        return None
    present = set(extensions)
    letters = "".join(ch for ch in SINGLE_LETTERS if ch in present)
    multi = sorted(e for e in present if len(e) > 1 and e[0] in "ZX")
    if set("IMAFD") <= set(letters) and {"Zicsr", "Zifencei"} <= present:
        letters = "G" + "".join(ch for ch in letters if ch not in "IMAFD")
        multi = [e for e in multi if e not in ("Zicsr", "Zifencei")]
    return f"RV{xlen}{letters}" + "".join(f"_{e}" for e in multi)


def finish(profile: HardwareProfile) -> HardwareProfile:
    """Derive the summary fields from the detailed ones, and list what is still unknown."""
    exts = set(profile.extensions)
    if exts:
        profile.set("implemented_isa", isa_string(profile.xlen, profile.extensions), profile.sources.get(
            "extensions", "derived"))
        modes = [m for m in MMU_ORDER if m in exts]
        profile.set("mmu_modes", modes, profile.sources.get("extensions", "derived"))
        if "S" in exts or "U" in exts:
            profile.set("privilege_modes", ["M"] + [m for m in ("S", "U") if m in exts],
                        profile.sources.get("extensions", "derived"))
    if profile.mmu_modes:
        profile.set("mmu_type", profile.mmu_modes[-1], profile.sources.get("mmu_modes", "derived"))
    elif profile.privilege_modes and "S" not in profile.privilege_modes:
        profile.set("mmu_type", "None", "no S-mode, so no address translation")
    profile.missing = [name for name in HardwareProfile.CORE_FIELDS if getattr(profile, name) in (None, [], "")]
    return profile


# ---------- one loader per format ----------

def _from_udb(data: dict[str, Any], src: str) -> HardwareProfile:
    p = HardwareProfile()
    params = data.get("params") or {}
    names = [str(e.get("name")) for e in data.get("implemented_extensions") or [] if isinstance(e, dict)]
    p.set("board_name", data.get("name"), f"{src}: name")
    description = " ".join(str(data.get("description") or "").split())
    core = CORE.search(description)
    p.set("core_name", core.group(1) if core else None, f"{src}: description")
    p.set("xlen", params.get("MXLEN"), f"{src}: params.MXLEN")
    p.set("extensions", names, f"{src}: implemented_extensions")
    priv = next((str(e.get("version", "")).strip("= ") for e in data.get("implemented_extensions") or []
                 if isinstance(e, dict) and e.get("name") == "Sm"), None)
    p.set("priv_version", priv, f"{src}: Sm version")
    return p


def _from_board_spec(data: dict[str, Any], src: str) -> HardwareProfile:
    p = HardwareProfile()
    dut = data.get("dut") or {}
    parts = [s.strip() for s in re.sub(r"\(.*?\)", "", str(dut.get("description") or "")).split(",") if s.strip()]
    core = CORE.search(str(dut.get("description") or ""))
    p.set("board_name", parts[0].rstrip(".") if parts else dut.get("name"), f"{src}: dut.description")
    p.set("soc", parts[1].rstrip(".") if len(parts) > 2 else None, f"{src}: dut.description")
    p.set("core_name", core.group(1) if core else None, f"{src}: dut.description")
    xlen, exts = parse_isa(str(data.get("isa") or ""))
    p.set("xlen", xlen, f"{src}: isa")
    exts += [e for e in data.get("extra_extensions") or [] if e not in exts]
    modes = [m[:1].upper() + m[1:] for m in data.get("virtual_memory") or []]
    exts += [m for m in modes if m not in exts]
    exts += [m for m in data.get("privilege_modes") or [] if m in ("S", "U") and m not in exts]
    p.set("extensions", exts, f"{src}: isa + extra_extensions + virtual_memory")
    p.set("privilege_modes", list(data.get("privilege_modes") or []) or None, f"{src}: privilege_modes")
    p.set("priv_version", data.get("priv_version"), f"{src}: priv_version")
    return p


def _from_board_knowledge(data: dict[str, Any], src: str) -> HardwareProfile:
    p = HardwareProfile()
    p.set("board_name", data.get("board"), f"{src}: board")
    p.set("spec_versions", {k: v for k, v in (data.get("spec_versions") or {}).items() if k != "source"},
          f"{src}: spec_versions")
    p.facts = [str(f) for f in data.get("facts") or []]
    p.set("core_type", data.get("core_type"), f"{src}: core_type")
    p.set("rtl_source", data.get("rtl_source"), f"{src}: rtl_source")
    # The measured spec version beats a declared one (an ACT config may declare a newer one so
    # that tests are generated, e.g. the Megrez: priv 1.11 implemented, 1.12 declared).
    priv = re.match(r"^priv-(\d+\.\d+)$", str((data.get("spec_versions") or {}).get("priv") or ""))
    p.set("priv_version", priv.group(1) if priv else None, f"{src}: spec_versions.priv (measured)")
    config = data.get("act_config")
    for root in ACT_ROOTS if config else []:
        if (root / config).is_file():
            p.merge(load_profile_object(root / config))
            break
    # The onboarding board spec with the same board id (runner.board_id) completes the rest.
    for spec in sorted(STRUCTURED_DIRS[0].glob("*.yaml")) if data.get("board") else []:
        try:
            raw = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if (raw.get("runner") or {}).get("board_id") == data["board"]:
            p.merge(_from_board_spec(raw, str(spec.relative_to(REPO)) if spec.is_relative_to(REPO) else str(spec)))
            break
    return p


def _from_markdown(text: str, src: str) -> HardwareProfile:
    p = HardwareProfile()
    platform = re.search(r"^Platform:\s*(.+?)\.?\s*$", text, re.M)
    if platform:
        parts = [s.strip() for s in platform.group(1).split("/")]
        p.set("board_name", parts[0], f"{src}: Platform line")
        p.set("soc", parts[1] if len(parts) > 2 else None, f"{src}: Platform line")
        core = CORE.search(platform.group(1))
        p.set("core_name", core.group(1) if core else (parts[-1] if len(parts) > 1 else None),
              f"{src}: Platform line")
    else:
        title = re.search(r"^#\s+(.+)$", text, re.M)
        p.set("board_name", title.group(1).replace("Platform Knowledge", "").strip() if title else None,
              f"{src}: title")
    isa = re.search(r"\b(rv(?:32|64)[a-z][a-z0-9_]*)\b", text, re.I)
    if isa:
        xlen, exts = parse_isa(isa.group(1))
        p.set("xlen", xlen, f"{src}: ISA string in text")
        p.set("extensions", exts, f"{src}: ISA string in text")
    modes = sorted({m.capitalize() for m in re.findall(r"\b(sv(?:32|39|48|57))\b", text, re.I)},
                   key=MMU_ORDER.index)
    p.set("mmu_modes", modes, f"{src}: Sv modes named in text")
    return p


def load_profile_object(path: Path) -> HardwareProfile:
    path = Path(path).resolve()
    src = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in (".md", ".markdown", ".txt"):
        return _from_markdown(text, src)
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a mapping")
    if data.get("udb_config"):  # an ACT test_config: the UDB config holds the hardware
        target = Path(data["udb_config"])
        if not target.is_absolute():
            target = path.parent / target
        profile = load_profile_object(target)
        profile.sources.setdefault("via", src)
        return profile
    if "implemented_extensions" in data or data.get("kind") == "architecture configuration":
        return _from_udb(data, src)
    if "isa" in data and ("dut" in data or "privilege_modes" in data):
        return _from_board_spec(data, src)
    if "facts" in data or "spec_versions" in data or "act_config" in data:
        return _from_board_knowledge(data, src)
    raise ValueError(f"{path}: not a known board description (UDB config, test_config, board spec, "
                     "board knowledge or platform markdown)")


def same_core_profile(profile: HardwareProfile) -> HardwareProfile | None:
    """A structured profile for the same core, to complete a sparse one (e.g. from markdown)."""
    if not profile.core_name:
        return None
    for directory in STRUCTURED_DIRS:
        for path in sorted(directory.glob("*.yaml")):
            try:
                candidate = load_profile_object(path)
            except (ValueError, yaml.YAMLError, OSError):
                continue
            if candidate.core_name and candidate.core_name.lower() == profile.core_name.lower():
                return candidate
    return None


def load_hardware_profile(config_path: str) -> dict[str, Any]:
    """Read a board description and return the standard profile as a dict."""
    profile = load_profile_object(Path(config_path))
    if any(getattr(profile, name) in (None, []) for name in ("xlen", "implemented_isa", "privilege_modes")):
        richer = same_core_profile(profile)
        if richer:
            profile.merge(richer)
    finish(profile)
    data = asdict(profile)
    return data


def get_hardware_context_string(profile: dict[str, Any]) -> str:
    """The profile as a short block for the agent's context."""
    def show(value: Any) -> str:
        return "unknown" if value in (None, [], "") else ", ".join(value) if isinstance(value, list) else str(value)

    name = show(profile.get("board_name"))
    soc = f" ({profile['soc']})" if profile.get("soc") else ""
    lines = [f"Target hardware: {name}{soc}, core {show(profile.get('core_name'))}",
             f"- XLEN: {show(profile.get('xlen'))}; ISA: {show(profile.get('implemented_isa'))}",
             f"- Privilege modes: {show(profile.get('privilege_modes'))}"
             + (f" (privileged spec {profile['priv_version']})" if profile.get("priv_version") else "")]
    mmu = show(profile.get("mmu_type"))
    if profile.get("mmu_modes") and len(profile["mmu_modes"]) > 1:
        mmu += f" (supports {', '.join(profile['mmu_modes'])})"
    lines.append(f"- Address translation: {mmu}")
    core_type = profile.get("core_type") or "unknown"
    lines.append("- Core type: " + {
        "hard_macro": "hard macro (fixed silicon): fixes are firmware, bootloader, runner, compiler, test or "
                      "configuration changes, or a vendor erratum; never RTL",
        "soft_core": f"soft core (open RTL{', ' + profile['rtl_source'] if profile.get('rtl_source') else ''}): "
                     "a hardware bug can be fixed in its HDL",
    }.get(core_type, "unknown: treat as fixed silicon (no RTL changes)"))
    if profile.get("spec_versions"):
        lines.append("- Spec versions implemented: " + ", ".join(f"{k} {v}" for k, v in profile["spec_versions"].items()))
    exts = set(profile.get("extensions") or [])
    if exts:
        absent = [e for e in NOTABLE if e not in exts]
        if absent:
            lines.append(f"- Not implemented: {', '.join(absent)} (a test needing these cannot pass on this core)")
    for fact in (profile.get("facts") or [])[:6]:
        lines.append(f"- Verified: {fact}")
    if profile.get("missing"):
        lines.append(f"- Unknown (no source states it; do not assume): {', '.join(profile['missing'])}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true", help="print the profile instead of the context block")
    args = parser.parse_args()
    profile = load_hardware_profile(args.path)
    print(json.dumps(profile, indent=2) if args.json else get_hardware_context_string(profile))
    return 0


if __name__ == "__main__":
    sys.exit(main())
