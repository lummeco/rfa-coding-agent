"""Workspace settings: `rfa.yaml` in RFA_HOME, merged over the packaged defaults.

One optional file. Anything at the top level applies to every stage; a `planner:`, `coder:` or
`reviewer:` block applies to that one only:

    repos:
      lummeco/lummepro-web: ~/dev/lummepro-web
    apps:
      lummeco/lummepro-web:
        serve: npm run dev -- --port 3000
        port: 3000
    models:
      qwen3.6:
        model_name: ollama_chat/rfa-qwen3.6
    default_model: qwen3.6
    coder:
      environment:
        image: node:22-bookworm
"""

from pathlib import Path

import yaml

from minisweagent.utils.serialize import recursive_merge
from rfa import tasks

CONFIG_DIR = Path(__file__).parent / "config"
STAGES = ("planner", "coder", "reviewer")
DEFAULTS = CONFIG_DIR / "planner.yaml"
# What `reasoning:` may say. litellm turns this into Ollama's `think`, so for a Qwen it is really
# on or off -- only the graded models (gpt-oss) tell the three levels apart. See `model_config`.
REASONING = ("none", "low", "medium", "high")


def path() -> Path:
    return tasks.home() / "rfa.yaml"


def load(stage: str = "planner") -> dict:
    packaged = yaml.safe_load((CONFIG_DIR / f"{stage}.yaml").read_text())
    if not path().exists():
        return packaged
    mine = yaml.safe_load(path().read_text()) or {}
    for_stage = mine.pop(stage, None) or {}
    for other in STAGES:
        mine.pop(other, None)
    return recursive_merge(packaged, mine, for_stage)


MAX_CONTEXT = 3
"""Reference branches a card may carry. Each one is a whole repository in the agent's context; past
a few they stop being a hint and start being noise the model has to read past."""


def repo_paths(config: dict, names: list[str], branches: dict[str, str] | None = None) -> dict[str, tuple[Path, str]]:
    """A task's `repos:` front matter, resolved to local checkouts and the ref each starts from.

    The key the packet must use is the last segment, so `lummeco/lummepro-web` is `lummepro-web/...`
    -- what the coder will see under /work/repos, and what it would type locally.

    The ref is the card's own `branches:` when it named one, and otherwise whatever `repos:` in
    rfa.yaml is pinned to. Picking a branch per card is the point: most work starts from `main`, and
    the work that does not would otherwise have to start from the wrong place.
    """
    configured = config.get("repos") or {}
    resolved = {}
    for name in names:
        if name not in configured:
            raise KeyError(f"`{name}` is not listed under `repos:` in {path()}")
        location, _, pinned = str(configured[name]).partition("@")
        ref = (branches or {}).get(name) or pinned or "HEAD"
        resolved[name.rpartition("/")[2]] = (Path(location).expanduser().resolve(), ref)
    return resolved


def app_specs(config: dict, names: list[str]) -> dict[str, dict]:
    """The `apps:` entries for a task's repositories, keyed the way the container sees them.

    This is what makes review opt-in. A repository nobody has written an `apps:` block for has no
    app the reviewer could start, so a task that touches only those repositories is done when the
    coder is done -- rather than being held in a stage that could never say anything about it.
    """
    configured = config.get("apps") or {}
    return {name.rpartition("/")[2]: dict(configured[name]) for name in names if name in configured}


def container(config: dict, name: str) -> dict:
    """The `containers:` block for a task's primary repository: the image the coder works in, what
    the host sets up in it before the coder starts, and the variables its checks read.

    Without one the coder gets the stage's plain image and builds its own test environment out of
    its budget -- or, more often, does not, and the packet's checks are red before it starts.
    """
    return dict((config.get("containers") or {}).get(name) or {})


def pairs(meta: dict, key: str) -> list[tuple[str, str]]:
    """`branches: [owner/name@branch]` front matter, in the order it was written."""
    items = meta.get(key) or []
    listed = [items] if isinstance(items, str) else items
    found = [tuple(str(item).split("@", 1)) for item in listed if "@" in str(item)]
    return list(dict.fromkeys(found))  # type: ignore[arg-type]


def start_branches(meta: dict) -> dict[str, str]:
    """Where each repository's work begins. One branch per repository: a diff has one base."""
    return dict(pairs(meta, "branches"))


def reference_paths(config: dict, meta: dict) -> dict[str, tuple[Path, str]]:
    """The read-only branches seeded beside the work, keyed by the folder the agent will see.

    Code to look at, not code to change: an implementation on another branch, the repository a
    convention came from. They are never diffed, so nothing an agent does to them can land.
    """
    configured = config.get("repos") or {}
    chosen = {}
    for name, branch in pairs(meta, "context_branches")[:MAX_CONTEXT]:
        if name not in configured:
            raise KeyError(f"`{name}` is not listed under `repos:` in {path()}")
        location = str(configured[name]).partition("@")[0]
        chosen[f"{name.rpartition('/')[2]}@{branch}"] = (Path(location).expanduser().resolve(), branch)
    return chosen


