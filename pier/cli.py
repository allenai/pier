"""pier CLI — interactive workspace manager for coding agents.

Commands:
    pier capture                   — extract agent trajectory for current workspace
    pier traces                    — list or export captured traces
    pier start <task_path>         — launch a workspace for a task
    pier start -d . --image <img>  — launch a task-free container workspace
    pier exec -- <cmd>             — run a command in the workspace
    pier verify                    — run verifier for the workspace
    pier view                      — web dashboard for trial trajectories
    pier summarize                 — AI summary of trial results
    pier stop                      — stop the container
    pier list                      — show active workspaces
    pier skills                    — install task skills (host mode)
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import logging
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import click

from pier import harbor_bridge

PIER_DIR = ".pier"

logger = logging.getLogger(__name__)
INDEX_PATH = Path.home() / ".pier" / "index.json"
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def _resolve_workspace_from_cwd_only() -> tuple[dict, Path]:
    """Resolve a workspace only from the current directory ancestry."""
    found = _find_workspace_from_cwd()
    if found is None:
        raise click.ClickException("Not inside a workspace.")
    return _load_session(found), found


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


def _tar_copy_to_container(workspace: Path, container: str, workdir: str) -> None:
    """Stream workspace files (excluding .pier/) into the container via tar pipe."""
    with subprocess.Popen(
        ["tar", "-cf", "-", "-C", str(workspace), "--exclude", ".pier", "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "COPYFILE_DISABLE": "1"},
    ) as tar_create:
        extract = subprocess.run(
            ["docker", "exec", "-i", container, "tar", "-xf", "-", "-C", workdir],
            stdin=tar_create.stdout,
            capture_output=True,
        )
        if tar_create.stdout is not None:
            tar_create.stdout.close()
        tar_create.wait()
        tar_create_stderr = b""
        if tar_create.stderr is not None:
            tar_create_stderr = tar_create.stderr.read()
    if extract.returncode != 0:
        stderr = extract.stderr.decode(errors="replace").strip()
        raise click.ClickException(
            f"Failed to extract workspace into container: {stderr or 'tar extraction failed'}"
        )
    if tar_create.returncode != 0:
        stderr = tar_create_stderr.decode(errors="replace").strip()
        raise click.ClickException(
            f"Failed to archive workspace for container copy: {stderr or 'tar create failed'}"
        )


def _tar_copy_from_container(container: str, workdir: str, workspace: Path) -> None:
    """Stream container workspace files (excluding .pier/) to host via tar pipe."""
    with subprocess.Popen(
        [
            "docker",
            "exec",
            container,
            "tar",
            "-cf",
            "-",
            "-C",
            workdir,
            "--exclude",
            ".pier",
            ".",
        ],
        stdout=subprocess.PIPE,
    ) as tar_create:
        extract = subprocess.run(
            ["tar", "-xf", "-", "--no-same-owner", "-C", str(workspace)],
            stdin=tar_create.stdout,
            env={**os.environ, "COPYFILE_DISABLE": "1"},
        )
        tar_create.wait()
    if tar_create.returncode != 0 or extract.returncode != 0:
        raise click.ClickException(
            f"tar copy from container failed (create={tar_create.returncode}, "
            f"extract={extract.returncode})"
        )


def _parse_env_file(path: Path) -> list[str]:
    """Parse a .env file into KEY=VALUE strings."""
    result = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            result.append(_validate_env_kv(line, f"{path}:{lineno}"))
        elif line and not line.startswith("#"):
            raise click.ClickException(
                f"Malformed environment entry in {path}:{lineno}: {line!r}. "
                "Expected KEY=VALUE."
            )
    return result


def _validate_env_kv(entry: str, source: str) -> str:
    if entry.startswith("export "):
        raise click.ClickException(
            f"Malformed environment entry from {source}: {entry!r}. "
            "Use KEY=VALUE without 'export '."
        )
    key, sep, value = entry.partition("=")
    if not sep or not key or not ENV_KEY_RE.fullmatch(key):
        raise click.ClickException(
            f"Malformed environment entry from {source}: {entry!r}. "
            "Expected KEY=VALUE with a valid env var name."
        )
    return f"{key}={value}"


def _validate_env_vars(env_list: tuple[str, ...], label: str) -> None:
    for entry in env_list:
        if "=" not in entry or entry.startswith("="):
            raise click.ClickException(
                f"Invalid {label} format: {entry!r}. Use KEY=VALUE."
            )


def _merge_env_lists(
    old: list[str] | None, new: tuple[str, ...] | list[str]
) -> list[str]:
    """Merge new env vars into old list, overriding by key."""
    merged: dict[str, str] = {}
    for entry in old or []:
        k, _, v = entry.partition("=")
        merged[k] = v
    for entry in new:
        k, _, v = entry.partition("=")
        merged[k] = v
    return [f"{k}={v}" for k, v in merged.items()]


def _resolve_restart_ports(
    requested_ports: tuple[int, ...] | list[int] | None, existing_sess: dict
) -> list[int]:
    if requested_ports:
        return list(requested_ports)
    return [int(port) for port in existing_sess.get("ports", [])]


def _resolve_restart_mounts(
    requested_mounts: list[str] | None, existing_sess: dict
) -> list[str]:
    if requested_mounts is not None:
        return requested_mounts
    return list(existing_sess.get("extra_mounts", []))


def _parse_mounts_json(mounts_json: str | None) -> list[str] | None:
    if not mounts_json:
        return None
    try:
        parsed = json.loads(mounts_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"--mounts-json must be valid JSON: {exc.msg}"
        ) from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise click.ClickException("--mounts-json must be a JSON array of strings")
    return parsed


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


def _seed_workspace(
    task_dir: Path, workspace: Path, *, container: bool = False
) -> None:
    """Seed the workspace with the task image's WORKDIR contents.

    Tries to build the Docker image and extract WORKDIR contents so the
    workspace matches what Harbor's container would have.  Falls back to
    copying the environment/ directory (approximates ``COPY . /app``).

    In host mode, .task/ is symlinked so task edits propagate immediately.
    In container mode, .task/ is populated separately (by _write_mounts_compose
    or the --no-mount copy path) using copies, so we skip it here.
    """
    try:
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
        # Host mode: symlink so edits to the task source propagate immediately.
        dot_task = workspace / ".task"
        dot_task.mkdir(exist_ok=True)
        instruction = task_dir / "instruction.md"
        if instruction.exists():
            link = dot_task / "instruction.md"
            if not link.exists():
                link.symlink_to(instruction.resolve())


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

        try:
            task_dir = harbor_bridge.download_task(git_url, task_subpath)
        except Exception as e:
            raise click.ClickException(f"Failed to download task: {e}")
        return task_dir

    task_dir = Path(task_path).resolve()
    if not (task_dir / "task.toml").exists():
        raise click.ClickException(f"No task.toml in {task_dir}")
    return task_dir


def _force_teardown(workspace: Path) -> None:
    """Remove an existing workspace directory and stop its container (if any)."""
    if _session_json_path(workspace).exists():
        try:
            sess = _load_session(workspace)
            if sess.get("mode") == "container":
                hsid = _get_hsid(sess, workspace)
                harbor_trial_dir = _harbor_trial_dir(workspace)
                try:
                    harbor_bridge.stop_environment(
                        Path(sess["task_dir"]),
                        hsid,
                        harbor_trial_dir,
                    )
                    click.echo(
                        f"Stopped existing container for {_workspace_label(workspace)!r}."
                    )
                except Exception as e:
                    click.echo(
                        f"Warning: could not stop container cleanly: {e}",
                        err=True,
                    )
                    _force_remove_container(hsid, workspace)
        except Exception as e:
            click.echo(f"Warning: could not load session: {e}", err=True)

        index = _index_load()
        ws_str = str(workspace.resolve())
        if ws_str in index:
            index.remove(ws_str)
            _index_save(index)

    if workspace.exists():
        shutil.rmtree(workspace)
        click.echo(f"Removed existing workspace at {workspace}.")


def _force_remove_container(hsid: str, workspace: Path) -> None:
    """Fallback: remove the container via Docker and clean up the session."""
    container = harbor_bridge.get_container_name(hsid)
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    sp = _session_json_path(workspace)
    if sp.exists():
        sp.unlink()
    index = _index_load()
    ws_str = str(workspace.resolve())
    if ws_str in index:
        index.remove(ws_str)
        _index_save(index)


def _print_reward(reward: dict) -> None:
    """Print reward value and any extra details from a reward dict."""
    click.echo(f"Reward: {reward.get('reward', 'N/A')}")
    for k, v in reward.items():
        if k != "reward":
            click.echo(f"  {k}: {v}")


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
            else:
                click.echo("Warning: trajectory extraction returned no data.", err=True)
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
    """pier — interactive workspace manager for coding agents."""


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
    help="Workspace directory. Required for local tasks and task-free mode.",
)
@click.option(
    "-a",
    "--agent",
    default=None,
    help="Agent to set up (container mode). E.g. claude-code, codex, goose.",
)
@click.option(
    "--image",
    default=None,
    help="Base Docker image for task-free mode (e.g. ubuntu:24.04).",
)
@click.option(
    "--ports",
    multiple=True,
    type=int,
    help="Expose container port to host (repeatable).",
)
@click.option(
    "--mounts-json",
    default=None,
    help="JSON array of volume mounts (e.g. '[\"./skills:/opt/asta-plugins/skills:ro\"]').",
)
@click.option(
    "-e",
    "extra_env_cli",
    multiple=True,
    help="Container-mode environment variable in KEY=VALUE format, forwarded to all pier exec commands (repeatable).",
)
@click.option(
    "--env-file",
    type=click.Path(exists=True),
    default=None,
    help="Path to a .env file to load into the container environment (container mode only).",
)
@click.option(
    "--no-mount",
    is_flag=True,
    default=False,
    help="Don't bind-mount the workspace into the container. Files stay inside the container only.",
)
@click.option(
    "-f",
    "--force",
    is_flag=True,
    default=False,
    help="Allow starting in a workspace directory that already has files (non-empty).",
)
@click.option(
    "--delete",
    "delete_workspace",
    is_flag=True,
    default=False,
    help="Remove any existing pier session at -d (stop container, delete workspace tree) before starting.",
)
@click.option(
    "--ae",
    "--agent-env",
    "agent_env",
    multiple=True,
    help="Agent/session env (KEY=VALUE). Persisted and passed to pier exec; overrides host *_API_KEY for the same name.",
)
@click.option(
    "--ee",
    "--environment-env",
    "environment_env",
    multiple=True,
    help="Compose service env at container start (KEY=VALUE). For exec-time vars use -e, --env-file, or --ae.",
)
@click.option(
    "--exec",
    "exec_cmd_str",
    default=None,
    help="Run a command in the container after start (container mode only).",
)
def start(
    task_path: str | None,
    host: bool,
    workspace_dir: str | None,
    agent: str | None,
    image: str | None,
    ports: tuple[int, ...],
    mounts_json: str | None,
    extra_env_cli: tuple[str, ...],
    env_file: str | None,
    no_mount: bool,
    force: bool,
    delete_workspace: bool,
    agent_env: tuple[str, ...],
    environment_env: tuple[str, ...],
    exec_cmd_str: str | None,
) -> None:
    """Launch a workspace, or install an agent into an existing one.

    TASK_PATH is a local directory containing task.toml, or a remote git
    reference in the form URL#path (e.g.
    https://github.com/org/repo#tasks/my-task).

    If TASK_PATH is omitted with --image and -d, starts a task-free
    container from the given image.

    If TASK_PATH is omitted without --image, operates on the current
    workspace (resolved from cwd). This is useful for installing an agent
    into an existing workspace: pier start --agent claude-code

    \b
    Examples:
        pier start ./tasks/my-task -d ./workspace
        pier start -d . --image ubuntu:24.04 --agent claude-code
        pier start --agent claude-code
    """
    extra_mounts = _parse_mounts_json(mounts_json)

    if agent_env:
        _validate_env_vars(agent_env, "--ae/--agent-env")
    if environment_env:
        _validate_env_vars(environment_env, "--ee/--environment-env")
    if exec_cmd_str and host:
        raise click.ClickException("--exec is not supported with --host.")

    if no_mount and host:
        raise click.ClickException("--no-mount and --host are mutually exclusive.")
    if host and extra_env_cli:
        raise click.ClickException("-e cannot be used with --host.")
    if host and env_file:
        raise click.ClickException("--env-file cannot be used with --host.")

    # Collect and validate extra env vars from -e and --env-file.
    # Stored in the session and forwarded on every pier exec.
    extra_env_list: list[str] = []
    for kv in extra_env_cli:
        extra_env_list.append(_validate_env_kv(kv, "-e"))
    if env_file:
        extra_env_list.extend(_parse_env_file(Path(env_file)))

    # Load into host env so task.toml resolution picks them up.
    for kv in extra_env_list:
        key, _, val = kv.partition("=")
        if key and _:
            os.environ[key] = val

    # Task-free mode: --image without task_path
    if task_path is None and image:
        if not workspace_dir:
            raise click.ClickException(
                "Task-free mode requires -d/--dir to specify the workspace directory, "
                "e.g. pier start -d . --image ubuntu:24.04 --agent claude-code"
            )
        p = Path(workspace_dir)
        workspace = p if p.is_absolute() else Path.cwd().resolve() / p
        workspace = workspace.resolve()
        _start_task_free(
            workspace,
            image,
            agents=[agent] if agent else None,
            ports=list(ports),
            extra_mounts=extra_mounts,
            extra_env=extra_env_list,
            no_mount=no_mount,
            force=force,
            delete_workspace=delete_workspace,
            agent_env=list(agent_env),
            environment_env=list(environment_env),
        )
        if exec_cmd_str:
            sess, ws = _resolve_workspace(str(workspace))
            _exec_container(sess, ws, shlex.split(exec_cmd_str))
        return

    # No task_path, no image → operate on existing workspace from cwd
    if task_path is None:
        _start_existing(
            agent=agent,
            agent_env=agent_env,
            environment_env=environment_env,
        )
        return

    if image:
        click.echo(
            "Warning: --image is ignored when a task path is provided.", err=True
        )

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

    workspace = workspace.resolve()
    if delete_workspace and _session_json_path(workspace).exists():
        _force_teardown(workspace)

    # Collision check — existing session in this workspace
    if _session_json_path(workspace).exists():
        existing_sess = _load_session(workspace)
        mode = existing_sess.get("mode")

        if mode == "container":
            hsid = _get_hsid(existing_sess, workspace)
            if harbor_bridge.is_environment_running(hsid):
                if agent:
                    _install_agents_into_running(
                        existing_sess,
                        workspace,
                        [agent],
                        agent_env=agent_env,
                        environment_env=environment_env,
                    )
                    return
                click.echo("Container is already running.")
                return
            else:
                # Container stopped — restart it
                existing_task_dir = Path(existing_sess["task_dir"]).resolve()
                if task_dir.resolve() != existing_task_dir:
                    raise click.ClickException(
                        "Workspace already exists for a different task. "
                        "Restart it with the original task path or run from inside the workspace."
                    )
                agents = _add_agent(existing_sess.get("agents", []), agent)
                resolved_ports = _resolve_restart_ports(ports, existing_sess)
                resolved_extra_mounts = _resolve_restart_mounts(
                    extra_mounts, existing_sess
                )
                merged_ae = (
                    _merge_env_lists(existing_sess.get("agent_env"), agent_env)
                    if agent_env
                    else list(existing_sess.get("agent_env", []))
                )
                merged_ee = (
                    _merge_env_lists(
                        existing_sess.get("environment_env"), environment_env
                    )
                    if environment_env
                    else list(existing_sess.get("environment_env", []))
                )
                _start_container(
                    task_dir,
                    workspace,
                    agents=agents,
                    ports=resolved_ports,
                    extra_mounts=resolved_extra_mounts,
                    extra_env=extra_env_list or existing_sess.get("extra_env", []),
                    no_mount=existing_sess.get("no_mount", False),
                    agent_env=merged_ae,
                    environment_env=merged_ee,
                )
                return
        else:
            # Host mode — workspace already exists, nothing to do
            click.echo(f"Workspace already exists at {workspace}.")
            return

    # Safety check: don't write into a non-empty directory by accident.
    if not force and workspace.exists() and any(workspace.iterdir()):
        raise click.ClickException(
            f"Directory {workspace} is not empty. Use -f/--force to proceed anyway."
        )
    workspace.mkdir(parents=True, exist_ok=True)

    # Seed workspace with the same files the container WORKDIR would have.
    if not no_mount:
        _seed_workspace(task_dir, workspace, container=not host)

    if host:
        if agent:
            click.echo("Hint: --agent is for container mode.")
        _start_host(task_dir, workspace)
    else:
        _start_container(
            task_dir,
            workspace,
            agents=[agent] if agent else None,
            ports=list(ports),
            extra_mounts=extra_mounts,
            extra_env=extra_env_list,
            no_mount=no_mount,
            agent_env=list(agent_env),
            environment_env=list(environment_env),
        )
        if exec_cmd_str:
            sess, ws = _resolve_workspace(str(workspace))
            _exec_container(sess, ws, shlex.split(exec_cmd_str))


def _start_existing(
    agent: str | None,
    agent_env: tuple[str, ...] = (),
    environment_env: tuple[str, ...] = (),
) -> None:
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

    hsid = _get_hsid(sess, ws)

    if not harbor_bridge.is_environment_running(hsid):
        # Restart the stopped container (reinstalls all agents)
        agents = _add_agent(sess.get("agents", []), agent)
        merged_ae = (
            _merge_env_lists(sess.get("agent_env"), agent_env)
            if agent_env
            else list(sess.get("agent_env", []))
        )
        merged_ee = (
            _merge_env_lists(sess.get("environment_env"), environment_env)
            if environment_env
            else list(sess.get("environment_env", []))
        )
        _start_container(
            Path(sess["task_dir"]),
            ws,
            agents=agents,
            ports=_resolve_restart_ports(None, sess),
            extra_mounts=_resolve_restart_mounts(None, sess),
            extra_env=sess.get("extra_env", []),
            no_mount=sess.get("no_mount", False),
            agent_env=merged_ae,
            environment_env=merged_ee,
        )
        return

    if agent:
        _install_agents_into_running(
            sess,
            ws,
            [agent],
            agent_env=agent_env,
            environment_env=environment_env,
        )
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
    click.echo("")
    click.echo("Task instruction is at .task/instruction.md.")


def _install_agent(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    agent: str,
) -> None:
    """Validate and install a Harbor agent into a running container."""
    if not harbor_bridge.is_valid_agent(agent):
        raise click.ClickException(f"Unknown agent {agent!r}.")
    click.echo(f"Installing {agent} in the container...")
    try:
        harbor_bridge.setup_agent(task_dir, harbor_session_id, trial_dir, agent)
        click.echo(f"{agent} installed.")
    except Exception as e:
        raise click.ClickException(f"Agent setup failed: {e}")


def _install_agents_into_running(
    sess: dict,
    workspace: Path,
    agents: list[str],
    *,
    agent_env: tuple[str, ...] = (),
    environment_env: tuple[str, ...] = (),
) -> None:
    """Install one or more agents into an already-running container and update session."""
    task_dir = Path(sess["task_dir"])
    hsid = _get_hsid(sess, workspace)
    trial_dir = _harbor_trial_dir(workspace)
    for agent in agents:
        _install_agent(task_dir, hsid, trial_dir, agent)
    current = sess.get("agents", [])
    for agent in agents:
        current = _add_agent(current, agent)
    sess["agents"] = current
    if agent_env:
        sess["agent_env"] = _merge_env_lists(sess.get("agent_env"), agent_env)
    if environment_env:
        sess["environment_env"] = _merge_env_lists(
            sess.get("environment_env"), environment_env
        )
    _save_session(workspace, sess)


def _start_container(
    task_dir: Path,
    workspace: Path,
    agents: list[str] | None = None,
    ports: list[int] | None = None,
    extra_mounts: list[str] | None = None,
    extra_env: list[str] | None = None,
    no_mount: bool = False,
    agent_env: list[str] | None = None,
    environment_env: list[str] | None = None,
) -> None:

    hsid = _harbor_session_id(workspace)
    harbor_trial_dir = _harbor_trial_dir(workspace)

    label = _workspace_label(workspace)
    click.echo(f"Starting {label} (building image if needed)...")
    try:
        harbor_bridge.start_environment(
            task_dir,
            hsid,
            harbor_trial_dir,
            workspace_dir=None if no_mount else workspace,
            ports=ports,
            extra_mounts=extra_mounts,
            environment_env=environment_env or [],
        )
    except Exception as e:
        raise click.ClickException(f"Failed to start container: {e}")

    if no_mount:
        container = harbor_bridge.get_container_name(hsid)
        workdir = harbor_bridge.get_container_workdir(task_dir)

        # Copy host workspace into container (excluding .pier/).
        # Fix git ownership (files change uid across docker cp).
        if (workspace / ".git").is_dir():
            subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "git",
                    "config",
                    "--global",
                    "--add",
                    "safe.directory",
                    workdir,
                ],
                capture_output=True,
            )
        # Copy task instruction into workspace/.task/ (same as bind-mount mode)
        # before tar so it's included in the transfer.
        harbor_bridge.copy_task_files(task_dir, workspace / ".task")

        if any(f for f in workspace.iterdir() if f.name != ".pier"):
            _tar_copy_to_container(workspace, container, workdir)

    for agent in agents or []:
        _install_agent(task_dir, hsid, harbor_trial_dir, agent)

    session_data: dict = {
        "mode": "container",
        "task_dir": str(task_dir),
        "task_ref": task_dir.name,
        "harbor_session_id": hsid,
        "agents": agents or [],
        "ports": ports or [],
        "extra_mounts": extra_mounts or [],
        "extra_env": extra_env or [],
        "no_mount": no_mount,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if agent_env:
        session_data["agent_env"] = agent_env
    if environment_env:
        session_data["environment_env"] = environment_env
    _save_session(workspace, session_data)

    click.echo(f"\nWorkspace {label!r} ready.")
    click.echo(f"  cd {workspace}")
    click.echo("  pier exec bash           # drop into the container")
    if agents:
        names = ", ".join(agents)
        click.echo(f"  pier exec <agent-cli>    # run an installed agent ({names})")
    if (task_dir / "instruction.md").exists() and (
        task_dir / "instruction.md"
    ).stat().st_size > 0:
        click.echo("")
        click.echo(
            "Task instruction is at .task/instruction.md (inside the container too)."
        )


def _start_task_free(
    workspace: Path,
    image: str,
    agents: list[str] | None = None,
    ports: list[int] | None = None,
    extra_mounts: list[str] | None = None,
    extra_env: list[str] | None = None,
    no_mount: bool = False,
    force: bool = False,
    delete_workspace: bool = False,
    agent_env: list[str] | None = None,
    environment_env: list[str] | None = None,
) -> None:
    """Start a task-free container from a base image.

    Creates a synthetic task directory (minimal Dockerfile + task.toml)
    so we can reuse Harbor's standard environment machinery instead of
    maintaining a separate code path.
    """
    ae_list = agent_env or []
    ee_list = environment_env or []

    if delete_workspace and _session_json_path(workspace).exists():
        _force_teardown(workspace)

    # If workspace already has a session, handle like _start_existing
    if _session_json_path(workspace).exists():
        existing_sess = _load_session(workspace)
        if existing_sess.get("mode") == "container":
            hsid = _get_hsid(existing_sess, workspace)
            if harbor_bridge.is_environment_running(hsid):
                if agents:
                    _install_agents_into_running(existing_sess, workspace, agents)
                else:
                    click.echo("Container is already running.")
                return
            else:
                # Container stopped — restart it
                existing_agents = _add_agent(existing_sess.get("agents", []), None)
                if agents:
                    for a in agents:
                        existing_agents = _add_agent(existing_agents, a)
                resolved_ports = _resolve_restart_ports(ports, existing_sess)
                resolved_extra_mounts = _resolve_restart_mounts(
                    extra_mounts, existing_sess
                )
                merged_ae = (
                    _merge_env_lists(existing_sess.get("agent_env"), tuple(ae_list))
                    if ae_list
                    else list(existing_sess.get("agent_env", []))
                )
                merged_ee = (
                    _merge_env_lists(
                        existing_sess.get("environment_env"), tuple(ee_list)
                    )
                    if ee_list
                    else list(existing_sess.get("environment_env", []))
                )
                _start_container(
                    harbor_bridge.create_synthetic_task_dir(
                        image, _pier_dir(workspace)
                    ),
                    workspace,
                    agents=existing_agents,
                    ports=resolved_ports,
                    extra_mounts=resolved_extra_mounts,
                    extra_env=extra_env or existing_sess.get("extra_env", []),
                    no_mount=existing_sess.get("no_mount", False),
                    agent_env=merged_ae,
                    environment_env=merged_ee,
                )
                sess = _load_session(workspace)
                sess["image"] = image
                _save_session(workspace, sess)
                return

    # Safety check for non-empty directory.
    if not force and workspace.exists() and any(workspace.iterdir()):
        raise click.ClickException(
            f"Directory {workspace} is not empty. Use -f/--force to proceed anyway."
        )
    workspace.mkdir(parents=True, exist_ok=True)

    # Create synthetic task so Harbor's environment machinery works
    pier_dir = _pier_dir(workspace)
    pier_dir.mkdir(parents=True, exist_ok=True)
    task_dir = harbor_bridge.create_synthetic_task_dir(image, pier_dir)

    # Reuse the standard container start path
    _start_container(
        task_dir,
        workspace,
        agents=agents,
        ports=ports,
        extra_mounts=extra_mounts,
        extra_env=extra_env,
        no_mount=no_mount,
        agent_env=ae_list,
        environment_env=ee_list,
    )

    # Update session with task-free metadata (overwrite what _start_container wrote)
    sess = _load_session(workspace)
    sess["image"] = image
    _save_session(workspace, sess)


@cli.command(
    "exec",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option(
    "-d",
    "--detach",
    is_flag=True,
    default=False,
    help="Run in the background (detached).",
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
def exec_cmd(detach: bool, command: tuple[str, ...]) -> None:
    """Run a command inside the workspace.

    Container mode uses docker exec (streaming output). Host mode runs
    the command directly with workspace env vars set.

    \b
    Examples:
        pier exec bash
        pier exec claude
        pier exec -d -- quarto preview --port 8888 --host 0.0.0.0 --no-browse
    """
    if not command:
        raise click.ClickException("No command specified.")

    sess, ws = _resolve_workspace()

    if sess.get("mode") == "host":
        _exec_host(sess, ws, list(command))
    else:
        _exec_container(sess, ws, list(command), detach=detach)


def _exec_container(
    sess: dict, workspace: Path, command: list[str], *, detach: bool = False
) -> None:
    hsid = _get_hsid(sess, workspace)
    if not harbor_bridge.is_environment_running(hsid):
        raise click.ClickException(
            "Container is not running. Start it with 'pier start'."
        )

    # Build env vars from task config, session, and agent setup.
    # TODO: -e/--env-file vars should ideally be scoped (agent-only vs container-wide)
    # but pier exec can't distinguish agent from non-agent commands.
    task_dir = Path(sess["task_dir"])
    env: dict[str, str] = {}
    env.update(harbor_bridge.resolve_task_env(task_dir))
    for var, val in os.environ.items():
        if var.endswith("_API_KEY") and var not in env:
            env[var] = val
    for kv in sess.get("extra_env", []):
        key, _, val = kv.partition("=")
        if key:
            env[key] = val
    for entry in sess.get("agent_env", []):
        k, _, v = entry.partition("=")
        if k:
            env[k] = v

    # Merge agent env vars and PATH prefixes.
    path_prefixes: list[str] = []
    for agent_name in sess.get("agents", []):
        agent_env, pfx = harbor_bridge.get_agent_exec_env(agent_name)
        env.update(agent_env)
        if pfx:
            path_prefixes.append(pfx)

    rc = harbor_bridge.exec_in_container(
        hsid,
        task_dir,
        command,
        env=env,
        path_prefix=":".join(path_prefixes),
        detach=detach,
    )
    raise SystemExit(rc)


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

    # Run the verifier in a temporary container with the workspace mounted,
    # since most task test.sh scripts assume a container environment.
    reward, start_time, end_time = _verify_host_in_container(
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
) -> tuple[dict, datetime, datetime]:
    """Run the verifier in a temporary container with the workspace mounted."""
    hsid = _harbor_session_id(workspace, prefix="pier-verify")

    click.echo("Starting verifier container...")
    try:
        harbor_bridge.start_environment(
            task_dir,
            hsid,
            trial_dir,
            workspace_dir=workspace,
        )

        start_time = datetime.now(timezone.utc)
        reward = harbor_bridge.verify_environment(
            task_dir,
            hsid,
            trial_dir,
        )
        end_time = datetime.now(timezone.utc)
    except Exception as e:
        # Best-effort cleanup
        try:
            harbor_bridge.stop_environment(
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
        harbor_bridge.stop_environment(
            task_dir,
            hsid,
            trial_dir,
            delete=True,
        )
    except Exception:
        pass  # non-fatal — container cleanup is best-effort

    return reward, start_time, end_time


@cli.command()
@click.option(
    "-d",
    "--workspace-dir",
    default=None,
    type=click.Path(),
    help="Workspace directory.",
)
@click.option(
    "--all",
    "stop_all",
    is_flag=True,
    default=False,
    help="Stop all active container-mode workspaces.",
)
def stop(workspace_dir: str | None, stop_all: bool) -> None:
    """Stop the container for a workspace."""
    if stop_all:
        workspaces = _all_workspaces()
        stopped = 0
        for s, ws in workspaces:
            if s.get("mode") != "container":
                continue
            try:
                _stop_one_workspace(s, ws)
                click.echo(f"Stopped {_workspace_label(ws)!r}.")
                stopped += 1
            except click.ClickException as e:
                click.echo(
                    f"Warning: could not stop {_workspace_label(ws)!r}: {e}",
                    err=True,
                )
        if stopped == 0:
            click.echo("No container-mode workspaces to stop.")
        return

    sess, workspace = _resolve_workspace(workspace_dir)

    if sess.get("mode") != "container":
        raise click.ClickException("No container to stop (host-mode workspace).")

    _stop_one_workspace(sess, workspace)
    click.echo(f"Container for {_workspace_label(workspace)!r} stopped.")


def _stop_one_workspace(sess: dict, workspace: Path) -> None:
    """Stop container for one workspace (no success message)."""
    if sess.get("no_mount"):
        hsid = _get_hsid(sess, workspace)
        container = harbor_bridge.get_container_name(hsid)
        workdir = harbor_bridge.get_container_workdir(Path(sess["task_dir"]))
        if harbor_bridge.is_environment_running(hsid):
            click.echo("Copying workspace files from container...")
            _tar_copy_from_container(container, workdir, workspace)

    _stop_container_env(sess, workspace)


def _stop_container_env(sess: dict, workspace: Path) -> None:
    """Stop the Docker container for a container-mode session."""
    hsid = _get_hsid(sess, workspace)
    harbor_trial_dir = _harbor_trial_dir(workspace)
    task_dir = Path(sess["task_dir"])

    try:
        harbor_bridge.stop_environment(
            task_dir,
            hsid,
            harbor_trial_dir,
        )
    except Exception:
        _force_remove_container(hsid, workspace)


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
            hsid = _get_hsid(s, ws)
            container_name = harbor_bridge.get_container_name(hsid)
            if harbor_bridge.is_environment_running(hsid):
                status = "running"
            elif harbor_bridge.does_environment_exist(hsid):
                status = "stopped"
            else:
                status = "not found"
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

    Reads skills_dir from task.toml, extracts skills from the task's
    container image, and registers them via npx skills add.
    In container mode with --agent, skills are installed automatically.
    """
    sess, ws = _resolve_workspace()

    if sess.get("mode") == "container":
        raise click.ClickException(
            "In container mode, skills are installed automatically with --agent."
        )

    task_dir = Path(sess["task_dir"])
    task_toml = _read_task_toml(task_dir)
    skills_dir_str = (task_toml.get("environment") or {}).get("skills_dir")
    if not skills_dir_str:
        click.echo("No skills_dir in task.toml.")
        return

    # Skills live inside the container image. Spin up a temporary container,
    # copy them out, and register with the host agent.
    click.echo("Extracting skills from task image...")
    hsid = _harbor_session_id(ws, prefix="pier-skills")
    container = harbor_bridge.get_container_name(hsid)
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            harbor_bridge.start_environment(
                task_dir,
                hsid,
                Path(tmpdir) / "trial",
            )
            dest = Path(tmpdir) / "skills"
            subprocess.run(
                ["docker", "cp", f"{container}:{skills_dir_str}", str(dest)],
                check=True,
                capture_output=True,
            )
            _install_skills_from_dir(dest, ws)
        except Exception as e:
            raise click.ClickException(f"Failed to extract skills: {e}")
        finally:
            try:
                harbor_bridge.stop_environment(
                    task_dir, hsid, Path(tmpdir) / "trial", delete=True
                )
            except Exception:
                pass


