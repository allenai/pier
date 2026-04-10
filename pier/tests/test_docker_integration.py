"""Integration tests that require Docker.

These tests start real containers and verify the end-to-end flow:
- workspace bind-mount works
- .task/instruction.md is readable inside the container
- skills are mounted
- pier exec forwards env vars and runs commands
- verifier runs and produces a reward
- pier list shows correct container status
- pier stop cleans up properly

Skipped automatically when Docker is not available.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pier.cli import cli

# ---------------------------------------------------------------------------
# Skip if Docker is not available
# ---------------------------------------------------------------------------

_docker_available: bool | None = None


def _check_docker() -> bool:
    global _docker_available
    if _docker_available is None:
        try:
            r = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10,
            )
            _docker_available = r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            _docker_available = False
    return _docker_available


pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32", reason="Docker Compose unsupported on Windows CI"
    ),
    pytest.mark.skipif(not _check_docker(), reason="Docker not available"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    from click.testing import CliRunner

    return CliRunner()


@pytest.fixture
def index_path(tmp_path, monkeypatch):
    idx = tmp_path / "index.json"
    monkeypatch.setattr("pier.cli.INDEX_PATH", idx)
    return idx


@pytest.fixture
def task_dir(tmp_path):
    """A minimal task with a Dockerfile, instruction, skills, and verifier."""
    td = tmp_path / "test-task"
    td.mkdir()
    (td / "task.toml").write_text(
        '[metadata]\nauthor_name = "test"\n[environment]\n[verifier]\n[agent]\n'
    )
    (td / "instruction.md").write_text("Create hello.txt with 'Hello, world!'\n")

    # Dockerfile
    env = td / "environment"
    env.mkdir()
    (env / "Dockerfile").write_text(
        "FROM python:3.12-slim\n"
        "WORKDIR /app\n"
        "RUN echo 'image built' > /app/.image-marker\n"
    )

    # Skills
    skills = env / "skills" / "greet"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# Greet\nSay hello.\n")

    # Verifier — Harbor mounts tests at /tests and verifier output at /logs/verifier
    tests = td / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text(
        "#!/bin/bash\n"
        "if [ -f /app/hello.txt ]; then\n"
        "  echo 1 > /logs/verifier/reward.txt\n"
        "else\n"
        "  echo 0 > /logs/verifier/reward.txt\n"
        "fi\n"
    )

    return td


@pytest.fixture
def workspace(tmp_path):
    return tmp_path / "ws"


def _cleanup_container(workspace: Path):
    """Best-effort cleanup of the container after a test."""
    try:
        from pier.cli import _harbor_trial_dir, _load_session
        from pier.harbor_bridge import stop_environment

        sess = _load_session(workspace)
        task_dir = Path(sess["task_dir"])
        hsid = sess.get("harbor_session_id", "")
        stop_environment(task_dir, hsid, _harbor_trial_dir(workspace), delete=True)
    except Exception:
        pass


def _get_container_name(workspace: Path) -> str:
    """Read session.json and derive the Docker container name."""
    sess = json.loads((workspace / ".pier" / "session.json").read_text())
    return f"{sess['harbor_session_id'].lower().replace('.', '-')}-main-1"


def _start_workspace(runner, task_dir: Path, workspace: Path) -> None:
    """Start a container workspace, asserting success."""
    result = runner.invoke(
        cli,
        ["start", str(task_dir), "-d", str(workspace)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestContainerLifecycle:
    """Start a container, check mounts, stop it."""

    def test_task_instruction_readable_in_container(
        self, runner, index_path, task_dir, workspace
    ):
        """instruction.md is a regular readable file inside the container."""
        try:
            _start_workspace(runner, task_dir, workspace)
            container = _get_container_name(workspace)

            # Check instruction.md is readable
            r = subprocess.run(
                ["docker", "exec", container, "cat", "/app/.task/instruction.md"],
                capture_output=True,
                text=True,
            )
            assert r.returncode == 0, r.stderr
            assert "hello.txt" in r.stdout.lower()

            # Check it's a regular file
            r = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "test",
                    "-f",
                    "/app/.task/instruction.md",
                ],
                capture_output=True,
            )
            assert r.returncode == 0, "instruction.md is not a regular file"

            # Check find can discover it
            r = subprocess.run(
                ["docker", "exec", container, "find", "/app/.task", "-name", "*.md"],
                capture_output=True,
                text=True,
            )
            assert r.returncode == 0
            assert "instruction.md" in r.stdout

        finally:
            _cleanup_container(workspace)

    def test_workspace_files_visible_in_container(
        self, runner, index_path, task_dir, workspace
    ):
        """Files created in the workspace appear inside the container."""
        try:
            _start_workspace(runner, task_dir, workspace)
            container = _get_container_name(workspace)

            # Image marker from Dockerfile RUN should be in workspace
            # (extracted by extract_image_workdir)
            assert (workspace / ".image-marker").exists()

            # Write a file on the host side
            (workspace / "hello.txt").write_text("Hello, world!")

            # Should be visible inside the container
            r = subprocess.run(
                ["docker", "exec", container, "cat", "/app/hello.txt"],
                capture_output=True,
                text=True,
            )
            assert r.returncode == 0
            assert "Hello, world!" in r.stdout

        finally:
            _cleanup_container(workspace)

    def test_container_writes_visible_on_host(
        self, runner, index_path, task_dir, workspace
    ):
        """Files created inside the container appear in the host workspace."""
        try:
            _start_workspace(runner, task_dir, workspace)
            container = _get_container_name(workspace)

            subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "sh",
                    "-c",
                    "echo 'from container' > /app/container-file.txt",
                ],
                check=True,
            )
            assert (
                workspace / "container-file.txt"
            ).read_text().strip() == "from container"

        finally:
            _cleanup_container(workspace)

    def test_pier_dir_hidden_in_container(
        self, runner, index_path, task_dir, workspace
    ):
        """.pier/ is hidden from the container via tmpfs overlay."""
        try:
            _start_workspace(runner, task_dir, workspace)
            container = _get_container_name(workspace)

            # session.json should NOT be visible inside the container
            r = subprocess.run(
                ["docker", "exec", container, "test", "-f", "/app/.pier/session.json"],
                capture_output=True,
            )
            assert r.returncode != 0, ".pier/session.json should be hidden"

        finally:
            _cleanup_container(workspace)

    def test_container_restart_preserves_task(
        self, runner, index_path, task_dir, workspace
    ):
        """After stop + restart, .task/instruction.md is still readable."""
        try:
            _start_workspace(runner, task_dir, workspace)

            # Stop
            result = runner.invoke(
                cli, ["stop", "-d", str(workspace)], catch_exceptions=False
            )
            assert result.exit_code == 0, result.output

            # Restart
            result = runner.invoke(
                cli,
                ["start", str(task_dir), "-d", str(workspace)],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output

            container = _get_container_name(workspace)
            r = subprocess.run(
                ["docker", "exec", container, "cat", "/app/.task/instruction.md"],
                capture_output=True,
                text=True,
            )
            assert r.returncode == 0, r.stderr
            assert "hello.txt" in r.stdout.lower()

        finally:
            _cleanup_container(workspace)

    def test_container_restart_preserves_workspace_files(
        self, runner, index_path, task_dir, workspace
    ):
        """Files written before stop persist after restart."""
        try:
            _start_workspace(runner, task_dir, workspace)
            (workspace / "my-work.txt").write_text("in progress")

            runner.invoke(cli, ["stop", "-d", str(workspace)], catch_exceptions=False)
            runner.invoke(
                cli,
                ["start", str(task_dir), "-d", str(workspace)],
                catch_exceptions=False,
            )

            container = _get_container_name(workspace)
            r = subprocess.run(
                ["docker", "exec", container, "cat", "/app/my-work.txt"],
                capture_output=True,
                text=True,
            )
            assert r.returncode == 0
            assert "in progress" in r.stdout

        finally:
            _cleanup_container(workspace)


# ---------------------------------------------------------------------------
# pier exec
# ---------------------------------------------------------------------------


class TestExecIntegration:
    """Run commands inside a container via pier exec.

    pier exec delegates to subprocess.run(["docker", "exec", ...]) which
    writes directly to stdout, bypassing Click's output capture.  These
    tests use subprocess to call `pier exec` so we can capture stdout.
    """

    def _run_pier_exec(
        self, workspace: Path, *cmd: str, env: dict | None = None
    ) -> subprocess.CompletedProcess:
        """Run `pier exec <cmd>` as a subprocess so stdout is captured."""
        run_env = {**os.environ, **(env or {})}
        return subprocess.run(
            ["pier", "exec", *cmd],
            capture_output=True,
            text=True,
            cwd=str(workspace),
            env=run_env,
        )

    def test_exec_runs_command(self, runner, index_path, task_dir, workspace):
        """pier exec runs a command and captures output."""
        try:
            _start_workspace(runner, task_dir, workspace)

            r = self._run_pier_exec(workspace, "echo", "hello from container")
            assert r.returncode == 0, r.stderr
            assert "hello from container" in r.stdout

        finally:
            _cleanup_container(workspace)

    def test_exec_sees_workspace_files(self, runner, index_path, task_dir, workspace):
        """pier exec runs in the workspace directory."""
        try:
            _start_workspace(runner, task_dir, workspace)
            (workspace / "test-file.txt").write_text("visible")

            r = self._run_pier_exec(workspace, "cat", "test-file.txt")
            assert r.returncode == 0, r.stderr
            assert "visible" in r.stdout

        finally:
            _cleanup_container(workspace)

    def test_exec_forwards_extra_env(self, runner, index_path, task_dir, workspace):
        """pier start -e forwards env vars into pier exec."""
        try:
            result = runner.invoke(
                cli,
                [
                    "start",
                    str(task_dir),
                    "-d",
                    str(workspace),
                    "-e",
                    "TEST_SECRET=s3cret",
                ],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output

            r = self._run_pier_exec(workspace, "sh", "-c", "echo $TEST_SECRET")
            assert r.returncode == 0, r.stderr
            assert "s3cret" in r.stdout

        finally:
            _cleanup_container(workspace)

    def test_exec_nonexistent_command_fails(
        self, runner, index_path, task_dir, workspace
    ):
        """pier exec with a nonexistent command returns non-zero exit code."""
        try:
            _start_workspace(runner, task_dir, workspace)

            r = self._run_pier_exec(workspace, "nonexistent-command-xyz")
            assert r.returncode != 0

        finally:
            _cleanup_container(workspace)

    def test_exec_sets_log_capture_env(self, runner, index_path, task_dir, workspace):
        """pier exec always sets CLAUDE_CONFIG_DIR and CODEX_HOME."""
        try:
            _start_workspace(runner, task_dir, workspace)

            r = self._run_pier_exec(workspace, "sh", "-c", "echo $CLAUDE_CONFIG_DIR")
            assert r.returncode == 0, r.stderr
            assert "/logs/agent/" in r.stdout
            assert "/sessions" in r.stdout

            r = self._run_pier_exec(workspace, "sh", "-c", "echo $CODEX_HOME")
            assert r.returncode == 0, r.stderr
            assert "/logs/agent/" in r.stdout

        finally:
            _cleanup_container(workspace)

    def test_exec_log_capture_env_writable(
        self, runner, index_path, task_dir, workspace
    ):
        """CLAUDE_CONFIG_DIR inside the container is writable (bind-mounted)."""
        try:
            _start_workspace(runner, task_dir, workspace)

            r = self._run_pier_exec(
                workspace,
                "sh",
                "-c",
                'mkdir -p "$CLAUDE_CONFIG_DIR/projects/test" && '
                'echo "session" > "$CLAUDE_CONFIG_DIR/projects/test/log.jsonl" && '
                'cat "$CLAUDE_CONFIG_DIR/projects/test/log.jsonl"',
            )
            assert r.returncode == 0, r.stderr
            assert "session" in r.stdout

            # Verify the file persists on the host via the bind mount
            agent_dir = workspace / ".pier" / "_harbor" / "agent"
            session_files = list(agent_dir.rglob("*.jsonl"))
            assert session_files, "session file should be on host via bind mount"

        finally:
            _cleanup_container(workspace)


# ---------------------------------------------------------------------------
# pier verify
# ---------------------------------------------------------------------------


class TestVerifyIntegration:
    """Run the verifier in a real container."""

    def test_verify_produces_reward(self, runner, index_path, task_dir, workspace):
        """Full start -> solve -> verify cycle produces a reward."""
        try:
            _start_workspace(runner, task_dir, workspace)

            # "Solve" the task
            (workspace / "hello.txt").write_text("Hello, world!")

            result = runner.invoke(cli, ["verify"], catch_exceptions=False)
            assert result.exit_code == 0, result.output
            assert "reward" in result.output.lower()

            # Check trial output was created
            trials_dir = workspace / ".pier" / "trials"
            assert trials_dir.exists()
            trial_dirs = list(trials_dir.iterdir())
            assert len(trial_dirs) == 1
            trial = trial_dirs[0]
            assert (trial / "result.json").exists()

        finally:
            _cleanup_container(workspace)

    def test_verify_unsolved_task(self, runner, index_path, task_dir, workspace):
        """Verify without solving produces reward 0."""
        try:
            _start_workspace(runner, task_dir, workspace)

            result = runner.invoke(cli, ["verify"], catch_exceptions=False)
            assert result.exit_code == 0, result.output

            # Check that reward is 0 (task not solved)
            trials_dir = workspace / ".pier" / "trials"
            trial_dirs = list(trials_dir.iterdir())
            trial = trial_dirs[0]
            result_data = json.loads((trial / "result.json").read_text())
            # Harbor format: verifier_result.rewards.reward or top-level reward
            rewards = result_data.get("verifier_result", {}).get("rewards", result_data)
            reward_val = rewards.get("reward", None)
            assert reward_val is not None
            assert float(reward_val) == 0.0

        finally:
            _cleanup_container(workspace)

    def test_verify_multiple_runs(self, runner, index_path, task_dir, workspace):
        """Multiple verify runs create separate trial directories."""
        import time

        try:
            _start_workspace(runner, task_dir, workspace)

            runner.invoke(cli, ["verify"], catch_exceptions=False)
            # Trial dirs are timestamped to the second — wait to avoid collision
            time.sleep(1.1)
            (workspace / "hello.txt").write_text("Hello, world!")
            runner.invoke(cli, ["verify"], catch_exceptions=False)

            trials_dir = workspace / ".pier" / "trials"
            trial_dirs = sorted(trials_dir.iterdir())
            assert len(trial_dirs) == 2

        finally:
            _cleanup_container(workspace)


# ---------------------------------------------------------------------------
# pier list
# ---------------------------------------------------------------------------


class TestListIntegration:
    """pier list with real containers."""

    def test_list_shows_running_container(
        self, runner, index_path, task_dir, workspace
    ):
        """pier list shows a running container with status 'running'."""
        try:
            _start_workspace(runner, task_dir, workspace)

            result = runner.invoke(cli, ["list"], catch_exceptions=False)
            assert result.exit_code == 0, result.output
            assert "running" in result.output
            assert str(workspace) in result.output

        finally:
            _cleanup_container(workspace)

    def test_list_shows_stopped_container(
        self, runner, index_path, task_dir, workspace
    ):
        """pier list shows a stopped container with status 'stopped'."""
        try:
            _start_workspace(runner, task_dir, workspace)
            runner.invoke(cli, ["stop", "-d", str(workspace)], catch_exceptions=False)

            result = runner.invoke(cli, ["list"], catch_exceptions=False)
            assert result.exit_code == 0, result.output
            assert "stopped" in result.output

        finally:
            _cleanup_container(workspace)


# ---------------------------------------------------------------------------
# pier stop
# ---------------------------------------------------------------------------


class TestStopIntegration:
    """pier stop with real containers."""

    def test_stop_removes_container(self, runner, index_path, task_dir, workspace):
        """pier stop removes the running container."""
        try:
            _start_workspace(runner, task_dir, workspace)
            container = _get_container_name(workspace)

            # Verify it's running
            r = subprocess.run(
                ["docker", "inspect", container, "--format", "{{.State.Running}}"],
                capture_output=True,
                text=True,
            )
            assert "true" in r.stdout.lower()

            result = runner.invoke(
                cli, ["stop", "-d", str(workspace)], catch_exceptions=False
            )
            assert result.exit_code == 0, result.output
            assert "stopped" in result.output

            # Container should no longer be running
            r = subprocess.run(
                ["docker", "inspect", container],
                capture_output=True,
            )
            assert r.returncode != 0, "container should be removed"

        finally:
            _cleanup_container(workspace)


# ---------------------------------------------------------------------------
# Host-mode verify in container
# ---------------------------------------------------------------------------


class TestHostVerifyInContainer:
    """Host-mode verify spins up a temporary container for the verifier."""

    def test_host_verify_runs_in_temp_container(
        self, runner, index_path, task_dir, workspace
    ):
        """pier verify in host mode starts a temp container, verifies, tears it down."""
        result = runner.invoke(
            cli,
            ["start", str(task_dir), "--host", "-d", str(workspace)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        # "Solve" the task
        (workspace / "hello.txt").write_text("Hello, world!")

        result = runner.invoke(cli, ["verify"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "reward" in result.output.lower()

        # Trial output should exist
        trials_dir = workspace / ".pier" / "trials"
        assert trials_dir.exists()
        trial_dirs = list(trials_dir.iterdir())
        assert len(trial_dirs) == 1
