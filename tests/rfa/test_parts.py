"""A card built in parts: real card files, a real git repository, the daemon's own choices."""

import subprocess

import pytest

from rfa import packet, parts, rounds, settings, tasks
from rfa.daemon import DaemonConfig, next_job, reclaim, running
from rfa.tester import write_tests

SPLIT = [
    {"title": "Schema", "goal": "The table exists.", "acceptance_criteria": ["It migrates."], "complexity": 2},
    {"title": "API", "goal": "It is served.", "acceptance_criteria": ["GET returns it."], "complexity": 2},
]


def git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    repo = tmp_path / "web"
    repo.mkdir()
    git(repo, "init", "-qb", "main")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    settings.path().write_text(f"repos:\n  lummeco/web: {repo}@main\n")
    return repo


def approved(**meta) -> tasks.Task:
    """A split card you moved to todo: the packet as the planner left it, with `split:` beside it."""
    body = packet.Packet(
        title="Tutorials",
        goal="Tutorials come from the database.",
        current_behavior="They are hardcoded.",
        decisions=["Tenant-scoped tables -- like every v3 table."],
        acceptance_criteria=["The tour reads its steps from the API."],
        complexity=4,
        complexity_reason="two layers",
        parts=SPLIT,
    ).render()
    task = tasks.save(tasks.create("tutorials", ["lummeco/web"]), split=SPLIT, title="Tutorials", **meta)
    task.body = "\n" + body
    return tasks.move(tasks.save(task), "todo")


def passes(task: tasks.Task) -> tasks.Task:
    """A part through coding and review, the moves the worker and the reviewer make."""
    task = tasks.move(task, "under-work", actor="worker")
    task = tasks.move(task, "review", actor="worker")
    return tasks.move(task, "done", actor="reviewer", status="built")


def test_approving_a_split_card_makes_its_parts_in_order_and_nothing_codes_the_card_itself(repo):
    card = approved(model="qwen3.8")
    assert parts.advance(settings.load()) == [f"{card.id} approved in 2 parts"]
    first, second = parts.children(tasks.find(card.id))
    assert (first.stage, first.meta.get("after"), second.meta["after"], second.meta["model"]) == (
        "todo",
        None,
        first.id,
        "qwen3.8",
    )
    # Each part is a whole ticket: its own criteria, and the card's decisions and current behavior.
    fields = packet.parse(second.body)
    assert fields["acceptance_criteria"] == ["GET returns it."]
    assert fields["decisions"] == ["Tenant-scoped tables -- like every v3 table."]
    assert "Part 2 of 2 of: Tutorials." in fields["goal"]
    assert tasks.find(card.id).status == "parts" and parts.advance(settings.load()) == []
    # The first part's turn: tests before code. The second waits, and the card is never coded.
    assert next_job(DaemonConfig()) == ("tests", first)
    tasks.save(first, tests_at=tasks.now())
    assert next_job(DaemonConfig()) == ("work", tasks.find(first.id))
    assert not parts.due(second) and not parts.workable(tasks.find(card.id))


def test_the_next_part_waits_for_the_one_before_it_to_pass_review(repo):
    card = approved()
    parts.advance(settings.load())
    first, second = parts.children(tasks.find(card.id))
    tasks.save(first, tests_at=tasks.now())
    passes(tasks.find(first.id))
    assert parts.due(tasks.find(second.id)) and next_job(DaemonConfig()) == ("tests", tasks.find(second.id))
    # A part that ran out of attempts holds the card, and says so.
    tasks.save(tasks.find(second.id), attempts=3)
    assert parts.blocked(tasks.find(card.id)) == "Part 2 is todo: API"


def test_a_part_starts_from_and_lands_on_the_cards_branch(repo):
    card = approved()
    parts.advance(settings.load())
    first, second = parts.children(tasks.find(card.id))
    assert parts.starts(first) == {} and parts.branch(first) == f"rfa/{card.id}"
    tasks.save(tasks.find(card.id), landed={"web": f"rfa/{card.id}"})
    assert parts.starts(second) == {"lummeco/web": f"rfa/{card.id}"}
    # An ordinary card is untouched: it starts where its own `branches:` say.
    plain = tasks.save(tasks.create("plain", ["lummeco/web"]), branches=["lummeco/web@dev"], landed={"web": "x"})
    assert parts.starts(plain) == {"lummeco/web": "dev"}


