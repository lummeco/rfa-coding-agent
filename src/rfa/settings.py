"""Workspace settings: `rfa.yaml` in RFA_HOME, merged over the packaged defaults.

One optional file. Anything at the top level applies to both stages; a `planner:` or `coder:` block
applies to that one only:

    repos:
      lummeco/lummepro-web: ~/dev/lummepro-web
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
STAGES = ("planner", "coder")
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


def repo_paths(config: dict, names: list[str]) -> dict[str, tuple[Path, str]]:
    """A task's `repos:` front matter, resolved to local checkouts.

    The key the packet must use is the last segment, so `lummeco/lummepro-web` is `lummepro-web/...`
    -- what the coder will see under /work/repos, and what it would type locally.
    """
    configured = config.get("repos") or {}
    resolved = {}
    for name in names:
        if name not in configured:
            raise KeyError(f"`{name}` is not listed under `repos:` in {path()}")
        location, _, ref = str(configured[name]).partition("@")
        resolved[name.rpartition("/")[2]] = (Path(location).expanduser().resolve(), ref or "HEAD")
    return resolved


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


def model_config(config: dict, name: str = "", reasoning: str = "") -> dict:
    """The model config a run hands to `get_model`.

    `reasoning` becomes litellm's `reasoning_effort`, which it turns into Ollama's `think`. Only
    the graded models read the level itself; for a Qwen, `none` is thinking off and the other three
    are all thinking on -- so the level is a dial on the models that have one, and a switch on the
    rest. What the command passes wins over what the preset says.
    """
    preset = dict(presets(config)[pick(config, name)])
    preset.pop("ollama", None)  # `rfa up`'s business: how to serve it, not how to call it
    level = reasoning or preset.pop("reasoning", "")
    preset.pop("reasoning", None)  # the line above keeps it when the command passed its own
    if level and level not in REASONING:
        raise ValueError(f"reasoning must be one of {', '.join(REASONING)}, not `{level}`")
    effort = {"model_kwargs": {"reasoning_effort": level}} if level else {}
    return recursive_merge(preset, config.get("model") or {}, effort)
