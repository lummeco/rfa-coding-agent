"""Tests for per-model average tokens-per-second analytics in GLOBAL_MODEL_STATS."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from minisweagent.exceptions import FormatError
from minisweagent.models import GLOBAL_MODEL_STATS
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.test_models import DeterministicModel, make_output


def _mock_litellm_response(completion_tokens: int) -> MagicMock:
    tool_call = MagicMock()
    tool_call.function.name = "bash"
    tool_call.function.arguments = '{"command": "echo test"}'
    tool_call.id = "call_1"
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.tool_calls = [tool_call]
    mock_response.choices[0].message.model_dump.return_value = {"role": "assistant", "content": None}
    mock_response.model_dump.return_value = {}
    mock_response.usage = Mock(completion_tokens=completion_tokens)
    return mock_response


def _bad_tool_call_response(completion_tokens: int) -> MagicMock:
    """Response whose only tool call is not 'bash', so _parse_actions raises FormatError."""
    response = _mock_litellm_response(completion_tokens)
    response.choices[0].message.tool_calls[0].function.name = "unknown_tool"
    return response


def test_two_models_expose_their_own_average_tokens_per_second(reset_global_stats):
    """Each model name exposes total completion tokens / total duration for its own calls only."""
    model_a = LitellmModel(model_name="model-a")
    model_b = LitellmModel(model_name="model-b")

    # time.time is called per query as: start, end (duration), timestamp
    with (
        patch("minisweagent.models.litellm_model.litellm.completion") as mock_completion,
        patch("minisweagent.models.litellm_model.litellm.cost_calculator.completion_cost", return_value=0.001),
        patch("time.time", side_effect=[1000.0, 1001.0, 1001.0, 2000.0, 2004.0, 2004.0]),
    ):
        mock_completion.side_effect = [_mock_litellm_response(100), _mock_litellm_response(200)]
        model_a.query([{"role": "user", "content": "test"}])
        model_b.query([{"role": "user", "content": "test"}])

    stats = GLOBAL_MODEL_STATS.model_stats
    assert set(stats) == {"model-a", "model-b"}
    assert stats["model-a"]["completion_tokens"] == 100
    assert stats["model-a"]["duration"] == 1.0
    assert stats["model-a"]["avg_tokens_per_second"] == 100.0
    assert stats["model-b"]["completion_tokens"] == 200
    assert stats["model-b"]["duration"] == 4.0
    assert stats["model-b"]["avg_tokens_per_second"] == 50.0
    assert GLOBAL_MODEL_STATS.n_calls == 2
    assert GLOBAL_MODEL_STATS.cost == 0.002


def test_average_is_aggregate_ratio_not_mean_of_rates(reset_global_stats):
    """Average is total tokens / total duration (400/4 = 100.0), not the mean of per-call rates (150.0)."""
    model = LitellmModel(model_name="model-x")

    # Call 1: 100 tokens in 1s; call 2: 300 tokens in 3s
    with (
        patch("minisweagent.models.litellm_model.litellm.completion") as mock_completion,
        patch("minisweagent.models.litellm_model.litellm.cost_calculator.completion_cost", return_value=0.001),
        patch("time.time", side_effect=[1000.0, 1001.0, 1001.0, 2000.0, 2003.0, 2003.0]),
    ):
        mock_completion.side_effect = [_mock_litellm_response(100), _mock_litellm_response(300)]
        model.query([{"role": "user", "content": "test"}])
        model.query([{"role": "user", "content": "test"}])

    stats = GLOBAL_MODEL_STATS.model_stats
    assert stats["model-x"]["completion_tokens"] == 400
    assert stats["model-x"]["duration"] == 4.0
    assert stats["model-x"]["avg_tokens_per_second"] == 100.0
    assert stats["model-x"]["avg_tokens_per_second"] != 150.0  # not the mean of (100/1) and (300/3)


def test_format_error_call_is_still_recorded(reset_global_stats):
    """A call that ends in FormatError is still billed and recorded, including tokens and duration."""
    model = LitellmModel(model_name="model-f")

    with (
        patch("minisweagent.models.litellm_model.litellm.completion") as mock_completion,
        patch("minisweagent.models.litellm_model.litellm.cost_calculator.completion_cost", return_value=0.001),
        patch("time.time", side_effect=[1000.0, 1002.0]),
    ):
        mock_completion.return_value = _bad_tool_call_response(100)
        with pytest.raises(FormatError):
            model.query([{"role": "user", "content": "test"}])

    assert GLOBAL_MODEL_STATS.n_calls == 1
    assert GLOBAL_MODEL_STATS.cost == 0.001
    stats = GLOBAL_MODEL_STATS.model_stats
    assert stats["model-f"]["completion_tokens"] == 100
    assert stats["model-f"]["duration"] == 2.0
    assert stats["model-f"]["avg_tokens_per_second"] == 50.0


def test_deterministic_model_zero_duration_reports_zero_average(reset_global_stats):
    """Deterministic test models have zero measured duration and must not raise; average is 0.0."""
    model = DeterministicModel(
        outputs=[make_output("```mswea_bash_command\necho hello\n```", [{"command": "echo hello"}])],
        model_name="deterministic-model",
    )

    result = model.query([{"role": "user", "content": "test"}])
    assert result["content"] == "```mswea_bash_command\necho hello\n```"

    assert GLOBAL_MODEL_STATS.n_calls == 1
    stats = GLOBAL_MODEL_STATS.model_stats
    assert stats["deterministic-model"]["completion_tokens"] == 0
    assert stats["deterministic-model"]["duration"] == 0.0
    assert stats["deterministic-model"]["avg_tokens_per_second"] == 0.0
