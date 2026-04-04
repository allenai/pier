"""Trajectory assembler for pier verify.

Writes a Harbor-compatible trial directory so ``pier view`` and
``pier summarize`` work on pier output.

Harbor already writes (in container mode):
  - verifier/reward.json (or reward.txt)
  - verifier/test-stdout.txt (combined stdout+stderr from test.sh)
  - agent/, verifier/, artifacts/ directories

This module adds:
  - result.json — Harbor TrialResult when possible, minimal fallback otherwise
    (e.g. retroactive capture outside a complete task directory)
  - config.json — trial config metadata via harbor_bridge.write_trial_config_json()
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pier import harbor_bridge

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

    # result.json
    try:
        result_json = harbor_bridge.build_trial_result_json(
            task_dir,
            task_ref,
            session_name,
            reward,
            start_time=start,
            end_time=end,
            agent_name=agent_name,
            agent_context=agent_context,
        )
    except Exception as exc:
        # Fallback when task dir is incomplete (e.g. retroactive capture)
        logger.debug("Harbor TrialResult construction failed, using fallback: %s", exc)
        result: dict = {
            "task_name": task_ref,
            "trial_name": session_name,
            "reward": reward.get("reward"),
            "rewards": reward,
            "started_at": start.isoformat(),
            "finished_at": end.isoformat(),
        }
        if agent_name:
            result["agent_info"] = {"name": agent_name}
        if agent_context:
            result["agent_context"] = agent_context
        result_json = json.dumps(result, indent=2)
    (trial_dir / "result.json").write_text(result_json)

    # config.json
    harbor_bridge.write_trial_config_json(trial_dir, task_dir, session_name, agent_name)

    # verifier/reward.json — write only if Harbor didn't already
    reward_file = trial_dir / "verifier" / "reward.json"
    if not reward_file.exists():
        reward_file.write_text(json.dumps(reward, indent=2))
