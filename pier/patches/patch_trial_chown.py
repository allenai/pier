"""Fix agent log permissions before trajectory conversion.

When Harbor runs agents in Docker with bind mounts, the agent (running as
root inside the container) creates session log files with 600 permissions.
Harbor's trajectory converter (populate_context_post_run) runs on the host
*before* Harbor's cleanup chown in stop(), so it hits "Permission denied"
reading those files and silently skips trajectory generation.

This patch inserts a _maybe_fix_agent_log_permissions() call before each
_maybe_populate_agent_context() call in trial.py, calling _chown_to_host_user
on the agent logs directory while the container is still running.

See: https://github.com/laude-institute/harbor/issues/178
"""

from __future__ import annotations

MARKER = "PIER_CHOWN_PATCH"


def _find_all_trial_py() -> list[str]:
    """Find trial.py in all uv tool venvs that have harbor installed."""
    import subprocess
    import sys
    from pathlib import Path

    paths = []
    tools_dir = Path.home() / ".local" / "share" / "uv" / "tools"
    if tools_dir.is_dir():
        for tool_dir in tools_dir.iterdir():
            python = tool_dir / "bin" / "python3"
            if not python.exists():
                python = tool_dir / "bin" / "python"
            if not python.exists():
                continue
            try:
                result = subprocess.run(
                    [
                        str(python),
                        "-c",
                        "import harbor.trial.trial; print(harbor.trial.trial.__file__)",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                p = result.stdout.strip()
                if p and p not in paths:
                    paths.append(p)
            except Exception:
                continue

    # Also check the current interpreter
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import harbor.trial.trial; print(harbor.trial.trial.__file__)",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        p = result.stdout.strip()
        if p and p not in paths:
            paths.append(p)
    except Exception:
        pass

    return paths


def is_applied() -> bool:
    paths = _find_all_trial_py()
    if not paths:
        return False
    return all(MARKER in open(p).read() for p in paths)


def apply() -> list[str]:
    """Apply the patch. Returns the patched file paths."""
    patched = []
    for path in _find_all_trial_py():
        src = open(path).read()
        if MARKER in src:
            continue

        helper = f"""\
    async def _maybe_fix_agent_log_permissions(self) -> None:  # {MARKER}
        \"\"\"Best-effort chown of agent logs so populate_context_post_run can read them.\"\"\"
        if self._environment.is_mounted and hasattr(self._environment, '_chown_to_host_user'):
            try:
                await self._environment._chown_to_host_user(
                    str(EnvironmentPaths.agent_dir), recursive=True
                )
            except Exception:
                pass

    def _maybe_populate_agent_context(self) -> None:"""

        src = src.replace(
            "    def _maybe_populate_agent_context(self) -> None:",
            helper,
            1,
        )
        src = src.replace(
            "                self._maybe_populate_agent_context()",
            "                await self._maybe_fix_agent_log_permissions()\n                self._maybe_populate_agent_context()",
        )

        open(path, "w").write(src)
        patched.append(path)

    return patched