def _install_skills_from_dir(skills_dir: Path, workspace: Path) -> None:
    """Run npx skills add for all skills in a directory."""
    skill_paths = [
        str(d) for d in sorted(skills_dir.iterdir()) if (d / "SKILL.md").is_file()
    ]
    if not skill_paths:
        click.echo("No skills found.")
        return

    click.echo(f"Installing {len(skill_paths)} skill(s)...")
    result = subprocess.run(
        ["npx", "skills", "add", *skill_paths],
        cwd=str(workspace),
    )
    if result.returncode != 0:
        raise click.ClickException("Skills installation failed.")
    click.echo("Skills installed.")


# ---------------------------------------------------------------------------
# pier capture — extract agent trajectory without verification
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "-a",
    "--agent",
    default=None,
    help="Agent name (e.g. claude-code). Auto-detected if omitted.",
)
@click.option(
    "--session-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Agent session/log directory. Auto-discovered if omitted.",
)
def capture(agent: str | None, session_dir: str | None) -> None:
    """Capture the agent's trajectory for the current workspace.

    In container mode, the session directory is detected automatically.
    Outside a container, pass --session-dir pointing to the agent's
    log directory.  No Harbor task required.

    \b
    Examples:
        pier capture --session-dir ~/.claude/projects/my-project
        pier capture --session-dir PATH -a claude-code
    """

    # Resolve workspace if we're inside one, otherwise use cwd
    try:
        sess, ws = _resolve_workspace_from_cwd_only()
    except click.ClickException:
        sess = None
        ws = Path.cwd().resolve()

    # Discover session directory
    resolved_session_dir: Path | None = None

    if session_dir:
        resolved_session_dir = Path(session_dir).resolve()
    elif sess and sess.get("mode") == "container":
        # Container mode: look inside the container's agent log directory
        if not agent:
            agents = sess.get("agents", [])
            if len(agents) == 1:
                agent = agents[0]
            elif agents:
                raise click.ClickException(
                    f"Multiple agents installed ({', '.join(agents)}). "
                    "Specify one with -a."
                )
        if agent:
            harbor_td = _harbor_trial_dir(ws)
            resolved_session_dir = harbor_bridge.find_container_agent_session_dir(
                agent, harbor_td / "agent"
            )
        if resolved_session_dir is None:
            raise click.ClickException(
                "Could not find agent session files in container.\n"
                "Pass --session-dir to specify explicitly."
            )
    else:
        raise click.ClickException(
            "Pass --session-dir pointing to the agent's log directory, e.g.:\n"
            "  pier capture --session-dir ~/.claude/projects/<project-name>"
        )

    # Auto-detect agent if not specified
    if not agent:
        agent = harbor_bridge.detect_agent_from_session_dir(resolved_session_dir)
        if not agent:
            raise click.ClickException(
                f"Could not detect agent from {resolved_session_dir}. Specify with -a."
            )
        click.echo(f"Detected agent: {agent}")

    # Create trial directory and assemble (no reward — capture only)
    trial_dir = _new_trial_dir(ws)
    now = datetime.now(timezone.utc)
    fake_sess = sess or {"task_dir": str(ws), "task_ref": ws.name}
    _assemble_trial_output(
        trial_dir,
        fake_sess,
        {},  # no reward
        now,
        now,
        ws,
        agent,
        str(resolved_session_dir),
    )


