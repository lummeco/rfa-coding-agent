"""`rfa daemon`: the loop that pulls cards into the planner, the coder and the reviewer on its own.

It is the three commands you would type -- `rfa plan`, `rfa work` and `rfa review` -- on a timer.
`rfa plan` is two runs on one card: the packet, then the tests written from it before any code.
Anything it does you can still do by hand, and stopping it stops nothing else. What it adds is the
gates: a laptop on a draining battery or short of memory should not start a container, and two runs
must never overlap, because every stage drives the same local model and two agent calls at once
split its memory and thrash the GPU.

The human gate is untouched. The daemon plans cards you moved into `planning`, codes cards you
approved into `todo`, and reviews what the coder landed; it never moves a packet from planning to
todo. That edge is yours, and it is the only thing standing between an idea and a machine writing
code for it. The reviewer's own edge -- back to `todo` when the app does not do what was asked --
is not that gate: it can only return work to be redone, never approve it in the first place.

Planning goes first, because it is short, bounded and it is what puts a packet in front of you --
until `plan_ahead` cards of work are already waiting, at which point the coder is the bottleneck and
another packet would only queue behind it.

A card too big for one run is built in parts (see parts.py). Each tick makes the parts of a split card
you approved and sends one whose parts have all passed on to review; a part has its tests written when
its turn comes, and is coded once they are.
"""

import signal
import time
from dataclasses import dataclass, field

from rfa import gates, parts, planner, service, settings, tasks
from rfa.tasks import Task


@dataclass
class DaemonConfig:
    """`daemon:` in rfa.yaml. Everything the loop decides with, and nothing it does not."""

    interval: int = 15
    """Seconds between looks at the board."""
    battery_min: int = 50
    """On battery, the charge below which nothing starts. Wall power ignores it."""
    require_power: bool = False
    """Start runs only while plugged in, whatever the charge."""
    memory_min_pct: int = 30
    """Free memory a run needs. Memory pressure above normal closes the gate whatever this says."""
    keep_awake: bool = True
    """Hold `caffeinate -is` for a run, so the Mac does not sleep in the middle of one."""
    plan_ahead: int = 2
    """Stop planning once this many cards of work are waiting for you or for the coder."""
    max_attempts: int = 3
    """A todo card that has come back this many times is left alone until you look at it."""
    error_backoff: int = 300
    """After a run fails on something underneath it -- Docker gone, the model unreachable -- wait
    this long before starting another. Hitting a broken machine every 15 seconds helps nobody."""
    fetch_interval: int = 600
    """Seconds between fetching every repository under `repos:`, so an offline run starts from a
    recent origin rather than from whenever a card last happened to be planned. 0 turns it off."""
    plan: bool = True
    work: bool = True
    review: bool = True

    @classmethod
    def load(cls) -> "DaemonConfig":
        return cls(**(settings.load().get("daemon") or {}))


def running() -> list[Task]:
    """Cards with a run on them right now: anything in under-work, and a card an agent holds -- a part
    has its tests written in todo."""
    return (
        tasks.tasks("under-work")
        + [t for t in tasks.tasks("planning") if t.status in ("planning", "writing-tests")]
        + [t for t in tasks.tasks("review") if t.status == "reviewing"]
        + [t for t in tasks.tasks("todo") if t.status == "writing-tests"]
    )


def reclaim() -> list[str]:
    """Cards left mid-run by a daemon that is no longer here: a crash, an `rfa restart`, a Mac that slept.

    Startup only, where `rfa up` has already refused to start a second daemon -- so a card in a
    running state has nobody on it by definition. The coding attempt it spent is kept: the run really
    did happen, it just did not finish, and a card that cannot survive its third interruption is one
    you should see rather than one the daemon should keep retrying.
    """
    reclaimed = [
        tasks.move(t, "todo", actor="worker", status="todo", error="interrupted").id
        for t in tasks.tasks("under-work")
    ]
    reclaimed += [
        tasks.save(t, status="queued").id for t in tasks.tasks("planning") if t.status in ("planning", "writing-tests")
    ]
    reclaimed += [tasks.save(t, status="queued").id for t in tasks.tasks("review") if t.status == "reviewing"]
    reclaimed += [tasks.save(t, status="todo").id for t in tasks.tasks("todo") if t.status == "writing-tests"]
    for id in reclaimed:
        tasks.log(type="reclaimed", id=id)
    return reclaimed


