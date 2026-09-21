"""Choosing a model: which preset a run gets, and what reasoning turns into."""

import pytest
import yaml

from rfa import settings, tasks

WORKSPACE = {
    "models": {
        "qwen3.6": {
            "model_name": "ollama_chat/rfa-qwen3.6-35b",
            "ollama": {"name": "x", "from": "y"},
            "reasoning": "high",
        },
        "qwen3.8": {"model_name": "ollama_chat/rfa-qwen3.8", "reasoning": "none"},
    },
    "default_model": "qwen3.6",
    "model": {"cost_tracking": "ignore_errors"},
    "coder": {"default_model": "qwen3.8"},
}


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    (tmp_path / "rfa.yaml").write_text(yaml.safe_dump(WORKSPACE))
    return tmp_path


def test_each_stage_can_default_to_a_different_model():
    """Planning is short and wants to think; a long coding run often does not."""
    assert settings.pick(settings.load("planner")) == "qwen3.6"
    assert settings.pick(settings.load("coder")) == "qwen3.8"


def test_what_the_command_says_beats_what_was_picked_beats_the_default():
    settings.choose("qwen3.8")
    assert settings.pick(settings.load("planner")) == "qwen3.8"
    assert settings.pick(settings.load("planner"), "qwen3.6") == "qwen3.6"


@pytest.mark.parametrize("name", ["qwen9", "", "QWEN3.6"])
def test_a_model_that_is_not_listed_is_refused_rather_than_guessed(name):
    """Falling back quietly would plan with one model and code with another."""
    config = settings.load("planner") | {"default_model": name}
    with pytest.raises(KeyError):
        settings.pick(config, name)


def test_the_serving_details_never_reach_the_model_call():
    """`ollama:` says how `rfa up` serves the model; `get_model` must not see it."""
    config = settings.model_config(settings.load("planner"))
    assert "ollama" not in config and "reasoning" not in config
    assert config["model_name"] == "ollama_chat/rfa-qwen3.6-35b"
    assert config["cost_tracking"] == "ignore_errors"  # top-level `model:` still applies


@pytest.mark.parametrize(
    ("name", "asked", "effort"),
    [
        ("qwen3.6", "", "high"),  # the preset's own level
        ("qwen3.8", "", "none"),
        ("qwen3.6", "low", "low"),  # the command overrides it
        ("qwen3.8", "high", "high"),
    ],
)
def test_reasoning_becomes_litellms_reasoning_effort(name, asked, effort):
    assert settings.model_config(settings.load("planner"), name, asked)["model_kwargs"]["reasoning_effort"] == effort


def test_a_reasoning_level_nobody_understands_is_refused():
    with pytest.raises(ValueError):
        settings.model_config(settings.load("planner"), "qwen3.6", "extreme")