def presets(config: dict) -> dict[str, dict]:
    """The named models under `models:`, which is where every run's model comes from."""
    return config.get("models") or {}


def chosen() -> str:
    """The model `rfa model <name>` (and the menu bar) picked, if anyone has picked one."""
    path = tasks.home() / "var" / "model"
    return path.read_text().strip() if path.exists() else ""


def choose(name: str) -> None:
    (path := tasks.home() / "var" / "model").parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name)


def chosen_reasoning() -> str:
    """The reasoning level `rfa reasoning <level>` (and the menu bar) picked, if anyone has picked one."""
    path = tasks.home() / "var" / "reasoning"
    return path.read_text().strip() if path.exists() else ""


def choose_reasoning(level: str) -> None:
    (path := tasks.home() / "var" / "reasoning").parent.mkdir(parents=True, exist_ok=True)
    path.write_text(level)


def pick(config: dict, name: str = "") -> str:
    """Which preset a run uses, in falling order: what the command said, what `rfa model` picked,
    then the stage's own `default_model`.

    A run never guesses. A name that is not in `models:` is an error rather than a fallback,
    because falling back quietly would plan with one model and code with another.
    """
    available = presets(config)
    if not available:
        raise KeyError(f"no `models:` in {path()}; add one before planning or coding")
    picked = name or chosen() or config.get("default_model") or ""
    if picked not in available:
        raise KeyError(f"`{picked or '(none chosen)'}` is not one of {', '.join(sorted(available))} in {path()}")
    return picked


def for_task(config: dict, meta: dict, model: str = "", reasoning: str = "") -> tuple[str, str]:
    """The model and the reasoning level this card runs with.

    What the command said wins, then what the card says. The preset's own level is left out on
    purpose: it is applied later, by `model_config`, so a card only ever carries a level somebody
    actually chose for it -- otherwise every card would freeze today's default into itself.
    """
    return pick(config, model or str(meta.get("model") or "")), reasoning or str(meta.get("reasoning") or "")


def validate(config: dict, model: str = "", reasoning: str = "") -> None:
    """Refuse a model or a level a run could not use, while the card is still being written.

    The same checks `model_config` makes, but now rather than in half an hour when the daemon picks
    the card up and the person who chose them has gone to lunch.
    """
    if model:
        pick(config, model)
    if reasoning and reasoning not in REASONING:
        raise ValueError(f"reasoning must be one of {', '.join(REASONING)}, not `{reasoning}`")


FORMAT_ERROR = """\
{% if finish_reason is defined and finish_reason == "length" -%}
Your last response ran into the output token limit before it got to a tool call, so nothing ran and
nothing you wrote was kept. Keep the thinking to a few lines and put the bash tool call in every
response.
{%- else -%}
{{ error }}
{%- endif %}"""
"""What the model is told when its response carried no action.

Both model classes answer that with the format rules again, which is the right answer to a model
that wrote the wrong thing and the wrong one to a model that was cut off in the middle of writing
the right thing -- and a local model with thinking on is cut off often. Told the truth it can be
brief; told the rules it writes the same long answer until the run dies of it.
"""


def context_window(config: dict, name: str = "") -> int:
    """How many tokens the chosen preset is served with, from the `ollama:` block `rfa up` uses.

    The number lives nowhere else: `num_ctx` goes into the Modelfile rather than into a call, so a
    run that wants to stay inside its window has to read it where the variant was created.
    """
    return int((presets(config)[pick(config, name)].get("ollama") or {}).get("num_ctx") or 0)


def model_config(config: dict, name: str = "", reasoning: str = "") -> dict:
    """The model config a run hands to `get_model`.

    `reasoning` becomes litellm's `reasoning_effort`, which it turns into Ollama's `think`. Only
    the graded models read the level itself; for a Qwen, `none` is thinking off and the other three
    are all thinking on -- so the level is a dial on the models that have one, and a switch on the
    rest. What the command passes wins, then what `rfa reasoning` picked, then what the preset says.
    """
    preset = dict(presets(config)[pick(config, name)])
    preset.pop("ollama", None)  # `rfa up`'s business: how to serve it, not how to call it
    level = reasoning or chosen_reasoning() or preset.pop("reasoning", "")
    preset.pop("reasoning", None)  # the line above keeps it when the command passed its own
    if level and level not in REASONING:
        raise ValueError(f"reasoning must be one of {', '.join(REASONING)}, not `{level}`")
    effort = {"model_kwargs": {"reasoning_effort": level}} if level else {}
    return recursive_merge({"format_error_template": FORMAT_ERROR}, preset, config.get("model") or {}, effort)
