"""pier CLI — interactive task runner (container or host workspaces).

Commands:
    pier start <task_path>         — launch a workspace for a task
    pier start <task_path> --host  — launch a host-based workspace (no Docker)
    pier exec -- <cmd>             — run a command in the workspace
    pier verify                    — run verifier for the workspace
    pier view                      — web dashboard for trial trajectories
    pier summarize                 — AI summary of trial results
    pier stop                      — tear down workspace, extract artifacts
    pier list                      — show active workspaces
    pier skills --install          — install task skills (host mode)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import click

PIER_DIR = ".pier"

logger = logging.getLogger(__name__)
INDEX_PATH = Path.home() / ".pier" / "index.json"

_HARBOR_INSTALL_HINT = (
    "Install: uv tool install --with harbor git+https://github.com/allenai/pier"
)


def _require_harbor(feature: str):
    """Import and return harbor_bridge, or raise a clear error."""
    try:
        from pier import harbor_bridge

        return harbor_bridge
    except ImportError:
        raise click.ClickException(
            f"{feature} requires Harbor.\n{_HARBOR_INSTALL_HINT}"
        )


# ---------------------------------------------------------------------------
# Session CRUD (workspace/.pier/session.json + global index)
# ---------------------------------------------------------------------------


def _pier_dir(workspace: Path) -> Path:
    return workspace / PIER_DIR


def _session_json_path(workspace: Path) -> Path:
    return _pier_dir(workspace) / "session.json"


def _save_session(workspace: Path, data: dict) -> None:
    pier_dir = _pier_dir(workspace)
    pier_dir.mkdir(parents=True, exist_ok=True)
    _session_json_path(workspace).write_text(json.dumps(data, indent=2) + "\n")
    _index_register(workspace)


def _load_session(workspace: Path) -> dict:
    path = _session_json_path(workspace)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise click.ClickException(
            f"No active session in {workspace}. Run 'pier start' first."
        )


def _all_workspaces() -> list[tuple[dict, Path]]:
    """Return (data, workspace) for every registered workspace.

    Prunes stale index entries where session.json no longer exists.
    """
    index = _index_load()
    out: list[tuple[dict, Path]] = []
    live: list[str] = []
    seen: set[str] = set()
    for ws_str in sorted(index):
        ws = Path(ws_str).resolve()
        resolved = str(ws)
        if resolved in seen:
            continue  # skip symlink alias
        try:
            data = json.loads(_session_json_path(ws).read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append((data, ws))
        live.append(resolved)
        seen.add(resolved)
    if live != sorted(index):
        _index_save(live)
    return out


def _find_workspace_from_cwd() -> Path | None:
    """Walk up from cwd looking for .pier/session.json."""
    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        if _session_json_path(p).exists():
            return p
    return None


def _resolve_workspace(workspace_arg: str | None = None) -> tuple[dict, Path]:
    """Resolve workspace from explicit path, cwd, or auto-select the only one.

    Returns (session_data, workspace_path).
    """
    if workspace_arg:
        ws = Path(workspace_arg).resolve()
        return _load_session(ws), ws

    # Try to find a workspace from cwd
    found = _find_workspace_from_cwd()
    if found is not None:
        return _load_session(found), found

    # Fall back to auto-select if only one workspace exists
    workspaces = _all_workspaces()
    if not workspaces:
        raise click.ClickException("No active workspaces. Run 'pier start' first.")
    if len(workspaces) > 1:
        paths = [str(ws) for _, ws in workspaces]
        raise click.ClickException(
            f"Multiple workspaces active: {', '.join(paths)}. "
            f"Run from inside the workspace directory."
        )
    return workspaces[0]


# ---------------------------------------------------------------------------
# Global index (~/.pier/index.json) — lightweight cache for pier list
# ---------------------------------------------------------------------------


def _index_load() -> list[str]:
    try:
        data = json.loads(INDEX_PATH.read_text())
        # Support both old dict format and new list format
        if isinstance(data, dict):
            return list(data.values())
        return data
    except (json.JSONDecodeError, OSError):
        return []


def _index_save(index: list[str]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(sorted(set(index)), indent=2) + "\n")


def _index_register(workspace: Path) -> None:
    index = _index_load()
    ws_str = str(workspace.resolve())
    if ws_str not in index:
        index.append(ws_str)
        _index_save(index)


# ---------------------------------------------------------------------------
# Task config
# ---------------------------------------------------------------------------


def _read_task_toml(task_dir: Path) -> dict:
    toml_path = task_dir / "task.toml"
    if not toml_path.exists():
        raise click.ClickException(f"No task.toml in {task_dir}")
    return tomllib.loads(toml_path.read_text())


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _workspace_label(workspace: Path) -> str:
    """Human-readable label for a workspace (its directory name)."""
    return workspace.name


def _harbor_session_id(workspace: Path, prefix: str = "pier") -> str:
    """Docker compose session ID derived from the workspace path.

    Includes a hash of the full resolved path so two workspaces with the
    same directory name (different parents) don't collide.
    """
    resolved = workspace.resolve()
    short_hash = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    return f"{prefix}-{resolved.name}-{short_hash}"


def _get_hsid(sess: dict, workspace: Path) -> str:
    """Get the Harbor session ID from session data, with workspace fallback."""
    return sess.get("harbor_session_id", _harbor_session_id(workspace))


def _harbor_trial_dir(workspace: Path) -> Path:
    """Path to Harbor's internal trial directory within the workspace."""
    return _pier_dir(workspace) / "_harbor"


