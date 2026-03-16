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
import functools
import json
import logging
import re
import subprocess
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


def _write_mounts_compose(
    trial_dir: Path,
    workspace_dir: Path,
    container_workdir: str,
    *,
    include_bind_mount: bool = True,
    task_dir: Path | None = None,
) -> Path:
    """Write a docker-compose override for workspace mounts.

    Always adds a tmpfs over .pier/ so pier's session data is hidden from
    the container. Optionally includes the workspace bind mount (needed on
    older Harbor without mounts_json support).

    When task_dir is provided, mounts instruction.md and skills/ read-only
    into {workdir}/.task/ so agents can discover the task without leaking
    tests or task.toml.
    """
    service: dict = {"tmpfs": [f"{container_workdir}/.pier"]}
    volumes: list[str] = []
    if include_bind_mount:
        volumes.append(f"{workspace_dir.resolve()}:{container_workdir}:rw")
    if task_dir:
        dot_task = f"{container_workdir}/.task"
        instruction = task_dir / "instruction.md"
        if instruction.exists():
            # Ensure .task/ exists in the workspace so Docker can mount into it
            (workspace_dir / ".task").mkdir(exist_ok=True)
            volumes.append(f"{instruction.resolve()}:{dot_task}/instruction.md:ro")
        skills_dir = task_dir / "environment" / "skills"
        if skills_dir.is_dir():
            volumes.append(f"{skills_dir.resolve()}:{dot_task}/skills:ro")
    if volumes:
        service["volumes"] = volumes
    compose = {"services": {"main": service}}
    path = trial_dir / "docker-compose-mounts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(compose, indent=2))
    return path


@functools.cache
def _harbor_supports_mounts_json() -> bool:
    """Check if the installed Harbor supports mounts_json in DockerEnvironment."""
    import inspect

    from harbor.environments.docker.docker import DockerEnvironment

    return "mounts_json" in inspect.signature(DockerEnvironment.__init__).parameters


def _make_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    workspace_dir: Path | None = None,
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

    kwargs: dict = {}
    if mount_spec and _harbor_supports_mounts_json():
        kwargs["mounts_json"] = [mount_spec]

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
            include_bind_mount=not _harbor_supports_mounts_json(),
            task_dir=task_dir,
        )
        _patch_compose_paths(environment, mounts_path)
    else:
        mounts_path = trial_dir / "docker-compose-mounts.json"
        if mounts_path.exists():
            _patch_compose_paths(environment, mounts_path)

    return environment, task, trial_paths


def _patch_compose_paths(environment: object, mounts_path: Path) -> None:
    """Monkey-patch _docker_compose_paths to include the mounts override.

    Older Harbor versions don't support mounts_json, so we append our
    compose override file to the paths property.  This is intentionally
    fragile — the version check in _make_environment ensures we only
    reach here on older Harbor, and the TODO above tracks removal.
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
) -> None:
    environment, task, _ = _make_environment(
        task_dir,
        harbor_session_id,
        trial_dir,
        workspace_dir=workspace_dir,
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
) -> None:
    """Build (if needed), start a Harbor Docker environment.

    If workspace_dir is provided, it is bind-mounted into /workspace.
    """
    asyncio.run(
        _async_start_environment(
            task_dir,
            harbor_session_id,
            trial_dir,
            workspace_dir=workspace_dir,
        )
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

    agent = AgentFactory.create_agent_from_name(
        AgentName(agent_name), logs_dir=trial_paths.agent_dir
    )
    await agent.setup(environment)

    # Prepare the container for interactive use via `pier exec`.
    # Harbor normally runs setup commands as the first step of
    # create_run_agent_commands before the actual agent invocation.
    # Claude Code's first ExecInput is a dedicated setup command (mkdir,
    # skills, MCP config); we run it and also mark onboarding complete
    # so interactive claude skips the login prompt.
    if agent_name == "claude-code":
        exec_inputs = agent.create_run_agent_commands(instruction="setup")
        if exec_inputs:
            setup = exec_inputs[0]
            config_dir = (setup.env or {}).get("CLAUDE_CONFIG_DIR", "")
            # Combine Harbor's setup command with onboarding flag in a
            # single docker exec to avoid an extra subprocess round-trip.
            # Harbor's --print mode bypasses onboarding, but pier exec
            # is interactive and needs hasCompletedOnboarding set.
            onboarding_cmd = (
                f" && echo '{{\"hasCompletedOnboarding\": true}}'"
                f" > {config_dir}/.claude.json"
                if config_dir
                else ""
            )
            await environment.exec(
                command=setup.command + onboarding_cmd, env=setup.env
            )


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
    asyncio.run(_async_setup_agent(task_dir, harbor_session_id, trial_dir, agent_name))


def verify_environment(task_dir: Path, harbor_session_id: str, trial_dir: Path) -> dict:
    """Run Harbor's verifier on a running environment. Returns reward dict."""
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
    asyncio.run(
        _async_stop_environment(task_dir, harbor_session_id, trial_dir, delete=delete)
    )


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
    return None


