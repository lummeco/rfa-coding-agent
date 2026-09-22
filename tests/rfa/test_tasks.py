"""The task store. Every test runs against real files in a real folder -- that is the whole design."""

import pytest

from rfa import board, tasks

CAPTURE_FILE = """---
status: draft
created: 2026-09-20T06:07:10.864Z
repos:
  - lummeco/rfa-agent
---

# Idea

Add a poll that fetches new issues.
"""


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    return tmp_path


def test_a_file_capture_wrote_is_read_without_touching_it(workspace):
    """Capture is unchanged, so its front matter is the contract: status, created, a repos list."""
    (workspace / "tasks" / "draft" / "20260920-060710-add-a-poll.md").write_text(CAPTURE_FILE)
    task = tasks.find("20260920-060710-add-a-poll")
    assert (task.stage, task.status, task.meta["repos"]) == ("draft", "draft", ["lummeco/rfa-agent"])
    assert task.title == "Add a poll that fetches new issues."  # no title yet: the idea stands in
    assert "Add a poll that fetches new issues." in task.body


def test_a_task_survives_a_write_unchanged(workspace):
    (workspace / "tasks" / "draft" / "20260920-060710-add-a-poll.md").write_text(CAPTURE_FILE)
    before = tasks.find("20260920-060710-add-a-poll")
    tasks.save(before, status="planning")
    after = tasks.find("20260920-060710-add-a-poll")
    assert after.body == before.body and after.meta["repos"] == ["lummeco/rfa-agent"]
    assert (after.status, after.meta["created"]) == ("planning", "2026-09-20T06:07:10.864Z")


def test_the_folder_is_the_stage(workspace):
    task = tasks.create("let users duplicate an invoice line", ["lummeco/lummepro-web"])
    was = task.path
    assert was.parent.name == "draft"
    assert tasks.move(task, "planning").path.parent.name == "planning"
    assert not was.exists() and tasks.find(task.id).stage == "planning"


def test_create_writes_what_capture_would(workspace):
    task = tasks.create("Let users duplicate an invoice line", ["a/b"])
    assert task.id.endswith("-let-users-duplicate-an-invoice-line")
    assert tasks.read(task.path).meta["status"] == "draft"
    assert "# Idea" in task.body


@pytest.mark.parametrize(
    ("frm", "to", "actor", "allowed"),
    [
        ("planning", "todo", "human", True),
        ("planning", "todo", "worker", False),  # the one gate that matters
        ("todo", "under-work", "worker", True),
        ("draft", "done", "human", False),  # not an edge at all
    ],
)
def test_only_a_human_turns_a_packet_into_work(frm, to, actor, allowed, workspace):
    task = tasks.create("an idea")
    if frm != "draft":
        task = tasks.move(task, "planning")
        if frm == "todo":
            task = tasks.move(task, "todo")
    if allowed:
        assert tasks.move(task, to, actor=actor).stage == to
    else:
        with pytest.raises(tasks.TransitionError):
            tasks.move(task, to, actor=actor)


def test_moving_records_history_the_board_can_show(workspace):
    task = tasks.create("an idea")
    tasks.move(tasks.move(task, "planning"), "todo")
    assert [e["type"] for e in tasks.events(task.id)] == ["created", "moved", "moved"]
    assert [e.get("to") for e in tasks.events(task.id)][1:] == ["planning", "todo"]


def test_tasks_are_listed_in_stage_order_so_the_board_never_sorts(workspace):
    a = tasks.create("first idea")
    b = tasks.create("second idea")
    tasks.move(b, "planning")
    assert [(t.id, t.stage) for t in tasks.tasks()] == [(a.id, "draft"), (b.id, "planning")]
    assert [t.id for t in tasks.tasks("planning")] == [b.id]


def test_a_move_onto_an_existing_file_refuses_rather_than_overwriting(workspace):
    task = tasks.create("an idea")
    (workspace / "tasks" / "planning" / f"{task.id}.md").write_text(CAPTURE_FILE)
    with pytest.raises(FileExistsError):
        tasks.move(task, "planning")
    assert tasks.find(task.id).stage == "draft"


