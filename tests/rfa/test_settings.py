"""Choosing a model: which preset a run gets, and what reasoning turns into."""

import pytest
import yaml
from jinja2 import StrictUndefined, Template

from rfa import settings, tasks

WORKSPACE = {
    "models": {
        "qwen3.6": {
            "model_name": "ollama_chat/rfa-qwen3.6-35b",
            "ollama": {"name": "x", "from": "y", "num_ctx": 131072},
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


def test_the_window_a_preset_is_served_with_is_what_a_run_can_watch():
    """`num_ctx` is written into the Modelfile, so this is the only place a run can read it."""
    assert settings.context_window(settings.load("planner"), "qwen3.6") == 131072
    assert settings.context_window(settings.load("planner"), "qwen3.8") == 0  # served however it was served


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [("length", "output token limit"), ("tool_calls", "Unknown tool 'edit'.")],
)
def test_a_cut_off_answer_is_told_so_rather_than_told_the_rules_again(finish_reason, expected):
    """Repeating the format rules to a model that was truncated makes it write the same long answer
    until the run dies of it."""
    template = settings.model_config(settings.load("coder"))["format_error_template"]
    rendered = Template(template, undefined=StrictUndefined).render(
        error="Unknown tool 'edit'.", actions=[], finish_reason=finish_reason
    )
    assert expected in rendered


def test_a_reasoning_level_nobody_understands_is_refused():
    with pytest.raises(ValueError):
        settings.model_config(settings.load("planner"), "qwen3.6", "extreme")


def test_the_workspace_default_level_is_used_when_nothing_else_says():
    """`rfa reasoning` picks a level for every run that does not carry one of its own, and it beats
    the preset's own level, which is the model's guess rather than a choice."""
    settings.choose_reasoning("low")
    assert settings.chosen_reasoning() == "low"
    assert settings.model_config(settings.load("planner"), "qwen3.6", "")["model_kwargs"]["reasoning_effort"] == "low"


def test_a_level_someone_asked_for_beats_the_workspace_default():
    settings.choose_reasoning("low")
    assert settings.model_config(settings.load("planner"), "qwen3.6", "high")["model_kwargs"]["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    ("meta", "model", "reasoning", "expected"),
    [
        ({}, "", "", ("qwen3.6", "")),  # nothing chosen: the stage default, and the preset's own level
        ({"model": "qwen3.8", "reasoning": "low"}, "", "", ("qwen3.8", "low")),  # the card's own
        ({"model": "qwen3.8", "reasoning": "low"}, "qwen3.6", "high", ("qwen3.6", "high")),  # the command's
        ({"reasoning": "none"}, "", "", ("qwen3.6", "none")),  # a level without a model is fine
    ],
)
def test_a_card_carries_its_own_model_and_level_until_a_command_says_otherwise(meta, model, reasoning, expected):
    assert settings.for_task(settings.load("planner"), meta, model, reasoning) == expected


def test_an_empty_level_leaves_the_preset_to_decide():
    """`for_task` never invents a level, so a card only pins one somebody chose for it. The preset's
    level is applied later, where changing rfa.yaml still changes what old cards run with."""
    assert settings.for_task(settings.load("planner"), {})[1] == ""
    assert settings.model_config(settings.load("planner"), "qwen3.6", "")["model_kwargs"]["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    ("model", "reasoning"),
    [("qwen9", ""), ("", "extreme"), ("qwen9", "high")],
)
def test_a_card_is_refused_at_capture_rather_than_half_an_hour_later(model, reasoning):
    """The board and the overlay both check here, so a typo comes back while you are still looking."""
    with pytest.raises((KeyError, ValueError)):
        settings.validate(settings.load(), model, reasoning)


def test_validate_passes_what_a_run_would_accept():
    settings.validate(settings.load(), "qwen3.8", "none")
    settings.validate(settings.load(), "", "")  # both blank: the workspace decides
