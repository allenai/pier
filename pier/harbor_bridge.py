"""Thin adapter isolating all Harbor imports.

Pier's only file that imports Harbor. If Harbor refactors internals,
only this file changes.

# Harbor API stability notes (Harbor is pre-1.0 as of 2026-03)
#
# Stable (exported in harbor.__all__):
#   Task, TrialPaths, Verifier
#
# Fragile (internal imports — update here if Harbor reorganizes):
#   harbor.environments.factory.EnvironmentFactory
#   harbor.verifier.verifier.Verifier  (re-exported as harbor.Verifier)
#
# Docker compose project naming convention:
#   session_id.lower().replace(".", "-")
#   Container name: {project}-main-1
#   See: environments/docker/docker.py -> _run_docker_compose_command()
#
# If Harbor adds public harbor.run / harbor.verify CLI APIs, switch to those.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Container naming helpers
# ---------------------------------------------------------------------------


def get_compose_project(harbor_session_id: str) -> str:
    """Docker compose project name derived from a pier harbor_session_id.

    Harbor lowercases the session_id and replaces dots with dashes.
    If Harbor changes this convention, update here and in get_container_name().
    """
    return harbor_session_id.lower().replace(".", "-")


def get_container_name(harbor_session_id: str) -> str:
    """Actual Docker container name for a pier session.

    Harbor docker-compose names containers: {project}-{service}-{index}.
    The primary service is always named 'main'.
    """
    project = get_compose_project(harbor_session_id)
    return f"{project}-main-1"


def is_environment_running(harbor_session_id: str) -> bool:
    """Check if a Harbor docker-compose environment has running containers."""
    project = get_compose_project(harbor_session_id)
    r = subprocess.run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "status=running",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def does_environment_exist(harbor_session_id: str) -> bool:
    """Check if a Harbor docker-compose environment has any containers (running or stopped)."""
    project = get_compose_project(harbor_session_id)
    r = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


# ---------------------------------------------------------------------------
# Environment lifecycle (container mode)
# ---------------------------------------------------------------------------


def _get_dockerfile_workdir(environment_dir: Path) -> str:
    """Parse WORKDIR from the task's Dockerfile, defaulting to /app."""
    dockerfile = environment_dir / "Dockerfile"
    if dockerfile.exists():
        for line in reversed(dockerfile.read_text().splitlines()):
            m = re.match(r"^\s*WORKDIR\s+(\S+)", line, re.IGNORECASE)
            if m:
                return m.group(1)
    return "/app"


def get_container_workdir(task_dir: Path) -> str:
    """Return the container WORKDIR for a task."""
    return _get_dockerfile_workdir(task_dir / "environment")


def extract_image_workdir(task_dir: Path, workspace: Path) -> None:
    """Build the task image and copy its WORKDIR contents into the workspace.

    This ensures the workspace starts with the same files the container's
    WORKDIR would have (from Dockerfile COPY/ADD/RUN), before pier overlays
    its own assets (instruction.md, skills/, etc.) and bind-mounts the
    workspace back into the container.
    """
    env_dir = task_dir / "environment"
    workdir = _get_dockerfile_workdir(env_dir)
    image_name = f"hb__{task_dir.name}"

    # Build (uses Docker layer cache if already built by Harbor)
    subprocess.run(
        ["docker", "build", "-t", image_name, str(env_dir.resolve())],
        check=True,
        capture_output=True,
    )

    # Create a temporary container (not started) and copy WORKDIR contents out
    r = subprocess.run(
        ["docker", "create", image_name],
        capture_output=True,
        text=True,
        check=True,
    )
    cid = r.stdout.strip()
    try:
        subprocess.run(
            ["docker", "cp", f"{cid}:{workdir}/.", str(workspace)],
            check=False,  # OK if workdir is empty in the image
            capture_output=True,
        )
    finally:
        subprocess.run(["docker", "rm", cid], capture_output=True, check=False)


