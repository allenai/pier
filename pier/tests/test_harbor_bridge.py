"""Tests for harbor_bridge helpers (no Harbor or Docker required)."""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pier.harbor_bridge import (
    _bridge_claude_code,
    _latest_session_dir,
    create_synthetic_task_dir,
    _get_dockerfile_workdir,
    _write_mounts_compose,
    build_trial_result_json,
    get_agent_binary,
    get_compose_project,
    get_agent_exec_env,
    get_post_run_commands,
    get_container_name,
    get_log_capture_env,
    is_valid_agent,
)


class TestGetDockerfileWorkdir:
    def test_parses_workdir(self, tmp_path: Path):
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        (env_dir / "Dockerfile").write_text("FROM ubuntu:24.04\nWORKDIR /app\n")
        assert _get_dockerfile_workdir(env_dir) == "/app"

    def test_last_workdir_wins(self, tmp_path: Path):
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        (env_dir / "Dockerfile").write_text(
            "FROM ubuntu:24.04\nWORKDIR /first\nRUN echo hi\nWORKDIR /second\n"
        )
        assert _get_dockerfile_workdir(env_dir) == "/second"

    def test_defaults_to_app(self, tmp_path: Path):
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        (env_dir / "Dockerfile").write_text("FROM ubuntu:24.04\nRUN echo hi\n")
        assert _get_dockerfile_workdir(env_dir) == "/app"

    def test_no_dockerfile(self, tmp_path: Path):
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        assert _get_dockerfile_workdir(env_dir) == "/app"

    def test_case_insensitive(self, tmp_path: Path):
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        (env_dir / "Dockerfile").write_text("FROM ubuntu:24.04\nworkdir /mydir\n")
        assert _get_dockerfile_workdir(env_dir) == "/mydir"


class TestWriteMountsCompose:
    def test_writes_valid_compose(self, tmp_path: Path):
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        ws = tmp_path / "workspace"
        ws.mkdir()

        path = _write_mounts_compose(trial_dir, ws, "/app")

        assert path == trial_dir / "docker-compose-pier.json"
        data = json.loads(path.read_text())
        volumes = data["services"]["main"]["volumes"]
        assert len(volumes) == 1
        assert volumes[0].endswith(":/app:rw")
        assert str(ws.resolve()) in volumes[0]
        # .pier/ is hidden via tmpfs
        assert data["services"]["main"]["tmpfs"] == ["/app/.pier"]

    def test_without_bind_mount(self, tmp_path: Path):
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        ws = tmp_path / "workspace"
        ws.mkdir()

        path = _write_mounts_compose(trial_dir, ws, "/app", include_bind_mount=False)

        data = json.loads(path.read_text())
        assert "volumes" not in data["services"]["main"]
        assert data["services"]["main"]["tmpfs"] == ["/app/.pier"]

    def test_creates_parent_dirs(self, tmp_path: Path):
        trial_dir = tmp_path / "deep" / "trial"
        ws = tmp_path / "workspace"
        ws.mkdir()

        path = _write_mounts_compose(trial_dir, ws, "/workspace")
        assert path.exists()

    def test_copies_task_instruction(self, tmp_path: Path):
        """task_dir copies instruction.md into workspace/.task/."""
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        ws = tmp_path / "workspace"
        ws.mkdir()
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Do the thing.\n")

        _write_mounts_compose(
            trial_dir, ws, "/app", include_bind_mount=False, task_dir=task_dir
        )

        assert (ws / ".task" / "instruction.md").read_text() == "Do the thing.\n"

    def test_ports(self, tmp_path: Path):
        """ports adds port mappings to compose override."""
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        ws = tmp_path / "workspace"
        ws.mkdir()

        path = _write_mounts_compose(trial_dir, ws, "/app", ports=[8888, 4200])

        data = json.loads(path.read_text())
        assert data["services"]["main"]["ports"] == ["8888:8888", "4200:4200"]

    def test_no_ports_by_default(self, tmp_path: Path):
        """No ports key when ports is not provided."""
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        ws = tmp_path / "workspace"
        ws.mkdir()

        path = _write_mounts_compose(trial_dir, ws, "/app")

        data = json.loads(path.read_text())
        assert "ports" not in data["services"]["main"]

    def test_copies_task_files_to_workspace(self, tmp_path: Path):
        """_write_mounts_compose copies .task/ files into workspace."""
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        ws = tmp_path / "workspace"
        ws.mkdir()
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Do the thing.\n")

        _write_mounts_compose(trial_dir, ws, "/app", task_dir=task_dir)

        assert (ws / ".task").is_dir()
        instruction = ws / ".task" / "instruction.md"
        assert not instruction.is_symlink()
        assert instruction.read_text() == "Do the thing.\n"


def _make_task_dir(tmp_path: Path) -> Path:
    """Create a minimal task directory that Harbor's Task() can load.

    Harbor >=0.7's ``Task._validate_tests`` requires the verifier script
    (``tests/test.sh`` on Linux) to exist whenever no ``verifier.env`` is
    declared, so this helper now creates a no-op script alongside the
    task.toml + instruction.md.
    """
    task = tmp_path / "my-task"
    task.mkdir()
    (task / "task.toml").write_text(
        "[metadata]\nauthor_name = 'test'\n[environment]\n[verifier]\n[agent]\n"
    )
    (task / "instruction.md").write_text("Do the thing.\n")
    tests = task / "tests"
    tests.mkdir()
    test_sh = tests / "test.sh"
    test_sh.write_text("#!/usr/bin/env bash\necho 1.0\n")
    test_sh.chmod(0o755)
    return task


