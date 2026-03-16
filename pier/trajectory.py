"""Trajectory assembler for pier verify.

Writes a Harbor-compatible trial directory so ``pier view`` and
``pier summarize`` work on pier output.

Harbor already writes (in container mode):
  - verifier/reward.json (or reward.txt)
  - verifier/test-stdout.txt (combined stdout+stderr from test.sh)
  - agent/, verifier/, artifacts/ directories

This module adds:
  - result.json — Harbor TrialResult (when Harbor is installed) or
    pier's own simplified format (without Harbor)
  - config.json — task reference, pier version
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def assemble_trial(
    trial_dir: Path,
    task_dir: Path,
    task_ref: str,
    session_name: str,
    reward: dict,
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    agent_name: str | None = None,
    agent_context: dict | None = None,
) -> None:
    """Write trial metadata to a trial directory.

    When Harbor is installed, writes a Harbor-compatible TrialResult so
    ``pier view`` and ``pier summarize`` work on pier output.
    Falls back to pier's own simplified format without Harbor.

    Args:
        trial_dir: Root directory for this trial.
        task_dir: Path to the task directory.
        task_ref: Task reference string (e.g. "hello-world").
        session_name: Pier session name.
        reward: Reward dict from verifier (e.g. {"reward": 0.75}).
        start_time: When pier verify started.
        end_time: When pier verify finished.
        agent_name: Agent name (e.g. "claude-code").
        agent_context: Agent usage from Harbor (cost_usd, tokens, etc.).
    """
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "agent").mkdir(exist_ok=True)
    (trial_dir / "verifier").mkdir(exist_ok=True)
    (trial_dir / "artifacts").mkdir(exist_ok=True)

    now = datetime.now(timezone.utc)
    start = start_time or now
    end = end_time or now

    # result.json — Harbor TrialResult when possible
    _write_result_json(
        trial_dir,
        task_dir,
        task_ref,
        session_name,
        reward,
        start,
        end,
        agent_name,
        agent_context,
    )

    # config.json — Harbor-compatible TrialConfig so harbor tools work on pier output
    _write_config_json(trial_dir, task_dir, task_ref, session_name, agent_name)

    # verifier/reward.json — write only if Harbor didn't already
    reward_file = trial_dir / "verifier" / "reward.json"
    if not reward_file.exists():
        reward_file.write_text(json.dumps(reward, indent=2))


def _write_config_json(
    trial_dir: Path,
    task_dir: Path,
    task_ref: str,
    session_name: str,
    agent_name: str | None,
) -> None:
    """Write config.json as a Harbor TrialConfig so Harbor tools can read it.

    Skips writing if Harbor isn't installed — Harbor tools that read
    config.json already handle the missing-file case gracefully.
    """
    try:
        from harbor.models.trial.config import AgentConfig, TaskConfig, TrialConfig

        kwargs: dict = dict(task=TaskConfig(path=task_dir), trial_name=session_name)
        if agent_name:
            kwargs["agent"] = AgentConfig(name=agent_name)
        trial_config = TrialConfig(**kwargs)
        (trial_dir / "config.json").write_text(trial_config.model_dump_json(indent=2))
    except Exception as e:
        logger.debug("Skipping config.json (Harbor not available): %s", e)


def _write_result_json(
    trial_dir: Path,
    task_dir: Path,
    task_ref: str,
    session_name: str,
    reward: dict,
    start_time: datetime,
    end_time: datetime,
    agent_name: str | None,
    agent_context: dict | None,
) -> None:
    """Write result.json — Harbor-compatible when Harbor is installed."""
    try:
        from pier.harbor_bridge import build_trial_result_json

        result_json = build_trial_result_json(
            task_dir,
            task_ref,
            session_name,
            reward,
            start_time=start_time,
            end_time=end_time,
            agent_name=agent_name,
            agent_context=agent_context,
        )
        (trial_dir / "result.json").write_text(result_json)
        return
    except Exception as e:
        logger.debug("Harbor TrialResult construction failed, using fallback: %s", e)

    # Fallback: pier's own format (readable but not Harbor-compatible)
    result: dict = {
        "task_name": task_ref,
        "trial_name": session_name,
        "reward": reward.get("reward"),
        "rewards": reward,
        "started_at": start_time.isoformat(),
        "finished_at": end_time.isoformat(),
    }
    if agent_name:
        result["agent_info"] = {"name": agent_name}
    if agent_context:
        result["agent_context"] = agent_context
    (trial_dir / "result.json").write_text(json.dumps(result, indent=2))
