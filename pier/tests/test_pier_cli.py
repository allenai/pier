"""Tests for pier CLI — start, verify, stop, list commands."""

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from pier.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def index_path(tmp_path, monkeypatch):
    """Redirect the global index to a temp file."""
    idx = tmp_path / "index.json"
    monkeypatch.setattr("pier.cli.INDEX_PATH", idx)
    return idx


@pytest.fixture
def task_dir(tmp_path):
    """Minimal task directory with task.toml."""
    td = tmp_path / "my-task"
    td.mkdir()
    (td / "task.toml").write_text(
        '[metadata]\nauthor_name = "test"\n[environment]\n[verifier]\n[agent]\n'
    )
    (td / "instruction.md").write_text("# Do the thing\n")
    tests = td / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text("#!/bin/bash\necho ok\n")
    env = td / "environment"
    env.mkdir()
    context = env / "context"
    context.mkdir()
    (context / "guide.md").write_text("# Guide\n")
    return td


def _container_session(
    task_dir: str = "/tmp/task",
    harbor_session_id: str = "pier-ws",
    agents: list[str] | None = None,
) -> dict:
    """Build a minimal container-mode session dict."""
    return {
        "mode": "container",
        "task_dir": task_dir,
        "task_ref": "my-task",
        "harbor_session_id": harbor_session_id,
        "agents": agents or [],
        "started_at": "2026-02-24T12:00:00+00:00",
    }


def _host_session(task_dir: str = "/tmp/task") -> dict:
    """Build a minimal host-mode session dict."""
    return {
        "mode": "host",
        "task_dir": task_dir,
        "task_ref": "my-task",
        "started_at": "2026-02-24T12:00:00+00:00",
    }


def _write_session(workspace: Path, data: dict, index_path: Path) -> None:
    """Write a session.json into workspace/.pier/ and register in index."""
    pier_dir = workspace / ".pier"
    pier_dir.mkdir(parents=True, exist_ok=True)
    (pier_dir / "session.json").write_text(json.dumps(data))
    # Update index
    index = json.loads(index_path.read_text()) if index_path.exists() else []
    ws_str = str(workspace.resolve())
    if ws_str not in index:
        index.append(ws_str)
    index_path.write_text(json.dumps(index))


# ---------------------------------------------------------------------------
# pier --help
# ---------------------------------------------------------------------------


def test_help(runner):
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "start" in result.output
    assert "exec" in result.output
    assert "verify" in result.output
    assert "stop" in result.output
    assert "list" in result.output


def test_help_no_init(runner):
    result = runner.invoke(cli, ["--help"])
    assert "init" not in result.output


# ---------------------------------------------------------------------------
# pier start — task path resolution
# ---------------------------------------------------------------------------


def test_start_rejects_url_without_fragment(runner, index_path):
    """Remote references must include #path."""
    result = runner.invoke(cli, ["start", "https://github.com/org/repo", "--host"])
    assert result.exit_code != 0
    assert "#" in result.output