def test_each_part_is_told_what_the_parts_before_it_did(repo):
    card = approved()
    parts.advance(settings.load())
    first, second = parts.children(tasks.find(card.id))
    branch = parts.branch(first)
    git(repo, "switch", "-qc", branch)
    (repo / "schema.sql").write_text("create table tutorial;\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "Schema")
    tasks.save(tasks.find(card.id), landed={"web": branch})
    parts.note(first, {"summary": "Added the table.", "notes": "Steps are ordered by `position`, not id."})
    passes(tasks.save(first, tests_at=tasks.now()))
    told = parts.handoff(tasks.find(second.id), settings.load())
    assert "1. Schema -- done\n2. API -- this part" in told
    assert "## Part 1: Schema\n\nAdded the table.\n\nSteps are ordered by `position`" in told
    assert "schema.sql" in told and parts.handoff(tasks.find(card.id), settings.load()) == ""
    # Planning the card again starts from what the parts already wrote.
    assert "Steps are ordered by `position`" in parts.progress(tasks.find(card.id))


def test_when_every_part_has_passed_the_whole_branch_goes_to_review(repo):
    card = approved()
    parts.advance(settings.load())
    base = git(repo, "rev-parse", "main").strip()
    branch = f"rfa/{card.id}"
    git(repo, "switch", "-qc", branch)
    for kid, name in zip(parts.children(tasks.find(card.id)), ("schema.sql", "api.py")):
        start = git(repo, "rev-parse", "HEAD").strip()
        (repo / name).write_text(f"{name}\n")
        git(repo, "add", "-A")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", kid.title)
        rounds.record(kid.id, {"n": 0, "commits": {"web": {"branch": branch, "base": start, "head": "x"}}}, [])
        passes(kid)
    git(repo, "switch", "-q", "main")
    tasks.save(tasks.find(card.id), landed={"web": branch})
    assert parts.finished(tasks.find(card.id))
    assert parts.advance(settings.load()) == [f"{card.id}: every part passed, the whole card goes to review"]
    reviewed = tasks.find(card.id)
    assert (reviewed.stage, reviewed.status, reviewed.meta["landed"]) == ("review", "queued", {"web": branch})
    # One round from where the first part started: the diff the pull request will show.
    (entry,) = rounds.load(card.id)["rounds"]
    assert entry["commits"]["web"]["base"] == base and "Built in 2 parts: Schema; API" in entry["summary"]
    patch = (rounds.round_dir(card.id, 0) / "web.patch").read_text()
    assert "+schema.sql" in patch and "+api.py" in patch


def test_planning_a_split_card_again_drops_the_parts_nothing_happened_on(repo):
    card = approved()
    parts.advance(settings.load())
    first, second = parts.children(tasks.find(card.id))
    passes(tasks.save(first, tests_at=tasks.now()))
    parts.drop_unstarted(tasks.find(card.id))
    assert [t.id for t in parts.children(tasks.find(card.id))] == [first.id]
    assert tasks.find(card.id).meta["parts"] == [first.id] and not second.path.exists()


def test_a_split_card_has_no_tests_of_its_own(repo):
    task = tasks.move(tasks.save(tasks.create("big", ["lummeco/web"]), split=SPLIT), "planning")
    written = write_tests(task, {})
    assert written.status == "ready" and "Built in 2 parts" in written.meta["tests_note"]


def test_a_part_writing_its_tests_holds_the_machine_and_a_restart_hands_it_back(repo):
    card = approved()
    parts.advance(settings.load())
    first, _ = parts.children(tasks.find(card.id))
    tasks.save(first, status="writing-tests")
    assert [t.id for t in running()] == [first.id]
    assert reclaim() == [first.id] and next_job(DaemonConfig()) == ("tests", tasks.find(first.id))