def _add_agent(agents: list[str], agent: str | None) -> list[str]:
    """Return agents list with agent appended (if not already present)."""
    if agent and agent not in agents:
        return [*agents, agent]
    return agents


def _link_task_files(task_dir: Path, workspace: Path) -> None:
    """Symlink task instruction and skills into .task/ in the workspace.

    Exposes only instruction.md and skills/ — not tests or task.toml
    which could leak verifier details to agents.
    """
    dot_task = workspace / ".task"
    dot_task.mkdir(exist_ok=True)

    instruction = task_dir / "instruction.md"
    if instruction.exists():
        link = dot_task / "instruction.md"
        if not link.exists():
            link.symlink_to(instruction.resolve())

    skills_dir = task_dir / "environment" / "skills"
    if skills_dir.is_dir():
        link = dot_task / "skills"
        if not link.exists():
            link.symlink_to(skills_dir.resolve())


def _seed_workspace(
    task_dir: Path, workspace: Path, *, container: bool = False
) -> None:
    """Seed the workspace with the task image's WORKDIR contents.

    Tries to build the Docker image and extract WORKDIR contents so the
    workspace matches what Harbor's container would have.  Falls back to
    copying the environment/ directory (approximates ``COPY . /app``).

    In container mode, .task/ files are mounted via Docker compose override
    so we skip creating symlinks (whose host-path targets confuse tools
    like glob/find inside the container).
    """
    try:
        from pier import harbor_bridge

        click.echo("Building image and extracting workspace files...")
        harbor_bridge.extract_image_workdir(task_dir, workspace)
    except Exception as e:
        logger.debug("extract_image_workdir failed: %s", e)

        # Fallback: copy environment/ contents directly (best effort without Docker)
        env_dir = task_dir / "environment"
        if env_dir.is_dir():
            click.echo("Copying task environment files into workspace...")
            for item in env_dir.iterdir():
                if item.name == "Dockerfile" or item.name.startswith("docker-compose"):
                    continue
                dest = workspace / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

    if not container:
        _link_task_files(task_dir, workspace)


def _is_remote_task(task_path: str) -> bool:
    return "://" in task_path or task_path.startswith("git@")


def _resolve_task_path(task_path: str) -> Path:
    """Resolve a task path — local directory or remote git URL#path.

    Remote references (URL#path) require Harbor to be installed for task
    download. Without Harbor, clone the repo yourself and use a local path.
    """
    if _is_remote_task(task_path):
        if "#" not in task_path:
            raise click.ClickException(
                "Remote task reference must include a path after '#', "
                "e.g. https://github.com/org/repo#tasks/my-task"
            )
        git_url, task_subpath = task_path.rsplit("#", 1)
        click.echo(f"Downloading task from {git_url} ({task_subpath})...")

        from pier import harbor_bridge

        try:
            task_dir = harbor_bridge.download_task(git_url, task_subpath)
        except ImportError:
            raise click.ClickException(
                f"Remote task references require Harbor.\n{_HARBOR_INSTALL_HINT}\n"
                "Or clone the repo and use a local path: pier start ./path/to/task -d ./workspace"
            )
        except Exception as e:
            raise click.ClickException(f"Failed to download task: {e}")
        return task_dir

    task_dir = Path(task_path).resolve()
    if not (task_dir / "task.toml").exists():
        raise click.ClickException(f"No task.toml in {task_dir}")
    return task_dir