def reviewable(task: Task) -> bool:
    """Is there landed work on this card for the reviewer to read?

    A card can reach `review` without any -- you can move it there by hand -- and the daemon has to
    leave that card alone rather than pick it up every fifteen seconds to fail on it again.
    """
    return bool(task.meta.get("landed"))


def planning_job(task: Task) -> str:
    """What a queued planning card waits for: its packet, or the tests written from the packet it has.

    A card with tests already written that is queued again -- dragged back into planning -- is
    planned again from the start, as it always was."""
    return "tests" if task.meta.get("planned_at") and not task.meta.get("tests_at") else "plan"


def urgent(task: Task) -> bool:
    """Marked to be done now, itself or as a part of a card that is."""
    return bool(task.meta.get("now") or ((owner := parts.parent(task)) and owner.meta.get("now")))


def job_for(task: Task, config: DaemonConfig) -> str | None:
    """The job one card is waiting for, or nothing while it waits on you."""
    if task.stage == "review":
        return "review" if config.review and task.status == "queued" and reviewable(task) else None
    if task.stage == "planning":
        return planning_job(task) if config.plan and task.status == "queued" else None
    if task.stage != "todo":
        return None
    if parts.needs_tests(task):
        return "tests" if config.plan else None
    return "work" if config.work and task.attempts < config.max_attempts and parts.workable(task) else None


def lined_up() -> list[Task]:
    """The cards you put in line for the desk, in your order -- whatever stage each has reached."""
    line = [t for t in tasks.tasks() if isinstance(t.meta.get("desk"), int) and not t.archived]
    return sorted(line, key=lambda t: t.meta["desk"])


def line_up(task: Task, index: int | None) -> Task:
    """Put a card at `index` in your line for the desk, or take it out of the line.

    The line is yours: the daemon works it first, in order, and goes back to its own order when the
    line has nothing it can do. A card that stops for you keeps its place, so it is next again the
    moment you answer. A draft put in line is handed to the planner, which is what being in line means.
    """
    if index is not None and task.stage == "draft":
        if not task.meta.get("repos"):
            raise ValueError("this draft names no repository; pick one before giving it to the desk")
        task = tasks.plan_now(task)
    line = [t for t in lined_up() if t.id != task.id]
    if index is None:
        tasks.save(task, desk=None)
    else:
        line.insert(index, task)
    for n, other in enumerate(line):
        if other.meta.get("desk") != n:
            tasks.save(other, desk=n)
    return tasks.find(task.id)


def do_now(task: Task) -> Task:
    """Put one card ahead of everything, and have whatever is running step aside for it.

    One card at a time: marking another takes the mark off the first. A run that steps aside goes
    to the front of your line, not held, so it is what the machine takes up again once this card is
    finished or waiting on you -- and a coding run carries on from the changes it had made.
    """
    for other in tasks.tasks():
        if other.meta.get("now") and other.id != task.id:
            tasks.save(other, now=None)
    for other in running():
        if other.id != task.id and other.meta.get("parent") != task.id:
            line_up(tasks.save(other, aside=True), 0)
    tasks.log(type="now", id=task.id)
    return tasks.save(tasks.plan_now(task) if task.stage == "draft" else task, now=True, paused=None, desk=None)


