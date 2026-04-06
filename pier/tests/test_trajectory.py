"""Tests for pier.trajectory — trial result assembly."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pier.trajectory import assemble_trial


class TestAssembleTrial:
    @staticmethod
    def _make_task(tmp_path: Path) -> Path:
        task = tmp_path / "task"
        task.mkdir()
        (task / "task.toml").write_text(
            '[metadata]\nauthor_name = "test"\n[environment]\n[verifier]\n[agent]\n'
        )
        (task / "instruction.md").write_text("Test instruction")
        return task

    def _call(self, trial_dir: Path, task_dir: Path, **kwargs):
        defaults = {
            "task_ref": "my-task",
            "session_name": "s",
            "reward": {"reward": 0.75},
            "start_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "end_time": datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
        }
        defaults.update(kwargs)
        assemble_trial(trial_dir, task_dir, **defaults)  # type: ignore[arg-type]

    def test_creates_directory_structure(self, tmp_path: Path):
        trial = tmp_path / "trial"
        task = self._make_task(tmp_path)
        self._call(trial, task)

        assert (trial / "agent").is_dir()
        assert (trial / "verifier").is_dir()
        assert (trial / "artifacts").is_dir()
        assert (trial / "result.json").exists()
        assert (trial / "config.json").exists()

    def test_config_json_is_valid_trial_config(self, tmp_path: Path):
        """config.json must be parseable by Harbor's TrialConfig."""
        trial = tmp_path / "trial"
        task = self._make_task(tmp_path)
        self._call(trial, task)

        from harbor.models.trial.config import TrialConfig

        config = TrialConfig.model_validate_json((trial / "config.json").read_text())
        assert config.trial_name == "s"
        assert config.task.path == task

    def test_config_json_includes_agent(self, tmp_path: Path):
        trial = tmp_path / "trial"
        task = self._make_task(tmp_path)
        self._call(trial, task, agent_name="claude-code")

        from harbor.models.trial.config import TrialConfig

        config = TrialConfig.model_validate_json((trial / "config.json").read_text())
        assert config.agent.name == "claude-code"

    def test_writes_verifier_reward_json(self, tmp_path: Path):
        trial = tmp_path / "trial"
        task = self._make_task(tmp_path)
        self._call(trial, task, reward={"reward": 0.9, "accuracy": 0.85})

        reward = json.loads((trial / "verifier" / "reward.json").read_text())
        assert reward["reward"] == 0.9
        assert reward["accuracy"] == 0.85

    def test_does_not_overwrite_existing_reward(self, tmp_path: Path):
        trial = tmp_path / "trial"
        task = self._make_task(tmp_path)
        (trial / "verifier").mkdir(parents=True)
        (trial / "verifier" / "reward.json").write_text('{"reward": 1.0}')

        self._call(trial, task, reward={"reward": 0.5})

        reward = json.loads((trial / "verifier" / "reward.json").read_text())
        assert reward["reward"] == 1.0  # original preserved


class TestResultJsonHarbor:
    """When Harbor is installed, result.json uses Harbor's TrialResult format."""

    def test_harbor_format(self, tmp_path: Path):
        trial = tmp_path / "trial"
        task = tmp_path / "task"
        task.mkdir()
        (task / "task.toml").write_text(
            '[metadata]\nauthor_name = "test"\n[environment]\n[verifier]\n[agent]\n'
        )
        (task / "instruction.md").write_text("Test")

        fake_json = json.dumps(
            {
                "task_name": "my-task",
                "trial_name": "s",
                "verifier_result": {"rewards": {"reward": 0.75}},
            }
        )
        with patch(
            "pier.harbor_bridge.build_trial_result_json",
            return_value=fake_json,
        ) as mock_build:
            assemble_trial(
                trial,
                task,
                "my-task",
                "s",
                {"reward": 0.75},
                start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                end_time=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
                agent_name="claude-code",
            )

        mock_build.assert_called_once()
        result = json.loads((trial / "result.json").read_text())
        assert result["task_name"] == "my-task"
        assert result["verifier_result"]["rewards"]["reward"] == 0.75

    def test_falls_back_when_harbor_result_build_fails(self, tmp_path: Path):
        trial = tmp_path / "trial"
        task = tmp_path / "task"
        task.mkdir()

        with patch(
            "pier.harbor_bridge.build_trial_result_json",
            side_effect=RuntimeError("missing Harbor task metadata"),
        ):
            assemble_trial(
                trial,
                task,
                "retro-task",
                "retro-session",
                {"reward": 0.0, "accuracy": 1.0},
                start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                end_time=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
                agent_name="codex",
                agent_context={"cost_usd": 1.25},
            )

        result = json.loads((trial / "result.json").read_text())
        assert result["task_name"] == "retro-task"
        assert result["trial_name"] == "retro-session"
        assert result["reward"] == 0.0
        assert result["rewards"]["accuracy"] == 1.0
        assert result["agent_info"]["name"] == "codex"
        assert result["agent_context"]["cost_usd"] == 1.25

    def test_fallback_still_writes_trial_config(self, tmp_path: Path):
        trial = tmp_path / "trial"
        task = tmp_path / "task"
        task.mkdir()

        with patch(
            "pier.harbor_bridge.build_trial_result_json",
            side_effect=RuntimeError("missing Harbor task metadata"),
        ):
            assemble_trial(
                trial,
                task,
                "retro-task",
                "retro-session",
                {"reward": 0.5},
            )

        assert (trial / "config.json").exists()