def _print_reward(reward: dict) -> None:
    """Print reward value and any extra details from a reward dict."""
    click.echo(f"Reward: {reward.get('reward', 'N/A')}")
    for k, v in reward.items():
        if k != "reward":
            click.echo(f"  {k}: {v}")


def _read_host_reward(verifier_dir: Path) -> dict:
    """Read reward from host-mode verifier output directory."""
    reward_path = verifier_dir / "reward.json"
    details_path = verifier_dir / "details.json"

    reward: dict = {}
    if reward_path.exists():
        try:
            reward = json.loads(reward_path.read_text())
        except json.JSONDecodeError:
            pass
    if details_path.exists():
        try:
            reward.update(json.loads(details_path.read_text()))
        except json.JSONDecodeError:
            pass
    return reward or {"reward": None}


def _assemble_trial_output(
    trial_dir: Path,
    sess: dict,
    reward: dict,
    start_time: datetime,
    end_time: datetime,
    workspace: Path,
    agent: str | None,
    session_dir: str | None,
) -> None:
    """Assemble trial directory with trajectory and optional agent logs."""
    from pier.trajectory import assemble_trial

    agent_context = None

    if session_dir:
        try:
            from pier import harbor_bridge

            # Auto-detect agent from session_dir if not specified
            if not agent:
                agent = harbor_bridge.detect_agent_from_session_dir(Path(session_dir))
                if agent:
                    click.echo(f"Detected agent: {agent}")

            if agent:
                agent_context = harbor_bridge.extract_agent_logs(
                    agent,
                    Path(session_dir),
                    trial_dir / "agent",
                )
                if agent_context:
                    click.echo("Agent trajectory extracted.")
        except ImportError:
            click.echo(
                "Warning: Harbor not installed, skipping agent log extraction.",
                err=True,
            )
    elif agent:
        click.echo(f"Tip: pass --session-dir to extract {agent} trajectory.")

    label = _workspace_label(workspace)
    assemble_trial(
        trial_dir,
        Path(sess["task_dir"]),
        sess.get("task_ref", "unknown"),
        label,
        reward,
        start_time=start_time,
        end_time=end_time,
        agent_name=agent,
        agent_context=agent_context,
    )
    click.echo(f"Trial output: {trial_dir}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """pier — interactive task runner."""


@cli.command()
@click.argument("task_path", required=False)
@click.option(
    "--host",
    is_flag=True,
    default=False,
    help="Skip Docker — workspace only (no container).",
)
@click.option(
    "-d",
    "--dir",
    "workspace_dir",
    default=None,
    type=click.Path(),
    help="Workspace directory. Required for local tasks; defaults to ./<task-name> for remote tasks.",
)
@click.option(
    "-a",
    "--agent",
    default=None,
    help="Agent to set up with skills (container mode). E.g. claude-code, codex, goose.",
)
def start(
    task_path: str | None,
    host: bool,
    workspace_dir: str | None,
    agent: str | None,
) -> None:
    """Launch a workspace for a task, or install an agent into an existing one.

    TASK_PATH is a local directory containing task.toml, or a remote git
    reference in the form URL#path (e.g.
    https://github.com/org/repo#tasks/my-task).

    If TASK_PATH is omitted, operates on the current workspace (resolved
    from cwd). This is useful for installing an agent into an existing
    workspace: pier start --agent claude-code

    A local workspace directory is created and seeded with the task
    image's WORKDIR contents.  In container mode (the default), the
    workspace is bind-mounted into the container so edits sync both
    ways.  Pass --host to skip the container.
    """
    # No task_path → operate on existing workspace from cwd
    if task_path is None:
        _start_existing(agent=agent)
        return

    is_remote = _is_remote_task(task_path)
    task_dir = _resolve_task_path(task_path)

    # Resolve workspace directory
    if workspace_dir:
        p = Path(workspace_dir)
        workspace = p if p.is_absolute() else Path.cwd().resolve() / p
    elif is_remote:
        workspace = Path.cwd().resolve() / task_dir.name
    else:
        raise click.ClickException(
            "Local tasks require -d/--dir to specify the workspace directory, "
            "e.g. pier start ./tasks/my-task -d ./my-workspace"
        )

    # Collision check — existing session in this workspace
    if _session_json_path(workspace).exists():
        existing_sess = _load_session(workspace)
        mode = existing_sess.get("mode")

        if mode == "container":
            harbor_bridge = _require_harbor("Container mode")
            hsid = _get_hsid(existing_sess, workspace)
            if harbor_bridge.is_environment_running(hsid):
                if agent:
                    # Install agent into the existing running container
                    harbor_trial_dir = _harbor_trial_dir(workspace)
                    _install_agent(
                        harbor_bridge,
                        Path(existing_sess["task_dir"]),
                        hsid,
                        harbor_trial_dir,
                        agent,
                    )
                    existing_sess["agents"] = _add_agent(
                        existing_sess.get("agents", []), agent
                    )
                    _save_session(workspace, existing_sess)
                    return
                click.echo("Container is already running.")
                return
            else:
                # Container stopped — restart it
                agents = _add_agent(existing_sess.get("agents", []), agent)
                _start_container(task_dir, workspace, agents=agents)
                return
        else:
            # Host mode — workspace already exists, nothing to do
            click.echo(f"Workspace already exists at {workspace}.")
            return

    # Create workspace
    if workspace.exists():
        raise click.ClickException(
            f"Directory {workspace} already exists. Pick a different -d path."
        )
    workspace.mkdir(parents=True)

    # Seed workspace with the same files the container WORKDIR would have.
    _seed_workspace(task_dir, workspace, container=not host)

    if host:
        if agent:
            click.echo("Hint: --agent is for container mode.")
        _start_host(task_dir, workspace)
    else:
        _start_container(task_dir, workspace, agents=[agent] if agent else None)


def _start_existing(agent: str | None) -> None:
    """Operate on an existing workspace (no task_path given).

    Without --agent: restart a stopped container.
    With --agent: install an agent into the running container.
    """
    ws = _find_workspace_from_cwd()
    if ws is None:
        if agent:
            raise click.ClickException(
                "Not inside a workspace. Either specify a task path or "
                "cd into a workspace directory."
            )
        raise click.ClickException(
            "No task path specified. Usage:\n"
            "  pier start <task_path> -d <workspace>   — create a new workspace\n"
            "  pier start --agent <name>               — install agent in current workspace\n"
            "  pier start                              — restart a stopped container (from within workspace)"
        )

    sess = _load_session(ws)
    mode = sess.get("mode")

    if mode != "container":
        if agent:
            raise click.ClickException("--agent is for container mode.")
        click.echo(f"Workspace already exists at {ws}.")
        return

    harbor_bridge = _require_harbor("Container mode")

    hsid = _get_hsid(sess, ws)
    task_dir = Path(sess["task_dir"])

    if not harbor_bridge.is_environment_running(hsid):
        # Restart the stopped container (reinstalls all agents)
        agents = _add_agent(sess.get("agents", []), agent)
        _start_container(task_dir, ws, agents=agents)
        return

    if agent:
        harbor_trial_dir = _harbor_trial_dir(ws)
        _install_agent(harbor_bridge, task_dir, hsid, harbor_trial_dir, agent)
        sess["agents"] = _add_agent(sess.get("agents", []), agent)
        _save_session(ws, sess)
    else:
        click.echo("Container is already running.")


def _start_host(task_dir: Path, workspace: Path) -> None:
    _save_session(
        workspace,
        {
            "mode": "host",
            "task_dir": str(task_dir),
            "task_ref": task_dir.name,
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    label = _workspace_label(workspace)
    click.echo(f"\nWorkspace {label!r} ready (host mode).")
    click.echo(f"  cd {workspace}")

    skills_dir = task_dir / "environment" / "skills"
    if skills_dir.is_dir() and any(skills_dir.iterdir()):
        click.echo("  pier skills --install    # install task skills for your agent")

    click.echo("")
    click.echo("Task instruction is at .task/instruction.md.")


def _install_agent(
    harbor_bridge: object,
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    agent: str,
) -> None:
    """Validate and install a Harbor agent into a running container."""
    if not harbor_bridge.is_valid_agent(agent):  # type: ignore[attr-defined]
        raise click.ClickException(f"Unknown agent {agent!r}.")
    click.echo(f"Installing {agent} in the container...")
    try:
        harbor_bridge.setup_agent(  # type: ignore[attr-defined]
            task_dir, harbor_session_id, trial_dir, agent
        )
        click.echo(f"{agent} installed.")
    except Exception as e:
        raise click.ClickException(f"Agent setup failed: {e}")


def _start_container(
    task_dir: Path,
    workspace: Path,
    agents: list[str] | None = None,
) -> None:
    harbor_bridge = _require_harbor("Container mode")

    hsid = _harbor_session_id(workspace)
    harbor_trial_dir = _harbor_trial_dir(workspace)

    label = _workspace_label(workspace)
    click.echo(f"Starting {label} (building image if needed)...")
    try:
        harbor_bridge.start_environment(
            task_dir,
            hsid,
            harbor_trial_dir,
            workspace_dir=workspace,
        )
    except Exception as e:
        raise click.ClickException(f"Failed to start container: {e}")

    for agent in agents or []:
        _install_agent(harbor_bridge, task_dir, hsid, harbor_trial_dir, agent)

    _save_session(
        workspace,
        {
            "mode": "container",
            "task_dir": str(task_dir),
            "task_ref": task_dir.name,
            "harbor_session_id": hsid,
            "agents": agents or [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    click.echo(f"\nWorkspace {label!r} ready.")
    click.echo(f"  cd {workspace}")
    click.echo("  pier exec bash           # drop into the container")
    if agents:
        names = ", ".join(agents)
        click.echo(f"  pier exec <agent-cli>    # run an installed agent ({names})")
    click.echo("")
    click.echo(
        "Task instruction is at .task/instruction.md (inside the container too)."
    )


@cli.command(
    "exec",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
def exec_cmd(command: tuple[str, ...]) -> None:
    """Run a command inside the workspace.

    Container mode uses docker exec (streaming output). Host mode runs
    the command directly with workspace env vars set.

    \b
    Examples:
        pier exec bash
        pier exec claude
    """
    if not command:
        raise click.ClickException("No command specified.")

    sess, ws = _resolve_workspace()

    if sess.get("mode") == "host":
        _exec_host(sess, ws, list(command))
    else:
        _exec_container(sess, ws, list(command))


def _exec_container(sess: dict, workspace: Path, command: list[str]) -> None:
    harbor_bridge = _require_harbor("Container exec")

    hsid = _get_hsid(sess, workspace)
    if not harbor_bridge.is_environment_running(hsid):
        raise click.ClickException(
            "Container is not running. Start it with 'pier start'."
        )
    container = harbor_bridge.get_container_name(hsid)
    workdir = harbor_bridge.get_container_workdir(Path(sess["task_dir"]))

    # Build env flags for docker exec.
    env_flags: list[str] = []

    # Forward host API keys into the container.
    for var, val in os.environ.items():
        if var.endswith("_API_KEY"):
            env_flags.extend(["-e", f"{var}={val}"])

    # Forward behavioral env vars and PATH prefixes from all installed
    # agents.  Merging is safe because the allowlisted vars don't conflict
    # between agents.  Harbor is the single source of truth.
    path_prefixes: list[str] = []
    for agent_name in sess.get("agents", []):
        agent_env, pfx = harbor_bridge.get_agent_exec_env(agent_name)
        for var, val in agent_env.items():
            env_flags.extend(["-e", f"{var}={val}"])
        if pfx:
            path_prefixes.append(pfx)
    path_prefix = ":".join(path_prefixes)

    if path_prefix:
        import shlex

        # Don't quote path_prefix — it may contain $HOME that must expand.
        # It comes from Harbor (trusted), not user input.
        shell_cmd = f"export PATH={path_prefix}:$PATH && exec " + " ".join(
            shlex.quote(c) for c in command
        )
        run_command = [container, "sh", "-c", shell_cmd]
    else:
        run_command = [container, *command]

    tty_flags = ["-it"] if sys.stdin.isatty() else []
    result = subprocess.run(
        ["docker", "exec", *tty_flags, "-w", workdir, *env_flags, *run_command],
    )
    raise SystemExit(result.returncode)


def _exec_host(sess: dict, workspace: Path, command: list[str]) -> None:
    if not workspace.exists():
        raise click.ClickException("Workspace does not exist.")

    env = {**os.environ, "TASK_WORKSPACE": str(workspace)}
    result = subprocess.run(command, env=env, cwd=str(workspace))
    raise SystemExit(result.returncode)


@cli.command()
@click.option(
    "--trial-dir",
    default=None,
    type=click.Path(),
    help="Directory for trial output (default: auto-generated).",
)
@click.option(
    "-a",
    "--agent",
    default=None,
    help="Agent name for trajectory extraction (e.g. claude-code).",
)
@click.option(
    "--session-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Agent session/log directory for trajectory extraction.",
)
def verify(
    trial_dir: str | None,
    agent: str | None,
    session_dir: str | None,
) -> None:
    """Run the verifier for the current workspace.

    Executes the verifier and reports the reward. Optionally assembles
    a trial directory with trajectory data.
    """
    sess, ws = _resolve_workspace()

    # Default agent from session if not explicitly provided
    if not agent:
        agents = sess.get("agents", [])
        if len(agents) == 1:
            agent = agents[0]

    if sess.get("mode") == "host":
        _verify_host(
            sess, ws, trial_dir=trial_dir, agent=agent, session_dir=session_dir
        )
    else:
        _verify_container(
            sess, ws, trial_dir=trial_dir, agent=agent, session_dir=session_dir
        )


def _new_trial_dir(workspace: Path) -> Path:
    """Create a timestamped trial directory for a verify run.

    Layout: workspace/.pier/trials/<timestamp>/result.json
    This is compatible with pier view / harbor view which expects:
      jobs_dir/<job_name>/<trial_name>/result.json
    where the single job is "trials".
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    trial_dir = _pier_dir(workspace) / "trials" / timestamp
    trial_dir.mkdir(parents=True, exist_ok=True)
    return trial_dir


def _verify_container(
    sess: dict,
    workspace: Path,
    *,
    trial_dir: str | None = None,
    agent: str | None = None,
    session_dir: str | None = None,
) -> None:
    harbor_bridge = _require_harbor("Container verify")

    hsid = _get_hsid(sess, workspace)
    if not harbor_bridge.is_environment_running(hsid):
        raise click.ClickException(
            "Container is not running. Start it with 'pier start'."
        )

    verify_trial_dir = Path(trial_dir) if trial_dir else _new_trial_dir(workspace)

    # The container's volume mounts point to _harbor_trial_dir (set at start
    # time). We must pass that same dir to verify_environment so the verifier
    # writes to a path the container can reach. Then copy results to the
    # per-verify trial dir.
    harbor_td = _harbor_trial_dir(workspace)
    start_time = datetime.now(timezone.utc)
    try:
        reward = harbor_bridge.verify_environment(
            Path(sess["task_dir"]),
            hsid,
            harbor_td,
        )
    except Exception as e:
        raise click.ClickException(f"Verifier failed: {e}")
    end_time = datetime.now(timezone.utc)

    # Copy verifier output from Harbor's dir to the per-verify trial dir
    harbor_verifier = harbor_td / "verifier"
    if harbor_verifier.is_dir():
        shutil.copytree(
            harbor_verifier, verify_trial_dir / "verifier", dirs_exist_ok=True
        )

    _print_reward(reward)

    # Container mode: auto-detect agent session dir from the mounted logs
    if agent and not session_dir:
        container_session = harbor_bridge.find_container_agent_session_dir(
            agent, harbor_td / "agent"
        )
        if container_session:
            session_dir = str(container_session)
            click.echo(f"Extracting {agent} trajectory from container logs.")
            click.echo("  (override with --session-dir to use a different session)")

    _assemble_trial_output(
        verify_trial_dir,
        sess,
        reward,
        start_time,
        end_time,
        workspace,
        agent,
        session_dir,
    )


def _verify_host(
    sess: dict,
    workspace: Path,
    *,
    trial_dir: str | None = None,
    agent: str | None = None,
    session_dir: str | None = None,
) -> None:
    task_dir = Path(sess["task_dir"])

    if not workspace.exists():
        raise click.ClickException(
            f"Workspace {workspace} does not exist. Was it cleaned up already?"
        )

    verify_trial_dir = Path(trial_dir) if trial_dir else _new_trial_dir(workspace)

    # Default: run the verifier in a temporary container with the workspace
    # mounted, since most task test.sh scripts assume a container environment.
    # Fall back to local execution if Harbor/Docker isn't available.
    try:
        from pier import harbor_bridge

        reward, start_time, end_time = _verify_host_in_container(
            task_dir, workspace, verify_trial_dir, harbor_bridge
        )
    except ImportError:
        reward, start_time, end_time = _verify_host_locally(
            task_dir, workspace, verify_trial_dir
        )

    _print_reward(reward)

    _assemble_trial_output(
        verify_trial_dir,
        sess,
        reward,
        start_time,
        end_time,
        workspace,
        agent,
        session_dir,
    )


def _verify_host_in_container(
    task_dir: Path,
    workspace: Path,
    trial_dir: Path,
    harbor_bridge: object,
) -> tuple[dict, datetime, datetime]:
    """Run the verifier in a temporary container with the workspace mounted."""
    hsid = _harbor_session_id(workspace, prefix="pier-verify")

    click.echo("Starting verifier container...")
    try:
        harbor_bridge.start_environment(  # type: ignore[attr-defined]
            task_dir,
            hsid,
            trial_dir,
            workspace_dir=workspace,
        )

        start_time = datetime.now(timezone.utc)
        reward = harbor_bridge.verify_environment(  # type: ignore[attr-defined]
            task_dir,
            hsid,
            trial_dir,
        )
        end_time = datetime.now(timezone.utc)
    except Exception as e:
        # Best-effort cleanup
        try:
            harbor_bridge.stop_environment(  # type: ignore[attr-defined]
                task_dir,
                hsid,
                trial_dir,
                delete=True,
            )
        except Exception as stop_err:
            logger.debug("Failed to stop verifier container: %s", stop_err)
        raise click.ClickException(f"Verifier failed: {e}")

    click.echo("Stopping verifier container...")
    try:
        harbor_bridge.stop_environment(  # type: ignore[attr-defined]
            task_dir,
            hsid,
            trial_dir,
            delete=True,
        )
    except Exception:
        pass  # non-fatal — container cleanup is best-effort

    return reward, start_time, end_time


def _verify_host_locally(
    task_dir: Path,
    workspace: Path,
    trial_dir: Path,
) -> tuple[dict, datetime, datetime]:
    """Run test.sh directly on the host (fallback when Harbor isn't installed)."""
    test_sh = task_dir / "tests" / "test.sh"
    if not test_sh.exists():
        raise click.ClickException(f"No tests/test.sh in {task_dir}")

    verifier_dir = trial_dir / "verifier"
    verifier_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "TASK_WORKSPACE": str(workspace),
        "VERIFIER_DIR": str(verifier_dir),
    }

    click.echo("Running verifier locally (no Harbor)...")
    start_time = datetime.now(timezone.utc)
    result = subprocess.run(
        ["bash", str(test_sh)],
        env=env,
        capture_output=True,
        text=True,
    )
    end_time = datetime.now(timezone.utc)

    if result.returncode != 0:
        if result.stderr:
            click.echo(result.stderr, err=True)
        if result.stdout:
            click.echo(result.stdout)
        raise click.ClickException(f"Verifier failed (exit {result.returncode})")

    return _read_host_reward(verifier_dir), start_time, end_time


