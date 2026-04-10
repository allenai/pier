"""Tests for harbor_bridge helpers (no Harbor or Docker required)."""

import json
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
    """Create a minimal task directory that Harbor's Task() can load."""
    task = tmp_path / "my-task"
    task.mkdir()
    (task / "task.toml").write_text(
        "[metadata]\nauthor_name = 'test'\n[environment]\n[verifier]\n[agent]\n"
    )
    (task / "instruction.md").write_text("Do the thing.\n")
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