def test_the_board_snapshot_carries_what_the_page_draws(workspace):
    task = tasks.create("let users duplicate a line", ["lummeco/lummepro-web"])
    tasks.save(tasks.move(task, "planning"), status="ready", title="Duplicate a line", complexity=2, attempts=1)
    snapshot = board.snapshot()
    assert snapshot["stages"] == list(tasks.STAGES)
    card = snapshot["tasks"][0]
    assert (card["title"], card["status"], card["attempts"], card["complexity"]) == ("Duplicate a line", "ready", 1, 2)
    assert card["repos"] == ["lummeco/lummepro-web"] and snapshot["events"][0]["type"] == "created"


def test_a_card_a_human_drops_into_planning_is_queued_not_being_planned(workspace):
    """The board must not claim the planner has it until `rfa plan` actually picks it up."""
    assert tasks.move(tasks.create("an idea"), "planning").status == "queued"
    assert tasks.move(tasks.create("another"), "planning", status="planning").status == "planning"


def test_the_board_shows_where_shipped_work_landed(workspace):
    task = tasks.create("an idea", ["lummeco/web"])
    task = tasks.move(tasks.move(tasks.move(task, "planning"), "todo"), "under-work", actor="worker")
    tasks.move(task, "done", actor="worker", status="shipped", branches={"web": "rfa/t1"})
    assert board.snapshot()["tasks"][0]["branches"] == {"web": "rfa/t1"}


def test_the_board_offers_the_configured_repositories_for_capture(workspace):
    (workspace / "rfa.yaml").write_text("repos:\n  lummeco/web: ~/dev/web\n  lummeco/api: ~/dev/api\n")
    assert board.snapshot()["repos"] == ["lummeco/api", "lummeco/web"]


def test_capturing_from_the_board_writes_the_same_draft_the_cli_would(workspace):
    captured = tasks.create("typed into the board", ["lummeco/web"])
    assert (captured.stage, captured.status) == ("draft", "draft")
    assert captured.meta["repos"] == ["lummeco/web"] and "typed into the board" in captured.body
    assert board.snapshot()["tasks"][0]["title"] == "typed into the board"


def test_a_task_is_found_by_the_prefix_rfa_ls_prints(workspace):
    """`rfa ls` shows the first 15 characters; that has to be enough to type back."""
    task = tasks.create("let users duplicate an invoice line")
    assert tasks.find(task.id[:15]).id == task.id
    assert tasks.find(task.id).id == task.id
    with pytest.raises(FileNotFoundError):
        tasks.find("20200101-000000")


def test_an_ambiguous_prefix_says_so_instead_of_picking_one(workspace):
    a, b = tasks.create("first idea"), tasks.create("second idea")
    shared = a.id[:13]  # same date, same minute
    with pytest.raises(ValueError, match="matches 2"):
        assert tasks.find(shared) and b


def test_archiving_hides_a_task_without_moving_it(workspace):
    """A flag on the task, not a move: the file stays in its stage folder, stage and status intact."""
    task = tasks.create("an idea")
    was = task.path
    before = (task.stage, task.status)
    tasks.save(task, archived=True)
    after = tasks.find(task.id)
    assert after.archived is True
    assert after.path == was and after.path.parent.name == "draft"
    assert (after.stage, after.status) == before


def test_unarchiving_brings_a_task_back_into_its_stage(workspace):
    task = tasks.create("an idea")
    tasks.save(task, archived=True)
    assert tasks.find(task.id).archived is True
    tasks.save(tasks.find(task.id), archived=False)
    after = tasks.find(task.id)
    assert after.archived is False and after.stage == "draft"


def test_the_archived_flag_survives_a_reload(workspace):
    """What the board reads is the file: a second read still reports the task as archived."""
    task = tasks.create("an idea")
    tasks.save(task, archived=True)
    assert tasks.read(task.path).archived is True
    assert tasks.find(task.id).archived is True
