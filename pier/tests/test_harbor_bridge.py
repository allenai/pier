"""Tests for harbor_bridge helpers (no Harbor or Docker required)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pier.harbor_bridge import (
    _bridge_claude_code,
    create_synthetic_task_dir,
    _get_dockerfile_workdir,
    _write_mounts_compose,
    build_trial_result_json,
    detect_agent_from_session_dir,
    find_container_agent_session_dir,
    get_compose_project,
    get_agent_exec_env,
    get_container_name,
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

    def test_defaults_agent_to_human(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path)
        result_json = build_trial_result_json(task_dir, "my-task", "s", {"reward": 1.0})

        from harbor.models.trial.result import TrialResult

        result = TrialResult.model_validate_json(result_json)
        assert result.agent_info.name == "human"

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


class TestDetectAgentFromSessionDir:
    def test_detects_claude_code(self, tmp_path: Path):
        session = tmp_path / "session"
        session.mkdir()
        (session / "log.jsonl").write_text("{}\n")
        assert detect_agent_from_session_dir(session) == "claude-code"

    def test_returns_none_for_empty_dir(self, tmp_path: Path):
        session = tmp_path / "session"
        session.mkdir()
        assert detect_agent_from_session_dir(session) is None

    def test_returns_none_for_nonexistent_dir(self, tmp_path: Path):
        session = tmp_path / "nonexistent"
        assert detect_agent_from_session_dir(session) is None


class TestFindContainerAgentSessionDir:
    def test_finds_single_claude_session(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        project = agent_dir / "sessions" / "projects" / "abc123"
        project.mkdir(parents=True)
        (project / "session.jsonl").write_text("{}\n")

        result = find_container_agent_session_dir("claude-code", agent_dir)
        assert result == project

    def test_returns_none_for_no_sessions(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        assert find_container_agent_session_dir("claude-code", agent_dir) is None

    def test_returns_none_for_empty_projects(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        projects = agent_dir / "sessions" / "projects"
        projects.mkdir(parents=True)
        assert find_container_agent_session_dir("claude-code", agent_dir) is None

    def test_returns_none_for_multiple_sessions(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        projects = agent_dir / "sessions" / "projects"
        for name in ("proj1", "proj2"):
            d = projects / name
            d.mkdir(parents=True)
            (d / "session.jsonl").write_text("{}\n")

        result = find_container_agent_session_dir("claude-code", agent_dir)
        assert result is None

    def test_returns_none_for_unknown_agent(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        assert find_container_agent_session_dir("unknown-agent", agent_dir) is None

    def test_ignores_dirs_without_jsonl(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        projects = agent_dir / "sessions" / "projects"
        # One dir with jsonl, one without
        real = projects / "real"
        real.mkdir(parents=True)
        (real / "session.jsonl").write_text("{}\n")
        empty = projects / "empty"
        empty.mkdir()

        result = find_container_agent_session_dir("claude-code", agent_dir)
        assert result == real

    def test_finds_single_codex_session(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        session = agent_dir / "sessions" / "2026" / "04" / "codex-session"
        session.mkdir(parents=True)

        result = find_container_agent_session_dir("codex", agent_dir)
        assert result == session

    def test_codex_returns_none_for_multiple_sessions(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        (agent_dir / "sessions" / "a" / "one").mkdir(parents=True)
        (agent_dir / "sessions" / "b" / "two").mkdir(parents=True)

        result = find_container_agent_session_dir("codex", agent_dir)
        assert result is None

    @pytest.mark.parametrize(
        ("agent_name", "filename"),
        [
            ("cursor-cli", "cursor-cli.txt"),
            ("gemini-cli", "gemini-cli.trajectory.json"),
            ("kimi-cli", "kimi-cli.txt"),
            ("opencode", "opencode.txt"),
        ],
    )
    def test_finds_direct_log_agents(
        self, tmp_path: Path, agent_name: str, filename: str
    ):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / filename).write_text("log\n")

        result = find_container_agent_session_dir(agent_name, agent_dir)

        assert result == agent_dir

    def test_finds_qwen_coder_session_root(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        session = agent_dir / "qwen-sessions" / "project-a"
        session.mkdir(parents=True)
        (session / "events.jsonl").write_text("{}\n")

        result = find_container_agent_session_dir("qwen-coder", agent_dir)

        assert result == agent_dir


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
        assert env["CLAUDE_CONFIG_DIR"] == "/logs/agent/sessions"
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
        if agent_name == "codex":
            assert env["CODEX_HOME"] == "/logs/agent"
        else:
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