@cli.command()
@click.option(
    "-d",
    "--workspace-dir",
    default=None,
    type=click.Path(),
    help="Workspace directory.",
)
def stop(workspace_dir: str | None) -> None:
    """Stop the container for a workspace."""
    sess, workspace = _resolve_workspace(workspace_dir)

    if sess.get("mode") != "container":
        raise click.ClickException("No container to stop (host-mode workspace).")

    _stop_container_env(sess, workspace)
    click.echo(f"Container for {_workspace_label(workspace)!r} stopped.")


def _stop_container_env(sess: dict, workspace: Path) -> None:
    """Stop the Docker container for a container-mode session."""
    harbor_bridge = _require_harbor("Container stop")

    hsid = _get_hsid(sess, workspace)
    harbor_trial_dir = _harbor_trial_dir(workspace)
    try:
        harbor_bridge.stop_environment(
            Path(sess["task_dir"]),
            hsid,
            harbor_trial_dir,
        )
    except Exception as e:
        raise click.ClickException(f"Failed to stop container: {e}")


@cli.command("list")
def list_workspaces() -> None:
    """List active workspaces."""
    workspaces = _all_workspaces()
    if not workspaces:
        click.echo("No active workspaces.")
        return

    click.echo(f"  {'Workspace':<40} {'Container':<30} {'Status'}")
    click.echo(f"  {'─' * 40} {'─' * 30} {'─' * 8}")
    for s, ws in workspaces:
        if s.get("mode") == "container":
            try:
                from pier import harbor_bridge

                hsid = _get_hsid(s, ws)
                container_name = harbor_bridge.get_container_name(hsid)
                if harbor_bridge.is_environment_running(hsid):
                    status = "running"
                elif harbor_bridge.does_environment_exist(hsid):
                    status = "stopped"
                else:
                    status = "not found"
            except ImportError:
                hsid = _get_hsid(s, ws)
                # Container name follows Harbor's convention: {project}-main-1
                project = hsid.lower().replace(".", "-")
                container_name = f"{project}-main-1"
                status = "unknown (no Harbor)"
        else:
            container_name = "—"
            status = "—"

        click.echo(f"  {str(ws):<40} {container_name:<30} {status}")


