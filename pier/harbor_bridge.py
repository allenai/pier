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
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

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
) -> Path:
    """Write a docker-compose override for workspace mounts.

    Always adds a tmpfs over .pier/ so pier's session data is hidden from
    the container. The workspace bind mount is handled by Harbor via mounts_json;
    this override adds the tmpfs and port mappings.

    When task_dir is provided, copies task instruction into workspace/.task/
    so agents can discover the task without leaking tests or task.toml.
    Skills are handled by Harbor via skills_dir in task.toml.

    When ports is provided, exposes those container ports to the host.
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
    extra_compose: list[str] | None = None,
):
    """Reconstruct a Harbor Docker environment from pier session data.

    Called for every container operation (exec, verify, stop) since the
    environment object is stateless — the running Docker container is the
    actual state. So *extra_compose* must be the same list on every call: a
    stop without an overlay's services leaves their containers running.

    Fragility: EnvironmentFactory is not in Harbor's public __all__.
    If Harbor reorganizes its package, update the import below.
    """
    from harbor import Task, TrialPaths
    from harbor.environments.factory import EnvironmentFactory  # internal import

    task = Task(task_dir)
    trial_paths = TrialPaths(trial_dir=trial_dir)
    trial_paths.mkdir()

    container_workdir = _get_dockerfile_workdir(task.paths.environment_dir)

    # Harbor 0.6+ expects ``mounts`` (list[ServiceVolumeConfig dict]) on the
    # env constructor — caller's responsibility — and silently drops the
    # legacy string-based ``mounts_json`` kwarg pier used to pass. We build
    # ServiceVolumeConfig dicts directly.
    from harbor.models.task.config import TaskOS
    from harbor.models.trial.paths import EnvironmentPaths

    env_os = getattr(task.config.environment, "os", TaskOS.LINUX)
    env_paths = EnvironmentPaths.for_os(env_os)

    mounts: list[dict] = []
    if workspace_dir is not None:
        mounts.append(
            {
                "type": "bind",
                "source": str(workspace_dir.resolve()),
                "target": container_workdir,
            }
        )
    # Always bind Harbor's trial log directories. Without these binds,
    # test.sh writes to /logs/verifier inside the container — invisible
    # to the host-side Verifier.verify() and raises RewardFileNotFoundError.
    for host_dir, container_dir in (
        (trial_paths.verifier_dir, env_paths.verifier_dir),
        (trial_paths.agent_dir, env_paths.agent_dir),
        (trial_paths.artifacts_dir, env_paths.artifacts_dir),
    ):
        mounts.append(
            {
                "type": "bind",
                "source": str(Path(host_dir).resolve()),
                "target": str(container_dir),
            }
        )
    # ``extra_mounts`` arrives as docker-style strings
    # (``host:container[:ro|rw]``) from the user's ``--mounts-json``.
    for raw in extra_mounts or []:
        parts = raw.split(":")
        if len(parts) < 2:
            continue
        host, container, *rest = parts
        spec: dict = {
            "type": "bind",
            "source": str(Path(host).expanduser().resolve()),
            "target": container,
        }
        if rest and rest[0] == "ro":
            spec["read_only"] = True
        mounts.append(spec)
    kwargs: dict = {}
    if mounts:
        kwargs["mounts"] = mounts

    # Always write a compose override for the tmpfs mount that hides .pier/
    # from the container. On older Harbor (no mounts_json), this file also
    # carries the workspace bind mount.
    override_paths: list[Path] = []
    if workspace_dir is not None:
        override_paths.append(
            _write_mounts_compose(
                trial_dir,
                workspace_dir,
                container_workdir,
                include_bind_mount=False,
                task_dir=task_dir,
                ports=ports,
            )
        )
    elif ports:
        # No workspace mount, but still need ports exposed.
        override_paths.append(
            _write_mounts_compose(
                trial_dir,
                workspace_dir or Path("/unused"),
                container_workdir,
                include_bind_mount=False,
                ports=ports,
            )
        )
    else:
        mounts_path = trial_dir / "docker-compose-pier.json"
        if mounts_path.exists():
            override_paths.append(mounts_path)

    # Harbor merges these after the task's own compose. pier's override is a
    # separate file from Harbor's docker-compose-mounts.json, which Harbor
    # rewrites.
    environment = EnvironmentFactory.create_environment(
        type="docker",
        environment_dir=task.paths.environment_dir,
        environment_name=task.name,
        session_id=harbor_session_id,
        trial_paths=trial_paths,
        task_env_config=task.config.environment,
        extra_docker_compose=[*(Path(p) for p in extra_compose or []), *override_paths],
        **kwargs,
    )

    return environment, task, trial_paths


async def _async_start_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    workspace_dir: Path | None = None,
    ports: list[int] | None = None,
    extra_mounts: list[str] | None = None,
    extra_compose: list[str] | None = None,
) -> None:
    environment, task, _ = _make_environment(
        task_dir,
        harbor_session_id,
        trial_dir,
        workspace_dir=workspace_dir,
        ports=ports,
        extra_mounts=extra_mounts,
        extra_compose=extra_compose,
    )
    await environment.start(force_build=False)


async def _async_verify_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    extra_compose: list[str] | None = None,
) -> dict:
    from harbor import Verifier

    environment, task, trial_paths = _make_environment(
        task_dir, harbor_session_id, trial_dir, extra_compose=extra_compose
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
    extra_compose: list[str] | None = None,
) -> None:
    environment, _, _ = _make_environment(
        task_dir, harbor_session_id, trial_dir, extra_compose=extra_compose
    )
    await environment.stop(delete=delete)


def start_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    workspace_dir: Path | None = None,
    ports: list[int] | None = None,
    extra_mounts: list[str] | None = None,
    extra_compose: list[str] | None = None,
) -> None:
    """Build (if needed), start a Harbor Docker environment.

    If workspace_dir is provided, it is bind-mounted into the container.
    If ports is provided, those container ports are exposed to the host.
    If extra_mounts is provided, they are added as volume mounts.
    If extra_compose is provided, those compose files are merged in.
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
                extra_compose=extra_compose,
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

    # harbor >=0.13 validates the task shape: a linux task must ship
    # tests/test.sh. Task-free mode never runs a verifier, so a stub.
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    test_sh = tests_dir / "test.sh"
    if not test_sh.exists():
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(0o755)

    return task_dir


