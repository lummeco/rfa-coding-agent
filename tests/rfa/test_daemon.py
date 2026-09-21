"""The daemon: what it takes next, what stops it taking anything, and what it finds left behind."""

import pytest

from rfa import tasks
from rfa.daemon import Daemon, DaemonConfig, next_job, reclaim, running

# draft -> the stage we want, through the moves a human would make.
ROUTE = {
    "draft": [],
    "planning": ["planning"],
    "todo": ["todo"],
    "under-work": ["todo", "under-work"],
    "done": ["todo", "under-work", "done"],
}


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    return tmp_path


def card(stage: str, idea: str, **meta) -> tasks.Task:
    task = tasks.create(idea)
    for step in ROUTE[stage]:
        task = tasks.move(task, step, actor="human")
    return tasks.save(task, **meta) if meta else task


def test_planning_goes_first_until_there_is_enough_work_to_do():
    """A packet is short and it is what reaches the owner; a coding run is the bottleneck."""
    card("planning", "add a duplicate button", status="queued")
    card("todo", "rename the invoice column")
    assert next_job(DaemonConfig(plan_ahead=2)) == ("plan", tasks.tasks("planning")[0])
    card("todo", "cache the price list")
    # Two cards of work now waiting: another packet would only queue behind the coder.
    assert next_job(DaemonConfig(plan_ahead=2))[0] == "work"


def test_with_nothing_to_code_there_is_nothing_to_yield_to():
    """Finished packets wait on the owner, not on the coder, so they must not stop the planner --
    otherwise a night of unapproved packets leaves the planning queue untouched."""
    card("planning", "add a duplicate button", status="queued")
    for n in range(5):
        card("planning", f"already planned {n}", status="ready")
    assert next_job(DaemonConfig(plan_ahead=2))[0] == "plan"


def test_a_card_that_keeps_coming_back_is_left_for_a_human():
    card("todo", "rewrite the pricing engine", attempts=3)
    assert next_job(DaemonConfig(max_attempts=3)) is None
    assert next_job(DaemonConfig(max_attempts=4))[0] == "work"


@pytest.mark.parametrize(
    ("config", "expected"), [(DaemonConfig(plan=False), "work"), (DaemonConfig(work=False), "plan")]
)
def test_either_half_can_be_switched_off(config, expected):
    card("planning", "add a duplicate button", status="queued")
    card("todo", "rename the invoice column")
    assert next_job(config)[0] == expected


def test_a_run_nobody_is_on_is_handed_back_keeping_the_attempt_it_spent():
    """What a crash, an `rfa restart` or a Mac that slept leaves behind: a card in a running stage
    with no process on it. Without this the slot gate stays shut and the pipeline never moves again."""
    coding = card("under-work", "rebuild the importer", attempts=1)
    planning = card("planning", "add a duplicate button", status="planning")
    assert sorted(reclaim()) == sorted([coding.id, planning.id])
    assert running() == []
    assert (tasks.find(coding.id).stage, tasks.find(coding.id).attempts) == ("todo", 1)
    assert tasks.find(planning.id).status == "queued"


def test_one_run_at_a_time_holds_the_next_card_instead_of_starting_it():
    """The slot gate, from the daemon's side: a card already running means the todo waits, untouched,
    and the daemon says which card is in the way rather than going quiet."""
    busy = card("under-work", "rebuild the importer")
    waiting = card("todo", "rename the invoice column")
    daemon = Daemon(config=DaemonConfig())
    daemon.tick()
    # Not an equality: power and memory are this machine's business and may be holding it too.
    assert daemon.said.startswith(f"holding {waiting.id}:") and f"{busy.id} is running" in daemon.said
    assert tasks.find(waiting.id).stage == "todo"


def test_it_only_says_something_when_something_changed():
    """A line every 15 seconds is not a log. But whatever it says after a run is news again."""
    daemon = Daemon(config=DaemonConfig())
    daemon.tick()
    assert daemon.said == "nothing to do"
    daemon.said = ""
    daemon.tick()
    assert daemon.said == "nothing to do"