# ---------------------------------------------------------------------------
# pier skills
# ---------------------------------------------------------------------------


@cli.command()
def skills() -> None:
    """Install task skills for your coding agent (host mode).

    Finds skills in the task's environment/skills/ directory and registers
    them via npx skills add.
    In container mode with --agent, skills are installed automatically.
    """
    sess, ws = _resolve_workspace()

    if sess.get("mode") == "container":
        raise click.ClickException(
            "In container mode, skills are installed automatically with --agent."
        )

    task_dir = Path(sess["task_dir"])
    skills_dir = task_dir / "environment" / "skills"
    if not skills_dir.is_dir():
        click.echo("No skills found for this task.")
        return

    skill_paths = [
        str(d) for d in sorted(skills_dir.iterdir()) if (d / "SKILL.md").is_file()
    ]
    if not skill_paths:
        click.echo("No skills found for this task.")
        return

    click.echo(f"Installing {len(skill_paths)} skill(s)...")
    result = subprocess.run(
        ["npx", "skills", "add", *skill_paths],
        cwd=str(ws),
    )
    if result.returncode != 0:
        raise click.ClickException("Skills installation failed.")
    click.echo("Skills installed.")


# ---------------------------------------------------------------------------
# pier view / pier summarize — thin wrappers around Harbor's Python API
# ---------------------------------------------------------------------------