def next_job(config: DaemonConfig) -> tuple[str, Task] | None:
    """The card to run next and what to run on it, or nothing to do.

    Reviewing comes first: a card in `review` is work that is already finished and only waiting to
    be told whether it counts. Starting another coding run ahead of it spends the machine on a
    fourth unfinished thing while three finished ones say nothing.

    A paused card is not there at all, and does not count as work waiting either. Nor is an
    archived one in planning: it was put away, and planning it would only send it on towards code.

    `plan_ahead` only ever holds planning back in favour of a coding run that can actually start:
    with nothing to code there is nothing to yield to, and the queue would sit there all night.
    """
    live = {
        stage: [t for t in tasks.tasks(stage) if not t.paused and not (t.archived and stage == "planning")]
        for stage in ("planning", "todo", "review")
    }
    # A card you said to do now is ahead of everything, reviews included, whatever it needs next.
    for task in (t for stage in live.values() for t in stage if urgent(t)):
        if job := job_for(task, config):
            return job, task
    # Then your own line, in your order: the first card in it the machine can do something for.
    for task in lined_up():
        if not task.paused and (job := job_for(task, config)):
            return job, task
    reviews = [t for t in live["review"] if t.status == "queued" and reviewable(t)] if config.review else []
    if reviews:
        return "review", reviews[0]
    # Tests for a packet you have already seen come before a new packet: that card is nearly in front of you.
    plans = [t for t in live["planning"] if t.status == "queued"] if config.plan else []
    plans.sort(key=lambda t: planning_job(t) != "tests")
    # A part whose turn has come has its tests written before anything else is coded: it is next.
    if config.plan and (due := [t for t in live["todo"] if parts.needs_tests(t)]):
        return "tests", due[0]
    work = (
        [t for t in live["todo"] if t.attempts < config.max_attempts and parts.workable(t)] if config.work else []
    )
    # A split card counts once: its parts are one piece of work waiting, not six.
    waiting = sum(not t.meta.get("parent") for t in live["todo"]) + sum(t.status == "ready" for t in live["planning"])
    if plans and (not work or waiting < config.plan_ahead):
        return planning_job(plans[0]), plans[0]
    return ("work", work[0]) if work else None


def run(job: tuple[str, Task], config: DaemonConfig) -> None:
    # Imported here, not at the top: these pull in the whole agent harness, and the board and
    # `rfa status` import this module only to ask what it would do next.
    from rfa.planner import plan_task
    from rfa.reviewer import review_task
    from rfa.scout import scout_task
    from rfa.tester import write_tests
    from rfa.worker import run_task

    kind, task = job
    # Told to step aside for a run that has since ended: this is its own turn again.
    if task.meta.get("aside"):
        task = tasks.save(task, aside=None)
    with gates.KeepAwake(config.keep_awake):
        if kind == "plan" and task.meta.get("scout"):
            scout_task(task, settings.load("scout"))
        elif kind == "plan":
            plan_task(task, settings.load("planner"))
        elif kind == "tests":
            write_tests(task, settings.load("tester"))
        elif kind == "review":
            review_task(task, settings.load("reviewer"))
        else:
            run_task(task, settings.load("coder"))


def finished() -> Task | None:
    """The run that ended most recently -- what the menu bar announces when it changes."""
    done = [t for t in tasks.tasks("done") if t.meta.get("finished_at")]
    return max(done, key=lambda t: str(t.meta["finished_at"]), default=None)


def report() -> dict:
    """One answer to "what is going on", for `rfa status`, the board and the menu bar alike.

    One function, so the three of them can never tell you different things about the same machine.
    """
    config, workspace = DaemonConfig.load(), settings.load()
    job, last = next_job(config), finished()
    # What is configured, not what a run would insist on: this must answer even when `models:`
    # is empty, where `settings.pick` is right to refuse.
    model = settings.chosen() or workspace.get("default_model") or ""

    def task_model(t: Task) -> str:
        # A run uses the card's own model when it named one, and the daemon's otherwise. A card
        # still to be planned runs with its planning model.
        planning = t.stage in ("draft", "planning")
        return str((planning and t.meta.get("plan_model")) or t.meta.get("model") or "") or model

    return {
        "home": str(tasks.home()),
        "model": model,
        "models": sorted(settings.presets(workspace)),
        "reasoning": list(settings.REASONING),
        "default_reasoning": settings.chosen_reasoning(),
        "run_with": settings.run_with(),
        "repos": sorted(workspace.get("repos") or {}),
        "board_url": service.url(),
        # 0 rather than null: not running is a state, not missing information.
        "services": {s.name: s.pid() or 0 for s in service.services()},
        "gates": [vars(gate) for gate in gates.check(config, running())],
        "stages": {stage: len(tasks.tasks(stage)) for stage in tasks.STAGES},
        "running": [
            {"id": t.id, "title": t.title, "status": t.status, "model": task_model(t)} for t in running()
        ],
        "next": (
            {"job": job[0], "id": job[1].id, "title": job[1].title, "model": task_model(job[1])}
            if job
            else None
        ),
        "last": None
        if last is None
        else {"id": last.id, "title": last.title, "status": last.status, "at": last.meta["finished_at"]},
    }


