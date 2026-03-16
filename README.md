# Pier

Pier lets people (and their coding agents) work on [Harbor](https://github.com/laude-institute/harbor) tasks interactively. Useful for human demonstrations and task authoring.

## Install

```bash
uv tool install --with harbor git+https://github.com/allenai/pier
```

Without Harbor (host mode only, local task paths only): `uv tool install git+https://github.com/allenai/pier`

## Quick start

The examples below use a remote task URL. For local tasks, use `-d` to specify the workspace: `pier start ./tasks/my-task -d ./workspace`

### Container mode

`pier start` builds the task's Docker image (from its `environment/Dockerfile`), starts the container, and bind-mounts a local workspace directory so you can see and edit files from the host.

```bash
pier start https://github.com/laude-institute/harbor#examples/tasks/hello-world --agent claude-code
cd hello-world

pier exec claude        # run the agent — tell it to read .task/instruction.md
pier exec bash          # or drop into the container shell
pier verify             # run the verifier and print the reward
pier stop               # stop the container when done
```

The task instruction is at `.task/instruction.md` in the workspace (available on the host and inside the container). Tell your agent to read it to get started. Skills are installed automatically with `--agent`.

`--agent` is optional — without it you get a plain container to work in manually. It accepts any [Harbor agent name](https://github.com/laude-institute/harbor) (e.g. `claude-code`, `codex`, `gemini-cli`, `goose`) and can be added later with `pier start --agent`. Multiple agents can be installed in the same container. `pier exec` runs any command installed in the container and forwards host env vars matching `*_API_KEY` into the container, so set your agent's API key before running (e.g. `export ANTHROPIC_API_KEY=...`). In container mode, `pier verify` automatically extracts the agent's conversation trajectory.

### Host mode

Work on the host with your editor or coding agent. You must install task dependencies yourself (the container handles this automatically in container mode). Verification still runs in a container for accuracy.

```bash
pier start https://github.com/laude-institute/harbor#examples/tasks/hello-world --host
cd hello-world
cat .task/instruction.md # view the task instruction
pier skills --install    # install skills for your coding agent
claude                   # work on the task — tell it to read .task/instruction.md
pier verify --session-dir ~/.claude/projects/hello-world
                         # run the verifier and include the agent's conversation
```

In host mode, pass `--session-dir` pointing to the agent's local log directory to extract `trajectory.json` (so it appears in `pier view`). The agent is auto-detected from the session directory contents; pass `-a` to override.

### Inspecting results

Each `pier verify` creates a new timestamped trial directory inside the workspace (under `.pier/`), with reward, verifier logs, and optionally the agent's trajectory. Inspect results with Harbor's tools:

```bash
pier view                                      # web dashboard
pier summarize                                 # AI summary
```

## Commands

### `pier start [task_path] [-d <workspace>]`

Launch a workspace for a task. Also restarts stopped containers and installs agents into existing workspaces. `task_path` can be a local directory or a remote git reference (`URL#path`).

```bash
pier start ./tasks/my-task -d ./my-workspace
pier start ./tasks/my-task -d ./my-workspace --host
pier start https://github.com/org/repo#tasks/my-task
pier start --agent claude-code                         # install agent in current workspace
pier start                                             # restart a stopped container
```

- `-d` specifies the workspace directory. Required for local tasks; defaults to `./<task-name>` for remote tasks.
- `--host` skips the container (workspace only).
- `--agent` installs a coding agent. Can be called multiple times to install multiple agents. When `task_path` is omitted, operates on the current workspace.

### `pier exec <command...>`

Run a command in the workspace context. Sets workspace env vars so task CLIs find the right workspace.

```bash
pier exec bash
pier exec claude
```

- **Container mode**: delegates to `docker exec` in the container's working directory
- **Host mode**: runs the command directly with `TASK_WORKSPACE` set

### `pier verify`

Run the verifier and report the reward. Each run creates a new timestamped directory under `<workspace>/.pier/trials/` in Harbor's trial format, so `pier view` and `pier summarize` work on pier output.

```bash
pier verify
```

- **Container mode**: uses Harbor's `Verifier` (same verifier as `harbor run`)
- **Host mode**: spins up a temporary container to run the verifier, then tears it down. Falls back to running `tests/test.sh` locally if Harbor isn't installed.

Trial output includes the reward, verifier test output, and timing. In container mode, the agent's conversation trajectory is extracted automatically when an agent is installed. In host mode, pass `--session-dir` to capture the trajectory:

```bash
pier verify --session-dir ~/.claude/projects/my-task
```

### `pier stop`

Stop the Docker container for the current workspace (container mode only). The workspace directory is preserved. Restart later with `pier start` (no arguments, from inside the workspace).

### `pier list`

Show active workspaces.

```
  Workspace                                Container                      Status
  ──────────────────────────────────────── ────────────────────────────── ────────
  /home/user/hello-world                   —                              —
  /home/user/my-workspace                  pier-my-workspace-a1b2-main-1  running
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

### `pier skills --install`

Install task skills for your coding agent (host mode only). Uses the [Agent Skills](https://agentskills.io) standard (`npx skills add`) which auto-detects installed agents. In container mode with `--agent`, skills are installed automatically.

## Task development

Clone an existing [Harbor task](https://github.com/laude-institute/harbor) as a starting point (hello-world shown here, but any task works). See Harbor's docs for the task directory format.

```bash
git clone https://github.com/laude-institute/harbor
cp -r harbor/examples/tasks/hello-world my-task
```

Edit the task, then iterate with pier:

```bash
pier start ./my-task -d ./workspace --host
cd workspace
cat .task/instruction.md            # view the instruction
# try solving the task yourself...
pier verify                         # run the verifier and check the reward
# edit tests or task setup, re-verify
```

## Development

```bash
git clone https://github.com/allenai/pier && cd pier
make check                      # run tests, lint, and typecheck
uv run pre-commit install       # optional: auto-lint and format on commit
uv tool install --with harbor -e .  # editable install — use `pier` from any directory
```