def _resolve_pier_dir(path: str | None) -> Path:
    """Resolve the .pier directory from an explicit path or the current workspace."""
    if path:
        p = Path(path).resolve()
        if p.name == PIER_DIR:
            return p
        # Assume it's a workspace directory
        pier = p / PIER_DIR
        if pier.is_dir():
            return pier
        raise click.ClickException(f"No {PIER_DIR}/ directory found in {p}")
    _, ws = _resolve_workspace()
    pier = _pier_dir(ws)
    if not pier.is_dir():
        raise click.ClickException(
            f"No {PIER_DIR}/ directory in {ws}. Run 'pier verify' first."
        )
    return pier


@cli.command()
@click.argument("path", required=False)
@click.option("-p", "--port", default="8080-8089", help="Port or port range.")
@click.option("--host", "bind_host", default="127.0.0.1", help="Host to bind to.")
def view(path: str | None, port: str, bind_host: str) -> None:
    """Open the web dashboard to browse trial trajectories.

    PATH is a .pier directory or workspace (default: current workspace).
    Requires Harbor to be installed.
    """
    pier_dir = _resolve_pier_dir(path)
    try:
        from harbor.cli.view import view_command
    except ImportError:
        raise click.ClickException(
            f"'pier view' requires Harbor.\n{_HARBOR_INSTALL_HINT}"
        )
    view_command(folder=pier_dir, port=port, host=bind_host)