def copy_task_files(task_dir: Path, dest_dir: Path) -> None:
    """Copy task instruction into a target directory.

    All modes (host, bind-mount, --no-mount) use this to populate
    workspace/.task/. Skills are handled by Harbor via skills_dir
    in task.toml.
    """
    import shutil

    dest_dir.mkdir(exist_ok=True)

    instruction = task_dir / "instruction.md"
    dest_instruction = dest_dir / "instruction.md"
    if instruction.exists() and instruction.stat().st_size > 0:
        if dest_instruction.is_symlink():
            dest_instruction.unlink()
        shutil.copy2(instruction, dest_instruction)


def _write_mounts_compose(
    trial_dir: Path,
    workspace_dir: Path,
    container_workdir: str,
    *,
    include_bind_mount: bool = True,
    task_dir: Path | None = None,
    ports: list[int] | None = None,
    environment_env: list[str] | None = None,
) -> Path:
    """Write a docker-compose override for workspace mounts.

    Always adds a tmpfs over .pier/ so pier's session data is hidden from
    the container. The workspace bind mount is handled by Harbor via mounts_json;
    this override adds the tmpfs and port mappings.

    When task_dir is provided, copies task instruction into workspace/.task/
    so agents can discover the task without leaking tests or task.toml.
    Skills are handled by Harbor via skills_dir in task.toml.

    When ports is provided, exposes those container ports to the host.
    When environment_env is provided, adds static environment entries to the service.
    """
    service: dict = {"tmpfs": [f"{container_workdir}/.pier"]}
    volumes: list[str] = []
    if include_bind_mount:
        volumes.append(f"{workspace_dir.resolve()}:{container_workdir}:rw")
    if task_dir:
        # Copy task files into workspace/.task/ — the workspace is bind-mounted
        # (via mounts_json or compose), so files placed there are visible inside
        # the container. Direct volume mounts inside a bind-mounted directory
        # fail on macOS with VirtioFS.
        dot_task = workspace_dir / ".task"
        copy_task_files(task_dir, dot_task)
    if volumes:
        service["volumes"] = volumes
    if ports:
        service["ports"] = [f"{p}:{p}" for p in ports]
    if environment_env:
        env_dict: dict[str, str] = {}
        for entry in environment_env:
            k, _, v = entry.partition("=")
            env_dict[k] = v
        service["environment"] = env_dict
    compose = {"services": {"main": service}}
    path = trial_dir / "docker-compose-pier.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(compose, indent=2))
    return path


def _make_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    workspace_dir: Path | None = None,
    ports: list[int] | None = None,
    extra_mounts: list[str] | None = None,
    environment_env: list[str] | None = None,
):
    """Reconstruct a Harbor Docker environment from pier session data.

    Called for every container operation (exec, verify, stop) since the
    environment object is stateless — the running Docker container is the
    actual state.

    Fragility: EnvironmentFactory is not in Harbor's public __all__.
    If Harbor reorganizes its package, update the import below.
    """
    from harbor import Task, TrialPaths
    from harbor.environments.factory import EnvironmentFactory  # internal import

    task = Task(task_dir)
    trial_paths = TrialPaths(trial_dir=trial_dir)
    trial_paths.mkdir()

    container_workdir = _get_dockerfile_workdir(task.paths.environment_dir)
    mount_spec = (
        f"{workspace_dir.resolve()}:{container_workdir}:rw"
        if workspace_dir is not None
        else None
    )

    mounts = []
    if mount_spec:
        mounts.append(mount_spec)
    if extra_mounts:
        mounts.extend(extra_mounts)
    kwargs: dict = {}
    if mounts:
        kwargs["mounts_json"] = mounts

    environment = EnvironmentFactory.create_environment(
        type="docker",
        environment_dir=task.paths.environment_dir,
        environment_name=task.name,
        session_id=harbor_session_id,
        trial_paths=trial_paths,
        task_env_config=task.config.environment,
        **kwargs,
    )

    # Always write a compose override for the tmpfs mount that hides .pier/
    # from the container. On older Harbor (no mounts_json), this file also
    # carries the workspace bind mount.
    if workspace_dir is not None:
        mounts_path = _write_mounts_compose(
            trial_dir,
            workspace_dir,
            container_workdir,
            include_bind_mount=False,
            task_dir=task_dir,
            ports=ports,
            environment_env=environment_env,
        )
        _patch_compose_paths(environment, mounts_path)
    elif ports or environment_env:
        # No workspace mount, but still need ports and/or compose env.
        mounts_path = _write_mounts_compose(
            trial_dir,
            workspace_dir or Path("/unused"),
            container_workdir,
            include_bind_mount=False,
            ports=ports,
            environment_env=environment_env,
        )
        _patch_compose_paths(environment, mounts_path)
    else:
        mounts_path = trial_dir / "docker-compose-pier.json"
        if mounts_path.exists():
            _patch_compose_paths(environment, mounts_path)

    return environment, task, trial_paths