@patch("pier.harbor_bridge.download_task")
def test_start_remote_host(
    mock_download, runner, index_path, task_dir, monkeypatch, tmp_path
):
    """URL#path downloads the task then starts a host session."""
    mock_download.return_value = task_dir
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PWD", str(cwd))
    result = runner.invoke(
        cli,
        ["start", "https://github.com/org/repo#tasks/my-task", "--host"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    mock_download.assert_called_once_with(
        "https://github.com/org/repo", "tasks/my-task"
    )
    ws = cwd / "my-task"
    assert (ws / ".pier" / "session.json").exists()


@patch("pier.harbor_bridge.start_environment")
@patch("pier.harbor_bridge.download_task")
def test_start_remote_container(
    mock_download, mock_start, runner, index_path, task_dir, monkeypatch, tmp_path
):
    """URL#path downloads the task then starts a container session."""
    mock_download.return_value = task_dir
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PWD", str(cwd))
    result = runner.invoke(
        cli,
        ["start", "https://github.com/org/repo#tasks/my-task"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    mock_download.assert_called_once()
    mock_start.assert_called_once()


@patch("pier.harbor_bridge.download_task", side_effect=Exception("clone failed"))
def test_start_remote_download_failure(mock_download, runner, index_path):
    result = runner.invoke(cli, ["start", "https://github.com/org/repo#tasks/bad"])
    assert result.exit_code != 0
    assert "failed" in result.output.lower()


@patch(
    "pier.harbor_bridge.download_task",
    side_effect=ImportError("No module named 'harbor'"),
)
def test_start_remote_harbor_import_error(mock_download, runner, index_path):
    """When Harbor's internals fail to import, error mentions Harbor."""
    result = runner.invoke(
        cli,
        ["start", "https://github.com/org/repo#tasks/my-task", "--host"],
    )
    assert result.exit_code != 0
    assert "harbor" in result.output.lower()


def test_start_local_requires_dir(runner, index_path, task_dir):
    """Local tasks without -d should fail."""
    result = runner.invoke(cli, ["start", str(task_dir)])
    assert result.exit_code != 0
    assert "-d" in result.output


@patch("pier.harbor_bridge.download_task")
def test_start_remote_defaults_dir(
    mock_download, runner, index_path, task_dir, monkeypatch, tmp_path
):
    """Remote tasks default workspace to ./<task-name>."""
    mock_download.return_value = task_dir
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PWD", str(cwd))
    result = runner.invoke(
        cli,
        ["start", "https://github.com/org/repo#tasks/my-task", "--host"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert (cwd / "my-task").exists()
    assert (cwd / "my-task" / ".pier" / "session.json").exists()


# ---------------------------------------------------------------------------
# pier start (container mode)
# ---------------------------------------------------------------------------


@patch("pier.harbor_bridge.start_environment")
def test_start_creates_session(mock_start, runner, index_path, task_dir, tmp_path):
    ws = tmp_path / "ws"
    result = runner.invoke(
        cli, ["start", str(task_dir), "-d", str(ws)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "ready" in result.output.lower()
    assert "pier exec bash" in result.output

    assert (ws / ".pier" / "session.json").exists()
    session = json.loads((ws / ".pier" / "session.json").read_text())
    assert session["harbor_session_id"].startswith(f"pier-{ws.resolve().name}-")
    assert session["mode"] == "container"
    assert session["task_dir"] == str(task_dir)
    mock_start.assert_called_once()


@patch("pier.harbor_bridge.start_environment")
def test_start_creates_workspace_with_env_files(
    mock_start, runner, index_path, task_dir, tmp_path
):
    """Container mode seeds workspace with environment files (fallback, no Docker)."""
    ws = tmp_path / "ws"
    runner.invoke(cli, ["start", str(task_dir), "-d", str(ws)], catch_exceptions=False)
    # Workspace gets environment/ contents (minus Dockerfile), not pier extras
    assert (ws / "context" / "guide.md").exists()
    assert not (ws / "instruction.md").exists()


@patch("pier.harbor_bridge.start_environment")
def test_start_container_no_task_symlinks(
    mock_start, runner, index_path, task_dir, tmp_path
):
    """Container mode does NOT create .task/ symlinks (Docker mounts handle it)."""
    ws = tmp_path / "ws"
    runner.invoke(cli, ["start", str(task_dir), "-d", str(ws)], catch_exceptions=False)
    dot_task = ws / ".task"
    # .task/ dir should not exist (created later by _write_mounts_compose)
    # or if it exists, should not contain symlinks
    if dot_task.exists():
        for child in dot_task.iterdir():
            assert not child.is_symlink(), f"{child} should not be a symlink"


@patch("pier.harbor_bridge.start_environment")
def test_start_passes_workspace_to_harbor(
    mock_start, runner, index_path, task_dir, tmp_path
):
    """Container mode passes workspace_dir to harbor_bridge for bind mount."""
    ws = tmp_path / "ws"
    runner.invoke(cli, ["start", str(task_dir), "-d", str(ws)], catch_exceptions=False)
    assert mock_start.called
    kwargs = mock_start.call_args[1]
    assert kwargs["workspace_dir"] == ws


@patch("pier.harbor_bridge.is_environment_running", return_value=True)
@patch("pier.harbor_bridge.start_environment")
def test_start_already_running_is_noop(
    mock_start, mock_running, runner, index_path, task_dir, tmp_path
):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    result = runner.invoke(cli, ["start", str(task_dir), "-d", str(ws)])
    assert result.exit_code == 0
    assert "already running" in result.output
    mock_start.assert_not_called()


def test_start_no_task_toml(runner, index_path, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(cli, ["start", str(empty), "-d", str(tmp_path / "ws")])
    assert result.exit_code != 0


@patch("pier.harbor_bridge.is_environment_running", return_value=False)
@patch("pier.harbor_bridge.start_environment")
def test_start_restarts_stopped_container(
    mock_start, mock_running, runner, index_path, task_dir, tmp_path
):
    """If a session file exists but the container is dead, start restarts it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    result = runner.invoke(
        cli, ["start", str(task_dir), "-d", str(ws)], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "ready" in result.output.lower()
    mock_start.assert_called_once()


@patch("pier.harbor_bridge.setup_agent")
@patch("pier.harbor_bridge.is_valid_agent", return_value=True)
@patch("pier.harbor_bridge.start_environment")
def test_start_with_agent_calls_setup(
    mock_start, mock_valid, mock_setup, runner, index_path, task_dir, tmp_path
):
    """--agent installs the agent via Harbor's setup()."""
    ws = tmp_path / "ws"
    result = runner.invoke(
        cli,
        ["start", str(task_dir), "-d", str(ws), "--agent", "claude-code"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "claude-code installed" in result.output
    mock_setup.assert_called_once()
    args = mock_setup.call_args[0]
    assert args[0] == task_dir  # task_dir
    assert args[3] == "claude-code"  # agent_name

    session = json.loads((ws / ".pier" / "session.json").read_text())
    assert session["agents"] == ["claude-code"]


@patch("pier.harbor_bridge.is_valid_agent", return_value=False)
@patch("pier.harbor_bridge.start_environment")
def test_start_with_unknown_agent_fails(
    mock_start, mock_valid, runner, index_path, task_dir, tmp_path
):
    """--agent with unknown agent name fails."""
    ws = tmp_path / "ws"
    result = runner.invoke(
        cli,
        ["start", str(task_dir), "-d", str(ws), "--agent", "unknown-agent"],
    )
    assert result.exit_code != 0
    assert "Unknown agent" in result.output


@patch("pier.harbor_bridge.setup_agent", side_effect=RuntimeError("install failed"))
@patch("pier.harbor_bridge.is_valid_agent", return_value=True)
@patch("pier.harbor_bridge.start_environment")
def test_start_agent_setup_failure(
    mock_start, mock_valid, mock_setup, runner, index_path, task_dir, tmp_path
):
    """--agent reports error when agent setup fails."""
    ws = tmp_path / "ws"
    result = runner.invoke(
        cli,
        ["start", str(task_dir), "-d", str(ws), "--agent", "claude-code"],
    )
    assert result.exit_code != 0
    assert "Agent setup failed" in result.output


@patch("pier.harbor_bridge.setup_agent")
@patch("pier.harbor_bridge.is_valid_agent", return_value=True)
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_start_agent_into_existing_session(
    mock_running, mock_valid, mock_setup, runner, index_path, task_dir, tmp_path
):
    """--agent on an existing running workspace installs the agent without recreating."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    result = runner.invoke(
        cli,
        ["start", str(task_dir), "-d", str(ws), "--agent", "claude-code"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "claude-code installed" in result.output
    mock_setup.assert_called_once()

    session = json.loads((ws / ".pier" / "session.json").read_text())
    assert session["agents"] == ["claude-code"]
    assert ws.exists()


@patch("pier.harbor_bridge.setup_agent")
@patch("pier.harbor_bridge.is_valid_agent", return_value=True)
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_start_second_agent_appends(
    mock_running, mock_valid, mock_setup, runner, index_path, task_dir, tmp_path
):
    """--agent on a workspace that already has an agent appends to the list."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = _container_session(task_dir=str(task_dir), agents=["claude-code"])
    _write_session(ws, sess, index_path)
    result = runner.invoke(
        cli,
        ["start", str(task_dir), "-d", str(ws), "--agent", "codex"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    session = json.loads((ws / ".pier" / "session.json").read_text())
    assert session["agents"] == ["claude-code", "codex"]


# ---------------------------------------------------------------------------
# pier start (no task_path — agent into existing workspace)
# ---------------------------------------------------------------------------


@patch("pier.harbor_bridge.setup_agent")
@patch("pier.harbor_bridge.is_valid_agent", return_value=True)
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_start_agent_from_cwd(
    mock_running,
    mock_valid,
    mock_setup,
    runner,
    index_path,
    task_dir,
    tmp_path,
    monkeypatch,
):
    """pier start --agent from inside a workspace installs the agent."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("PWD", str(ws))
    result = runner.invoke(
        cli,
        ["start", "--agent", "claude-code"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "claude-code installed" in result.output
    mock_setup.assert_called_once()
    session = json.loads((ws / ".pier" / "session.json").read_text())
    assert session["agents"] == ["claude-code"]


def test_start_no_task_no_agent(runner, index_path):
    """pier start with no task_path and no --agent gives a helpful error."""
    result = runner.invoke(cli, ["start"])
    assert result.exit_code != 0
    assert "task path" in result.output.lower()


def test_start_agent_outside_workspace(runner, index_path, tmp_path, monkeypatch):
    """pier start --agent outside a workspace gives a helpful error."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("PWD", str(outside))
    result = runner.invoke(cli, ["start", "--agent", "claude-code"])
    assert result.exit_code != 0
    assert "Not inside a workspace" in result.output


@patch("pier.harbor_bridge.setup_agent")
@patch("pier.harbor_bridge.is_valid_agent", return_value=True)
@patch("pier.harbor_bridge.start_environment")
@patch("pier.harbor_bridge.is_environment_running", return_value=False)
def test_start_agent_from_cwd_container_stopped(
    mock_running,
    mock_start,
    mock_valid,
    mock_setup,
    runner,
    index_path,
    task_dir,
    tmp_path,
    monkeypatch,
):
    """pier start --agent from inside a workspace with stopped container restarts it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("PWD", str(ws))
    result = runner.invoke(cli, ["start", "--agent", "claude-code"])
    assert result.exit_code == 0
    assert "starting" in result.output.lower()
    mock_start.assert_called_once()


@patch("pier.harbor_bridge.start_environment")
@patch("pier.harbor_bridge.is_environment_running", return_value=False)
def test_start_no_args_restarts_stopped_container(
    mock_running, mock_start, runner, index_path, task_dir, tmp_path, monkeypatch
):
    """pier start (no args) from inside a workspace restarts a stopped container."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("PWD", str(ws))
    result = runner.invoke(cli, ["start"])
    assert result.exit_code == 0
    assert "starting" in result.output.lower()
    mock_start.assert_called_once()


@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_start_no_args_running_container_is_noop(
    mock_running, runner, index_path, task_dir, tmp_path, monkeypatch
):
    """pier start (no args) when container is already running is a noop."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("PWD", str(ws))
    result = runner.invoke(cli, ["start"])
    assert result.exit_code == 0
    assert "already running" in result.output.lower()


def test_start_no_args_host_mode_is_noop(runner, index_path, tmp_path, monkeypatch):
    """pier start (no args) in a host-mode workspace is a noop."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("PWD", str(ws))
    result = runner.invoke(cli, ["start"])
    assert result.exit_code == 0
    assert "already exists" in result.output.lower()


def test_start_agent_host_mode_error(runner, index_path, tmp_path, monkeypatch):
    """pier start --agent in a host-mode workspace gives a helpful error."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("PWD", str(ws))
    result = runner.invoke(cli, ["start", "--agent", "claude-code"])
    assert result.exit_code != 0
    assert "container mode" in result.output.lower()


# ---------------------------------------------------------------------------
# pier start --host
# ---------------------------------------------------------------------------


def test_start_host_creates_workspace(runner, index_path, task_dir, tmp_path):
    ws = tmp_path / "ws"
    result = runner.invoke(
        cli,
        ["start", str(task_dir), "--host", "-d", str(ws)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "host mode" in result.output.lower()
    assert "cd " in result.output

    assert (ws / ".pier" / "session.json").exists()
    session = json.loads((ws / ".pier" / "session.json").read_text())
    assert session["mode"] == "host"
    assert session["task_ref"] == "my-task"

    assert ws.exists()
    # Workspace gets environment/ contents (minus Dockerfile)
    assert (ws / "context" / "guide.md").exists()
    assert not (ws / "instruction.md").exists()


def test_start_host_creates_task_symlinks(runner, index_path, task_dir, tmp_path):
    """Host mode creates .task/ symlinks so edits to the task propagate."""
    ws = tmp_path / "ws"
    runner.invoke(
        cli,
        ["start", str(task_dir), "--host", "-d", str(ws)],
        catch_exceptions=False,
    )
    dot_task = ws / ".task"
    assert dot_task.is_dir()
    instruction = dot_task / "instruction.md"
    assert instruction.is_symlink()
    assert instruction.resolve() == (task_dir / "instruction.md").resolve()
    assert instruction.read_text() == "# Do the thing\n"


def test_start_host_existing_session_is_noop(runner, index_path, task_dir, tmp_path):
    """pier start on an existing host workspace is a no-op."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    result = runner.invoke(
        cli, ["start", str(task_dir), "--host", "-d", str(ws)], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "already exists" in result.output.lower()


def test_start_host_collision_no_session(runner, index_path, task_dir, tmp_path):
    """Can't start if workspace dir already exists without a session."""
    ws = tmp_path / "ws"
    ws.mkdir()
    result = runner.invoke(cli, ["start", str(task_dir), "--host", "-d", str(ws)])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_start_host_no_docker_image_ok(runner, index_path, tmp_path):
    """Host mode works without docker_image in task.toml."""
    td = tmp_path / "no-image"
    td.mkdir()
    (td / "task.toml").write_text("[metadata]\n[environment]\n[verifier]\n[agent]\n")
    (td / "instruction.md").write_text("# Task\n")
    ws = tmp_path / "ws"
    result = runner.invoke(
        cli,
        ["start", str(td), "--host", "-d", str(ws)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# pier exec (container mode)
# ---------------------------------------------------------------------------


@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_exec_container(mock_running, runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(), index_path)
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        runner.invoke(cli, ["exec", "my-tool", "list"])
    assert mock_run.called
    args = mock_run.call_args[0][0]
    assert args[0] == "docker"
    assert "exec" in args
    assert "-w" in args
    assert "/app" in args
    assert "my-tool" in args
    assert "list" in args


@patch(
    "pier.harbor_bridge.get_agent_exec_env",
    return_value=({"IS_SANDBOX": "1"}, "$HOME/.local/bin"),
)
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_exec_container_with_agent_prepends_path(
    mock_running, mock_env, runner, index_path, tmp_path
):
    """pier exec wraps command in a shell with the agent's PATH prefix."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = _container_session(agents=["claude-code"])
    _write_session(ws, sess, index_path)
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        runner.invoke(cli, ["exec", "claude", "--version"])
    args = mock_run.call_args[0][0]
    # Should wrap in sh -c with PATH export
    assert "sh" in args
    assert "-c" in args
    shell_cmd = args[args.index("-c") + 1]
    assert "$HOME/.local/bin:$PATH" in shell_cmd
    assert "claude" in shell_cmd
    assert "--version" in shell_cmd
    # Should forward agent env vars
    assert "IS_SANDBOX=1" in args


@patch(
    "pier.harbor_bridge.get_agent_exec_env",
    return_value=({}, ""),
)
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_exec_container_without_path_prefix_runs_directly(
    mock_running, mock_env, runner, index_path, tmp_path
):
    """When agent has no PATH prefix, command runs directly (no shell wrapper)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = _container_session(agents=["codex"])
    _write_session(ws, sess, index_path)
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        runner.invoke(cli, ["exec", "codex", "run"])
    args = mock_run.call_args[0][0]
    # Should NOT wrap in sh -c
    assert "sh" not in args
    assert "codex" in args
    assert "run" in args


@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_exec_container_forwards_api_keys(
    mock_running, runner, index_path, tmp_path, monkeypatch
):
    """pier exec forwards *_API_KEY env vars into the container."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(), index_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-456")
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        runner.invoke(cli, ["exec", "bash"])
    args = mock_run.call_args[0][0]
    # Should have -e flags for both keys
    assert "-e" in args
    assert "ANTHROPIC_API_KEY=sk-test-123" in args
    assert "OPENAI_API_KEY=sk-openai-456" in args


@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_exec_container_resolves_from_cwd(
    mock_running, runner, index_path, tmp_path, monkeypatch
):
    """exec resolves the workspace from cwd."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(harbor_session_id="pier-ws"), index_path)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("PWD", str(ws))
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        runner.invoke(cli, ["exec", "my-tool", "list"])
    args = mock_run.call_args[0][0]
    assert "pier-ws-main-1" in args


@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_exec_container_uses_dockerfile_workdir(
    mock_running, runner, index_path, tmp_path
):
    """docker exec -w uses the WORKDIR from the task's Dockerfile."""
    task = tmp_path / "my-task"
    env = task / "environment"
    env.mkdir(parents=True)
    (env / "Dockerfile").write_text("FROM ubuntu:24.04\nWORKDIR /custom\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task)), index_path)
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        runner.invoke(cli, ["exec", "echo", "hi"])
    args = mock_run.call_args[0][0]
    assert "-w" in args
    assert "/custom" in args


@patch("pier.harbor_bridge.is_environment_running", return_value=False)
def test_exec_container_not_running(mock_running, runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(), index_path)
    result = runner.invoke(cli, ["exec", "my-tool", "list"])
    assert result.exit_code != 0
    assert "not running" in result.output


# ---------------------------------------------------------------------------
# pier exec (host mode)
# ---------------------------------------------------------------------------


def test_exec_host_runs_command(runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        runner.invoke(cli, ["exec", "my-tool", "list"])
    args = mock_run.call_args[0][0]
    assert args == ["my-tool", "list"]
    env = mock_run.call_args[1]["env"]
    assert env["TASK_WORKSPACE"] == str(ws)


def test_exec_no_command(runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    result = runner.invoke(cli, ["exec"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# cwd-based workspace resolution
# ---------------------------------------------------------------------------


def test_resolve_multiple_workspaces_outside(runner, index_path, tmp_path, monkeypatch):
    """Multiple workspaces + cwd outside any workspace → error."""
    ws_a = tmp_path / "ws-a"
    ws_a.mkdir()
    ws_b = tmp_path / "ws-b"
    ws_b.mkdir()
    _write_session(ws_a, _host_session(), index_path)
    _write_session(ws_b, _host_session(), index_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("PWD", str(outside))
    result = runner.invoke(cli, ["exec", "echo", "hi"])
    assert result.exit_code != 0
    assert "Multiple" in result.output


def test_resolve_from_workspace_cwd(runner, index_path, tmp_path, monkeypatch):
    """When cwd is the workspace, auto-resolves without ambiguity."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    ws_other = tmp_path / "other"
    ws_other.mkdir()
    _write_session(ws_other, _host_session(), index_path)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("PWD", str(ws))
    # verify auto-resolves to ws (not "Multiple" error)
    result = runner.invoke(cli, ["verify"])
    # Will fail because test.sh doesn't exist, but the point is it resolved
    assert "Multiple" not in result.output


def test_resolve_exec_from_workspace_cwd(runner, index_path, tmp_path, monkeypatch):
    """exec from inside a workspace resolves to that workspace, even with multiple."""
    ws_a = tmp_path / "ws-a"
    ws_a.mkdir()
    ws_b = tmp_path / "ws-b"
    ws_b.mkdir()
    _write_session(ws_a, _host_session(), index_path)
    _write_session(ws_b, _host_session(), index_path)
    monkeypatch.chdir(ws_a)
    monkeypatch.setenv("PWD", str(ws_a))
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        result = runner.invoke(cli, ["exec", "echo", "hi"], catch_exceptions=False)
    assert result.exit_code == 0
    # Ran in ws_a, not ws_b
    assert mock_run.call_args.kwargs.get("cwd") == str(ws_a)


def test_resolve_from_workspace_subdir(runner, index_path, tmp_path, monkeypatch):
    """When cwd is inside the workspace, auto-resolves."""
    ws = tmp_path / "ws"
    sub = ws / "subdir"
    sub.mkdir(parents=True)
    _write_session(ws, _host_session(), index_path)
    monkeypatch.chdir(sub)
    result = runner.invoke(cli, ["verify"])
    assert "No active workspaces" not in result.output
    assert "Multiple" not in result.output


# ---------------------------------------------------------------------------
# pier verify (container mode)
# ---------------------------------------------------------------------------


@patch("pier.cli._assemble_trial_output")
@patch("pier.harbor_bridge.verify_environment", return_value={"reward": 0.75})
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_runs_and_reads_reward(
    mock_running, mock_verify, mock_assemble, runner, index_path, task_dir, tmp_path
):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    result = runner.invoke(cli, ["verify"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "0.75" in result.output
    mock_assemble.assert_called_once()


@patch("pier.cli._assemble_trial_output")
@patch("pier.harbor_bridge.verify_environment", return_value={"reward": 1.0})
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_container_uses_harbor_trial_dir(
    mock_running, mock_verify, mock_assemble, runner, index_path, task_dir, tmp_path
):
    """verify_environment must receive _harbor trial dir (matching container mounts),
    not the per-verify timestamped trial dir."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    result = runner.invoke(cli, ["verify"], catch_exceptions=False)
    assert result.exit_code == 0
    # verify_environment should be called with the _harbor dir, not trials/<ts>
    call_args = mock_verify.call_args[0]
    harbor_td = call_args[2]  # third positional arg is trial_dir
    assert harbor_td == ws / ".pier" / "_harbor"


@patch("pier.cli._assemble_trial_output")
@patch("pier.harbor_bridge.verify_environment", return_value={"reward": 1.0})
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_container_copies_verifier_output(
    mock_running, mock_verify, mock_assemble, runner, index_path, task_dir, tmp_path
):
    """Verifier output from _harbor/verifier/ is copied to the per-verify trial dir."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)

    # Simulate Harbor writing verifier output to _harbor/verifier/
    harbor_verifier = ws / ".pier" / "_harbor" / "verifier"
    harbor_verifier.mkdir(parents=True, exist_ok=True)
    (harbor_verifier / "reward.txt").write_text("1.0")
    (harbor_verifier / "test-stdout.txt").write_text("all tests passed")

    result = runner.invoke(cli, ["verify"], catch_exceptions=False)
    assert result.exit_code == 0

    # Find the timestamped trial dir
    trials_dir = ws / ".pier" / "trials"
    trial_dirs = list(trials_dir.iterdir())
    assert len(trial_dirs) == 1
    trial_verifier = trial_dirs[0] / "verifier"
    assert (trial_verifier / "reward.txt").read_text() == "1.0"
    assert (trial_verifier / "test-stdout.txt").read_text() == "all tests passed"


def test_verify_no_session(runner, index_path):
    result = runner.invoke(cli, ["verify"])
    assert result.exit_code != 0
    assert "No active workspaces" in result.output


@patch("pier.harbor_bridge.is_environment_running", return_value=False)
def test_verify_container_not_running(mock_running, runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(), index_path)
    result = runner.invoke(cli, ["verify"])
    assert result.exit_code != 0
    assert "not running" in result.output


@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_multiple_workspaces_outside_cwd(
    mock_running, runner, index_path, tmp_path, monkeypatch
):
    ws_a = tmp_path / "ws-a"
    ws_a.mkdir()
    ws_b = tmp_path / "ws-b"
    ws_b.mkdir()
    _write_session(ws_a, _container_session(), index_path)
    _write_session(ws_b, _container_session(), index_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("PWD", str(outside))
    result = runner.invoke(cli, ["verify"])
    assert result.exit_code != 0
    assert "Multiple" in result.output


@patch("pier.cli._assemble_trial_output")
@patch(
    "pier.harbor_bridge.verify_environment",
    return_value={
        "reward": 0.82,
        "f1": 0.90,
        "cost_penalty": -0.02,
    },
)
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_shows_details(
    mock_running, mock_verify, mock_assemble, runner, index_path, task_dir, tmp_path
):
    """Extra fields from the reward dict are printed."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    result = runner.invoke(cli, ["verify"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "0.82" in result.output
    assert "f1" in result.output
    assert "cost_penalty" in result.output


@patch("pier.cli._assemble_trial_output")
@patch(
    "pier.harbor_bridge.verify_environment", side_effect=Exception("score.py crashed")
)
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_failure_propagates(
    mock_running, mock_verify, mock_assemble, runner, index_path, task_dir, tmp_path
):
    """Verifier failure propagates as exit code != 0."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    result = runner.invoke(cli, ["verify"])
    assert result.exit_code != 0
    mock_assemble.assert_not_called()


# ---------------------------------------------------------------------------
# pier verify (host mode — container verifier)
# ---------------------------------------------------------------------------


@patch("pier.cli._assemble_trial_output")
@patch("pier.harbor_bridge.stop_environment")
@patch("pier.harbor_bridge.verify_environment", return_value={"reward": 0.91})
@patch("pier.harbor_bridge.start_environment")
def test_verify_host_uses_container(
    mock_start,
    mock_verify,
    mock_stop,
    mock_assemble,
    runner,
    index_path,
    task_dir,
    tmp_path,
):
    """Host verify spins up a temporary container for the verifier."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(
        ws,
        {
            "mode": "host",
            "task_dir": str(task_dir),
            "task_ref": "my-task",
            "started_at": "2026-02-24T12:00:00+00:00",
        },
        index_path,
    )
    result = runner.invoke(cli, ["verify"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "0.91" in result.output

    # Temporary container started with workspace mounted
    mock_start.assert_called_once()
    kwargs = mock_start.call_args[1]
    assert kwargs["workspace_dir"] == ws

    # Container stopped after verification
    mock_stop.assert_called_once()


@patch("pier.harbor_bridge.stop_environment")
@patch("pier.harbor_bridge.verify_environment", side_effect=Exception("test failed"))
@patch("pier.harbor_bridge.start_environment")
def test_verify_host_container_failure_cleans_up(
    mock_start,
    mock_verify,
    mock_stop,
    runner,
    index_path,
    task_dir,
    tmp_path,
):
    """If the verifier fails, the temporary container is still cleaned up."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(
        ws,
        {
            "mode": "host",
            "task_dir": str(task_dir),
            "task_ref": "my-task",
            "started_at": "2026-02-24T12:00:00+00:00",
        },
        index_path,
    )
    result = runner.invoke(cli, ["verify"])
    assert result.exit_code != 0
    mock_stop.assert_called_once()


# ---------------------------------------------------------------------------
# pier verify (host mode — local fallback)
# ---------------------------------------------------------------------------


@patch("pier.cli._verify_host_in_container", side_effect=ImportError("no harbor"))
def test_verify_host_falls_back_to_local(
    mock_container,
    runner,
    index_path,
    task_dir,
    tmp_path,
):
    """When Harbor isn't available, host verify runs test.sh locally."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(
        ws,
        {
            "mode": "host",
            "task_dir": str(task_dir),
            "task_ref": "my-task",
            "started_at": "2026-02-24T12:00:00+00:00",
        },
        index_path,
    )
    with patch("subprocess.run") as mock_run:

        def fake_run(cmd, **kw):
            vdir = Path(kw.get("env", {}).get("VERIFIER_DIR", "/tmp/v"))
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / "reward.json").write_text('{"reward": 0.91}')
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run
        result = runner.invoke(cli, ["verify"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "0.91" in result.output
    assert "locally" in result.output


@patch("pier.cli._verify_host_in_container", side_effect=ImportError("no harbor"))
@pytest.mark.skipif(sys.platform == "win32", reason="bash not available on Windows")
def test_verify_host_local_runs_real_test_sh(
    mock_container,
    runner,
    index_path,
    tmp_path,
):
    """Integration test: actually runs test.sh instead of mocking subprocess.run."""
    task = tmp_path / "real-task"
    task.mkdir()
    (task / "task.toml").write_text(
        '[metadata]\nauthor_name = "test"\n[environment]\n[verifier]\n[agent]\n'
    )
    tests = task / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text(
        '#!/bin/bash\nmkdir -p "$VERIFIER_DIR"\n'
        'echo \'{"reward": 0.75}\' > "$VERIFIER_DIR/reward.json"\n'
    )
    (tests / "test.sh").chmod(0o755)

    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(
        ws,
        {
            "mode": "host",
            "task_dir": str(task),
            "task_ref": "real-task",
            "started_at": "2026-02-24T12:00:00+00:00",
        },
        index_path,
    )
    result = runner.invoke(cli, ["verify"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "0.75" in result.output


def test_verify_host_missing_workspace(runner, index_path, tmp_path):
    ws = tmp_path / "nonexistent"
    sess_data = {
        "mode": "host",
        "task_dir": str(tmp_path),
        "task_ref": "gone",
        "started_at": "2026-02-24T12:00:00+00:00",
    }
    with patch("pier.cli._resolve_workspace", return_value=(sess_data, ws)):
        result = runner.invoke(cli, ["verify"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


# ---------------------------------------------------------------------------
# pier stop
# ---------------------------------------------------------------------------


@patch("pier.harbor_bridge.stop_environment")
def test_stop_container(mock_stop, runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(), index_path)
    result = runner.invoke(cli, ["stop"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "stopped" in result.output.lower()
    mock_stop.assert_called_once()
    # Session and workspace preserved
    assert (ws / ".pier" / "session.json").exists()
    assert ws.exists()


def test_stop_rejects_delete_flag(runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(), index_path)
    result = runner.invoke(cli, ["stop", "--delete"], catch_exceptions=False)
    assert result.exit_code != 0


def test_stop_host_errors(runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    result = runner.invoke(cli, ["stop"], catch_exceptions=False)
    assert result.exit_code != 0
    assert "host" in result.output.lower()


# ---------------------------------------------------------------------------
# pier list
# ---------------------------------------------------------------------------


def test_list_empty(runner, index_path):
    result = runner.invoke(cli, ["list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No active workspaces" in result.output


def test_list_prunes_stale_entries(runner, index_path, tmp_path):
    """Workspaces whose directory is deleted are pruned from the index."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    # Simulate user deleting the workspace
    shutil.rmtree(ws)
    result = runner.invoke(cli, ["list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No active workspaces" in result.output
    # Index should be pruned
    assert json.loads(index_path.read_text()) == []


@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_list_shows_workspaces(mock_running, runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(), index_path)
    result = runner.invoke(cli, ["list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "running" in result.output
    assert str(ws) in result.output


def test_list_shows_host_workspace(runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    result = runner.invoke(cli, ["list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "—" in result.output
    assert str(ws) in result.output


@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_list_mixed_modes(mock_running, runner, index_path, tmp_path):
    ws_c = tmp_path / "ws-c"
    ws_c.mkdir()
    _write_session(ws_c, _container_session(), index_path)
    ws_h = tmp_path / "ws-h"
    ws_h.mkdir()
    _write_session(ws_h, _host_session(), index_path)
    result = runner.invoke(cli, ["list"], catch_exceptions=False)
    assert "running" in result.output
    assert "—" in result.output


def test_list_deduplicates_symlink_paths(runner, index_path, tmp_path):
    """Symlink aliases of the same workspace appear only once in list."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    # Manually add a symlink alias to the index
    link = tmp_path / "link"
    link.symlink_to(ws)
    idx = json.loads(index_path.read_text())
    idx.append(str(link))
    index_path.write_text(json.dumps(idx))
    result = runner.invoke(cli, ["list"], catch_exceptions=False)
    assert result.exit_code == 0
    # Only one workspace row should appear
    lines = [line for line in result.output.splitlines() if str(ws.resolve()) in line]
    assert len(lines) == 1
    # Index should be cleaned up
    assert len(json.loads(index_path.read_text())) == 1


@patch("pier.harbor_bridge.does_environment_exist", return_value=False)
@patch("pier.harbor_bridge.is_environment_running", return_value=False)
def test_list_container_not_found(
    mock_running, mock_exists, runner, index_path, tmp_path
):
    """Container that no longer exists (e.g. docker prune) shows 'not found'."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(), index_path)
    result = runner.invoke(cli, ["list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "not found" in result.output


@patch("pier.harbor_bridge.does_environment_exist", return_value=True)
@patch("pier.harbor_bridge.is_environment_running", return_value=False)
def test_list_container_stopped(
    mock_running, mock_exists, runner, index_path, tmp_path
):
    """Stopped container shows 'stopped'."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(), index_path)
    result = runner.invoke(cli, ["list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "stopped" in result.output


def test_stop_outside_workspace(runner, index_path):
    """pier stop outside any workspace gives a clear error."""
    result = runner.invoke(cli, ["stop"])
    assert result.exit_code != 0
    assert "session" in result.output.lower() or "workspace" in result.output.lower()


# ---------------------------------------------------------------------------
# pier verify — trajectory assembly
# ---------------------------------------------------------------------------


@patch("pier.cli._assemble_trial_output")
@patch("pier.harbor_bridge.verify_environment", return_value={"reward": 0.5})
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_container_assembles_trial(
    mock_running,
    mock_verify,
    mock_assemble,
    runner,
    index_path,
    task_dir,
    tmp_path,
):
    """Container verify calls _assemble_trial_output with correct args."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    result = runner.invoke(cli, ["verify"], catch_exceptions=False)
    assert result.exit_code == 0
    mock_assemble.assert_called_once()

    args = mock_assemble.call_args[0]
    # trial_dir, sess, reward, start_time, end_time, workspace, agent, session_dir
    assert (
        args[0].parent == ws / ".pier" / "trials"
    )  # timestamped trial dir under .pier/trials
    assert args[2] == {"reward": 0.5}  # reward
    assert args[5] == ws  # workspace
    assert args[6] is None  # agent
    assert args[7] is None  # session_dir


@patch("pier.cli._assemble_trial_output")
@patch("pier.harbor_bridge.verify_environment", return_value={"reward": 0.5})
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_container_custom_trial_dir(
    mock_running,
    mock_verify,
    mock_assemble,
    runner,
    index_path,
    task_dir,
    tmp_path,
):
    """--trial-dir overrides the default trial directory."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    custom = tmp_path / "my-trial"
    result = runner.invoke(
        cli,
        ["verify", "--trial-dir", str(custom)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert mock_assemble.call_args[0][0] == custom  # trial_dir is first arg


@patch("pier.cli._assemble_trial_output")
@patch("pier.harbor_bridge.verify_environment", return_value={"reward": 1.0})
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_with_agent_flag(
    mock_running,
    mock_verify,
    mock_assemble,
    runner,
    index_path,
    task_dir,
    tmp_path,
):
    """-a/--agent is passed through to _assemble_trial_output."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _container_session(task_dir=str(task_dir)), index_path)
    result = runner.invoke(
        cli,
        ["verify", "-a", "claude-code"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0

    args = mock_assemble.call_args[0]
    assert args[6] == "claude-code"  # agent


@patch("pier.cli._assemble_trial_output")
@patch("pier.harbor_bridge.verify_environment", return_value={"reward": 1.0})
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_defaults_agent_from_session(
    mock_running,
    mock_verify,
    mock_assemble,
    runner,
    index_path,
    task_dir,
    tmp_path,
):
    """Agent name is read from session when only one agent is installed."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = _container_session(task_dir=str(task_dir), agents=["claude-code"])
    _write_session(ws, sess, index_path)
    result = runner.invoke(
        cli,
        ["verify"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    args = mock_assemble.call_args[0]
    assert args[6] == "claude-code"  # agent defaulted from session


@patch("pier.cli._assemble_trial_output")
@patch("pier.harbor_bridge.verify_environment", return_value={"reward": 1.0})
@patch("pier.harbor_bridge.is_environment_running", return_value=True)
def test_verify_agent_flag_overrides_session(
    mock_running,
    mock_verify,
    mock_assemble,
    runner,
    index_path,
    task_dir,
    tmp_path,
):
    """Explicit --agent overrides the agent stored in the session."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = _container_session(task_dir=str(task_dir), agents=["claude-code"])
    _write_session(ws, sess, index_path)
    result = runner.invoke(
        cli,
        ["verify", "-a", "codex"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    args = mock_assemble.call_args[0]
    assert args[6] == "codex"  # explicit flag wins


def test_verify_host_custom_trial_dir(runner, index_path, task_dir, tmp_path):
    """--trial-dir overrides the auto-generated trial directory for host mode."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(
        ws,
        {
            "mode": "host",
            "task_dir": str(task_dir),
            "task_ref": "my-task",
            "started_at": "2026-02-24T12:00:00+00:00",
        },
        index_path,
    )
    custom = tmp_path / "my-trial"
    with (
        patch(
            "pier.cli._verify_host_in_container", side_effect=ImportError("no harbor")
        ),
        patch("subprocess.run") as mock_run,
    ):

        def fake_run(cmd, **kw):
            vdir = Path(kw.get("env", {}).get("VERIFIER_DIR", "/tmp/v"))
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / "reward.json").write_text('{"reward": 1.0}')
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run
        result = runner.invoke(
            cli,
            ["verify", "--trial-dir", str(custom)],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert (custom / "verifier" / "reward.json").exists()


# ---------------------------------------------------------------------------
# pier skills
# ---------------------------------------------------------------------------


def test_skills_no_skills(runner, index_path, tmp_path):
    td = tmp_path / "my-task"
    td.mkdir()
    (td / "task.toml").write_text("[metadata]\n[environment]\n[verifier]\n[agent]\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(
        ws,
        {
            "mode": "host",
            "task_dir": str(td),
            "task_ref": "my-task",
            "started_at": "2026-02-24T12:00:00+00:00",
        },
        index_path,
    )
    result = runner.invoke(cli, ["skills"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "no skills" in result.output.lower()


def test_skills_container_mode_error(runner, index_path, tmp_path):
    td = tmp_path / "my-task"
    td.mkdir()
    (td / "task.toml").write_text("[metadata]\n[environment]\n[verifier]\n[agent]\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(
        ws,
        {
            "mode": "container",
            "task_dir": str(td),
            "task_ref": "my-task",
            "harbor_session_id": "pier-ws-abc",
            "agents": [],
            "started_at": "2026-02-24T12:00:00+00:00",
        },
        index_path,
    )
    result = runner.invoke(cli, ["skills"])
    assert result.exit_code != 0
    assert "container mode" in result.output.lower()


# ---------------------------------------------------------------------------
# pier view
# ---------------------------------------------------------------------------


def test_view_no_workspace(runner, index_path):
    result = runner.invoke(cli, ["view"])
    assert result.exit_code != 0


def test_view_no_trials_dir(runner, index_path, tmp_path, monkeypatch):
    """view works even when .pier/ exists but has no trials — it just passes the dir."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    monkeypatch.chdir(ws)
    # .pier/ exists (from _write_session) but that's fine — view shows whatever is there
    mock_view = MagicMock()
    with patch.dict(
        "sys.modules", {"harbor.cli.view": MagicMock(view_command=mock_view)}
    ):
        result = runner.invoke(cli, ["view"])
    assert result.exit_code == 0
    mock_view.assert_called_once()


def test_view_delegates_to_harbor(runner, index_path, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    # Create a .pier/trials dir so pier_dir exists
    (ws / ".pier" / "trials").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(ws)

    mock_view = MagicMock()
    with patch.dict(
        "sys.modules", {"harbor.cli.view": MagicMock(view_command=mock_view)}
    ):
        result = runner.invoke(cli, ["view"])
    assert result.exit_code == 0
    mock_view.assert_called_once()
    call_kwargs = mock_view.call_args
    assert call_kwargs[1]["folder"] == ws / ".pier"


def test_view_explicit_path(runner, index_path, tmp_path):
    pier_dir = tmp_path / "ws" / ".pier"
    pier_dir.mkdir(parents=True)

    mock_view = MagicMock()
    with patch.dict(
        "sys.modules", {"harbor.cli.view": MagicMock(view_command=mock_view)}
    ):
        result = runner.invoke(cli, ["view", str(pier_dir)])
    assert result.exit_code == 0
    mock_view.assert_called_once()


def test_view_explicit_workspace_path(runner, index_path, tmp_path):
    ws = tmp_path / "ws"
    pier_dir = ws / ".pier"
    pier_dir.mkdir(parents=True)

    mock_view = MagicMock()
    with patch.dict(
        "sys.modules", {"harbor.cli.view": MagicMock(view_command=mock_view)}
    ):
        result = runner.invoke(cli, ["view", str(ws)])
    assert result.exit_code == 0
    mock_view.assert_called_once()


# ---------------------------------------------------------------------------
# pier summarize
# ---------------------------------------------------------------------------


def test_summarize_no_workspace(runner, index_path):
    result = runner.invoke(cli, ["summarize"])
    assert result.exit_code != 0


def test_summarize_no_trials(runner, index_path, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    (ws / ".pier").mkdir(exist_ok=True)
    monkeypatch.chdir(ws)
    result = runner.invoke(cli, ["summarize"])
    assert result.exit_code != 0
    assert "no trials" in result.output.lower()


def test_summarize_delegates_to_harbor(runner, index_path, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    (ws / ".pier" / "trials").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(ws)

    mock_summarizer_cls = MagicMock()
    mock_summarizer_cls.return_value.summarize.return_value = (
        ws / ".pier" / "summary.md"
    )
    mock_module = MagicMock()
    mock_module.Summarizer = mock_summarizer_cls

    with patch.dict("sys.modules", {"harbor.cli.summarize.summarizer": mock_module}):
        result = runner.invoke(cli, ["summarize"])
    assert result.exit_code == 0
    assert "summary" in result.output.lower()
    mock_summarizer_cls.assert_called_once_with(
        ws / ".pier" / "trials",
        n_concurrent=5,
        model="haiku",
        only_failed=True,
        overwrite=False,
    )


def test_summarize_all_flag(runner, index_path, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_session(ws, _host_session(), index_path)
    (ws / ".pier" / "trials").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(ws)

    mock_summarizer_cls = MagicMock()
    mock_summarizer_cls.return_value.summarize.return_value = None
    mock_module = MagicMock()
    mock_module.Summarizer = mock_summarizer_cls

    with patch.dict("sys.modules", {"harbor.cli.summarize.summarizer": mock_module}):
        result = runner.invoke(cli, ["summarize", "--all"])
    assert result.exit_code == 0
    call_kwargs = mock_summarizer_cls.call_args[1]
    assert call_kwargs["only_failed"] is False