def _claude_config_dir() -> str:
    """Return the CLAUDE_CONFIG_DIR path used inside Harbor containers."""
    # Where harbor defines it: 0.24 stopped re-exporting it from the agents.
    from harbor.models.trial.paths import EnvironmentPaths

    return (EnvironmentPaths.agent_dir / "sessions").as_posix()


def _codex_home_dir() -> str:
    """Return the CODEX_HOME path used inside Harbor containers."""
    # Where harbor defines it: 0.24 stopped re-exporting it from the agents.
    from harbor.models.trial.paths import EnvironmentPaths

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
    *,
    skills_dir_override: str | None = None,
) -> None:
    from harbor.agents.factory import AgentFactory
    from harbor.models.agent.name import AgentName

    environment, task, trial_paths = _make_environment(
        task_dir, harbor_session_id, trial_dir
    )

    extra_kwargs: dict = {}
    # pier's --skill flag may inject skills into the container; in that
    # case the caller passes skills_dir_override pointing at the in-
    # container path the bind mount targets. Otherwise fall back to
    # task.toml's [environment].skills_dir.
    effective_skills_dir = skills_dir_override or task.config.environment.skills_dir
    if effective_skills_dir:
        extra_kwargs["skills_dir"] = effective_skills_dir
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
    *,
    skills_dir_override: str | None = None,
) -> None:
    """Install a Harbor agent in a running environment.

    Calls the agent's setup() method which uploads and runs the install
    script (e.g. install-claude-code.sh).  Does NOT call run() — the user
    drives the agent interactively via ``pier exec``.

    skills_dir_override: in-container path where pier has already
    bind-mounted composed skill bundles (via ``--skill``). When set,
    overrides task.toml's [environment].skills_dir for this agent
    install so the agent's cp step picks up the injected bundle.
    """
    with _placeholder_task_env_vars(task_dir):
        asyncio.run(
            _async_setup_agent(
                task_dir,
                harbor_session_id,
                trial_dir,
                agent_name,
                skills_dir_override=skills_dir_override,
            )
        )