def _patch_compose_paths(environment: object, mounts_path: Path) -> None:
    """Monkey-patch _docker_compose_paths to include pier's compose override.

    TODO: Replace when Harbor supports a public API for compose overrides.

    Pier writes a separate compose override (docker-compose-pier.json) for
    the tmpfs that hides .pier/ from the container. This must be a different
    file from Harbor's docker-compose-mounts.json to avoid being overwritten.
    """
    orig_property = type(environment)._docker_compose_paths  # type: ignore[attr-defined]

    @property  # type: ignore[misc]
    def _patched(self: object) -> list[Path]:
        paths: list[Path] = orig_property.fget(self)  # type: ignore[union-attr]
        if mounts_path not in paths:
            paths.append(mounts_path)
        return paths

    # Patch on the instance's class would affect all instances, so use a
    # one-off subclass instead.
    patched_cls = type(
        type(environment).__name__,
        (type(environment),),
        {"_docker_compose_paths": _patched},
    )
    environment.__class__ = patched_cls


async def _async_start_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    workspace_dir: Path | None = None,
    ports: list[int] | None = None,
    extra_mounts: list[str] | None = None,
    environment_env: list[str] | None = None,
) -> None:
    environment, task, _ = _make_environment(
        task_dir,
        harbor_session_id,
        trial_dir,
        workspace_dir=workspace_dir,
        ports=ports,
        extra_mounts=extra_mounts,
        environment_env=environment_env,
    )
    await environment.start(force_build=False)


async def _async_verify_environment(
    task_dir: Path, harbor_session_id: str, trial_dir: Path
) -> dict:
    from harbor import Verifier

    environment, task, trial_paths = _make_environment(
        task_dir, harbor_session_id, trial_dir
    )
    verifier = Verifier(task=task, trial_paths=trial_paths, environment=environment)
    await verifier.verify()

    details_file = trial_paths.verifier_dir / "details.json"
    if details_file.exists():
        return json.loads(details_file.read_text())

    if trial_paths.reward_json_path.exists():
        return json.loads(trial_paths.reward_json_path.read_text())

    if trial_paths.reward_text_path.exists():
        return {"reward": float(trial_paths.reward_text_path.read_text().strip())}

    return {"reward": None}


async def _async_stop_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    *,
    delete: bool = False,
) -> None:
    environment, _, _ = _make_environment(task_dir, harbor_session_id, trial_dir)
    await environment.stop(delete=delete)


def start_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    workspace_dir: Path | None = None,
    ports: list[int] | None = None,
    extra_mounts: list[str] | None = None,
    environment_env: list[str] | None = None,
) -> None:
    """Build (if needed), start a Harbor Docker environment.

    If workspace_dir is provided, it is bind-mounted into the container.
    If ports is provided, those container ports are exposed to the host.
    If extra_mounts is provided, they are added as volume mounts.
    If environment_env is provided, those KEY=VALUE pairs are added to the compose service environment.
    """
    with _placeholder_task_env_vars(task_dir):
        asyncio.run(
            _async_start_environment(
                task_dir,
                harbor_session_id,
                trial_dir,
                workspace_dir=workspace_dir,
                ports=ports,
                extra_mounts=extra_mounts,
                environment_env=environment_env,
            )
        )


