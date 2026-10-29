"""A card too big for one coding run, built in parts: small cards coded one after another on one branch.

The planner splits such a packet into `split:` on the card, and nothing more happens until you
approve it: moving the card to `todo` approves every part at once, and the daemon's next tick makes
the parts as cards in `todo`, each `after:` the one before. A part has its tests written when its turn
comes -- against the branch as the parts before it left it, which is why they cannot be written up
front -- then it codes, lands on the card's branch and has its code reviewed. When the last part has
passed, the card itself goes to `review` with the whole branch, has its app driven, and ends in `done`
for your verdict and its pull request like any other card.

What each part did is appended to `var/runs/<card>/progress.md` by the host, from the part's own
summary, and handed to every part after it: how a part learns what the ones before it found.
"""

import subprocess
from pathlib import Path

from rfa import packet, rounds, settings, tasks
from rfa.tasks import Task

PASSED = ("built", "shipped")
COPIED = ("model", "reasoning", "plan_model", "plan_reasoning", "review_model", "context_branches")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def parent(task: Task) -> Task | None:
    return tasks.find(str(task.meta["parent"])) if task.meta.get("parent") else None


def children(task: Task, everything: list[Task] | None = None) -> list[Task]:
    """A card's parts, in order. Read off the parts themselves, so one you deleted by hand is simply gone."""
    found = [t for t in everything or tasks.tasks() if t.meta.get("parent") == task.id]
    return sorted(found, key=lambda t: int(t.meta.get("part") or 0))


def passed(task: Task) -> bool:
    return task.stage == "done" and task.status in PASSED


def finished(task: Task, everything: list[Task] | None = None) -> bool:
    """Has every part of an approved split passed review?"""
    kids = children(task, everything)
    return bool(task.meta.get("split_made")) and bool(kids) and all(passed(k) for k in kids)


def blocked(task: Task, everything: list[Task] | None = None, max_attempts: int = 3) -> str:
    """The part a split card is stuck on, in words, or nothing while its parts are moving."""
    for kid in children(task, everything):
        if kid.status in ("failed", "question") or (kid.stage == "todo" and kid.attempts >= max_attempts):
            return f"Part {kid.meta.get('part')} is {kid.status}: {kid.title}"
    return ""


def due(task: Task) -> bool:
    """Is it this part's turn: its card approved, and the part before it through review?"""
    if (owner := parent(task)) is None or owner.stage != "todo":
        return False
    return not (after := task.meta.get("after")) or passed(tasks.find(str(after)))


def needs_tests(task: Task) -> bool:
    return bool(task.meta.get("parent")) and task.status == "todo" and not task.meta.get("tests_at") and due(task)


def workable(task: Task) -> bool:
    """Can the coder take this todo card now? A split card waits for its parts, a part for its turn and tests."""
    if task.meta.get("split"):
        return finished(task)
    if not task.meta.get("parent"):
        return True
    return bool(task.meta.get("tests_at")) and task.status == "todo" and due(task)


def branch(task: Task) -> str:
    """Where a card's work lands. A part's goes on its card's branch, so the parts make one pull request."""
    return f"rfa/{task.meta.get('parent') or task.id}"


def starts(task: Task) -> dict[str, str]:
    """Where a card's work begins: its `branches:`, except that a part -- or a split card planned again --
    starts each repository a part has already landed in from that branch."""
    owner = parent(task) or task
    if not owner.meta.get("split"):
        return settings.start_branches(task.meta)
    landed = owner.meta.get("landed") or {}
    return settings.start_branches(owner.meta) | {
        name: landed[short] for name in owner.meta.get("repos") or [] if (short := name.rpartition("/")[2]) in landed
    }


def make(task: Task) -> list[Task]:
    """The approved card's parts, as cards in `todo`, each after the one before it -- including any part
    an earlier split of the same card already finished."""
    kids = children(task)
    fields = packet.parse(task.body)
    split = [packet.Part(**part) for part in task.meta["split"]]
    total, after, made = len(kids) + len(split), kids[-1].id if kids else None, []
    for n, part in enumerate(split, len(kids) + 1):
        body = packet.Packet(
            title=part.title,
            goal=f"{part.goal}\n\nPart {n} of {total} of: {task.title}. {fields['goal']}",
            current_behavior=fields["current_behavior"],
            decisions=fields["decisions"],
            acceptance_criteria=part.acceptance_criteria,
            complexity=part.complexity,
            complexity_reason="",
        ).render()
        id = f"{task.id}-p{n}"
        meta = {
            "status": "todo",
            "created": tasks.now(),
            "repos": task.meta.get("repos") or [],
            "parent": task.id,
            "part": n,
            "after": after,
            "title": part.title,
            "complexity": part.complexity,
            "packet_files": part.paths(),
            "planned_at": tasks.now(),
            **{key: task.meta[key] for key in COPIED if task.meta.get(key)},
        }
        made.append(tasks.save(Task(id, "todo", tasks.stage_dir("todo") / f"{id}.md", meta, "\n" + body)))
        tasks.log(type="created", id=id, parent=task.id)
        after = id
    tasks.save(task, status="parts", split_made=True, parts=[t.id for t in kids + made])
    return made