class TestBuildTrialResultJson:
    def test_produces_valid_harbor_trial_result(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)

        result_json = build_trial_result_json(
            task_dir,
            "my-task",
            "s",
            {"reward": 0.75},
            start_time=start,
            end_time=end,
            agent_name="claude-code",
        )

        # Validate it deserializes as a Harbor TrialResult
        from harbor.models.trial.result import TrialResult

        result = TrialResult.model_validate_json(result_json)
        assert result.task_name == "my-task"
        assert result.trial_name == "s"
        assert result.agent_info.name == "claude-code"
        assert result.verifier_result.rewards == {"reward": 0.75}
        assert result.started_at == start
        assert result.finished_at == end

    def test_defaults_agent_to_unknown(self, tmp_path: Path):
        """When no agent is specified, agent_info.name defaults to 'unknown'."""
        task_dir = _make_task_dir(tmp_path)
        result_json = build_trial_result_json(task_dir, "my-task", "s", {"reward": 1.0})

        from harbor.models.trial.result import TrialResult

        result = TrialResult.model_validate_json(result_json)
        assert result.agent_info.name == "unknown"

    def test_scanner_discovers_pier_layout(self, tmp_path: Path):
        """Verify that Harbor's JobScanner finds trials in pier's .pier/ layout."""
        from harbor.viewer.scanner import JobScanner

        task_dir = _make_task_dir(tmp_path)
        pier_dir = tmp_path / "workspace" / ".pier"

        # Simulate two pier verify runs — trials go under .pier/trials/
        for ts in ("20260101T000000Z", "20260101T000500Z"):
            trial = pier_dir / "trials" / ts
            trial.mkdir(parents=True, exist_ok=True)
            result_json = build_trial_result_json(
                task_dir, "my-task", "my-task", {"reward": 1.0}
            )
            (trial / "result.json").write_text(result_json)

        scanner = JobScanner(pier_dir)
        assert "trials" in scanner.list_jobs()
        trials = scanner.list_trials("trials")
        assert trials == ["20260101T000000Z", "20260101T000500Z"]
        result = scanner.get_trial_result("trials", trials[0])
        assert result.verifier_result.rewards == {"reward": 1.0}

    def test_multiple_reward_keys(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path)
        result_json = build_trial_result_json(
            task_dir,
            "my-task",
            "s",
            {"reward": 0.9, "accuracy": 0.85, "completeness": 0.95},
        )

        from harbor.models.trial.result import TrialResult

        result = TrialResult.model_validate_json(result_json)
        assert result.verifier_result.rewards["reward"] == 0.9
        assert result.verifier_result.rewards["accuracy"] == 0.85


class TestContainerNaming:
    def test_compose_project_lowercases(self):
        assert get_compose_project("Pier-WS-AbCd") == "pier-ws-abcd"

    def test_compose_project_replaces_dots(self):
        assert get_compose_project("pier.ws.1234") == "pier-ws-1234"

    def test_container_name(self):
        assert get_container_name("pier-ws-1234") == "pier-ws-1234-main-1"

    def test_container_name_with_uppercase(self):
        assert get_container_name("Pier-WS") == "pier-ws-main-1"


class TestBridgeClaudeCode:
    def test_creates_symlink_structure(self, tmp_path: Path):
        session_dir = tmp_path / "my-session"
        session_dir.mkdir()
        (session_dir / "log.jsonl").write_text("{}\n")

        logs_dir = tmp_path / "logs"
        _bridge_claude_code(session_dir, logs_dir)

        link = logs_dir / "sessions" / "projects" / "my-session"
        assert link.is_symlink()
        assert link.resolve() == session_dir.resolve()
        assert (link / "log.jsonl").read_text() == "{}\n"

    def test_idempotent(self, tmp_path: Path):
        session_dir = tmp_path / "my-session"
        session_dir.mkdir()
        logs_dir = tmp_path / "logs"

        _bridge_claude_code(session_dir, logs_dir)
        _bridge_claude_code(session_dir, logs_dir)  # should not raise

        link = logs_dir / "sessions" / "projects" / "my-session"
        assert link.is_symlink()


class TestGetAgentExecEnv:
    def test_claude_code_env(self):
        env, path_prefix = get_agent_exec_env("claude-code")
        # CLAUDE_CONFIG_DIR is now in get_log_capture_env(), not here.
        assert "CLAUDE_CONFIG_DIR" not in env
        assert env["IS_SANDBOX"] == "1"
        assert path_prefix == "$HOME/.local/bin"

    @pytest.mark.parametrize(
        "agent_name", ["cursor-cli", "kimi-cli", "goose", "hermes"]
    )
    def test_local_bin_agent_path_prefix(self, agent_name: str):
        env, path_prefix = get_agent_exec_env(agent_name)
        assert env == {}
        assert path_prefix == "$HOME/.local/bin"

    @pytest.mark.parametrize(
        "agent_name", ["codex", "gemini-cli", "qwen-coder", "opencode"]
    )
    def test_nvm_agent_path_prefix(self, agent_name: str):
        env, path_prefix = get_agent_exec_env(agent_name)
        # CODEX_HOME is now in get_log_capture_env(), not here.
        assert env == {}
        assert (
            path_prefix
            == '$(find "$HOME/.nvm/versions/node" -mindepth 1 -maxdepth 1 -type d '
            "2>/dev/null | sort | tail -n1)/bin"
        )

    def test_unknown_agent_has_no_special_env(self):
        env, path_prefix = get_agent_exec_env("unknown-agent")
        assert env == {}
        assert path_prefix == ""


class TestIsValidAgent:
    @pytest.mark.parametrize(
        "agent_name",
        [
            "claude-code",
            "codex",
            "cursor-cli",
            "gemini-cli",
            "kimi-cli",
            "opencode",
            "qwen-coder",
        ],
    )
    def test_accepts_known_harbor_agent_names(self, agent_name: str):
        assert is_valid_agent(agent_name) is True

    def test_rejects_old_qwen_alias(self):
        assert is_valid_agent("qwen-code") is False


class TestCreateSyntheticTaskDir:
    def test_creates_dockerfile(self, tmp_path: Path):
        task_dir = create_synthetic_task_dir("ubuntu:24.04", tmp_path)
        dockerfile = task_dir / "environment" / "Dockerfile"
        assert dockerfile.exists()
        assert "FROM ubuntu:24.04" in dockerfile.read_text()
        assert "WORKDIR /app" in dockerfile.read_text()

    def test_creates_task_toml(self, tmp_path: Path):
        task_dir = create_synthetic_task_dir("ubuntu:24.04", tmp_path)
        assert (task_dir / "task.toml").exists()

    def test_idempotent(self, tmp_path: Path):
        """Calling twice doesn't overwrite existing files."""
        task_dir = create_synthetic_task_dir("ubuntu:24.04", tmp_path)
        (task_dir / "task.toml").write_text("custom")
        task_dir2 = create_synthetic_task_dir("different:image", tmp_path)
        assert task_dir == task_dir2
        assert (task_dir / "task.toml").read_text() == "custom"

    def test_path_is_under_temp_root(self, tmp_path: Path):
        task_dir = create_synthetic_task_dir("myimage:latest", tmp_path)
        assert str(task_dir).startswith(str(tmp_path))