def create_synthetic_task_dir(image: str, temp_root: Path) -> Path:
    """Create a minimal task directory for task-free container mode.

    Generates a temporary task with a Dockerfile that just pulls the
    given image and a minimal task.toml.  This lets task-free mode
    reuse Harbor's standard environment machinery instead of
    maintaining a separate code path.

    The temp_root should be a persistent directory (e.g., inside
    .pier/) so the task survives container restarts.
    """
    task_dir = temp_root / "pier-task-free"
    task_dir.mkdir(parents=True, exist_ok=True)

    env_dir = task_dir / "environment"
    env_dir.mkdir(exist_ok=True)

    dockerfile = env_dir / "Dockerfile"
    expected = f"FROM {image}\nWORKDIR /app\n"
    if not dockerfile.exists() or dockerfile.read_text() != expected:
        dockerfile.write_text(expected)

    toml = task_dir / "task.toml"
    if not toml.exists():
        toml.write_text(
            '[metadata]\nauthor_name = "pier"\n[environment]\n[verifier]\n[agent]\n'
        )

    # Harbor's Task() reads instruction.md eagerly
    instruction = task_dir / "instruction.md"
    if not instruction.exists():
        instruction.write_text("")

    return task_dir


def _claude_config_dir() -> str:
    """Return the CLAUDE_CONFIG_DIR path used inside Harbor containers."""
    from harbor.agents.installed.claude_code import EnvironmentPaths

    return (EnvironmentPaths.agent_dir / "sessions").as_posix()


def _codex_home_dir() -> str:
    """Return the CODEX_HOME path used inside Harbor containers."""
    from harbor.agents.installed.codex import EnvironmentPaths

    return EnvironmentPaths.agent_dir.as_posix()


def _codex_path_prefix() -> str:
    """Return the Node bin dir Harbor's NVM-based installers place on disk."""
    return (
        '$(find "$HOME/.nvm/versions/node" -mindepth 1 -maxdepth 1 -type d '
        "2>/dev/null | sort | tail -n1)/bin"
    )


def _local_bin_path_prefix() -> str:
    """Return the default user-local bin dir used by several agent installers."""
    return "$HOME/.local/bin"


async def _run_interactive_setup(
    agent: object, agent_name: str, environment: object
) -> None:
    """Run agent-specific interactive setup after install.

    Harbor's run() registers skills, MCP servers, and marks onboarding
    complete, but pier doesn't call run() — the user drives the agent
    interactively.  This replicates the setup portions of run().

    TODO: Replace with a Harbor public API for interactive agent setup.
    Currently calls private methods (_build_register_skills_command, etc.).
    """
    if agent_name != "claude-code":
        return

    config_dir = _claude_config_dir()
    env = {"CLAUDE_CONFIG_DIR": config_dir}

    setup_parts = [
        f"mkdir -p {config_dir}/debug {config_dir}/projects/-app "
        f"{config_dir}/shell-snapshots {config_dir}/statsig "
        f"{config_dir}/todos {config_dir}/skills",
        f"if [ -d ~/.claude/skills ]; then "
        f"cp -r ~/.claude/skills/. {config_dir}/skills/ 2>/dev/null || true; fi",
    ]

    skills_cmd = agent._build_register_skills_command()  # type: ignore[attr-defined]
    if skills_cmd:
        setup_parts.append(skills_cmd)

    mcp_cmd = agent._build_register_mcp_servers_command()  # type: ignore[attr-defined]
    if mcp_cmd:
        setup_parts.append(mcp_cmd)

    # Mark onboarding complete so `claude` doesn't prompt
    setup_parts.append(
        f"echo '{{\"hasCompletedOnboarding\": true}}' > {config_dir}/.claude.json"
    )

    await environment.exec(  # type: ignore[attr-defined]
        command=" && ".join(setup_parts), env=env
    )