def drop_unstarted(task: Task) -> None:
    """Planning a split card again throws away the parts nothing has happened on yet; finished ones stay."""
    for kid in children(task):
        if kid.stage in ("planning", "todo") and not kid.meta.get("landed") and not kid.attempts:
            kid.path.unlink()
            tasks.log(type="dropped", id=kid.id, parent=task.id)
    if task.meta.get("parts"):
        tasks.save(task, parts=[t.id for t in children(task)] or None)


def review(task: Task, config: dict) -> Task:
    """Send a card whose parts have all passed to `review`, with the whole branch as its one original round."""
    landed = task.meta.get("landed") or {}
    repos = settings.repo_paths(config, task.meta.get("repos") or [], settings.start_branches(task.meta))
    kids = children(task)
    bases = {name: ref for name, (_, ref) in repos.items()}
    for kid in reversed(kids):
        bases |= rounds.first_bases(rounds.load(kid.id))
    output = rounds.round_dir(task.id, 0)
    output.mkdir(parents=True, exist_ok=True)
    commits = {}
    for name, landed_branch in landed.items():
        head = git(repos[name][0], "rev-parse", landed_branch).strip()
        (output / f"{name}.patch").write_text(git(repos[name][0], "diff", "--binary", bases[name], head))
        commits[name] = {"branch": landed_branch, "base": bases[name], "head": head}
    rounds.record(
        task.id,
        {
            "n": 0,
            "kind": "original",
            "finished": tasks.now(),
            "landed": True,
            "plan": task.body,
            "summary": f"Built in {len(kids)} parts: " + "; ".join(k.title for k in kids),
            "comments": [],
            "outside": {},
            "commits": commits,
            "tests": None,
            "error": None,
        },
        answered=[],
    )
    task = tasks.move(task, "under-work", actor="worker")
    return tasks.move(task, "review", actor="worker", run=str(output), landed=landed, error=None)


def advance(config: dict) -> list[str]:
    """What the daemon does for split cards on every tick: make the parts of a card you approved, and
    send a card whose last part has passed to `review`. A card that has been reviewed once is coded
    from then on like any other: its fix rounds land on the same branch."""
    said = []
    for task in tasks.tasks("todo"):
        if not task.meta.get("split") or task.paused:
            continue
        if not task.meta.get("split_made"):
            said.append(f"{task.id} approved in {len(make(task))} parts")
        elif finished(task) and not rounds.load(task.id)["rounds"]:
            said.append(f"{review(task, config).id}: every part passed, the whole card goes to review")
    return said


def progress_path(task: Task) -> Path:
    return rounds.run_dir(str(task.meta.get("parent") or task.id)) / "progress.md"


def progress(task: Task) -> str:
    """What the parts of a card wrote as they landed, newest last. Empty for a card never split."""
    path = progress_path(task)
    return path.read_text()[-8000:] if path.exists() else ""


def note(task: Task, written: dict) -> None:
    """Append what a part that just landed wrote about itself to its card's progress."""
    path = progress_path(task)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"## Part {task.meta.get('part')}: {task.title}\n\n{written.get('summary') or ''}\n\n{written.get('notes') or ''}".strip()
    path.write_text((path.read_text() if path.exists() else "") + entry + "\n\n")


def handoff(task: Task, config: dict) -> str:
    """What a part is told about the work around it: the parts, what the earlier ones wrote, and their commits."""
    if (owner := parent(task)) is None:
        return ""
    listed = "\n".join(
        f"{k.meta.get('part')}. {k.title} -- {'this part' if k.id == task.id else 'done' if passed(k) else 'later'}"
        for k in children(owner)
    )
    landed, log = owner.meta.get("landed") or {}, []
    for name, (path, ref) in settings.repo_paths(
        config, owner.meta.get("repos") or [], settings.start_branches(owner.meta)
    ).items():
        if name in landed:
            log.append(f"# {name}\n" + git(path, "log", "--stat", "--format=%n%s", f"{ref}..{landed[name]}").strip())
    commits = "\n\n".join(log)[-6000:]
    return (
        f"This card, **{owner.title}**, is built in parts, one coding run each, one after another on one "
        f"branch. Do this part and only this part; later parts are someone else's.\n\n{listed}\n\n"
        + (f"### What the parts before this one wrote\n\n{progress(task)}\n\n" if progress(task) else "")
        + (f"### Their commits\n\n```\n{commits}\n```\n" if commits else "")
    ).strip()
