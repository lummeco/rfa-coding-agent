"""Workspace settings: `rfa.yaml` in RFA_HOME, merged over the packaged defaults.

One optional file. Anything at the top level applies to both stages; a `planner:` or `coder:` block
applies to that one only:

    repos:
      lummeco/lummepro-web: ~/dev/lummepro-web
    model:
      model_name: ollama_chat/qwen3-coder:30b
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