async def _async_setup_agent(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    agent_name: str,
) -> None:
    from harbor.agents.factory import AgentFactory
    from harbor.models.agent.name import AgentName

    environment, task, trial_paths = _make_environment(
        task_dir, harbor_session_id, trial_dir
    )

    extra_kwargs: dict = {}
    if task.config.environment.skills_dir:
        extra_kwargs["skills_dir"] = task.config.environment.skills_dir
    if task.config.environment.mcp_servers:
        extra_kwargs["mcp_servers"] = task.config.environment.mcp_servers

    agent = AgentFactory.create_agent_from_name(
        AgentName(agent_name), logs_dir=trial_paths.agent_dir, **extra_kwargs
    )
    await agent.setup(environment)
    await _run_interactive_setup(agent, agent_name, environment)


def setup_agent(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    agent_name: str,
) -> None:
    """Install a Harbor agent in a running environment.

    Calls the agent's setup() method which uploads and runs the install
    script (e.g. install-claude-code.sh).  Does NOT call run() — the user
    drives the agent interactively via ``pier exec``.
    """
    with _placeholder_task_env_vars(task_dir):
        asyncio.run(
            _async_setup_agent(task_dir, harbor_session_id, trial_dir, agent_name)
        )


def verify_environment(task_dir: Path, harbor_session_id: str, trial_dir: Path) -> dict:
    """Run Harbor's verifier on a running environment. Returns reward dict."""
    with _placeholder_task_env_vars(task_dir):
        return asyncio.run(
            _async_verify_environment(task_dir, harbor_session_id, trial_dir)
        )


def stop_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    *,
    delete: bool = False,
) -> None:
    """Stop the Harbor Docker environment.

    delete=False (default): removes the container but keeps images (fast restart).
    delete=True: removes the container, images, and volumes (full cleanup).
    """
    # Harbor reconstructs the environment on stop, resolving task env vars.
    # Set placeholders for missing vars so stop doesn't fail.
    # TODO: Harbor should not require env vars for stop.
    with _placeholder_task_env_vars(task_dir):
        asyncio.run(
            _async_stop_environment(
                task_dir, harbor_session_id, trial_dir, delete=delete
            )
        )


@contextlib.contextmanager
def _placeholder_task_env_vars(task_dir: Path):
    """Temporarily set empty placeholders for task env vars not in the host env."""
    toml_path = task_dir / "task.toml"
    added: list[str] = []
    if toml_path.exists():
        content = toml_path.read_text()
        for match in re.finditer(r"\$\{(\w+)(?::-.+?)?\}", content):
            var = match.group(1)
            if var not in os.environ:
                os.environ[var] = ""
                added.append(var)
    try:
        yield
    finally:
        for var in added:
            os.environ.pop(var, None)


# ---------------------------------------------------------------------------
# Trial result (Harbor-compatible)
# ---------------------------------------------------------------------------


def build_trial_result_json(
    task_dir: Path,
    task_ref: str,
    session_name: str,
    reward: dict,
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    agent_name: str | None = None,
    agent_context: dict | None = None,
) -> str:
    """Build a Harbor-compatible TrialResult JSON string.

    Constructs the same TrialResult Pydantic model that `harbor run` writes,
    so `pier view` and `pier summarize` work on pier output.
    """
    from harbor import Task
    from harbor.models.agent.context import AgentContext
    from harbor.models.task.id import LocalTaskId
    from harbor.models.trial.config import AgentConfig, TaskConfig, TrialConfig
    from harbor.models.trial.result import AgentInfo, TrialResult, VerifierResult

    task = Task(task_dir)

    task_config = TaskConfig(path=task_dir)
    agent_cfg = AgentConfig(name=agent_name) if agent_name else AgentConfig()
    config = TrialConfig(task=task_config, trial_name=session_name, agent=agent_cfg)

    agent_info = AgentInfo(name=agent_name or "human", version="unknown")

    verifier_result = VerifierResult(rewards=reward) if reward else None

    agent_result = None
    if agent_context:
        try:
            agent_result = AgentContext(**agent_context)
        except Exception:
            pass

    result = TrialResult(
        task_name=task.name,
        trial_name=session_name,
        trial_uri=str(task_dir),
        task_id=LocalTaskId(path=task_dir),
        task_checksum=task.checksum,
        config=config,
        agent_info=agent_info,
        agent_result=agent_result,
        verifier_result=verifier_result,
        started_at=start_time,
        finished_at=end_time,
    )
    return result.model_dump_json(indent=2)