def terminate(*_) -> None:
    """SIGTERM has to unwind rather than stop the process where it stands: `rfa down` in the middle
    of a coding run must still reach the `finally` that removes the container."""
    raise SystemExit(0)


@dataclass
class Daemon:
    config: DaemonConfig = field(default_factory=DaemonConfig.load)
    paused_until: float = 0.0
    fetch_due: float = 0.0
    said: str = ""

    def say(self, line: str) -> None:
        """One line per change of mind. A daemon that repeats itself every 15 seconds has no log."""
        if line != self.said:
            print(f"{tasks.now()}  {line}", flush=True)
            self.said = line

    def fetch(self) -> None:
        """Every repository from origin, once `fetch_interval` has passed. Offline is not an error:
        the runs fall back to the last fetch, and this only says which repositories it could not reach."""
        if not self.config.fetch_interval or time.monotonic() < self.fetch_due:
            return
        self.fetch_due = time.monotonic() + self.config.fetch_interval
        config = settings.load()
        if failed := planner.refresh(settings.repo_paths(config, list(config.get("repos") or {}))):
            tasks.log(type="fetch_failed", repos=failed)
            print(f"{tasks.now()}  could not fetch {', '.join(failed)}; runs start from the last fetch", flush=True)

    def tick(self) -> None:
        """One look at the board. Runs at most one card, and only with every gate open."""
        self.fetch()
        try:
            for line in parts.advance(settings.load()):
                print(f"{tasks.now()}  {line}", flush=True)
        except Exception as e:
            # A card whose body no longer parses is that card's problem; planning and coding go on.
            tasks.log(type="parts_error", error=str(e))
            self.say(f"parts: {e}")
        if (left := self.paused_until - time.monotonic()) > 0:
            return self.say(f"paused for {left / 60:.0f} more min after the last error")
        if (job := next_job(self.config)) is None:
            return self.say("nothing to do")
        kind, task = job
        if blocked := [g for g in gates.check(self.config, running()) if not g.ok]:
            return self.say(f"holding {task.id}: " + "; ".join(g.detail for g in blocked))
        self.say(f"{kind} {task.id}")
        tasks.log(type="daemon_started", id=task.id, job=kind)
        try:
            run(job, self.config)
        except Exception as e:
            # The daemon outlives whatever it runs. run_task has already put the card back in todo,
            # so the card is fine; what broke is underneath us, and trying again at once would only
            # spend the card's attempts on a machine that has not had time to come back.
            self.paused_until = time.monotonic() + self.config.error_backoff
            tasks.log(type="daemon_error", id=task.id, error=str(e))
            print(f"{tasks.now()}  {task.id} failed: {e}", flush=True)
        self.said = ""  # whatever it says next is news, even if it said the same thing before

    def serve(self) -> None:
        signal.signal(signal.SIGTERM, terminate)
        print(f"{tasks.now()}  daemon up on {tasks.home()}, every {self.config.interval}s", flush=True)
        if taken := reclaim():
            print(f"{tasks.now()}  requeued {len(taken)} interrupted: {', '.join(taken)}", flush=True)
        try:
            while True:
                self.tick()
                time.sleep(self.config.interval)
        except (KeyboardInterrupt, SystemExit):
            print(f"{tasks.now()}  daemon down", flush=True)
