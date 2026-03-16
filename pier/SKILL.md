---
name: pier
description: This skill should be used when the user asks to "start a task", "run a verifier", "verify my solution", "work on a Harbor task", "pier start", "pier verify", or needs to interactively work on Harbor benchmark tasks.
---

# pier — Interactive Task Runner

pier launches workspaces for Harbor benchmark tasks, providing the same environment and verifier as automated evaluation.

## Install

```bash
uv tool install --with harbor git+https://github.com/allenai/pier
```

## Modes

- **Container mode** (default): Full Docker isolation, same image as `harbor run`. Workspace is bind-mounted into the container.
- **Host mode** (`--host`): No Docker. Uses a local workspace directory with native tools and auth.

Both modes always create a local workspace with task assets (instruction.md, context/, skills/, examples/).

## Commands

### Start a workspace

```bash
# Container mode (default) — local task
pier start ./tasks/my-task -d ./my-workspace

# Host mode — local task
pier start ./tasks/my-task -d ./my-workspace --host

# Remote task (workspace defaults to ./<task-name>)
pier start https://github.com/org/repo#tasks/my-task --host
```

`-d` is required for local tasks. Container mode builds and starts a Docker container via Harbor.

### Run a command in the workspace

```bash
pier exec <command...>
```

Sets workspace env vars so task CLIs find the right workspace. The workspace is auto-resolved from your current directory.

### Verify your solution

```bash
pier verify [-a claude-code] [--session-dir <path>] [--trial-dir <path>]
```

Runs the task's verifier and reports the reward. In container mode, automatically extracts the agent's conversation trajectory. In host mode, pass `--session-dir` to capture the trajectory. `-a` selects the agent when multiple are installed.

### Stop the workspace

```bash
pier stop    # stop the container
```

Container mode only: stops the Docker container. The workspace directory is preserved.

### List workspaces

```bash
pier list
```

## Workflow (host mode)

1. `pier start ./tasks/my-task -d ./workspace --host` — create workspace
2. `cd workspace` — work on the task
3. `pier verify` — check your score
4. Delete the workspace directory when done

## Workflow (container mode)

1. `pier start ./tasks/my-task -d ./workspace` — build and start container
2. `cd workspace` — edit files locally (bind-mounted into container)
3. `pier exec bash` — enter the container
4. `pier verify` — check your score
5. `pier stop` — stop the container when done