# ---------------------------------------------------------------------------
# pier traces export — package traces for sharing
# ---------------------------------------------------------------------------


@cli.command("traces")
@click.argument("trial_name", required=False)
@click.option(
    "-o",
    "--output",
    default=None,
    type=click.Path(),
    help="Output file path. Without -o, lists trials instead of exporting.",
)
@click.option(
    "--all",
    "all_trials",
    is_flag=True,
    default=False,
    help="Export all trials (default: latest only).",
)
def traces_cmd(trial_name: str | None, output: str | None, all_trials: bool) -> None:
    """List or export captured traces.

    Without -o, lists available trials. With -o, packages trials into
    a tar.gz for sharing. TRIAL_NAME selects a specific trial by its
    timestamp directory name.

    \b
    Examples:
        pier traces                              # list trials
        pier traces -o trace.tar.gz              # export latest trial
        pier traces 2026-04-02_15-30-00 -o t.gz  # export specific trial
        pier traces --all -o traces.tar.gz       # export all trials
    """
    try:
        _, ws = _resolve_workspace_from_cwd_only()
    except click.ClickException:
        ws = Path.cwd().resolve()

    trials_dir = _pier_dir(ws) / "trials"
    if not trials_dir.is_dir() or not any(trials_dir.iterdir()):
        if output:
            raise click.ClickException(
                "No trials found. Run 'pier capture' or 'pier verify' first."
            )
        click.echo("No trials found. Run 'pier capture' or 'pier verify' first.")
        return

    all_trial_dirs = sorted(
        (d for d in trials_dir.iterdir() if d.is_dir()), key=lambda d: d.name
    )

    # List mode (no -o)
    if not output:
        for td in all_trial_dirs:
            agent = ""
            reward = ""
            result_path = td / "result.json"
            if result_path.exists():
                try:
                    result = json.loads(result_path.read_text())
                    agent_info = (
                        result.get("agent_info") or result.get("agent_result") or {}
                    )
                    if isinstance(agent_info, dict):
                        agent = agent_info.get("name", "")
                    r = result.get("reward")
                    if r is None:
                        rewards = result.get("rewards") or {}
                        if isinstance(rewards, dict):
                            r = rewards.get("reward")
                    if r is None:
                        verifier_result = result.get("verifier_result") or {}
                        if isinstance(verifier_result, dict):
                            verifier_rewards = verifier_result.get("rewards") or {}
                            if isinstance(verifier_rewards, dict):
                                r = verifier_rewards.get("reward")
                    if r is not None:
                        reward = f"reward={r}"
                except (json.JSONDecodeError, KeyError):
                    pass

            has_trajectory = (td / "agent" / "trajectory.json").exists()
            parts = [td.name]
            if agent:
                parts.append(agent)
            if reward:
                parts.append(reward)
            if has_trajectory:
                parts.append("trajectory")
            click.echo("  ".join(parts))
        return

    # Export mode (-o)
    import tarfile

    if trial_name:
        specific = trials_dir / trial_name
        if not specific.is_dir():
            raise click.ClickException(
                f"Trial {trial_name!r} not found. Run 'pier traces' to see available trials."
            )
        export_dirs = [specific]
    elif all_trials:
        export_dirs = all_trial_dirs
    else:
        export_dirs = [all_trial_dirs[-1]]

    out_path = Path(output)
    with tarfile.open(out_path, "w:gz") as tar:
        for td in export_dirs:
            tar.add(td, arcname=td.name)

    n = len(export_dirs)
    label = "trial" if n == 1 else "trials"
    click.echo(f"Exported {n} {label} to {out_path}")


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
    try:
        _, ws = _resolve_workspace_from_cwd_only()
    except click.ClickException:
        ws = Path.cwd().resolve()
    pier = _pier_dir(ws)
    if not pier.is_dir():
        raise click.ClickException(
            f"No {PIER_DIR}/ directory in {ws}. Run 'pier capture' or 'pier verify' first."
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
    harbor_bridge.run_view_command(folder=pier_dir, port=port, host=bind_host)


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

    summary_path = harbor_bridge.run_summarize(
        trials_dir,
        n_concurrent=n_concurrent,
        model=model,
        only_failed=not all_trials,
        overwrite=overwrite,
    )
    if summary_path:
        click.echo(f"Summary: {summary_path}")
    else:
        click.echo("No summary generated (no matching trials).")
