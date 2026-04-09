"""Docker-backed agent install/exec integration smoke tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.docker_integration


if os.environ.get("PIER_RUN_DOCKER_INTEGRATION") != "1":
    pytest.skip(
        "set PIER_RUN_DOCKER_INTEGRATION=1 to run Docker integration tests",
        allow_module_level=True,
    )

if shutil.which("docker") is None:
    pytest.skip(
        "docker is required for Docker integration tests", allow_module_level=True
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_IMAGE = os.environ.get("PIER_TEST_IMAGE", "ubuntu:24.04")
# Keep this set representative rather than exhaustive:
# - claude-code exercises the Claude-specific interactive setup path
# - codex/gemini-cli exercise NVM-installed CLIs
# - kimi-cli exercises ~/.local/bin installs on plain base images
ALL_AGENT_CASES: list[tuple[str, str]] = [
    ("claude-code", "claude"),
    ("codex", "codex"),
    ("gemini-cli", "gemini"),
    ("kimi-cli", "kimi"),
]


def _selected_agent_cases() -> list[tuple[str, str]]:
    selected = os.environ.get("PIER_TEST_AGENTS")
    if not selected:
        return ALL_AGENT_CASES

    selected_names = {name.strip() for name in selected.split(",") if name.strip()}
    return [case for case in ALL_AGENT_CASES if case[0] in selected_names]


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(("agent_name", "cli_name"), _selected_agent_cases())
def test_task_free_agent_install_and_exec(
    tmp_path: Path, agent_name: str, cli_name: str
):
    workspace = tmp_path / f"ws-{agent_name}"

    start = _run(
        [
            "uv",
            "run",
            "pier",
            "start",
            "-d",
            str(workspace),
            "--image",
            TEST_IMAGE,
            "--agent",
            agent_name,
        ],
        cwd=REPO_ROOT,
    )

    try:
        assert start.returncode == 0, start.stdout + start.stderr

        session = json.loads((workspace / ".pier" / "session.json").read_text())
        assert session["agents"] == [agent_name]
        assert session["image"] == TEST_IMAGE

        version = _run(
            ["uv", "run", "pier", "exec", "--", cli_name, "--version"],
            cwd=workspace,
        )
        assert version.returncode == 0, version.stdout + version.stderr
        assert version.stdout.strip()
    finally:
        if (workspace / ".pier" / "session.json").exists():
            _run(["uv", "run", "pier", "stop"], cwd=workspace)
