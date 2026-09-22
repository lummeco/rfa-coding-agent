"""`rfa daemon`: the loop that pulls cards into the planner, the coder and the reviewer on its own.

It is the three commands you would type -- `rfa plan`, `rfa work` and `rfa review` -- on a timer.
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

Sentry is the fourth command, `rfa sentry`: with a `sentry:` block in rfa.yaml, every half hour by
default, the unresolved issues of each listed project are captured as drafts. Drafts and nothing
further -- each lands in front of you exactly as an idea typed into the capture box would.
"""

import signal
import time
from dataclasses import dataclass, field

from rfa import gates, sentry, service, settings, tasks
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
    plan: bool = True
    work: bool = True
    review: bool = True

    @classmethod
    def load(cls) -> "DaemonConfig":
        return cls(**(settings.load().get("daemon") or {}))


def running() -> list[Task]:
    """Cards with a run on them right now: anything in under-work, and a card an agent holds."""
    return (
        tasks.tasks("under-work")
        + [t for t in tasks.tasks("planning") if t.status == "planning"]
        + [t for t in tasks.tasks("review") if t.status == "reviewing"]
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
    reclaimed += [tasks.save(t, status="queued").id for t in tasks.tasks("planning") if t.status == "planning"]
    reclaimed += [tasks.save(t, status="queued").id for t in tasks.tasks("review") if t.status == "reviewing"]
    for id in reclaimed:
        tasks.log(type="reclaimed", id=id)
    return reclaimed


def reviewable(task: Task) -> bool:
    """Is there an app on this card the reviewer could actually start?

    A card can reach `review` without one -- you can move it there by hand -- and the daemon has to
    leave that card alone rather than pick it up every fifteen seconds to fail on it again.
    """
    config = settings.load("reviewer")
    return bool(set(task.meta.get("landed") or {}) & set(settings.app_specs(config, task.meta.get("repos") or [])))


def next_job(config: DaemonConfig) -> tuple[str, Task] | None:
    """The card to run next and what to run on it, or nothing to do.

    Reviewing comes first: a card in `review` is work that is already finished and only waiting to
    be told whether it counts. Starting another coding run ahead of it spends the machine on a
    fourth unfinished thing while three finished ones say nothing.

    `plan_ahead` only ever holds planning back in favour of a coding run that can actually start:
    with nothing to code there is nothing to yield to, and the queue would sit there all night.
    """
    reviews = [t for t in tasks.tasks("review") if t.status == "queued" and reviewable(t)] if config.review else []
    if reviews:
        return "review", reviews[0]
    plans = [t for t in tasks.tasks("planning") if t.status == "queued"] if config.plan else []
    work = [t for t in tasks.tasks("todo") if t.attempts < config.max_attempts] if config.work else []
    waiting = len(tasks.tasks("todo")) + sum(t.status == "ready" for t in tasks.tasks("planning"))
    if plans and (not work or waiting < config.plan_ahead):
        return "plan", plans[0]
    return ("work", work[0]) if work else None


def run(job: tuple[str, Task], config: DaemonConfig) -> None:
    # Imported here, not at the top: these pull in the whole agent harness, and the board and
    # `rfa status` import this module only to ask what it would do next.
    from rfa.planner import plan_task
    from rfa.reviewer import review_task
    from rfa.worker import run_task

    kind, task = job
    with gates.KeepAwake(config.keep_awake):
        if kind == "plan":
            plan_task(task, settings.load("planner"))
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
    return {
        "home": str(tasks.home()),
        # What is configured, not what a run would insist on: this must answer even when `models:`
        # is empty, where `settings.pick` is right to refuse.
        "model": settings.chosen() or workspace.get("default_model") or "",
        "models": sorted(settings.presets(workspace)),
        "reasoning": list(settings.REASONING),
        "repos": sorted(workspace.get("repos") or {}),
        "board_url": service.url(),
        # 0 rather than null: not running is a state, not missing information.
        "services": {s.name: s.pid() or 0 for s in service.services()},
        "gates": [vars(gate) for gate in gates.check(config, running())],
        "stages": {stage: len(tasks.tasks(stage)) for stage in tasks.STAGES},
        "running": [{"id": t.id, "title": t.title, "status": t.status} for t in running()],
        "next": {"job": job[0], "id": job[1].id, "title": job[1].title} if job else None,
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
    sentry_due: float = 0.0
    said: str = ""

    def say(self, line: str) -> None:
        """One line per change of mind. A daemon that repeats itself every 15 seconds has no log."""
        if line != self.said:
            print(f"{tasks.now()}  {line}", flush=True)
            self.said = line

    def poll(self) -> None:
        """Sentry, once `sentry.interval` has passed since the last look. Ticks happen between runs, so
        a coding run that took an hour is followed by one poll rather than four -- and a poll that
        failed waits the same interval, rather than hitting an expired token every fifteen seconds."""
        if (config := sentry.SentryConfig.load()) is None or time.monotonic() < self.sentry_due:
            return
        self.sentry_due = time.monotonic() + config.interval
        try:
            if drafted := sentry.pull(config, sentry.token(config)):
                print(f"{tasks.now()}  {len(drafted)} new from Sentry: {', '.join(t.id for t in drafted)}", flush=True)
        except Exception as e:
            # Sentry being down, or a token that has expired, is no reason to stop planning and coding.
            tasks.log(type="sentry_error", error=str(e))
            print(f"{tasks.now()}  sentry: {e}", flush=True)

    def tick(self) -> None:
        """One look at the board. Runs at most one card, and only with every gate open."""
        self.poll()
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