def verify_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    extra_compose: list[str] | None = None,
) -> dict:
    """Run Harbor's verifier on a running environment. Returns reward dict."""
    with _placeholder_task_env_vars(task_dir):
        return asyncio.run(
            _async_verify_environment(
                task_dir, harbor_session_id, trial_dir, extra_compose=extra_compose
            )
        )


def stop_environment(
    task_dir: Path,
    harbor_session_id: str,
    trial_dir: Path,
    *,
    delete: bool = False,
    extra_compose: list[str] | None = None,
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
                task_dir,
                harbor_session_id,
                trial_dir,
                delete=delete,
                extra_compose=extra_compose,
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
# Scoring apart: a task declaring [verifier] environment_mode = "separate"
# ---------------------------------------------------------------------------
#
# Harbor scores such a task in an environment of its own, built from the
# task's tests/, after collecting the agent's work: the tests never enter the
# container the agent worked in. pier does the same through Harbor's own
# re-scoring (`harbor trial regrade`): it records the workspace's work as a
# trial, stops the workspace's container, and regrades that record.

#: What every Harbor trial collects besides a task's own artifacts.
ARTIFACTS_CONVENTION = "/logs/artifacts"
AGENT_LOGS = "/logs/agent"


def scores_apart(task_dir: Path) -> bool:
    """Whether any of the task's verifiers runs in an environment of its own:
    the task's, or for a task with steps, any step's."""
    from harbor.models.task.config import TaskConfig
    from harbor.models.task.verifier_mode import task_has_any_separate_verifier

    config = TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())
    return task_has_any_separate_verifier(config)


def _docker(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout
    )


def _declared_artifacts(task_dir: Path) -> list:
    """The artifacts to collect, as Harbor lists them: the task's own, with the
    convention's added unless the task declares it.

    An entry with excludes, or from a service other than main, is refused
    rather than collected differently from how Harbor collects it."""
    from harbor.models.task.artifacts import with_convention_entry
    from harbor.models.task.config import TaskConfig

    config = TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())
    entries = with_convention_entry(
        config.artifacts, convention_source=ARTIFACTS_CONVENTION
    )
    for entry in entries:
        if entry.exclude or (entry.service or "main") != "main":
            raise RuntimeError(
                f"artifact {entry.source}: excludes and services other than "
                "main are not collected by pier verify yet"
            )
    return entries


def _drop_symlinks(root: Path) -> list[Path]:
    """Delete every symlink under *root*, and return them.

    ``docker cp`` keeps a symlink the agent left, and Harbor's regrade copies
    the record following symlinks, so one would resolve on this host rather
    than in the container: a host file copied into the scored trial."""
    dropped: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            if path.is_symlink():
                path.unlink()
                dropped.append(path)
    return dropped