class TestExecInContainer:
    def test_basic_exec(self, tmp_path: Path):
        from unittest.mock import MagicMock, patch

        from pier.harbor_bridge import exec_in_container

        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            rc = exec_in_container("pier-ws", tmp_path, ["echo", "hi"])
        assert rc == 0
        args = mock_run.call_args[0][0]
        assert args[0] == "docker"
        assert "exec" in args
        assert "echo" in args

    def test_detach_mode(self, tmp_path: Path):
        from unittest.mock import MagicMock, patch

        from pier.harbor_bridge import exec_in_container

        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            exec_in_container("pier-ws", tmp_path, ["sleep", "999"], detach=True)
        args = mock_run.call_args[0][0]
        assert "-d" in args
        assert "-it" not in args

    def test_no_detach_has_tty(self, tmp_path: Path):
        from unittest.mock import MagicMock, patch

        from pier.harbor_bridge import exec_in_container

        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            exec_in_container("pier-ws", tmp_path, ["bash"])
        args = mock_run.call_args[0][0]
        assert "-it" in args
        assert "-d" not in args

    def test_env_vars_forwarded(self, tmp_path: Path):
        from unittest.mock import MagicMock, patch

        from pier.harbor_bridge import exec_in_container

        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            exec_in_container(
                "pier-ws", tmp_path, ["cmd"], env={"FOO": "bar", "BAZ": "qux"}
            )
        args = mock_run.call_args[0][0]
        assert "FOO=bar" in args
        assert "BAZ=qux" in args

    def test_path_prefix(self, tmp_path: Path):
        from unittest.mock import MagicMock, patch

        from pier.harbor_bridge import exec_in_container

        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            exec_in_container(
                "pier-ws", tmp_path, ["claude"], path_prefix="$HOME/.local/bin"
            )
        args = mock_run.call_args[0][0]
        # Should wrap in sh -c with PATH export
        assert "sh" in args
        assert "-c" in args
        cmd_str = " ".join(args)
        assert "$HOME/.local/bin" in cmd_str
        assert "claude" in cmd_str


class TestGetLogCaptureEnv:
    def test_returns_config_dir_env_vars(self):
        env = get_log_capture_env()
        assert env["CLAUDE_CONFIG_DIR"] == "/logs/agent/sessions"
        assert env["CODEX_HOME"] == "/logs/agent"

    def test_values_derived_from_harbor(self):
        """Env var values match what Harbor's EnvironmentPaths says."""
        from harbor.models.trial.paths import EnvironmentPaths

        env = get_log_capture_env()
        assert (
            env["CLAUDE_CONFIG_DIR"]
            == (EnvironmentPaths.agent_dir / "sessions").as_posix()
        )
        assert env["CODEX_HOME"] == EnvironmentPaths.agent_dir.as_posix()


class TestGetAgentBinary:
    def test_claude_code(self):
        assert get_agent_binary("claude-code") == "claude"

    def test_codex(self):
        assert get_agent_binary("codex") == "codex"

    def test_cursor_cli(self):
        assert get_agent_binary("cursor-cli") == "cursor-agent"

    def test_goose(self):
        assert get_agent_binary("goose") == "goose"

    def test_unknown_agent_returns_none(self):
        assert get_agent_binary("nonexistent-agent") is None


class TestExecInContainerTee:
    def test_script_wraps_command(self, tmp_path: Path):
        from pier.harbor_bridge import exec_in_container

        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            exec_in_container(
                "pier-ws",
                tmp_path,
                ["claude", "--print", "hello"],
                log_path="/logs/agent/exec/2026-01-01_00-00-00/claude-code.txt",
            )
        args = mock_run.call_args[0][0]
        assert "sh" in args
        assert "-c" in args
        cmd_str = args[args.index("-c") + 1]
        assert "script -q -c" in cmd_str
        assert "/logs/agent/exec/2026-01-01_00-00-00/claude-code.txt" in cmd_str
        # script preserves TTY — -it should still be present
        assert "-it" in args

    def test_no_tee_without_flag(self, tmp_path: Path):
        from pier.harbor_bridge import exec_in_container

        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            exec_in_container("pier-ws", tmp_path, ["bash"])
        args = mock_run.call_args[0][0]
        cmd_str = " ".join(args)
        assert "tee" not in cmd_str


class TestLogCapturePipeline:
    """End-to-end: raw agent output → trajectory → trial result."""

    def test_script_output_produces_valid_trial(self, tmp_path: Path):
        """script(1) output with ANSI codes → extract → assemble → valid
        TrialResult that pier view/summarize can read.
        """
        from pier.harbor_bridge import extract_agent_logs
        from pier.trajectory import assemble_trial

        # Simulate script(1) output with ANSI codes.
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "goose.txt").write_text(
            "Script started on 2026-04-10 00:00:00+00:00\n"
            "\x1b[1m> goose\x1b[0m starting session...\n"
            "--- \x1b[32mtool_call\x1b[0m: bash ---\n"
            "echo hello\n"
            "--- \x1b[32mresult\x1b[0m ---\n"
            "hello\n"
            "Script done on 2026-04-10 00:01:00+00:00\n"
        )

        # Step 1: extract_agent_logs (same as verify/capture).
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        agent_context = extract_agent_logs("goose", session_dir, trial_dir / "agent")
        assert (trial_dir / "agent" / "trajectory.json").exists()

        # Step 2: assemble_trial (same as verify/capture).
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        (task_dir / "task.toml").write_text(
            '[metadata]\nauthor_name = "test"\n[environment]\n[verifier]\n[agent]\n'
        )
        (task_dir / "instruction.md").write_text("")
        # Harbor >=0.7 ``Task._validate_tests`` requires the verifier
        # script when ``verifier.env`` isn't declared.
        (task_dir / "tests").mkdir()
        (task_dir / "tests" / "test.sh").write_text("#!/usr/bin/env bash\necho 1.0\n")
        (task_dir / "tests" / "test.sh").chmod(0o755)
        assemble_trial(
            trial_dir,
            task_dir,
            "test-task",
            "test-session",
            {"reward": 1.0},
            agent_name="goose",
            agent_context=agent_context,
        )

        # Step 3: result.json must be valid for Harbor's viewer/summarizer.
        result_json = trial_dir / "result.json"
        assert result_json.exists()
        from harbor.models.trial.result import TrialResult

        result = TrialResult.model_validate_json(result_json.read_text())
        assert result.agent_info.name == "goose"


