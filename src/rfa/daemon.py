"""`rfa daemon`: the loop that pulls cards into the planner and the coder on its own.

It is the two commands you would type -- `rfa plan` and `rfa work` -- on a timer. Anything it does
you can still do by hand, and stopping it stops nothing else. What it adds is the gates: a laptop on
a draining battery or short of memory should not start a container, and two runs must never overlap,
because planning and coding drive the same local model and two agent calls at once split its memory
and thrash the GPU.

The human gate is untouched. The daemon plans cards you moved into `planning` and codes cards you
approved into `todo`; it never moves a packet between the two. That edge is yours, and it is the
only thing standing between an idea and a machine writing code for it.

Planning goes first, because it is short, bounded and it is what puts a packet in front of you --
until `plan_ahead` cards of work are already waiting, at which point the coder is the bottleneck and
another packet would only queue behind it.
"""

import signal
import time
from dataclasses import dataclass, field

from rfa import gates, settings, tasks
from rfa.planner import plan_task
from rfa.tasks import Task
from rfa.worker import run_task


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
    """Hold `caffeinate` for a run, so the Mac does not idle-sleep in the middle of one."""
    plan_ahead: int = 2
    """Stop planning once this many cards of work are waiting for you or for the coder."""
    max_attempts: int = 3
    """A todo card that has come back this many times is left alone until you look at it."""
    error_backoff: int = 300
    """After a run fails on something underneath it -- Docker gone, the model unreachable -- wait
    this long before starting another. Hitting a broken machine every 15 seconds helps nobody."""
    plan: bool = True
    work: bool = True

    @classmethod
    def load(cls) -> "DaemonConfig":
        return cls(**(settings.load().get("daemon") or {}))


def running() -> list[Task]:
    """Cards with a run on them right now: anything in under-work, and a card the planner holds."""
    return tasks.tasks("under-work") + [t for t in tasks.tasks("planning") if t.status == "planning"]


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
    for id in reclaimed:
        tasks.log(type="reclaimed", id=id)
    return reclaimed


def next_job(config: DaemonConfig) -> tuple[str, Task] | None:
    """The card to run next and what to run on it, or nothing to do.

    `plan_ahead` only ever holds planning back in favour of a coding run that can actually start:
    with nothing to code there is nothing to yield to, and the queue would sit there all night.
    """
    plans = [t for t in tasks.tasks("planning") if t.status == "queued"] if config.plan else []
    work = [t for t in tasks.tasks("todo") if t.attempts < config.max_attempts] if config.work else []
    waiting = len(tasks.tasks("todo")) + sum(t.status == "ready" for t in tasks.tasks("planning"))
    if plans and (not work or waiting < config.plan_ahead):
        return "plan", plans[0]
    return ("work", work[0]) if work else None


def run(job: tuple[str, Task], config: DaemonConfig) -> None:
    kind, task = job
    with gates.KeepAwake(config.keep_awake):
        if kind == "plan":
            plan_task(task, settings.load("planner"))
        else:
            run_task(task, settings.load("coder"))


def terminate(*_) -> None:
    """SIGTERM has to unwind rather than stop the process where it stands: `rfa down` in the middle
    of a coding run must still reach the `finally` that removes the container."""
    raise SystemExit(0)


@dataclass
class Daemon:
    config: DaemonConfig = field(default_factory=DaemonConfig.load)
    paused_until: float = 0.0
    said: str = ""

    def say(self, line: str) -> None:
        """One line per change of mind. A daemon that repeats itself every 15 seconds has no log."""
        if line != self.said:
            print(f"{tasks.now()}  {line}", flush=True)
            self.said = line

    def tick(self) -> None:
        """One look at the board. Runs at most one card, and only with every gate open."""
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