@cli.command()
@click.argument("path", required=False)
@click.option(
    "-m",
    "--model",
    default="haiku",
    help="Model for summarization (haiku, sonnet, opus).",
)
@click.option(
    "-n", "--n-concurrent", default=5, help="Max concurrent summarization queries."
)
@click.option(
    "--all", "all_trials", is_flag=True, help="Summarize all trials, not just failures."
)
@click.option("--overwrite", is_flag=True, help="Overwrite existing summaries.")
def summarize(
    path: str | None,
    model: str,
    n_concurrent: int,
    all_trials: bool,
    overwrite: bool,
) -> None:
    """Summarize trial results using AI.

    PATH is a .pier directory or workspace (default: current workspace).
    Requires Harbor to be installed.
    """
    pier_dir = _resolve_pier_dir(path)
    trials_dir = pier_dir / "trials"
    if not trials_dir.is_dir():
        raise click.ClickException(
            f"No trials found in {pier_dir}. Run 'pier verify' first."
        )

    try:
        from harbor.cli.summarize.summarizer import Summarizer
    except ImportError:
        raise click.ClickException(
            f"'pier summarize' requires Harbor.\n{_HARBOR_INSTALL_HINT}"
        )

    summarizer = Summarizer(
        trials_dir,
        n_concurrent=n_concurrent,
        model=model,
        only_failed=not all_trials,
        overwrite=overwrite,
    )
    summary_path = summarizer.summarize()
    if summary_path:
        click.echo(f"Summary: {summary_path}")
    else:
        click.echo("No summary generated (no matching trials).")