class TestLatestRunDir:
    def test_returns_none_when_no_runs(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        assert _latest_session_dir(agent_dir, "claude-code") is None

    def test_returns_none_when_empty(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        (agent_dir / "exec").mkdir(parents=True)
        assert _latest_session_dir(agent_dir, "claude-code") is None

    def test_finds_run_with_tee_file(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        run = agent_dir / "exec" / "2026-01-01_00-00-00"
        run.mkdir(parents=True)
        (run / "claude-code.txt").write_text("output\n")
        assert _latest_session_dir(agent_dir, "claude-code") == run

    def test_finds_run_with_sessions(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        run = agent_dir / "exec" / "2026-01-01_00-00-00"
        (run / "sessions").mkdir(parents=True)
        assert _latest_session_dir(agent_dir, "claude-code") == run

    def test_picks_most_recent(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        old = agent_dir / "exec" / "2026-01-01_00-00-00"
        old.mkdir(parents=True)
        (old / "claude-code.txt").write_text("old\n")
        new = agent_dir / "exec" / "2026-01-01_00-05-00"
        new.mkdir(parents=True)
        (new / "claude-code.txt").write_text("new\n")
        assert _latest_session_dir(agent_dir, "claude-code") == new

    def test_skips_runs_for_other_agents(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        goose_run = agent_dir / "exec" / "2026-01-01_00-05-00"
        goose_run.mkdir(parents=True)
        (goose_run / "goose.txt").write_text("output\n")
        assert _latest_session_dir(agent_dir, "claude-code") is None


class TestGetPostRunCommands:
    def test_gemini_copies_trajectory(self):
        cmds = get_post_run_commands("gemini-cli", "/logs/agent/ts")
        assert len(cmds) == 1
        assert "gemini-cli.trajectory.json" in cmds[0]
        assert "/logs/agent/ts" in cmds[0]

    def test_hermes_exports_session(self):
        cmds = get_post_run_commands("hermes", "/logs/agent/ts")
        assert len(cmds) == 1
        assert "hermes-session.jsonl" in cmds[0]
        assert "/logs/agent/ts" in cmds[0]

    def test_unknown_agent_returns_empty(self):
        assert get_post_run_commands("claude-code", "/logs/agent/ts") == []
        assert get_post_run_commands("goose", "/logs/agent/ts") == []


class TestDownloadTask:
    """download_task follows Harbor's TaskClient across its two shapes: a
    coroutine returning a BatchDownloadResult (>= 0.7) and, before that, a
    plain list of paths."""

    def _run(self, download_tasks):
        client_cls = MagicMock()
        client_cls.return_value.download_tasks = download_tasks
        with patch("harbor.tasks.client.TaskClient", client_cls):
            from pier.harbor_bridge import download_task

            return download_task("https://github.com/org/repo", "tasks/t")

    def test_awaits_a_coroutine_and_reads_batch_paths(self, tmp_path: Path):
        batch = MagicMock()
        batch.paths = [tmp_path / "t"]

        async def download_tasks(task_ids):
            assert len(task_ids) == 1
            return batch

        assert self._run(download_tasks) == tmp_path / "t"

    def test_accepts_the_older_list_return(self, tmp_path: Path):
        def download_tasks(task_ids):
            return [tmp_path / "t"]

        assert self._run(download_tasks) == tmp_path / "t"


class TestExtraCompose:
    """Overlays and pier's own override reach Harbor through its public
    ``extra_docker_compose``, in that order, after the task's compose."""

    def _task(self, tmp_path: Path) -> Path:
        td = tmp_path / "task"
        (td / "environment").mkdir(parents=True)
        (td / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
        (td / "task.toml").write_text("[environment]\n[verifier]\n[agent]\n")
        (td / "instruction.md").write_text("do it\n")
        (td / "tests").mkdir()
        (td / "tests" / "test.sh").write_text("#!/bin/bash\n")
        return td

    def test_overlays_then_pier_override(self, tmp_path: Path):
        from pier.harbor_bridge import _make_environment

        overlay = tmp_path / "gateway.yaml"
        overlay.write_text("services: {}\n")
        ws = tmp_path / "ws"
        ws.mkdir()
        environment, _, _ = _make_environment(
            self._task(tmp_path),
            "hsid",
            tmp_path / "trial",
            workspace_dir=ws,
            extra_compose=[str(overlay)],
        )
        extra = [Path(p).name for p in environment.extra_docker_compose_paths]
        assert extra == ["gateway.yaml", "docker-compose-pier.json"]
        names = [Path(p).name for p in environment._docker_compose_paths]
        assert "gateway.yaml" in names and "docker-compose-pier.json" in names

    def test_no_overlays_still_merges_pier_override(self, tmp_path: Path):
        from pier.harbor_bridge import _make_environment

        ws = tmp_path / "ws"
        ws.mkdir()
        environment, _, _ = _make_environment(
            self._task(tmp_path), "hsid", tmp_path / "trial", workspace_dir=ws
        )
        names = [Path(p).name for p in environment._docker_compose_paths]
        assert "docker-compose-pier.json" in names


# ---------------------------------------------------------------------------
# Scoring apart ([verifier] environment_mode = "separate")
# ---------------------------------------------------------------------------

from pier import harbor_bridge  # noqa: E402

SEPARATE = (
    'artifacts = ["/workspace"]\n[verifier]\nenvironment_mode = "separate"\n'
    '[[verifier.collect]]\ncommand = "collect-it"\n'
)


def _task(tmp_path: Path, toml: str) -> Path:
    task = tmp_path / "my-task"
    task.mkdir()
    (task / "task.toml").write_text(toml)
    return task


@pytest.mark.parametrize(
    "toml,apart",
    [
        (SEPARATE, True),
        ('[verifier]\nenvironment_mode = "shared"\n', False),
        ("[verifier]\n", False),
        (
            '[verifier]\nenvironment_mode = "shared"\n[[steps]]\nname = "one"\n'
            '[steps.verifier]\nenvironment_mode = "separate"\n',
            True,
        ),
    ],
)
def test_scores_apart_reads_the_tasks_declaration(tmp_path: Path, toml, apart):
    assert harbor_bridge.scores_apart(_task(tmp_path, toml)) is apart


def _fake_docker(
    calls: list,
    files: dict[str, str],
    exec_rc: int = 0,
    file_sources: tuple[str, ...] = (),
    links: dict[str, tuple[str, Path]] | None = None,
):
    """`docker exec` runs a hook (exit *exec_rc*) or answers `test -d`: every
    source is a directory but *file_sources*. `docker cp` writes `files` for a
    directory source, a file for a file source, and plants *links*: for a
    source, a symlink of that name pointing at a host path."""

    def docker(*args, timeout=None):
        calls.append(args)
        if args[0] == "exec" and "test" in args:
            is_file = args[-1] in file_sources
            return MagicMock(returncode=1 if is_file else 0, stdout="", stderr="")
        if args[0] == "cp":
            source = args[1].split(":", 1)[1]
            dest = Path(args[2])
            if source.endswith("/."):
                source = source.removesuffix("/.")
                if source in files:
                    (dest / files[source]).write_text("x")
                if source in (links or {}):
                    name, host_path = (links or {})[source]
                    (dest / name).symlink_to(host_path)
            elif source in file_sources:
                dest.write_text("x")
        rc = exec_rc if args[0] == "exec" else 0
        return MagicMock(returncode=rc, stdout="", stderr="it failed")

    return docker


def test_record_workspace_writes_what_regrade_reads(tmp_path: Path):
    task = _task(tmp_path, SEPARATE)
    record = tmp_path / "record"
    record.mkdir()
    calls: list = []
    docker = _fake_docker(calls, {"/workspace": "submission.json"})
    with patch("pier.harbor_bridge._docker", docker):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier:claude-code")
    container = harbor_bridge.get_container_name("pier-ws")
    assert calls[0] == ("exec", container, "bash", "-c", "collect-it"), "hook first"
    manifest = json.loads((record / "artifacts" / "manifest.json").read_text())
    assert [(e["source"], e["status"]) for e in manifest] == [
        ("/logs/artifacts", "empty"),
        ("/workspace", "ok"),
    ]
    assert (record / "artifacts" / "workspace" / "submission.json").exists()
    result = json.loads((record / "result.json").read_text())
    assert result["task_name"] == "my-task"
    assert result["agent_info"]["name"] == "pier:claude-code"


def test_a_failing_collect_hook_warns_and_the_scoring_goes_on(tmp_path: Path, caplog):
    """As Harbor runs collect hooks: best effort."""
    task = _task(tmp_path, SEPARATE)
    record = tmp_path / "record"
    record.mkdir()
    with patch("pier.harbor_bridge._docker", _fake_docker([], {}, exec_rc=3)):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    assert "collect hook 'collect-it' failed (exit 3" in caplog.text
    assert (record / "result.json").exists()


def test_a_collect_hook_runs_as_its_user_in_bash(tmp_path: Path):
    """As Harbor runs a hook in main."""
    toml = (
        '[verifier]\nenvironment_mode = "separate"\n'
        '[[verifier.collect]]\ncommand = "collect-it"\nuser = "agent"\n'
    )
    task = _task(tmp_path, toml)
    record = tmp_path / "record"
    record.mkdir()
    calls: list = []
    with patch("pier.harbor_bridge._docker", _fake_docker(calls, {})):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    container = harbor_bridge.get_container_name("pier-ws")
    assert calls[0] == ("exec", "-u", "agent", container, "bash", "-c", "collect-it")


def test_a_file_artifact_is_copied_as_a_file(tmp_path: Path):
    toml = (
        'artifacts = ["/workspace/out.txt"]\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    task = _task(tmp_path, toml)
    record = tmp_path / "record"
    record.mkdir()
    docker = _fake_docker([], {}, file_sources=("/workspace/out.txt",))
    with patch("pier.harbor_bridge._docker", docker):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    assert (record / "artifacts" / "workspace" / "out.txt").is_file()
    manifest = json.loads((record / "artifacts" / "manifest.json").read_text())
    entry = next(e for e in manifest if e["source"] == "/workspace/out.txt")
    assert (entry["type"], entry["status"]) == ("file", "ok")


def test_an_artifact_lands_at_its_declared_destination(tmp_path: Path):
    toml = (
        'artifacts = [{source = "/workspace/results", destination = "results"}]\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    task = _task(tmp_path, toml)
    record = tmp_path / "record"
    record.mkdir()
    docker = _fake_docker([], {"/workspace/results": "scores.json"})
    with patch("pier.harbor_bridge._docker", docker):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    assert (record / "artifacts" / "results" / "scores.json").exists()
    manifest = json.loads((record / "artifacts" / "manifest.json").read_text())
    entry = next(e for e in manifest if e["source"] == "/workspace/results")
    assert entry["destination"] == "artifacts/results"


def test_a_symlink_the_agent_left_is_not_in_the_record(tmp_path: Path, caplog):
    """Regrade copies the record following symlinks, so one left in it would
    copy this host's file into the scored trial."""
    secret = tmp_path / "host-secret"
    secret.write_text("not the agent's")
    task = _task(tmp_path, SEPARATE)
    record = tmp_path / "record"
    record.mkdir()
    docker = _fake_docker([], {}, links={"/workspace": ("leak", secret)})
    with patch("pier.harbor_bridge._docker", docker):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    leak = record / "artifacts" / "workspace" / "leak"
    assert not leak.exists() and not leak.is_symlink()
    assert "artifacts/workspace/leak" in caplog.text
    manifest = json.loads((record / "artifacts" / "manifest.json").read_text())
    entry = next(e for e in manifest if e["source"] == "/workspace")
    assert entry["status"] == "empty", "what the symlink was is not counted"


def test_pier_session_data_in_the_workspace_stays_out_of_the_record(tmp_path: Path):
    """In the container the workspace's .pier/ is an empty tmpfs, and `docker
    cp` reads through it to pier's session and earlier trials beneath."""
    toml = 'artifacts = ["/app"]\n[verifier]\nenvironment_mode = "separate"\n'
    task = _task(tmp_path, toml)
    record = tmp_path / "record"
    record.mkdir()
    fake = _fake_docker([], {})

    def docker(*args, timeout=None):
        if args[0] == "cp" and args[1].endswith(":/app/."):
            dest = Path(args[2])
            (dest / ".pier" / "trials").mkdir(parents=True)
            (dest / ".pier" / "session.json").write_text("{}")
            (dest / "submission.json").write_text("{}")
        return fake(*args, timeout=timeout)

    with patch("pier.harbor_bridge._docker", docker):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    workspace = record / "artifacts" / "app"
    assert (workspace / "submission.json").exists()
    assert list((workspace / ".pier").iterdir()) == []


def test_an_artifact_harbor_would_collect_differently_is_refused(tmp_path: Path):
    toml = (
        'artifacts = [{source = "/workspace", exclude = ["*.log"]}]\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    task = _task(tmp_path, toml)
    record = tmp_path / "record"
    record.mkdir()
    with (
        patch("pier.harbor_bridge._docker", _fake_docker([], {})),
        pytest.raises(RuntimeError, match="excludes"),
    ):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")


def test_a_container_that_will_not_stop_is_not_scored():
    failed = MagicMock(returncode=1, stdout="", stderr="no such container")
    with (
        patch("pier.harbor_bridge._docker", return_value=failed),
        pytest.raises(RuntimeError, match="not scored"),
    ):
        harbor_bridge.stop_workspace_container("pier-ws")


def test_binary_agent_map_reads_the_public_registry(monkeypatch):
    """harbor 0.23+ lists its agents with registered_names()."""
    from harbor.agents import factory

    from pier import harbor_bridge as hb

    class Factory:
        @staticmethod
        def registered_names():
            return ["claude-code"]

    monkeypatch.setattr(factory, "AgentFactory", Factory)
    monkeypatch.setattr(hb, "_binary_agent_map", None)
    monkeypatch.setattr(hb, "get_agent_binary", lambda name: "claude")
    assert hb.get_binary_agent_map() == {"claude": "claude-code"}


def test_binary_agent_map_reads_the_private_map_before_it(monkeypatch):
    """harbor 0.21 and 0.22 have no registered_names(), only _AGENT_MAP."""
    import enum

    from harbor.agents import factory

    from pier import harbor_bridge as hb

    class Name(enum.Enum):
        CLAUDE_CODE = "claude-code"

    class Factory:
        _AGENT_MAP = {Name.CLAUDE_CODE: object}

    monkeypatch.setattr(factory, "AgentFactory", Factory)
    monkeypatch.setattr(hb, "_binary_agent_map", None)
    monkeypatch.setattr(hb, "get_agent_binary", lambda name: "claude")
    assert hb.get_binary_agent_map() == {"claude": "claude-code"}


def test_binary_agent_map_finds_claude_in_the_installed_harbor(monkeypatch):
    """Against the harbor actually installed, not a stand-in."""
    from pier import harbor_bridge as hb

    monkeypatch.setattr(hb, "_binary_agent_map", None)
    assert hb.get_binary_agent_map().get("claude") == "claude-code"


def test_seed_agent_config_is_only_for_another_claude_config_dir():
    setup = harbor_bridge._claude_config_dir()
    exec_dir = "/logs/agent/exec/x/sessions"
    assert harbor_bridge.seed_agent_config_command("claude-code", exec_dir)
    assert harbor_bridge.seed_agent_config_command("claude-code", setup) is None
    assert harbor_bridge.seed_agent_config_command("codex", exec_dir) is None


@pytest.mark.skipif(os.name == "nt", reason="the command runs in a Linux container")
def test_seed_agent_config_copies_what_setup_registered_and_no_history(
    tmp_path: Path, monkeypatch
):
    """Setup registers the onboarding flag, MCP servers, model, skills and
    memory files; an earlier session's history stays where it is."""
    import subprocess

    setup = tmp_path / "sessions"
    for path, text in {
        ".claude.json": "{}",
        "settings.json": "{}",
        "skills/my-skill/SKILL.md": "body",
        "projects/-app/memory/notes.md": "remember",
        "projects/-app/earlier-session.jsonl": "history",
        "todos/t.json": "[]",
    }.items():
        (setup / path).parent.mkdir(parents=True, exist_ok=True)
        (setup / path).write_text(text)
    monkeypatch.setattr(harbor_bridge, "_claude_config_dir", lambda: str(setup))
    exec_dir = tmp_path / "exec" / "x" / "sessions"
    cmd = harbor_bridge.seed_agent_config_command("claude-code", str(exec_dir))
    assert cmd
    subprocess.run(["sh", "-c", cmd], check=True)
    copied = sorted(
        p.relative_to(exec_dir).as_posix() for p in exec_dir.rglob("*") if p.is_file()
    )
    assert copied == [
        ".claude.json",
        "projects/-app/memory/notes.md",
        "settings.json",
        "skills/my-skill/SKILL.md",
    ]


def _mounted_logs(tmp_path: Path) -> tuple[Path, Path]:
    """The agent logs dir mounted from the container, and one exec's session."""
    root = tmp_path / "agent"
    session = root / "exec" / "2026-01-01_00-00-00-000000"
    session.mkdir(parents=True)
    return root, session


@pytest.mark.skipif(
    not harbor_bridge._can_open_without_links(),
    reason="this platform leaves the trajectory beside the logs",
)
def test_copy_trajectory_puts_harbors_trajectory_in_the_trial(tmp_path: Path):
    root, session = _mounted_logs(tmp_path)
    (session / "trajectory.json").write_text('{"steps": []}')
    into = tmp_path / "trial" / "agent"
    assert harbor_bridge.copy_trajectory("claude-code", session, into, root=root)
    assert (into / "trajectory.json").read_text() == '{"steps": []}'


def test_copy_trajectory_declines_where_it_cannot_open_without_links(
    tmp_path: Path, monkeypatch, caplog
):
    """Without dir_fd and O_NOFOLLOW (Windows) a component could be swapped
    for a link between a check and the open: the trajectory stays put."""
    root, session = _mounted_logs(tmp_path)
    (session / "trajectory.json").write_text('{"steps": []}')
    monkeypatch.setattr(harbor_bridge, "_can_open_without_links", lambda: False)
    into = tmp_path / "trial" / "agent"
    assert not harbor_bridge.copy_trajectory("claude-code", session, into, root=root)
    assert not (into / "trajectory.json").exists()
    assert "beside the session's logs" in caplog.text


def test_copy_trajectory_does_not_follow_a_symlinked_file(tmp_path: Path):
    """A symlink the agent left in its mounted logs would resolve on this host."""
    secret = tmp_path / "host-secret"
    secret.write_text("not the agent's")
    root, session = _mounted_logs(tmp_path)
    (session / "trajectory.json").symlink_to(secret)
    into = tmp_path / "trial" / "agent"
    assert not harbor_bridge.copy_trajectory("claude-code", session, into, root=root)
    assert not (into / "trajectory.json").exists()


def test_copy_trajectory_does_not_follow_a_symlinked_directory(tmp_path: Path):
    """The agent can make a session dir itself a link to a host directory."""
    host_dir = tmp_path / "host-dir"
    host_dir.mkdir()
    (host_dir / "trajectory.json").write_text("not the agent's")
    root = tmp_path / "agent"
    (root / "exec").mkdir(parents=True)
    session = root / "exec" / "2026-01-01_00-00-00-000000"
    session.symlink_to(host_dir)
    into = tmp_path / "trial" / "agent"
    assert not harbor_bridge.copy_trajectory("claude-code", session, into, root=root)
    assert not (into / "trajectory.json").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_copy_trajectory_does_not_hang_on_a_fifo(tmp_path: Path):
    """The agent can leave a FIFO where the trajectory goes; opening one for
    reading blocks until someone writes to it."""
    root, session = _mounted_logs(tmp_path)
    os.mkfifo(session / "trajectory.json")
    into = tmp_path / "trial" / "agent"
    copied: list[bool] = []
    worker = threading.Thread(
        target=lambda: copied.append(
            harbor_bridge.copy_trajectory("claude-code", session, into, root=root)
        ),
        daemon=True,
    )
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "copying the trajectory blocked on a FIFO"
    assert copied == [False]


@pytest.mark.skipif(
    not harbor_bridge._can_open_without_links(),
    reason="this platform leaves the trajectory beside the logs",
)
@pytest.mark.parametrize("link", ["symlink", "hard link"])
def test_copy_trajectory_replaces_a_link_in_the_trial_rather_than_writing_through(
    tmp_path: Path, link
):
    """A trial placed with --trial-dir can sit where the agent writes."""
    root, session = _mounted_logs(tmp_path)
    (session / "trajectory.json").write_text('{"steps": []}')
    host_file = tmp_path / "host-file"
    host_file.write_text("untouched")
    into = tmp_path / "trial" / "agent"
    into.mkdir(parents=True)
    if link == "symlink":
        (into / "trajectory.json").symlink_to(host_file)
    else:
        os.link(host_file, into / "trajectory.json")
    assert harbor_bridge.copy_trajectory("claude-code", session, into, root=root)
    assert host_file.read_text() == "untouched"
    written = into / "trajectory.json"
    assert not written.is_symlink() and written.read_text() == '{"steps": []}'


def test_copy_trajectory_does_not_enter_a_linked_agent_dir(tmp_path: Path):
    root, session = _mounted_logs(tmp_path)
    (session / "trajectory.json").write_text('{"steps": []}')
    host_dir = tmp_path / "host-dir"
    host_dir.mkdir()
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "agent").symlink_to(host_dir)
    assert not harbor_bridge.copy_trajectory(
        "claude-code", session, trial / "agent", root=root
    )
    assert list(host_dir.iterdir()) == []


def _harbor_writes_a_trajectory(read):
    """Stands in for Harbor's extraction: records where it read, and writes a
    trajectory there, as Harbor does."""

    def extract(agent_name, logs_dir):
        read.append(logs_dir)
        (logs_dir / "trajectory.json").write_text('{"steps": ["fresh"]}')
        return {"cost_usd": 0.05}

    return extract


@pytest.mark.skipif(
    not harbor_bridge._can_open_without_links(),
    reason="this platform extracts the session where it lies",
)
def test_extract_session_reads_and_writes_a_copy(tmp_path: Path, monkeypatch):
    root, session = _mounted_logs(tmp_path)
    (session / "claude-code.txt").write_text("a session\n")
    read: list[Path] = []
    monkeypatch.setattr(
        harbor_bridge, "extract_agent_context", _harbor_writes_a_trajectory(read)
    )
    context, trajectory = harbor_bridge.extract_session("claude-code", session, root)
    assert context == {"cost_usd": 0.05}
    assert trajectory == b'{"steps": ["fresh"]}'
    assert not read[0].is_relative_to(root), "Harbor read the mounted session"
    assert not (session / "trajectory.json").exists(), "Harbor wrote into it"


@pytest.mark.skipif(
    not harbor_bridge._can_open_without_links(),
    reason="this platform extracts the session where it lies",
)
def test_extract_session_does_not_follow_a_linked_session_dir(
    tmp_path: Path, monkeypatch
):
    """Harbor reads and writes a session dir following links: one the agent
    made a link to a host directory had Harbor write a file there."""
    host_dir = tmp_path / "host-dir"
    host_dir.mkdir()
    root = tmp_path / "agent"
    (root / "exec").mkdir(parents=True)
    session = root / "exec" / "2026-01-01_00-00-00-000000"
    session.symlink_to(host_dir)
    read: list[Path] = []
    monkeypatch.setattr(
        harbor_bridge, "extract_agent_context", _harbor_writes_a_trajectory(read)
    )
    assert harbor_bridge.extract_session("claude-code", session, root) == (None, None)
    assert read == []
    assert list(host_dir.iterdir()) == []


@pytest.mark.skipif(
    not harbor_bridge._can_open_without_links(),
    reason="this platform extracts the session where it lies",
)
def test_extract_session_leaves_links_inside_the_session_out(
    tmp_path: Path, monkeypatch
):
    """A session file linked to a host file would have Harbor read the host's."""
    secret = tmp_path / "host-secret.jsonl"
    secret.write_text('{"not": "the agent\'s"}\n')
    root, session = _mounted_logs(tmp_path)
    projects = session / "sessions" / "projects" / "-workspace"
    projects.mkdir(parents=True)
    (projects / "real.jsonl").write_text("{}\n")
    (projects / "planted.jsonl").symlink_to(secret)
    copied: list[set[str]] = []

    def extract(agent_name, logs_dir):
        copied.append(
            {
                p.name
                for p in (logs_dir / "sessions" / "projects" / "-workspace").iterdir()
            }
        )
        return {"cost_usd": 0.05}

    monkeypatch.setattr(harbor_bridge, "extract_agent_context", extract)
    harbor_bridge.extract_session("claude-code", session, root)
    assert copied == [{"real.jsonl"}]


def test_extract_session_declines_where_links_cannot_be_refused(
    tmp_path: Path, monkeypatch, caplog
):
    """Without dir_fd and O_NOFOLLOW (Windows), reading the session where it
    lies would follow any link the agent left in it."""
    root, session = _mounted_logs(tmp_path)
    read: list[Path] = []
    monkeypatch.setattr(harbor_bridge, "_can_open_without_links", lambda: False)
    monkeypatch.setattr(
        harbor_bridge, "extract_agent_context", _harbor_writes_a_trajectory(read)
    )
    assert harbor_bridge.extract_session("claude-code", session, root) == (None, None)
    assert read == []
    assert "did not extract the agent's session" in caplog.text


@pytest.mark.skipif(
    not harbor_bridge._can_open_without_links(),
    reason="this platform does not extract the session",
)
@pytest.mark.parametrize("bound", ["bytes", "files", "depth"])
def test_extract_session_declines_a_session_past_its_bounds(
    tmp_path: Path, monkeypatch, caplog, bound
):
    """The agent arranges its session: a copy without bounds could fill the
    disk or recurse until verify fails."""
    root, session = _mounted_logs(tmp_path)
    if bound == "bytes":
        monkeypatch.setattr(harbor_bridge, "MAX_SESSION_BYTES", 10)
        (session / "big.jsonl").write_text("x" * 11)
    elif bound == "files":
        monkeypatch.setattr(harbor_bridge, "MAX_SESSION_FILES", 2)
        for i in range(3):
            (session / f"{i}.jsonl").write_text("{}")
    else:
        monkeypatch.setattr(harbor_bridge, "MAX_SESSION_DEPTH", 2)
        (session / "a" / "b" / "c").mkdir(parents=True)
    read: list[Path] = []
    monkeypatch.setattr(
        harbor_bridge, "extract_agent_context", _harbor_writes_a_trajectory(read)
    )
    assert harbor_bridge.extract_session("claude-code", session, root) == (None, None)
    assert read == []
    assert "did not extract the agent's session" in caplog.text


def test_a_symlink_one_artifact_leaves_cannot_redirect_the_next(tmp_path: Path):
    """The first artifact plants a symlink where the second would land: it
    must not write through it, onto this host."""
    host_dir = tmp_path / "host-dir"
    host_dir.mkdir()
    toml = (
        'artifacts = ["/workspace", {source = "/data", destination = "workspace/sub"}]\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    task = _task(tmp_path, toml)
    record = tmp_path / "record"
    record.mkdir()
    docker = _fake_docker(
        [], {"/data": "planted.txt"}, links={"/workspace": ("sub", host_dir)}
    )
    with patch("pier.harbor_bridge._docker", docker):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    assert list(host_dir.iterdir()) == []
    manifest = json.loads((record / "artifacts" / "manifest.json").read_text())
    entry = next(e for e in manifest if e["source"] == "/data")
    assert entry["status"] == "skipped"


@pytest.mark.parametrize(
    "toml,refusal",
    [
        (
            '[verifier]\nenvironment_mode = "separate"\n[[steps]]\nname = "one"\n',
            "with steps",
        ),
        (
            '[environment]\nos = "windows"\n[verifier]\nenvironment_mode = "separate"\n',
            "Windows-container",
        ),
    ],
)
def test_a_task_pier_cannot_record_is_refused_before_anything_runs(
    tmp_path: Path, toml, refusal
):
    task = _task(tmp_path, toml)
    record = tmp_path / "record"
    record.mkdir()
    calls: list = []
    with (
        patch("pier.harbor_bridge._docker", _fake_docker(calls, {})),
        pytest.raises(RuntimeError, match=refusal),
    ):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    assert calls == []


def test_an_artifact_the_agent_never_wrote_stops_the_scoring(tmp_path: Path):
    """Harbor's regrade refuses a record missing a declared artifact, so the
    error names the artifact rather than leaving regrade to refuse."""
    toml = (
        'artifacts = ["/workspace/out.txt"]\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    task = _task(tmp_path, toml)
    record = tmp_path / "record"
    record.mkdir()
    fake = _fake_docker([], {}, file_sources=("/workspace/out.txt",))

    def docker(*args, timeout=None):
        if args[0] == "cp" and args[1].endswith(":/workspace/out.txt"):
            return MagicMock(returncode=1, stdout="", stderr="no such path")
        return fake(*args, timeout=timeout)

    with (
        patch("pier.harbor_bridge._docker", docker),
        pytest.raises(RuntimeError, match="could not copy /workspace/out.txt"),
    ):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    assert not (record / "result.json").exists()


class _Accepted(Exception):
    """Raised where Harbor's regrade first uses a record it has validated."""


@pytest.mark.parametrize(
    "artifacts",
    ["", 'artifacts = ["/workspace"]\n', 'artifacts = ["/logs/artifacts"]\n'],
)
def test_harbor_regrade_accepts_the_record(tmp_path: Path, artifacts):
    """Harbor's regrade checks a record before using it, and refuses one with
    an artifact failed or skipped: the record pier writes passes."""
    from harbor.trial.regrade import RegradeTrial

    task = _task(tmp_path, artifacts + '[verifier]\nenvironment_mode = "separate"\n')
    (task / "instruction.md").write_text("Do it.\n")
    for part in ("environment", "tests"):
        (task / part).mkdir()
        (task / part / "Dockerfile").write_text("FROM alpine\n")
    (task / "tests" / "test.sh").write_text("#!/bin/sh\n")
    record = tmp_path / "record"
    record.mkdir()
    docker = _fake_docker([], {"/workspace": "out.json"})
    with patch("pier.harbor_bridge._docker", docker):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    with (
        patch.object(RegradeTrial, "_seed_from_source", side_effect=_Accepted()),
        pytest.raises(RuntimeError, match="^_Accepted"),
    ):
        harbor_bridge.regrade(record, task, tmp_path / "trials", "scored")


def test_scoring_stops_every_container_of_the_workspace():
    """An overlay's services stop with main: nothing keeps running while the
    work is scored."""
    calls = []

    def docker(*args, timeout=None):
        calls.append(args)
        out = "abc123\ndef456\n" if args[0] == "ps" else ""
        return MagicMock(returncode=0, stdout=out, stderr="")

    with patch("pier.harbor_bridge._docker", docker):
        harbor_bridge.stop_workspace_container("pier-ws")
    project = harbor_bridge.get_compose_project("pier-ws")
    assert calls[0] == (
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={project}",
    )
    assert calls[1] == (
        "stop",
        harbor_bridge.get_container_name("pier-ws"),
        "abc123",
        "def456",
    )


def test_a_failed_agent_log_copy_stops_the_scoring(tmp_path: Path):
    """Regrade without the agent's trajectory would score it misleadingly."""
    task = _task(tmp_path, SEPARATE)
    record = tmp_path / "record"
    record.mkdir()
    fake = _fake_docker([], {})

    def docker(*args, timeout=None):
        if args[0] == "cp" and args[1].endswith(f":{harbor_bridge.AGENT_LOGS}/."):
            return MagicMock(returncode=1, stdout="", stderr="no such path")
        return fake(*args, timeout=timeout)

    with (
        patch("pier.harbor_bridge._docker", docker),
        pytest.raises(RuntimeError, match="agent's logs"),
    ):
        harbor_bridge.record_workspace("pier-ws", task, record, "pier")
    assert not (record / "result.json").exists()


def test_removing_a_workspace_finds_its_containers_by_project():
    calls = []

    def docker(*args, timeout=None):
        calls.append(args)
        out = "abc123\n" if args[0] == "ps" else ""
        return MagicMock(returncode=0, stdout=out, stderr="")

    with patch("pier.harbor_bridge._docker", docker):
        assert harbor_bridge.remove_workspace_containers("pier-ws") == ["abc123"]
    project = harbor_bridge.get_compose_project("pier-ws")
    assert calls == [
        ("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"),
        ("rm", "-f", "abc123"),
    ]