def write_trial_config_json(
    trial_dir: Path,
    task_dir: Path,
    session_name: str,
    agent_name: str | None,
) -> None:
    """Write config.json as a Harbor TrialConfig.

    Skips gracefully if the task directory is incomplete (e.g. missing
    instruction.md) — config.json is optional metadata.
    """
    try:
        from harbor.models.trial.config import AgentConfig, TaskConfig, TrialConfig

        kwargs: dict = dict(task=TaskConfig(path=task_dir), trial_name=session_name)
        if agent_name:
            kwargs["agent"] = AgentConfig(name=agent_name)
        trial_config = TrialConfig(**kwargs)
        (trial_dir / "config.json").write_text(trial_config.model_dump_json(indent=2))
    except Exception as e:
        logger.debug("Skipping config.json: %s", e)


# ---------------------------------------------------------------------------
# Task download
# ---------------------------------------------------------------------------


def download_task(
    git_url: str,
    task_path: str,
    git_commit_id: str | None = None,
) -> Path:
    """Download a task directory via Harbor's TaskClient.

    Uses TaskClient (not in Harbor's public __all__) — internal import.
    """
    from harbor import GitTaskId
    from harbor.tasks.client import TaskClient  # internal import

    task_id = GitTaskId(
        git_url=git_url,
        path=Path(task_path),
        git_commit_id=git_commit_id,
    )
    client = TaskClient()
    paths = client.download_tasks([task_id])
    return paths[0]


# ---------------------------------------------------------------------------
# Agent log extraction
# ---------------------------------------------------------------------------
#
# Harbor agents each expect their session logs in a specific layout under
# logs_dir.  The _AGENT_BRIDGE dict maps agent names to functions that
# arrange the user's local session directory into that layout.
#
# Agents without a bridge entry fall back to a generic symlink that works
# when the session dir already matches what Harbor expects.


def _bridge_claude_code(session_dir: Path, logs_dir: Path) -> None:
    """Bridge local Claude Code sessions into the layout Harbor's reader expects.

    Harbor's ClaudeCode._get_session_dir() looks for
    logs_dir/sessions/projects/<dir>/*.jsonl.
    """
    projects_dir = logs_dir / "sessions" / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    link = projects_dir / session_dir.name
    if not link.exists():
        link.symlink_to(session_dir.resolve())


_AGENT_BRIDGE: dict[str, Callable[[Path, Path], None]] = {
    "claude-code": _bridge_claude_code,
}

# Heuristics to detect which agent produced a session directory.
# Each entry maps an agent name to a predicate on the session_dir.
_AGENT_DETECT: dict[str, Callable[[Path], bool]] = {
    "claude-code": lambda d: any(d.glob("*.jsonl")),
}


def detect_agent_from_session_dir(session_dir: Path) -> str | None:
    """Guess the agent name from the contents of a session directory.

    Returns the agent name (e.g. ``"claude-code"``) or None if no known
    pattern matches.
    """
    for agent_name, check in _AGENT_DETECT.items():
        try:
            if check(session_dir):
                return agent_name
        except OSError:
            continue
    return None


