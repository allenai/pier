# Pier

Pier is an interactive workspace manager for coding agents (Claude Code, Codex, Gemini CLI, OpenHands, Aider, [and more](https://github.com/laude-institute/harbor)). Run agents in managed workspaces (containerized or host), capture and share traces, and interactively solve and develop [Harbor](https://github.com/laude-institute/harbor) benchmark tasks.

## Install

```bash
uv tool install git+https://github.com/allenai/pier
```

## Quick start

### Start a workspace

`pier start` creates a workspace directory and, in container mode, builds and starts a container with your agent installed. All subsequent commands run from inside the workspace.

```bash
# From a base image:
pier start -d ./workspace --image ubuntu:24.04 --agent claude-code \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
cd ./workspace

# From a Harbor task:
pier start ./tasks/my-task -d ./workspace --agent claude-code \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
cd ./workspace

# Host mode (no container):
pier start ./tasks/my-task --host -d ./workspace
cd ./workspace
pier skills             # install task skills for your agent
```

- `--agent` installs a [supported coding agent](https://github.com/laude-institute/harbor) and registers skills from `skills_dir` in task.toml. Optional — without it you get a plain workspace. To install multiple agents, run `pier start --agent <name>` again from the workspace.
- `--image` accepts any Docker image — a stock OS, a project image with tools pre-installed, etc.
- `--no-mount` keeps workspace files inside the container only (no bind-mount to host). `pier stop` copies files back. Note: Harbor's internal mounts (agent logs, verifier output) still write to the host under `.pier/`.
- `-f` / `--force` allows starting in a non-empty directory.

### Work in the workspace

```bash
cat .task/instruction.md  # read the task instruction (Harbor tasks)

# Container mode:
pier exec claude          # run the agent interactively
pier exec bash            # drop into the container shell

# Non-interactive (scripted or automated use):
pier exec -- claude -p "Read .task/instruction.md and do the task" --dangerously-skip-permissions
# TODO: pier run — run the agent non-interactively and verify, via harbor run

# Run a background process (e.g., live document preview):
pier exec -d -- quarto preview --port 8888 --host 0.0.0.0 --no-browse

# Host mode:
claude                    # run the agent directly
```

### Score and review

For Harbor tasks, score your work with the verifier:

```bash
pier verify             # run tests/test.sh, print reward, save results and trajectory to .pier/trials/
```

Without a Harbor task (e.g. `--image` mode), save the trajectory with `pier capture`:

```bash
pier capture                                       # save trajectory to .pier/trials/
pier capture --session-dir <path> -a claude-code   # from any agent session outside pier
```

Browse, export, and review:

```bash
pier traces                         # list traces in .pier/trials/
pier traces -o trace.tar.gz         # export latest trace as archive
pier view                           # web dashboard (via Harbor)
pier summarize                      # AI summary (via Harbor)
```

Works with all [supported agents](https://github.com/laude-institute/harbor).

### Iterate on a task

```bash
vim ../tasks/my-task/tests/test.sh   # edit the verifier
pier verify                          # re-run — changes picked up immediately

vim ../tasks/my-task/instruction.md  # edit the instruction or Dockerfile
pier stop
pier start                           # rebuild and restart
```

To start a new task, clone an existing [Harbor task](https://github.com/laude-institute/harbor) as a template (see Harbor's docs for the task format):

```bash
git clone https://github.com/laude-institute/harbor
cp -r harbor/examples/tasks/hello-world ./tasks/my-task
```

## Commands

### `pier capture`

Extract the agent's trajectory (conversation, tool use, and cost data) for the current workspace. No Harbor task required.

```bash
pier capture                                       # container mode with registered agent
pier capture --session-dir <path> -a claude-code   # host mode or external session
```

In container mode with a registered agent (`pier start --agent`), the trajectory is extracted automatically. In host mode, a claude-code session for the workspace (from `pier exec claude`) is auto-detected — bare `pier capture` / `pier verify` extract it. Otherwise, pass `--session-dir` and `-a` to specify the session location and agent. In container mode, `--session-dir` refers to a path inside the container.

### `pier traces`

List or export captured trials. Without `-o`, lists available trials. With `-o`, packages them for sharing.

```bash
pier traces                              # list trials
pier traces -o trace.tar.gz              # export latest trial
pier traces 2026-04-02_15-30-00 -o t.gz  # export specific trial
pier traces --all -o traces.tar.gz       # export all trials
```

### `pier start [task_path] [-d <workspace>]`

Launch a workspace. Also restarts stopped containers and installs agents into existing workspaces.

```bash
# Task-free (any Docker image, no task definition)
pier start -d . --image ubuntu:24.04
pier start -d . --image my-project-image --agent claude-code --ports 8888

# With a Harbor task
pier start ./tasks/my-task -d ./my-workspace
pier start https://github.com/org/repo#tasks/my-task

# Manage existing workspace
pier start --agent claude-code              # install agent in current workspace
pier start                                  # restart a stopped container
```

- `task_path` can be a local directory or a remote git reference (`URL#path`). Optional — omit for task-free mode.
- `-d` specifies the workspace directory. Required for local tasks and task-free mode; defaults to `./<task-name>` for remote tasks.
- `--image` specifies the base Docker image (task-free mode). Ignored when a task is provided (the task's Dockerfile is used).
- `--ports` exposes container ports to the host (e.g., `--ports 8888`).
- `--mounts-json` adds volume mounts as a JSON array (e.g., `--mounts-json '["./skills:/opt/skills:ro"]'`).
- `-e` passes container-mode environment variables in `KEY=VALUE` format (repeatable). Stored in the session and forwarded on every `pier exec`.
- `--env-file` loads container-mode environment variables from a `.env` file. Same behavior as `-e` for each line.
- `--extra-docker-compose` adds a compose overlay, as `harbor run --extra-docker-compose` does — a service beside the agent's container, or a change to that container (repeatable, container mode only). Its path is kept in the session and reused by restart and `pier verify`; keep the file available for these commands.
  Restart without this option retains the saved overlays; it does not clear them. Relative build and volume paths in overlays resolve against the task's `environment/` directory, which Harbor uses as the Compose project directory, rather than the overlay's directory. Use absolute paths when an overlay lives elsewhere.
- `--no-mount` keeps workspace files inside the container only (no bind-mount to host). `pier stop` copies files back. Note: Harbor's internal mounts (agent logs, verifier output) still write to the host under `.pier/`.
- `-f` / `--force` allows starting in a non-empty directory.
- `--host` skips the container (workspace only).
- `--agent` installs a coding agent. To install additional agents, run `pier start --agent <name>` again from the workspace. When `task_path` is omitted, it operates on the current workspace.

### `pier exec <command...>`

Run a command in the workspace context. Sets workspace env vars so task CLIs find the right workspace.

```bash
pier exec bash
pier exec claude                    # agent auto-detected → full output captured
pier exec -d -- quarto preview --port 8888 --host 0.0.0.0 --no-browse
```

- **Container mode**: delegates to `docker exec` in the container's working directory. Agent commands are auto-detected and their output is recorded via `script`. Each exec gets its own timestamped directory so sessions don't overwrite each other. `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are always set so structured session logs land in the mounted volume.
- **Host mode**: runs the command directly with `TASK_WORKSPACE` set
- `-d` / `--detach`: runs in the background (useful for servers like quarto preview)

### `pier verify`

Run the verifier and report the reward (requires a Harbor task). Each run creates a timestamped directory under `<workspace>/.pier/trials/` in Harbor's trial format.

```bash
pier verify                          # uses agent from pier start --agent
```

The agent is inferred from the session (set by `pier start --agent`). If no agent is registered, the verifier still runs and saves the reward but the trajectory (conversation, tool use, and cost data) is skipped.

- **Container mode**: uses Harbor's `Verifier` (same verifier as `harbor run`). The trajectory is captured automatically when an agent is registered. For unregistered agents (e.g. baked into the image), pass `--session-dir` (container path) and `-a`.
- **Host mode**: spins up a temporary container to run the verifier, then tears it down. Pass `--session-dir` and `-a` to capture the trajectory.
- **A task scored apart** — one whose `task.toml` declares `[verifier] environment_mode = "separate"` — is scored through Harbor's `trial regrade`: pier collects main-service hooks, artifacts and agent logs, stops main before collecting sidecar evidence, then stops all remaining workspace services, including compose overlays. The record is scored in an environment built from the task's `tests/`, which never enter the workspace. `pier start` restarts the workspace; the scored trial is under the verify run's `scored/`. Symlinks are removed from each copy before the next artifact is collected. Overlapping artifacts are skipped with a warning, as Harbor collects them; regrade refuses a record missing a declared input. Multi-step tasks and artifact exclusions are rejected before collection; use `harbor run` for those tasks.
  Collect hooks require `timeout` in their service image; its timer kills the hook's process group before artifact collection. Hook failures warn and collection continues, but an unresponsive Docker client aborts verification because hook completion cannot be confirmed. Artifact and agent-log copy failures also abort verification: regrade requires every declared input. Automatic trajectory extraction uses the copied record's logs; an explicit `--session-dir` can still be copied from the stopped container.

### `pier stop`

Stop the Docker container for the current workspace (container mode only). The workspace directory is preserved. Restart later with `pier start` (no arguments, from inside the workspace).

If Compose cleanup fails (for example, an overlay file was moved or deleted), `pier stop` stops all running containers with the workspace's Compose project label and warns that cleanup was incomplete. Stopped containers and other resources may remain; restore the overlay and run `pier stop` again to clean them up.

### `pier list`

Show active workspaces.

```
  Workspace                                Container                      Status
  ──────────────────────────────────────── ────────────────────────────── ────────
  /home/user/my-workspace                  pier-my-workspace-a1b2-main-1  running
  /home/user/hello-world                   —                              —
```

### `pier view [path]`

Open a web dashboard to browse trial trajectories. Defaults to the current workspace's `.pier/` directory.

```bash
pier view                          # current workspace
pier view /path/to/.pier           # explicit path
pier view --port 9000              # custom port
```

### `pier summarize [path]`

Summarize trial results using AI (requires an Anthropic API key). Defaults to the current workspace.

```bash
pier summarize                     # summarize failures in current workspace
pier summarize --all               # include successful trials too
pier summarize -m sonnet           # use a different model (default: haiku)
```

### `pier skills`

Install task skills for your coding agent (host mode only). Reads `skills_dir` from task.toml, extracts skills from the task's container image, and registers them via `npx skills add`. In container mode with `--agent`, skills are installed automatically.


## Development

See [DEVELOPER.md](DEVELOPER.md) for setup, testing, and architecture.