def record_workspace(
    harbor_session_id: str, task_dir: Path, record: Path, agent_name: str
) -> None:
    """The workspace's work, as a trial record Harbor's regrade reads: the
    task's collect hooks run in the container, then its artifacts and the
    agent's logs copied out, with the manifest and result.json beside them."""
    from harbor.models.task.config import TaskConfig, TaskOS
    from harbor.models.trial.artifact_manifest import ArtifactManifestEntry
    from harbor.trial.artifact_handler import artifact_host_path

    container = get_container_name(harbor_session_id)
    config = TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())
    if config.steps:
        raise RuntimeError(
            "a task with steps is not scored apart by pier verify yet: its record "
            "would need each step's results"
        )
    if config.environment.os == TaskOS.WINDOWS:
        raise RuntimeError(
            "a Windows-container task is not scored apart by pier verify: it "
            "collects with POSIX paths and bash"
        )
    for hook in config.verifier.collect:
        if (hook.service or "main") != "main":
            raise RuntimeError(
                f"collect hook {hook.command!r} runs in {hook.service!r}; "
                "pier verify runs hooks in main only"
            )
        # As Harbor runs them in main: in bash, as the hook's user, and best
        # effort, so a hook that fails leaves its output out rather than
        # stopping the scoring.
        user = ["-u", str(hook.user)] if hook.user is not None else []
        try:
            ran = _docker(
                "exec",
                *user,
                container,
                "bash",
                "-c",
                hook.command,
                timeout=hook.timeout_sec,
            )
            failed = (
                f"exit {ran.returncode}: " + (ran.stderr or ran.stdout).strip()[-300:]
                if ran.returncode
                else ""
            )
        except subprocess.TimeoutExpired:
            failed = f"timed out after {hook.timeout_sec:g}s"
        if failed:
            logger.warning("collect hook %r failed (%s)", hook.command, failed)

    artifacts_dir = record / "artifacts"
    inside = record.resolve()
    # pier's session data lies under the workspace, hidden in the container by
    # an empty tmpfs that `docker cp` reads through: the record holds what the
    # container shows there, so no session, earlier trial or env of pier's.
    hidden = PurePosixPath(get_container_workdir(task_dir), ".pier")
    collected: list[tuple[str, str, Path, bool]] = []
    skipped: list[tuple[str, str]] = []
    dropped: list[Path] = []
    for artifact in _declared_artifacts(task_dir):
        source = artifact.source
        target = artifact_host_path(artifacts_dir, artifact)
        destination = (
            "artifacts"
            if target == artifacts_dir
            else f"artifacts/{target.relative_to(artifacts_dir).as_posix()}"
        )
        # As Harbor collects: an artifact overlapping one collected before it
        # is left out, so nothing one copy left can lie on another's path.
        earlier = [t for _, _, t, _ in collected]
        if any(
            target == t or t in target.parents or target in t.parents for t in earlier
        ):
            logger.warning(
                "artifact %s overlaps one collected before it; left out, as Harbor "
                "leaves it",
                source,
            )
            skipped.append((source, destination))
            continue
        if not target.resolve().is_relative_to(inside):
            raise RuntimeError(f"artifact {source} would be written outside the record")
        is_dir = _docker("exec", "-u", "root", container, "test", "-d", source)
        if is_dir.returncode == 0:
            target.mkdir(parents=True, exist_ok=True)
            copied = _docker("cp", f"{container}:{source}/.", str(target))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            copied = _docker("cp", f"{container}:{source}", str(target))
        if copied.returncode:
            # Harbor's regrade refuses a record missing a declared artifact, so
            # one the agent never wrote leaves nothing it could score.
            raise RuntimeError(
                f"could not copy {source} out of the workspace, and the task's "
                "verifier reads it: " + copied.stderr.strip()[-300:]
            )
        copied_from = PurePosixPath(source)
        if is_dir.returncode == 0 and (
            copied_from == hidden or copied_from in hidden.parents
        ):
            under = target / hidden.relative_to(copied_from)
            if under.is_dir() and not under.is_symlink():
                shutil.rmtree(under)
                under.mkdir()
        collected.append((source, destination, target, is_dir.returncode == 0))
        dropped += _drop_symlinks(record)
    (record / "agent").mkdir(exist_ok=True)
    copied = _docker("cp", f"{container}:{AGENT_LOGS}/.", str(record / "agent"))
    if copied.returncode:
        raise RuntimeError(
            "could not copy the agent's logs out of the workspace, so it was not "
            "scored: " + copied.stderr.strip()[-300:]
        )
    dropped += _drop_symlinks(record)
    for link in dropped:
        logger.warning(
            "left %s out of the scored record: it is a symlink, which would "
            "resolve on this host",
            link.relative_to(record).as_posix(),
        )

    manifest = []
    for source, destination, target, a_directory in collected:
        if a_directory:
            kind, status = "directory", "ok" if any(target.iterdir()) else "empty"
        else:
            kind, status = "file", "ok" if target.is_file() else "failed"
        manifest.append(
            ArtifactManifestEntry(
                source=source, destination=destination, type=kind, status=status
            ).model_dump(mode="json")
        )
    for source, destination in skipped:
        manifest.append(
            ArtifactManifestEntry(
                source=source,
                destination=destination,
                type="file" if Path(source).suffix else "directory",
                status="skipped",
            ).model_dump(mode="json")
        )
    (artifacts_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    name = config.task.name if config.task is not None else task_dir.name
    trial_config = {
        "task": {"path": str(task_dir)},
        "trial_name": record.name,
        "agent": {"name": agent_name},
    }
    result = {
        "task_name": name,
        "trial_name": record.name,
        "trial_uri": record.resolve().as_uri(),
        "task_id": {"path": str(task_dir)},
        "task_checksum": "",
        "config": trial_config,
        "agent_info": {"name": agent_name, "version": "", "model_info": None},
        "started_at": datetime.now().astimezone().isoformat(),
    }
    (record / "result.json").write_text(json.dumps(result, indent=2))
    (record / "config.json").write_text(json.dumps(trial_config, indent=2))


def remove_workspace_containers(harbor_session_id: str) -> list[str]:
    """Remove every container of the workspace's compose project, found by its
    label, and return their ids: what stopping does when the environment cannot
    be rebuilt, say because an overlay it was started with has moved."""
    project = get_compose_project(harbor_session_id)
    listed = _docker(
        "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"
    )
    if listed.returncode:
        raise RuntimeError(
            "could not list the workspace's containers: " + listed.stderr.strip()
        )
    ids = listed.stdout.split()
    if ids:
        removed = _docker("rm", "-f", *ids)
        if removed.returncode:
            raise RuntimeError(
                "could not remove the workspace's containers: " + removed.stderr.strip()
            )
    return ids


def stop_workspace_container(harbor_session_id: str) -> None:
    """Stop the workspace's containers — main, and any service an overlay
    added — keeping them for `pier start` to restart: nothing the agent left
    running runs while it is scored. Main is named as well as listed, so a
    project that lists nothing cannot leave it running."""
    project = get_compose_project(harbor_session_id)
    listed = _docker(
        "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"
    )
    if listed.returncode:
        raise RuntimeError(
            "could not list the workspace's containers, so it was not scored: "
            + listed.stderr.strip()
        )
    stopped = _docker(
        "stop", get_container_name(harbor_session_id), *listed.stdout.split()
    )
    if stopped.returncode:
        raise RuntimeError(
            "could not stop the workspace's container, so it was not scored: "
            + stopped.stderr.strip()
        )


async def _async_regrade(
    record: Path, task_dir: Path, trials_dir: Path, trial_name: str
) -> dict:
    from harbor.models.trial.config import SourceTrialConfig, TaskConfig, TrialConfig
    from harbor.models.trial.result import TrialResult
    from harbor.trial.trial import Trial

    source = TrialResult.model_validate_json((record / "result.json").read_text())
    # Harbor looks the agent's class up by name for its setup timeout unless
    # one is given, and a pier session's agent is not one of Harbor's. A
    # regrade sets no agent up, so the timeout is never used.
    agent = source.config.agent.model_copy(update={"override_setup_timeout_sec": 0.0})
    config = TrialConfig(
        task=TaskConfig(path=task_dir),
        trial_name=trial_name,
        trials_dir=trials_dir,
        agent=agent,
        artifacts=source.config.artifacts,
        source_trial=SourceTrialConfig(
            action="regrade", type="local", trial_id=source.id, path=record.resolve()
        ),
    )
    trial = await Trial.create(config)
    result = await trial.run()
    if result.exception_info:
        raise RuntimeError(
            f"{result.exception_info.exception_type}: "
            f"{result.exception_info.exception_message}"
        )
    rewards = result.verifier_result.rewards if result.verifier_result else None
    return dict(rewards) if rewards else {"reward": None}


def regrade(record: Path, task_dir: Path, trials_dir: Path, trial_name: str) -> dict:
    """Score a trial record with the task, in an environment built from its
    tests/, through Harbor's regrade. Returns the rewards; the scored trial is
    `trials_dir/trial_name`."""
    with _placeholder_task_env_vars(task_dir):
        return asyncio.run(_async_regrade(record, task_dir, trials_dir, trial_name))


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

    agent_info = AgentInfo(name=agent_name or "unknown", version="unknown")

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
    import asyncio
    import inspect

    from harbor import GitTaskId
    from harbor.tasks.client import TaskClient  # internal import

    task_id = GitTaskId(
        git_url=git_url,
        path=Path(task_path),
        git_commit_id=git_commit_id,
    )
    client = TaskClient()
    result: Any = client.download_tasks([task_id])
    if inspect.iscoroutine(result):
        # Harbor >= 0.7 made download_tasks a coroutine; older versions return
        # the list directly.
        result = asyncio.run(result)
    return _downloaded_paths(result)[0]


def _downloaded_paths(result: Any) -> list[Path]:
    """The paths in a TaskClient.download_tasks result, whichever shape it has.

    Older Harbor returned ``list[Path]``; current Harbor returns a
    ``BatchDownloadResult`` whose ``paths`` property lists them.
    """
    paths = getattr(result, "paths", result)
    return [Path(p) for p in paths]


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


_CONFIG_DIR_AGENTS = {"claude-code", "codex"}


def _claude_code_host_session_dir(workspace: Path) -> Path | None:
    """claude-code keys host session logs by cwd: ~/.claude/projects/<slug>,
    <slug> = absolute path with ``/`` and ``.`` replaced by ``-``."""
    if os.name == "nt":  # slug convention below is POSIX; unverified on Windows
        return None
    slug = str(workspace.resolve()).replace("/", "-").replace(".", "-")
    p = Path.home() / ".claude" / "projects" / slug
    if p.is_dir() and any(p.glob("*.jsonl")):
        return p
    return None


# Host-mode session locators, per agent — the host-side analogue of
# _AGENT_BRIDGES: where a locally-run agent CLI keeps the workspace's
# session logs. Harbor can't own this knowledge (it only manages
# containerized agents), so it is quarantined here with the rest of the
# per-agent residue.
_HOST_SESSION_LOCATORS = {
    "claude-code": _claude_code_host_session_dir,
}


def detect_host_session(
    workspace: Path, agent: str | None = None
) -> tuple[str, Path] | None:
    """Find a host-mode agent session for `workspace`. Checks the given
    agent's locator, or every registered locator when agent is None."""
    names = [agent] if agent else list(_HOST_SESSION_LOCATORS)
    for name in names:
        locator = _HOST_SESSION_LOCATORS.get(name)
        if locator and (found := locator(workspace)):
            return name, found
    return None


def get_agent_session_dirs(harbor_agent_dir: Path, agent_name: str) -> list[Path]:
    """Return session directories containing files for *agent_name*, newest first.

    Scans ``harbor_agent_dir/exec/`` for timestamped directories that
    contain the agent's output (``<agent_name>.txt``).  For config-dir
    agents (claude-code, codex), a ``sessions/`` subdirectory also
    counts as a match.
    """
    exec_dir = harbor_agent_dir / "exec"
    if not exec_dir.is_dir():
        return []

    def _matches(d: Path) -> bool:
        if (d / f"{agent_name}.txt").exists():
            return True
        if agent_name in _CONFIG_DIR_AGENTS and (d / "sessions").is_dir():
            return True
        return False

    return [
        d
        for d in sorted(exec_dir.iterdir(), key=lambda d: d.name, reverse=True)
        if d.is_dir() and _matches(d)
    ]


def _latest_session_dir(harbor_agent_dir: Path, agent_name: str) -> Path | None:
    """Return the most recent session directory for *agent_name*, or None."""
    dirs = get_agent_session_dirs(harbor_agent_dir, agent_name)
    return dirs[0] if dirs else None


# ---------------------------------------------------------------------------
# Agent log capture — pier-side knowledge of Harbor agent internals
#
# Harbor's run() handles env vars, output tee, and post-run artifact
# collection.  Pier doesn't call run() — agents are driven interactively
# — so these functions replicate the parts needed for log persistence.
#
# TODO: Replace when Harbor exposes APIs for get_log_env_vars(),
# get_post_run_commands(), and get_cli_binary().
# ---------------------------------------------------------------------------


def get_log_capture_env(base_dir: str | None = None) -> dict[str, str]:
    """Env vars that direct agent session logs to the mounted volume.

    Only claude-code and codex use config-dir env vars for log routing;
    all other agents are covered by tee wrapping.  When *base_dir* is
    provided (e.g. ``/logs/agent/<ts>``), the default agent_dir
    prefix is replaced so logs land in the per-run directory.
    """
    from harbor.models.trial.paths import EnvironmentPaths

    agent_dir = str(EnvironmentPaths.agent_dir)
    env = {
        "CLAUDE_CONFIG_DIR": _claude_config_dir(),
        "CODEX_HOME": _codex_home_dir(),
    }
    if base_dir:
        env = {k: v.replace(agent_dir, base_dir) for k, v in env.items()}
    return env


def get_post_run_commands(agent_name: str, log_dir: str) -> list[str]:
    """Shell commands to collect agent artifacts after an interactive run.

    Replicates the post-run steps from Harbor's ``run()`` methods that
    copy or export structured session data to the log directory.
    """
    import shlex

    safe_dir = shlex.quote(log_dir)
    if agent_name == "gemini-cli":
        return [
            "find ~/.gemini/tmp -type f -name 'session-*.json' 2>/dev/null | "
            f"head -n 1 | xargs -r -I{{}} cp {{}} {safe_dir}/gemini-cli.trajectory.json"
        ]
    if agent_name == "hermes":
        return [
            'export PATH="$HOME/.local/bin:$PATH" && '
            f"hermes sessions export {safe_dir}/hermes-session.jsonl "
            "--source cli 2>/dev/null || true"
        ]
    return []


def get_agent_exec_env(agent_name: str) -> tuple[dict[str, str], str]:
    """Return env vars and PATH prefix needed to run an agent interactively.

    Returns ``(env_dict, path_prefix)`` where *path_prefix* is a string like
    ``"$HOME/.local/bin"`` or empty.

    Log-dir env vars (CLAUDE_CONFIG_DIR, CODEX_HOME) are set unconditionally
    by :func:`get_log_capture_env` — this function only adds agent-specific
    behavior flags and PATH prefixes.
    """
    env: dict[str, str] = {}
    path_prefix = ""

    if agent_name == "claude-code":
        env["IS_SANDBOX"] = "1"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        path_prefix = _local_bin_path_prefix()
    elif agent_name == "codex":
        path_prefix = _codex_path_prefix()
    elif agent_name in {"cursor-cli", "kimi-cli", "goose", "hermes"}:
        path_prefix = _local_bin_path_prefix()
    elif agent_name in {"gemini-cli", "qwen-coder", "opencode"}:
        path_prefix = _codex_path_prefix()
    return env, path_prefix


def get_agent_binary(agent_name: str) -> str | None:
    """Extract the CLI binary name for an agent from Harbor's version command.

    Instantiates the agent via Harbor's factory and parses the binary
    from ``get_version_command()``.  Returns None if the binary can't
    be determined (e.g. agents that use ``python -m ...``).
    """
    from harbor.agents.factory import AgentFactory
    from harbor.models.agent.name import AgentName

    _NON_AGENT_BINARIES = {"python", "pip", "uv"}

    try:
        agent = AgentFactory.create_agent_from_name(
            AgentName(agent_name), logs_dir=Path("/tmp/pier-probe")
        )
        cmd = agent.get_version_command()
        if not cmd:
            return None
        # Take the last semicolon-separated part and extract the first token.
        last_part = cmd.split(";")[-1].strip()
        binary = last_part.split()[0] if last_part else None
        if binary and Path(binary).name in _NON_AGENT_BINARIES:
            return None
        return Path(binary).name if binary else None
    except Exception:
        return None


# Cached binary→agent map, built once per process from Harbor's agent registry.
_binary_agent_map: dict[str, str] | None = None


def get_binary_agent_map() -> dict[str, str]:
    """Return a mapping of CLI binary names to Harbor agent names.

    Built from Harbor's agent registry by inspecting each agent's
    ``get_version_command()``.  Cached after first call.
    """
    global _binary_agent_map
    if _binary_agent_map is not None:
        return _binary_agent_map

    from harbor.agents.factory import AgentFactory

    # harbor's public list where it has one (0.23+), else the keys of its
    # private map (0.21, 0.22). harbor dropped the private list pier read
    # before, and every agent became undetectable.
    if hasattr(AgentFactory, "registered_names"):
        names = [str(n) for n in AgentFactory.registered_names()]
    else:
        names = [str(getattr(n, "value", n)) for n in AgentFactory._AGENT_MAP]

    result: dict[str, str] = {}
    for name in names:
        try:
            binary = get_agent_binary(name)
            if binary:
                result[binary] = name
        except Exception:
            continue
    _binary_agent_map = result
    return result


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


# --- Skill resolution (re-exported from harbor.skills) -------------------
#
# Same fragile-internal pattern as the env helpers below: pier centralizes
# the harbor import in this adapter layer.


def resolve_skill_paths(paths: list[Path]) -> list[tuple[str, Path]]:
    """Resolve skill input paths to (name, source_dir) pairs.

    Wraps ``harbor.skills.resolve_skills`` so callers get Harbor's exact
    semantics:
    - Each input may be a single skill directory (containing SKILL.md)
      OR a root containing skill subdirectories.
    - Dotfile dirs at root level are skipped.
    - Duplicate skill names use last-wins (later inputs override earlier).
    - Names sorted alphabetically.

    Raises FileNotFoundError / ValueError on malformed inputs (Harbor's
    own exceptions surfacing).
    """
    from harbor.skills import resolve_skills

    return [(s.name, s.source) for s in resolve_skills(paths)]


# --- Sensitive-env helpers (re-exported from harbor.utils.env) ------------
#
# harbor.utils.env is internal-fragile per the API stability notes at the
# top of this file. Re-exporting here centralizes the import so callers
# (pier/cli.py) don't import from harbor.* directly.


def sanitize_env_kv(kv: str) -> str:
    """Sanitize a KEY=VALUE string for persistence into session.json.

    Sensitive keys (matching Harbor's regex KEY|SECRET|TOKEN|PASSWORD|
    CREDENTIAL|AUTH) get templated to ``KEY=${KEY}`` if the value matches
    the current host env, or redacted (``KEY=ab****cd``) otherwise.
    Non-sensitive keys pass through verbatim.
    """
    from harbor.utils.env import sanitize_env_assignment

    return sanitize_env_assignment(kv)


def resolve_env_kv(kv: str) -> str:
    """Resolve a KEY=${KEY} (or ${KEY:-default}) entry from the current
    shell at pier-exec time.

    Raises ``KeyError`` if a referenced env var isn't in os.environ —
    caller should surface a clear message rather than forwarding the
    unresolved template to the container.
    """
    import re

    from harbor.utils.env import is_env_template

    key, _, value = kv.partition("=")
    if not is_env_template(value):
        return kv
    # is_env_template matched, so this is "${NAME}" or "${NAME:-default}".
    m = re.fullmatch(r"\$\{([^}:]+)(?::-(.*))?\}", value)
    if m is None:
        return kv  # defensive; shouldn't happen given is_env_template
    var_name, default = m.group(1), m.group(2)
    if var_name in os.environ:
        return f"{key}={os.environ[var_name]}"
    if default is not None:
        return f"{key}={default}"
    raise KeyError(var_name)


def exec_in_container(
    harbor_session_id: str,
    task_dir: Path,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    path_prefix: str = "",
    detach: bool = False,
    log_path: str | None = None,
) -> int:
    """Run a command in a running container via docker exec.

    Returns the process exit code. Handles TTY allocation, env vars,
    PATH prefix, detached mode, and optional session recording.

    When *log_path* is set (an absolute container path), the command is
    wrapped with ``script -q`` to record terminal output while preserving
    full TTY behavior (colors, cursor, interactive prompts).
    """
    import shlex

    container = get_container_name(harbor_session_id)
    workdir = get_container_workdir(task_dir)

    env_flags: list[str] = []
    for var, val in (env or {}).items():
        env_flags.extend(["-e", f"{var}={val}"])

    # Build the shell command.  When path_prefix or log_path is set we
    # must wrap in sh -c; otherwise run the command directly.
    needs_shell = bool(path_prefix) or bool(log_path)

    if needs_shell:
        parts = []
        if path_prefix:
            parts.append(f"export PATH={path_prefix}:$PATH")
        cmd_str = " ".join(shlex.quote(c) for c in command)
        if log_path:
            # Use script(1) to record output while preserving TTY.
            cmd_str = f"script -q -c {shlex.quote(cmd_str)} {shlex.quote(log_path)}"
        else:
            cmd_str = f"exec {cmd_str}"
        parts.append(cmd_str)
        shell_cmd = " && ".join(parts)
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

    return extract_agent_context(agent_name, logs_dir)


def extract_agent_context(agent_name: str, logs_dir: Path) -> dict | None:
    """Extract trajectory and usage from an agent's logs directory.

    When *logs_dir* contains per-session timestamped directories (created
    by ``pier exec``), the latest session for *agent_name* is used
    automatically.  Otherwise expects the standard Harbor layout directly
    under *logs_dir*.
    """
    session = _latest_session_dir(logs_dir, agent_name)
    if session:
        logs_dir = session
    from harbor.agents.factory import AgentFactory
    from harbor.models.agent.context import AgentContext
    from harbor.models.agent.name import AgentName

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
            logs_dir,
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
