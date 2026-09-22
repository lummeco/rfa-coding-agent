"""A task is one markdown file. The folder it sits in is its stage, so moving the file moves the task.

That is the whole storage layer: no database, no daemon state, nothing to migrate. You can fix
anything here with `mv` and a text editor, which is the point.

Inside a stage, `status` says what is happening right now and `attempts` counts the retries -- the
two things the board draws. Everything else in the front matter is whoever wrote it's business.
"""

import contextlib
import fcntl
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

STAGES = ("draft", "planning", "todo", "under-work", "review", "done")

# Who may take each step. The one that matters is that no agent can approve its own packet into
# todo: that edge is the human's, and it is the only gate between a draft and a machine writing code.
#
# The reviewer is allowed the edge the coder is not -- `review -> todo` -- because that is the whole
# point of the stage: work that does not do what the packet asked goes back to be coded again,
# without waiting for you. It is still bounded, by the same `attempts` the coder spends.
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("draft", "planning"): {"human"},
    ("draft", "todo"): {"human"},
    ("planning", "todo"): {"human"},
    ("planning", "draft"): {"human"},
    ("todo", "planning"): {"human"},
    ("todo", "under-work"): {"human", "worker"},
    ("under-work", "review"): {"human", "worker"},
    ("under-work", "done"): {"human", "worker"},
    ("under-work", "todo"): {"human", "worker"},
    ("under-work", "planning"): {"human", "worker"},
    ("review", "done"): {"human", "reviewer"},
    ("review", "todo"): {"human", "reviewer"},
    ("review", "planning"): {"human"},
    ("done", "review"): {"human"},
    ("done", "todo"): {"human"},
    ("done", "planning"): {"human"},
}

# The `status` a task takes when it arrives in a stage, unless whoever moved it says otherwise.
# `queued` is the one that matters: a card a human drops into planning is waiting for the planner,
# not being planned, and the board should not claim otherwise until `rfa plan` picks it up.
ON_ARRIVAL = {"planning": "queued", "under-work": "starting", "review": "queued"}

ID_RE = re.compile(r"[0-9a-z][0-9a-z-]{0,79}")
FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


class Loader(yaml.SafeLoader):
    """YAML, except that a timestamp stays the string it was written as.

    `created: 2026-09-20T06:07:10.864Z` is a datetime to PyYAML, which then dumps it back in a
    different format and cannot be put in the board's JSON at all. Front matter is a text format:
    what went in is what comes out.
    """


Loader.yaml_implicit_resolvers = {
    first: [(tag, regex) for tag, regex in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class TransitionError(ValueError):
    """An edge that is not in TRANSITIONS, or an actor not allowed to take it."""


@dataclass
class Task:
    id: str
    stage: str
    path: Path
    meta: dict
    body: str

    @property
    def title(self) -> str:
        """The planner's title once there is one; until then the idea itself, never the raw id."""
        if title := self.meta.get("title"):
            return str(title)
        for line in self.body.splitlines():
            if (text := line.strip()) and not text.startswith("#"):
                return text[:90]
        return self.id

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or self.stage)

    @property
    def attempts(self) -> int:
        return int(self.meta.get("attempts") or 0)

    @property
    def archived(self) -> bool:
        """Hidden from the stage columns, but still in its stage: a flag, not a move."""
        return bool(self.meta.get("archived"))

    def text(self) -> str:
        meta = {k: v for k, v in self.meta.items() if v is not None}
        return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n" + self.body


def home() -> Path:
    return Path(os.environ.get("RFA_HOME") or Path.cwd())


def stage_dir(stage: str) -> Path:
    return home() / "tasks" / stage


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def mint_id(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "task"
    return f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{slug}"


@contextlib.contextmanager
def _lock():
    """One lock for the whole workspace. Moves are rare and instant; contention is not a problem."""
    path = home() / "var" / "rfa.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read(path: Path, stage: str = "") -> Task:
    match = FRONT_MATTER_RE.match(path.read_text())
    if not match:
        raise ValueError(f"{path}: no front matter")
    meta = yaml.load(match.group(1), Loader) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: front matter is not a mapping")
    return Task(path.stem, stage or path.parent.name, path, meta, match.group(2))


def tasks(stage: str = "") -> list[Task]:
    """Every task, or every task in one stage, oldest id first (ids start with their timestamp)."""
    wanted = [stage] if stage else list(STAGES)
    return [
        read(path, name)
        for name in wanted
        for path in sorted(stage_dir(name).glob("*.md"))
        if ID_RE.fullmatch(path.stem)
    ]


def find(id: str) -> Task:
    """A task by id, or by any prefix of one -- the timestamp `rfa ls` prints is enough to type."""
    for stage in STAGES:
        if (path := stage_dir(stage) / f"{id}.md").exists():
            return read(path, stage)
    if len(matches := [t for t in tasks() if t.id.startswith(id)]) == 1:
        return matches[0]
    if matches:
        raise ValueError(f"{id!r} matches {len(matches)}: {', '.join(t.id for t in matches)}")
    raise FileNotFoundError(f"no task {id!r} in {home() / 'tasks'}")


def save(task: Task, **updates) -> Task:
    """Write the task back where it is. Atomic, so a reader never sees half a file."""
    task.meta.update(updates)
    with _lock():
        task.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = task.path.with_name(f".{task.path.name}.tmp{os.getpid()}")
        tmp.write_text(task.text())
        os.replace(tmp, task.path)
    return task


def create(idea: str, repos: list[str] | None = None, **meta) -> Task:
    """A new draft, in the shape Capture writes so the two are interchangeable."""
    id = mint_id(idea.splitlines()[0] if idea.strip() else "task")
    task = Task(
        id=id,
        stage="draft",
        path=stage_dir("draft") / f"{id}.md",
        meta={"status": "draft", "created": now(), "repos": repos or [], **meta},
        body=f"\n# Idea\n\n{idea.strip()}\n",
    )
    save(task)
    log(type="created", id=id)
    return task


def move(task: Task, to: str, actor: str = "human", **updates) -> Task:
    """Move a task to another stage. The rename is the claim: it either happens or it does not."""
    if (allowed := TRANSITIONS.get((task.stage, to))) is None:
        raise TransitionError(f"no such transition: {task.stage} -> {to}")
    if actor not in allowed:
        raise TransitionError(f"{actor!r} may not move {task.stage} -> {to} (allowed: {sorted(allowed)})")
    destination = stage_dir(to) / task.path.name
    if destination.exists():
        raise FileExistsError(destination)
    with _lock():
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(task.path, destination)
    task.path, task.stage = destination, to
    save(task, status=updates.pop("status", ON_ARRIVAL.get(to, to)), **updates)
    log(type="moved", id=task.id, to=to, actor=actor)
    return task


def log(**event) -> None:
    """Append one line to var/events.jsonl. This is the history the board shows for retries."""
    path = home() / "var" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        handle.write(json.dumps({**event, "ts": now()}) + "\n")


def events(id: str = "") -> list[dict]:
    path = home() / "var" / "events.jsonl"
    if not path.exists():
        return []
    found = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [e for e in found if e.get("id") == id] if id else found


def init() -> None:
    """Make the folders. Safe to run again."""
    for stage in STAGES:
        stage_dir(stage).mkdir(parents=True, exist_ok=True)
    (home() / "var").mkdir(parents=True, exist_ok=True)
