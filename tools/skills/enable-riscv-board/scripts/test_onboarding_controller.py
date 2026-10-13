#!/usr/bin/env python3
"""Integration tests for the durable onboarding controller."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
CONTROLLER = SCRIPT_DIR / "onboarding_controller.py"
FIXTURES = SCRIPT_DIR.parent / "assets" / "manifests" / "milkv_megrez.yaml"
REPO_ROOT = SCRIPT_DIR.parents[3]
RESEARCH = REPO_ROOT / "artifacts" / "onboarding" / "milkv_megrez_20261001" / "research_v2"


def command(*args: str, cwd: Path | None = None, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode != expected:
        raise AssertionError(
            f"command returned {result.returncode}, expected {expected}: {args}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


class OnboardingControllerTests(unittest.TestCase):
    def test_resume_across_invocations_and_scoped_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "runner"
            inputs = root / "inputs"
            reports = inputs / "research"
            run_dir = root / "state" / "megrez-run"
            repo.mkdir()
            reports.mkdir(parents=True)

            command("git", "init", "-b", "stable", cwd=repo)
            command("git", "config", "user.email", "controller-test@example.invalid", cwd=repo)
            command("git", "config", "user.name", "Controller Test", cwd=repo)
            command(
                "git",
                "remote",
                "add",
                "origin",
                "https://github.com/example/runner.git",
                cwd=repo,
            )

            build_script = repo / "cert_harness" / "tools" / "build_runner.sh"
            profile = repo / "cert_harness" / "profiles" / "UART_M_MODE.env"
            build_script.parent.mkdir(parents=True)
            profile.parent.mkdir(parents=True)
            profile.write_text("PROFILE_NAME=UART_M_MODE\n", encoding="utf-8")
            build_script.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
board=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --board) board="$2"; shift 2 ;;
    *) exit 2 ;;
  esac
done
out="cert_harness/build/$board/UART_M_MODE"
mkdir -p "$out"
printf 'firmware' > "$out/firmware.bin"
printf 'image' > "$out/boot_image.bin"
printf '{"board":"%s"}\n' "$board" > "$out/manifest.json"
(cd "$out" && sha256sum firmware.bin boot_image.bin > sha256sums.txt)
""",
                encoding="utf-8",
            )
            (repo / ".gitignore").write_text("cert_harness/build/\n", encoding="utf-8")
            command("git", "add", ".", cwd=repo)
            command("git", "commit", "-m", "stable runner", cwd=repo)
            stable_sha = command("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
            command("git", "update-ref", "refs/remotes/origin/stable", stable_sha, cwd=repo)
            command("git", "switch", "-c", "onboard/megrez/test", cwd=repo)

            manifest = inputs / "milkv_megrez.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(FIXTURES.read_text(encoding="utf-8"), encoding="utf-8")
            for source in RESEARCH.glob("*.yaml"):
                (reports / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

            contract = inputs / "system-contract.yaml"
            contract.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 1,
                        "repositories": {
                            "runner": {
                                "canonical_url": "https://github.com/example/runner.git",
                                "accepted_origins": ["https://github.com/example/runner.git"],
                                "stable_branch": "stable",
                                "stable_remote_ref": "refs/remotes/origin/stable",
                                "production_profile": "UART_M_MODE",
                                "build_entrypoint": "cert_harness/tools/build_runner.sh",
                                "profile_file": "cert_harness/profiles/UART_M_MODE.env",
                                "board_environment_pattern": "cert_harness/boards/{board}.env",
                                "board_linker_pattern": "cert_harness/boards/{board}/link.ld",
                                "board_adapter_pattern": "cert_harness/board_adapters/{board}.sh",
                            }
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            command(
                sys.executable,
                str(CONTROLLER),
                "init",
                "--repo-root",
                str(repo),
                "--manifest",
                str(manifest),
                "--research-dir",
                str(reports),
                "--run-dir",
                str(run_dir),
                "--run-id",
                "megrez-test",
                "--contract",
                str(contract),
            )
            command(
                sys.executable,
                str(CONTROLLER),
                "resume",
                "--run-dir",
                str(run_dir),
                "--max-transitions",
                "3",
            )
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_state"], "workspace_ready")
            self.assertEqual(state["status"], "waiting_agent")

            command("git", "switch", "-c", "unrelated-branch", cwd=repo)
            command(
                sys.executable,
                str(CONTROLLER),
                "resume",
                "--run-dir",
                str(run_dir),
                expected=2,
            )
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_state"], "blocked")
            self.assertIn("feature branch changed", state["history"][-1]["detail"])
            command("git", "switch", "onboard/megrez/test", cwd=repo)
            command(
                sys.executable,
                str(CONTROLLER),
                "retry",
                "--run-dir",
                str(run_dir),
            )

            board = "milkv_megrez_eic7700x"
            environment = repo / "cert_harness" / "boards" / f"{board}.env"
            linker = repo / "cert_harness" / "boards" / board / "link.ld"
            adapter = repo / "cert_harness" / "board_adapters" / f"{board}.sh"
            environment.parent.mkdir(parents=True, exist_ok=True)
            linker.parent.mkdir(parents=True, exist_ok=True)
            adapter.parent.mkdir(parents=True, exist_ok=True)
            environment.write_text(f"BOARD_NAME={board}\n", encoding="utf-8")
            linker.write_text("ENTRY(_start)\n", encoding="utf-8")
            adapter.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

            command(
                sys.executable,
                str(CONTROLLER),
                "resume",
                "--run-dir",
                str(run_dir),
                "--execute-build",
            )
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_state"], "awaiting_external_approval")
            self.assertEqual(state["status"], "waiting_approval")
            patch_hash = state["review"]["patch_sha256"]
            self.assertTrue(Path(state["review"]["patch"]).is_file())

            command(
                sys.executable,
                str(CONTROLLER),
                "approve",
                "--run-dir",
                str(run_dir),
                "--gate",
                "push_or_open_pull_request",
                "--actor",
                "test-reviewer",
                "--artifact-sha256",
                patch_hash,
            )
            command(
                sys.executable,
                str(CONTROLLER),
                "resume",
                "--run-dir",
                str(run_dir),
            )
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_state"], "offline_complete")
            self.assertEqual(state["status"], "complete")
            self.assertGreaterEqual(len(state["history"]), 9)


if __name__ == "__main__":
    unittest.main()