def _extract_path_prefixes(commands: list[str]) -> str:
    """Extract PATH prefixes from shell command strings.

    Parses ``export PATH="...:$PATH"`` patterns and returns the
    prefix segments joined by ``:``, e.g. ``"$HOME/.local/bin"``
    or ``"/root/.local/bin:/other/bin"``.  Returns empty string
    if no PATH modifications are found.
    """
    path_prefixes: list[str] = []
    for cmd in commands:
        for m in re.finditer(
            r"""PATH=["']?([^"'\s;]+):\$PATH["']?""",
            cmd,
        ):
            for segment in m.group(1).split(":"):
                if segment and segment not in path_prefixes:
                    path_prefixes.append(segment)
    return ":".join(path_prefixes)


# Env vars from Harbor's run commands that are safe to forward to
# interactive pier exec sessions.  We use an allowlist (not denylist)
# because most agent env vars are secrets or model names populated from
# pier's dummy values.  Behavioral flags and container-correct paths
# (derived from EnvironmentPaths class constants, not the dummy logs_dir)
# belong here.
_AGENT_ENV_ALLOW = frozenset(
    {
        # Claude Code
        "CLAUDE_CONFIG_DIR",  # /logs/agent/sessions — session logs go to mounted dir
        "IS_SANDBOX",  # skip interactive onboarding
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",  # no telemetry
        "FORCE_AUTO_BACKGROUND_TASKS",
        "ENABLE_BACKGROUND_TASKS",
    }
)


def get_agent_exec_env(agent_name: str) -> tuple[dict[str, str], str]:
    """Return env vars and PATH prefix Harbor sets when running an agent.

    Asks Harbor for the agent's ``create_run_agent_commands()`` output and
    extracts:
    - Behavioral env vars (e.g. ``IS_SANDBOX=1``) that affect how the agent
      runs, filtering out secrets and paths specific to ``--print`` mode.
    - PATH prefixes from ``export PATH="...:$PATH"`` in command strings.

    Returns ``(env_dict, path_prefix)`` where *path_prefix* is a string like
    ``"$HOME/.local/bin"`` or empty.  Harbor is the single source of truth.
    """
    from harbor.agents.factory import AgentFactory
    from harbor.models.agent.name import AgentName

    # Use a dummy model_name so agents that validate it don't raise early.
    agent = AgentFactory.create_agent_from_name(
        AgentName(agent_name),
        logs_dir=Path("/tmp"),
        model_name="anthropic/dummy",
    )
    try:
        exec_inputs = agent.create_run_agent_commands("__pier_dummy__")
    except Exception:
        logger.debug("Could not get run commands for agent %s", agent_name)
        return {}, ""

    # Merge env from all ExecInput steps, keeping only allowlisted vars.
    merged: dict[str, str] = {}
    for ei in exec_inputs:
        if ei.env:
            for k, v in ei.env.items():
                if k in _AGENT_ENV_ALLOW and v:
                    merged[k] = v

    path_prefix = _extract_path_prefixes([ei.command for ei in exec_inputs])

    return merged, path_prefix


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
