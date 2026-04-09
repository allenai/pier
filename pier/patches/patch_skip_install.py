"""Skip agent install when the binary is already in the Docker image.

Harbor's built-in agents (claude-code, codex, etc.) always run a full
apt-get + curl/npm install cycle in their install() method, even when
the Dockerfile already baked the agent in. This adds ~5 minutes of
redundant setup per trial.

This patch adds an early-return guard to the install() method: if the
agent binary is already on PATH, skip the install.

See: https://github.com/laude-institute/harbor/issues/1279
"""

from __future__ import annotations

import subprocess
import sys

MARKER = "PIER_SKIP_INSTALL_PATCH"

_AGENTS = {
    "claude_code": ("claude", 'export PATH="$HOME/.local/bin:$PATH"; which claude'),
    "codex": ("codex", "which codex"),
    "goose": ("goose", "which goose"),
    "hermes": ("hermes", "which hermes"),
}


def _find_all_agent_py(module_name: str) -> list[str]:
    """Find agent module in all uv tool venvs that have harbor installed."""
    from pathlib import Path

    paths: list[str] = []
    tools_dir = Path.home() / ".local" / "share" / "uv" / "tools"
    interpreters = []
    if tools_dir.is_dir():
        for tool_dir in tools_dir.iterdir():
            python = tool_dir / "bin" / "python3"
            if not python.exists():
                python = tool_dir / "bin" / "python"
            if python.exists():
                interpreters.append(str(python))
    interpreters.append(sys.executable)

    for python in interpreters:
        try:
            result = subprocess.run(
                [
                    python,
                    "-c",
                    f"import harbor.agents.installed.{module_name}; "
                    f"print(harbor.agents.installed.{module_name}.__file__)",
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
    return paths


def is_applied() -> bool:
    found_any = False
    for module_name in _AGENTS:
        for path in _find_all_agent_py(module_name):
            found_any = True
            if MARKER not in open(path).read():
                return False
    return found_any


def apply() -> list[str]:
    """Apply the patch. Returns list of patched file paths."""
    patched = []
    for module_name, (binary, check_cmd) in _AGENTS.items():
        for path in _find_all_agent_py(module_name):
            src = open(path).read()
            if MARKER in src:
                continue

            guard = (
                f"        # {MARKER}\n"
                f"        try:\n"
                f"            _check = await environment.exec(command={check_cmd!r})\n"
                f"            if _check.return_code == 0:\n"
                f"                return\n"
                f"        except Exception:\n"
                f"            pass\n"
            )

            target = "    async def install(self, environment: BaseEnvironment) -> None:\n"
            if target not in src:
                continue

            lines = src.split("\n")
            insert_idx = None
            found_def = False
            for i, line in enumerate(lines):
                if "async def install(self, environment" in line:
                    found_def = True
                    continue
                if found_def and line.strip() and not line.strip().startswith("#"):
                    insert_idx = i
                    break

            if insert_idx is None:
                continue

            lines.insert(insert_idx, guard)
            open(path, "w").write("\n".join(lines))
            patched.append(path)

    return patched
