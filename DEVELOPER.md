# Developer Guide

## Setup

```bash
git clone https://github.com/allenai/pier && cd pier
make check                      # run tests, lint, and typecheck
uv run pre-commit install       # optional: auto-lint and format on commit
uv tool install -e .            # editable install — use `pier` from any directory
```

## Testing

```bash
uv run --extra dev pytest -rs   # fast — skips Docker agent tests
```

Docker-backed agent integration suite:

```bash
PIER_RUN_DOCKER_INTEGRATION=1 uv run --extra dev pytest -rs pier/tests/test_agent_integration.py
PIER_RUN_DOCKER_INTEGRATION=1 PIER_TEST_AGENTS=codex uv run --extra dev pytest -rs pier/tests/test_agent_integration.py
PIER_RUN_DOCKER_INTEGRATION=1 PIER_TEST_IMAGE=ubuntu:24.04 uv run --extra dev pytest -rs pier/tests/test_agent_integration.py
```

## Agent Log Capture

When agents run inside containers via `pier exec`, their output and session
logs need to survive container teardown. Harbor mounts
`workspace/.pier/_harbor/agent/` → `/logs/agent` in the container, so
anything written there persists on the host.

### Two capture mechanisms

**Session recording** — `pier exec` auto-detects agent commands by matching
the command name against Harbor's agent registry (`get_binary_agent_map()`).
When an agent is detected, the command is wrapped with `script -q -c` to
record terminal output to `/logs/agent/exec/<ts>/<agent>.txt` while preserving
full TTY behavior (colors, cursor, interactive prompts).

**Config-dir env vars** — `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are set on
every container exec, pointing into the per-session directory. These agents
write structured session logs (JSONL, session dirs) that survive container
teardown. User `-e` overrides win via `setdefault`.

### Per-session isolation

Every `pier exec` creates a timestamped directory directly under
the agent log dir:

```
workspace/.pier/_harbor/agent/
  exec/
    2026-04-09_18-30-00-a1b2c3/       # pier exec claude ...
      claude-code.txt                 # session recording
      sessions/projects/hash/         # Claude JSONL (via CLAUDE_CONFIG_DIR)
    2026-04-09_18-35-00-d4e5f6/       # pier exec goose ...
      goose.txt                       # session recording
    2026-04-09_18-40-00-g7h8i9/       # pier exec bash
      sessions/projects/hash/         # Claude JSONL if run inside bash
  setup/                              # Harbor agent install logs (not pier-managed)
```

Each session directory mirrors Harbor's flat `/logs/agent/` layout. When
`pier verify` or `pier capture` extracts trajectory,
`_latest_session_dir()` (called by `extract_agent_context()`) scans by
reverse timestamp to find the most recent directory containing the
requested agent's files.

### What pier hardcodes vs derives from Harbor

| Knowledge | Source | Location |
|-----------|--------|----------|
| CLI binary names (claude, codex, goose...) | Harbor's `get_version_command()` | `get_binary_agent_map()` |
| Log-dir env vars (CLAUDE_CONFIG_DIR, CODEX_HOME) | Harbor's `EnvironmentPaths` | `get_log_capture_env()` |
| Post-run artifact commands (gemini, hermes) | Hardcoded from Harbor's `run()` | `get_post_run_commands()` |
| Behavior env vars (IS_SANDBOX, PATH prefixes) | Hardcoded per-agent | `get_agent_exec_env()` |
| Session recording filename | Convention: `<agent-name>.txt` | `_exec_container()` |

Binary names and log-dir values are derived from Harbor at runtime.
The rest is hardcoded in `harbor_bridge.py` — all grouped under the
"Agent log capture" section. When Harbor exposes APIs for
`get_log_env_vars()`, `get_post_run_commands()`, and `get_cli_binary()`,
these can be replaced.

### Post-run artifact collection

Some agents produce structured artifacts beyond raw output. Harbor's
`run()` collects these after the agent exits:

- **gemini-cli**: copies `~/.gemini/tmp/session-*.json` → `gemini-cli.trajectory.json`
- **hermes**: runs `hermes sessions export` → `hermes-session.jsonl`

Pier replicates these via `get_post_run_commands()`, run after the agent
exits but while the container is still up. If a new Harbor agent adds
post-run steps, add them there.

### Multiple sessions

`pier verify` and `pier capture` auto-find the most recent session
for the requested agent. Older sessions are preserved on disk and can be
selected with `--session <timestamp>` on verify/capture.

### Limitations

- **`pier exec bash` + manual agent** — only config-dir agents
  (Claude, Codex) get structured logs captured. Other agents need the
  `script` wrapper, which only applies when pier detects the agent command.
- **Agents with complex entry points** (openhands, swe-agent) aren't
  auto-detected — their binary is `python`/`pip`, not a unique name.