def find_container_agent_session_dir(
    agent_name: str, harbor_agent_dir: Path
) -> Path | None:
    """Find an agent's session directory within the container's mounted agent dir.

    When CLAUDE_CONFIG_DIR is set during ``pier exec``, Claude writes session
    JSONL to ``harbor_agent_dir/sessions/projects/<hash>/``.  This function
    finds that project directory so it can be passed to ``extract_agent_logs``.

    Returns None if the agent isn't supported or no unambiguous session is found.
    """
    detect = _AGENT_DETECT.get(agent_name)
    direct_log_agents = {
        "cursor-cli": "cursor-cli.txt",
        "gemini-cli": "gemini-cli.trajectory.json",
        "kimi-cli": "kimi-cli.txt",
        "opencode": "opencode.txt",
    }
    if agent_name == "claude-code" and detect:
        projects_dir = harbor_agent_dir / "sessions" / "projects"
        if projects_dir.is_dir():
            project_dirs = [
                d for d in projects_dir.iterdir() if d.is_dir() and detect(d)
            ]
            if len(project_dirs) == 1:
                return project_dirs[0]
            if len(project_dirs) > 1:
                logger.warning(
                    "Multiple Claude session dirs found in %s; "
                    "pass --session-dir explicitly",
                    projects_dir,
                )
    elif agent_name == "codex":
        sessions_dir = harbor_agent_dir / "sessions"
        if sessions_dir.is_dir():
            session_dirs = [d for d in sessions_dir.rglob("*") if d.is_dir()]
            if session_dirs:
                max_depth = max(len(d.parts) for d in session_dirs)
                session_dirs = [d for d in session_dirs if len(d.parts) == max_depth]
                if len(session_dirs) == 1:
                    return session_dirs[0]
                if len(session_dirs) > 1:
                    logger.warning(
                        "Multiple Codex session dirs found in %s; "
                        "pass --session-dir explicitly",
                        sessions_dir,
                    )
    elif agent_name == "qwen-coder":
        sessions_dir = harbor_agent_dir / "qwen-sessions"
        if sessions_dir.is_dir() and any(sessions_dir.rglob("*.jsonl")):
            return harbor_agent_dir
    elif agent_name in direct_log_agents:
        if (harbor_agent_dir / direct_log_agents[agent_name]).exists():
            return harbor_agent_dir
    return None


def get_agent_exec_env(agent_name: str) -> tuple[dict[str, str], str]:
    """Return env vars and PATH prefix needed to run an agent interactively.

    Returns ``(env_dict, path_prefix)`` where *path_prefix* is a string like
    ``"$HOME/.local/bin"`` or empty.

    TODO: Harbor should expose an API for this so pier doesn't hardcode
    agent-specific env vars.
    """
    env: dict[str, str] = {}
    path_prefix = ""

    if agent_name == "claude-code":
        env["CLAUDE_CONFIG_DIR"] = _claude_config_dir()
        env["IS_SANDBOX"] = "1"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        path_prefix = _local_bin_path_prefix()
    elif agent_name == "codex":
        env["CODEX_HOME"] = _codex_home_dir()
        path_prefix = _codex_path_prefix()
    elif agent_name in {"cursor-cli", "kimi-cli", "goose", "hermes"}:
        path_prefix = _local_bin_path_prefix()
    elif agent_name in {"gemini-cli", "qwen-coder", "opencode"}:
        path_prefix = _codex_path_prefix()
    return env, path_prefix


def resolve_task_env(task_dir: Path) -> dict[str, str]:
    """Resolve [environment.env] from task.toml using Harbor's resolver.

    Returns resolved {VAR: value} dict. Returns empty dict if task.toml
    is missing or has no env vars.
    """
    if not (task_dir / "task.toml").exists():
        return {}
    try:
        import tomllib

        from harbor.utils.env import resolve_env_vars

        config = tomllib.loads((task_dir / "task.toml").read_text())
        env_config = (config.get("environment") or {}).get("env")
        if not env_config:
            return {}
        with _placeholder_task_env_vars(task_dir):
            return resolve_env_vars(env_config)
    except Exception:
        return {}


