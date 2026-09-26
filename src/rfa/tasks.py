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
    ("todo", "draft"): {"human"},
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
    ("done", "draft"): {"human"},
}

# The `status` a task takes when it arrives in a stage, unless whoever moved it says otherwise.
# `queued` is the one that matters: a card a human drops into planning is waiting for the planner,
# not being planned, and the board should not claim otherwise until `rfa plan` picks it up.
# `built` in done means working code waiting for your verdict: only you call it shipped or trashed.
ON_ARRIVAL = {"planning": "queued", "under-work": "starting", "review": "queued", "done": "built"}

# Statuses with an agent on the card right now. Such a card cannot go back to draft under the run:
# the run still holds the old path and would write the card back where it was.
RUNNING = {"planning", "starting", "coding", "checking", "reviewing"}

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


class Paused(Exception):
    """The card was paused while an agent was on it. Not an error: the run stops and hands it back."""


class Pausable:
    """An agent that looks at its card before every model call, and stops when you paused it.

    Between calls, not mid-call: what the model is answering now is lost either way, and stopping
    here means the next thing that happens is the host tidying up rather than one more command.
    """

    def __init__(self, *args, task_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.task_id = task_id

    def query(self) -> dict:
        if self.task_id and find(self.task_id).paused:
            raise Paused(self.task_id)
        return super().query()


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

    @property
    def paused(self) -> bool:
        """Left alone by the daemon until resumed. Also a flag, so it survives any move."""
        return bool(self.meta.get("paused"))

    def text(self) -> str:
        meta = {k: v for k, v in self.meta.items() if v is not None}
        return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n" + self.body


def home() -> Path:
    return Path(os.environ.get("RFA_HOME") or Path.cwd())


def stage_dir(stage: str) -> Path:
    return home() / "tasks" / stage


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def mint_id(text: str, at: datetime | None = None) -> str:
    """Timestamp and slug. Given the same `at` and text it is the same id, which is what lets a
    source that may be asked twice -- Sentry, by the daemon and by hand at once -- make one card."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "task"
    return f"{(at or datetime.now(timezone.utc)).astimezone(timezone.utc):%Y%m%d-%H%M%S}-{slug}"


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


def _order_key(task: Task):
    """A card you have placed comes first, in the position you gave it; the ones you have not
    placed yet keep their oldest-id-first order, after the ones you have. A column you have never
    touched therefore looks exactly as it did before."""
    if isinstance(order := task.meta.get("order"), int):
        return (0, order, "")
    return (1, 0, task.path.name)


def tasks(stage: str = "") -> list[Task]:
    """Every task, or every task in one stage.

    The stage's own order: a card's explicit `order` first, in the order you set it, then the
    cards you have not placed yet, oldest id first (ids start with their timestamp)."""
    found: list[Task] = []
    for name in [stage] if stage else STAGES:
        paths = [path for path in sorted(stage_dir(name).glob("*.md")) if ID_RE.fullmatch(path.stem)]
        found.extend(sorted((read(path, name) for path in paths), key=_order_key))
    return found


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


def create(idea: str, repos: list[str] | None = None, at: datetime | None = None, **meta) -> Task:
    """A new draft, in the shape Capture writes so the two are interchangeable.

    Never over an existing card, in any stage: the same id again is the same idea again, and the
    second writer loses rather than the first card.
    """
    id = mint_id(idea.splitlines()[0] if idea.strip() else "task", at)
    if taken := [s for s in STAGES if (stage_dir(s) / f"{id}.md").exists()]:
        raise FileExistsError(f"{id} is already in {taken[0]}")
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
    if to == "draft":
        if task.status in RUNNING:
            raise TransitionError(f"{task.id} is {task.status} right now; pause it first")
        updates = {"paused": None, **updates}
    if actor == "human":
        # Your call is a fresh start: the retries it spent before are not held against it again.
        updates = {"attempts": None, "error": None, **updates}
    if task.stage == "done":
        # Sent back for more work: whatever you said about the old result is not about the next one.
        updates = {"verdict": None, **updates}
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
