"""Tests for harbor_bridge helpers (no Harbor or Docker required)."""

import json
from datetime import datetime, timezone
from pathlib import Path

from pier.harbor_bridge import (
    _bridge_claude_code,
    _extract_path_prefixes,
    _get_dockerfile_workdir,
    _write_mounts_compose,
    build_trial_result_json,
    detect_agent_from_session_dir,
    find_container_agent_session_dir,
    get_compose_project,
    get_container_name,
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

        assert path == trial_dir / "docker-compose-mounts.json"
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

    def test_mounts_task_instruction(self, tmp_path: Path):
        """task_dir adds instruction.md mount into .task/."""
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        ws = tmp_path / "workspace"
        ws.mkdir()
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Do the thing.\n")

        path = _write_mounts_compose(
            trial_dir, ws, "/app", include_bind_mount=False, task_dir=task_dir
        )

        data = json.loads(path.read_text())
        volumes = data["services"]["main"]["volumes"]
        assert any("/app/.task/instruction.md:ro" in v for v in volumes)

    def test_mounts_task_skills(self, tmp_path: Path):
        """task_dir adds skills/ mount into .task/."""
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        ws = tmp_path / "workspace"
        ws.mkdir()
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        skills = task_dir / "environment" / "skills"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("# Skill\n")

        path = _write_mounts_compose(
            trial_dir, ws, "/app", include_bind_mount=False, task_dir=task_dir
        )

        data = json.loads(path.read_text())
        volumes = data["services"]["main"]["volumes"]
        assert any("/app/.task/skills:ro" in v for v in volumes)

    def test_creates_dot_task_dir_in_workspace(self, tmp_path: Path):
        """_write_mounts_compose creates .task/ in workspace as a mount point."""
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        ws = tmp_path / "workspace"
        ws.mkdir()
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Do the thing.\n")

        _write_mounts_compose(trial_dir, ws, "/app", task_dir=task_dir)

        assert (ws / ".task").is_dir()
        # Should be a plain directory, not contain symlinks
        for child in (ws / ".task").iterdir():
            assert not child.is_symlink()


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


class TestExtractPathPrefixes:
    def test_double_quoted_home(self):
        cmd = 'export PATH="$HOME/.local/bin:$PATH" && claude --print'
        assert _extract_path_prefixes([cmd]) == "$HOME/.local/bin"

    def test_double_quoted_absolute(self):
        cmd = 'export PATH="/root/.local/bin:$PATH" && goose run'
        assert _extract_path_prefixes([cmd]) == "/root/.local/bin"

    def test_single_quoted(self):
        cmd = "export PATH='/opt/agent/bin:$PATH' && agent run"
        assert _extract_path_prefixes([cmd]) == "/opt/agent/bin"

    def test_unquoted(self):
        cmd = "export PATH=/usr/local/agent:$PATH; agent run"
        assert _extract_path_prefixes([cmd]) == "/usr/local/agent"

    def test_multiple_prefixes_across_commands(self):
        cmds = [
            'export PATH="/a:$PATH" && setup',
            'export PATH="/b:$PATH" && run',
        ]
        assert _extract_path_prefixes(cmds) == "/a:/b"

    def test_no_path_modification(self):
        assert _extract_path_prefixes(["echo hello", "run agent"]) == ""

    def test_deduplicates(self):
        cmds = [
            'export PATH="/same:$PATH"',
            'export PATH="/same:$PATH"',
        ]
        assert _extract_path_prefixes(cmds) == "/same"

    def test_empty_commands(self):
        assert _extract_path_prefixes([]) == ""


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