def exec_in_container(
    harbor_session_id: str,
    task_dir: Path,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    path_prefix: str = "",
    detach: bool = False,
) -> int:
    """Run a command in a running container via docker exec.

    Returns the process exit code. Handles TTY allocation, env vars,
    PATH prefix, and detached mode.
    """
    import shlex

    container = get_container_name(harbor_session_id)
    workdir = get_container_workdir(task_dir)

    env_flags: list[str] = []
    for var, val in (env or {}).items():
        env_flags.extend(["-e", f"{var}={val}"])

    if path_prefix:
        shell_cmd = f"export PATH={path_prefix}:$PATH && exec " + " ".join(
            shlex.quote(c) for c in command
        )
        run_command = [container, "sh", "-c", shell_cmd]
    else:
        run_command = [container, *command]

    if detach:
        result = subprocess.run(
            ["docker", "exec", "-d", "-w", workdir, *env_flags, *run_command],
        )
    else:
        tty_flags = ["-it"] if sys.stdin.isatty() else []
        result = subprocess.run(
            ["docker", "exec", *tty_flags, "-w", workdir, *env_flags, *run_command],
        )
    return result.returncode


def is_valid_agent(agent_name: str) -> bool:
    """Check if agent_name is a valid Harbor agent name."""
    from harbor.models.agent.name import AgentName

    try:
        AgentName(agent_name)
    except ValueError:
        return False
    return True


def extract_agent_logs(
    agent_name: str,
    session_dir: Path,
    logs_dir: Path,
) -> dict | None:
    """Extract trajectory and usage from local agent logs using Harbor's reader.

    Uses Harbor's agent-specific populate_context_post_run() to parse logs
    and produce trajectory.json.  Works for any Harbor-supported agent —
    agents with a known local log layout (e.g. claude-code) get a bridge
    that arranges files; others fall back to symlinking session_dir contents
    directly into logs_dir.

    Args:
        agent_name: Harbor agent name (e.g. "claude-code").
        session_dir: Path to directory containing agent session files.
        logs_dir: Trial's agent dir — bridge structure is created here.

    Returns:
        Dict with cost_usd, n_input_tokens, etc. — or None if extraction
        failed.
    """
    from harbor.agents.factory import AgentFactory
    from harbor.models.agent.context import AgentContext
    from harbor.models.agent.name import AgentName

    bridge = _AGENT_BRIDGE.get(agent_name)
    if bridge is not None:
        bridge(session_dir, logs_dir)
    else:
        # Generic fallback: symlink session dir contents into logs_dir
        # so Harbor's reader can find them.
        logs_dir.mkdir(parents=True, exist_ok=True)
        for item in session_dir.iterdir():
            link = logs_dir / item.name
            if not link.exists():
                link.symlink_to(item.resolve())

    agent = AgentFactory.create_agent_from_name(
        AgentName(agent_name), logs_dir=logs_dir
    )
    context = AgentContext()

    try:
        agent.populate_context_post_run(context)
    except Exception:
        logger.warning(
            "Failed to extract logs for %r from %s",
            agent_name,
            session_dir,
            exc_info=True,
        )
        return None

    result = context.model_dump(exclude_none=True)
    return result if result else None


# ---------------------------------------------------------------------------
# Harbor CLI wrappers
# ---------------------------------------------------------------------------


def run_view_command(folder: Path, port: str, host: str) -> None:
    """Launch the Harbor trial viewer web dashboard."""
    from harbor.cli.view import view_command

    view_command(folder=folder, port=port, host=host)


def run_summarize(
    trials_dir: Path,
    n_concurrent: int,
    model: str,
    only_failed: bool,
    overwrite: bool,
) -> Path | None:
    """Summarize trial results using Harbor's Summarizer."""
    from harbor.cli.summarize.summarizer import Summarizer

    summarizer = Summarizer(
        trials_dir,
        n_concurrent=n_concurrent,
        model=model,
        only_failed=only_failed,
        overwrite=overwrite,
    )
    return summarizer.summarize()
