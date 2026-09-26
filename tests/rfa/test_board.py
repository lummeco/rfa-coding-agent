"""The board's front door, and what it shows of a run.

Binding to localhost is not access control: every process on this Mac can reach the board, and so
can any page you have open, because a cross-site form post to 127.0.0.1 needs nobody's permission.
`POST /api/move` carries a card from `planning` to `todo`, which is the one gate between an idea and
a machine writing code, so these are the tests that gate depends on.
"""

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from rfa import board, tasks
from rfa.packet import Packet

TOKEN = "s3cret-token"
ORIGIN = "http://127.0.0.1:4380"


def headers(**fields) -> Message:
    """Real headers, built the way http.server hands them over."""
    message = Message()
    for name, value in fields.items():
        message[name.replace("_", "-")] = value
    return message


def test_the_page_from_the_printed_link_gets_in():
    assert board.allowed(headers(X_RFA_Token=TOKEN), TOKEN, ORIGIN) is True


@pytest.mark.parametrize(
    ("sent", "why"),
    [
        ({}, "no token at all: a bare curl, or the board opened without its fragment"),
        ({"X_RFA_Token": ""}, "an empty token"),
        ({"X_RFA_Token": "s3cret-toke"}, "a prefix of the token"),
        ({"X_RFA_Token": "S3CRET-TOKEN"}, "the right letters, wrong case"),
        ({"X_RFA_Token": TOKEN + "x"}, "the token with something appended"),
    ],
)
def test_anything_but_the_token_is_refused(sent, why):
    assert board.allowed(headers(**sent), TOKEN, ORIGIN) is False, why


def test_a_page_on_another_site_is_refused_even_holding_the_token():
    """Belt and braces: a browser that would send the token cross-site still says where it came from."""
    assert board.allowed(headers(X_RFA_Token=TOKEN, Origin="https://evil.example"), TOKEN, ORIGIN) is False
    assert board.allowed(headers(X_RFA_Token=TOKEN, Origin=ORIGIN), TOKEN, ORIGIN) is True


def test_the_board_stays_where_you_leave_it():
    """Nothing re-fetches the snapshot on a timer: the server saying something changed, R, or an
    action is what redraws it, so a diff you are reading does not jump under you. The one timer on
    the page is the run log you opened on purpose, and it is allowed to keep moving."""
    page = board.PAGE.read_text()
    timers = [line for line in page.splitlines() if "setInterval" in line]
    assert all("refresh" not in line for line in timers), "an always-on timer must not redraw the board"
    assert any("drawRun" in line for line in timers), "the opt-in run log still updates while you watch"
    assert re.search(r"^live\(\);$", page, re.M), "the page listens for changes from the moment it loads"
    assert "location.reload" not in page, "a change is drawn in place, never by reloading the page"


def test_the_stream_speaks_only_when_a_file_the_board_draws_changes(tmp_path, monkeypatch):
    """Quiet while nothing moves, "changed" once per change, and a ping so a closed tab is noticed."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    card = tasks.create("an idea")
    sent = []

    def write(chunk: bytes) -> None:
        sent.append(chunk)
        if len(sent) == 1:
            tasks.move(card, "planning")
        elif len(sent) == 3:
            raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        board.stream(write, every=0.01, ping=0.05)
    assert sent == [b": ping\n\n", b"data: changed\n\n", b": ping\n\n"]


def test_a_run_reports_every_command_and_what_is_happening_now(tmp_path, monkeypatch):
    """The trajectory is rewritten after each step, so the board can watch a run rather than wait."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    run = tmp_path / "var" / "runs" / "20260921-120000-a-task"
    run.mkdir(parents=True)
    (run / "trajectory.json").write_text(
        '{"messages": ['
        '{"role": "system"}, {"role": "user"},'
        '{"role": "assistant", "extra": {"actions": [{"command": "ls src"}]}},'
        '{"role": "tool", "content": "<returncode>0</returncode>\\n<output>\\na.js\\n</output>", "extra": {"returncode": 0}},'
        '{"role": "assistant", "extra": {"actions": [{"command": "npm test"}]}},'
        '{"role": "tool", "content": "1 failing", "extra": {"returncode": 1}},'
        '{"role": "assistant", "extra": {"actions": [{"command": "npm test -- --fix"}]}}'
        "]}"
    )
    found = board.progress("20260921-120000-a-task")
    assert [s["commands"] for s in found["steps"]] == [["ls src"], ["npm test"]]
    assert [s["returncode"] for s in found["steps"]] == [0, 1]
    assert [s["output"] for s in found["steps"]] == ["a.js", "1 failing"], "what came back, not the tags around it"
    # The last command has no observation yet: that is what the run is doing this second.
    assert found["now"] == ["npm test -- --fix"]


def test_a_finished_run_is_not_still_doing_its_last_command(tmp_path, monkeypatch):
    """The command that submits has no observation; the exit after it is its answer, not a wait."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    run = tmp_path / "var" / "runs" / "20260921-120000-a-task"
    run.mkdir(parents=True)
    (run / "trajectory.json").write_text(
        '{"messages": ['
        '{"role": "assistant", "extra": {"actions": [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]}},'
        '{"role": "exit", "content": "all done"}'
        "]}"
    )
    assert board.progress("20260921-120000-a-task") == {
        "steps": [{"commands": [], "returncode": 0, "output": "all done", "exit": True}],
        "now": [],
    }


def test_a_half_written_trajectory_is_not_ready_rather_than_broken(tmp_path, monkeypatch):
    """mini rewrites the file in place, so a poll every 1.5s will sometimes catch it mid-write."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    run = tmp_path / "var" / "runs" / "20260921-120000-a-task"
    run.mkdir(parents=True)
    (run / "trajectory.json").write_text('{"messages": [{"role": "assis')
    assert board.progress("20260921-120000-a-task") == {"steps": [], "again": True}


@pytest.mark.parametrize("id", ["../../../etc/passwd", "..", "a/b", "", "Nope"])
def test_a_run_id_that_is_not_a_run_id_reads_nothing(id, tmp_path, monkeypatch):
    """The id arrives in a query string, so it is the one thing on this endpoint a stranger picks."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    assert board.progress(id) == {"steps": []}
    assert tasks.ID_RE.fullmatch(id) is None


def test_the_copy_line_carries_a_landed_branch_into_what_you_have_checked_out(tmp_path):
    """The case the plain `git apply` lost: by the time you press it, your branch has moved on.

    Your branch has a commit of its own in the same file, and an uncommitted edit on top. Both
    survive, the coder's change arrives beside them, and nothing is staged.
    """
    repo = tmp_path / "my repo"  # a space, because a path is whatever the person's disk says
    repo.mkdir()
    run = lambda *args, at=repo: subprocess.run(["git", "-C", str(at), *args], check=True, capture_output=True)
    commit = lambda at, message: run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message, at=at)
    run("init", "-qb", "main")
    (repo / "a.txt").write_text("one\ntwo\nthree\nfour\nfive\n")
    run("add", "-A")
    commit(repo, "base")

    run("worktree", "add", "-q", "-b", "rfa/a-task", str(tree := tmp_path / "tree"), "main")
    (tree / "a.txt").write_text("one\ntwo\nCODER\nfour\nfive\n")
    (tree / "logo.bin").write_bytes(bytes(range(256)))
    run("add", "-A", at=tree)
    commit(tree, "work")
    run("worktree", "remove", "--force", str(tree))

    run("checkout", "-qb", "mine")
    (repo / "a.txt").write_text("ONE\ntwo\nthree\nfour\nfive\n")
    run("add", "-A")
    commit(repo, "what I did while it ran")
    (repo / "a.txt").write_text("ONE\ntwo\nthree\nfour\nMINE\n")  # and what I have not committed

    command = board.copy_command(repo, "rfa/a-task")
    assert subprocess.run(command, shell=True, capture_output=True, text=True).returncode == 0, command
    assert (repo / "a.txt").read_text() == "ONE\ntwo\nCODER\nfour\nMINE\n"
    assert (repo / "logo.bin").read_bytes() == bytes(range(256))
    # In the working tree and nowhere else: nothing staged, still on your branch, no index left behind.
    assert subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                          capture_output=True, text=True).stdout == ""
    assert subprocess.run(["git", "-C", str(repo), "branch", "--show-current"],
                          capture_output=True, text=True).stdout.strip() == "mine"
    assert not (repo / ".git" / "rfa-apply-index").exists()


def test_a_command_is_offered_for_every_landed_repo_that_is_still_configured(tmp_path):
    """The path comes from `repos:`, so a card whose repository has left it gets no command at all."""
    found = board.copy_commands(
        {"web": "rfa/a-task", "gone": "rfa/a-task"},
        {"lummeco/web": f"{tmp_path}/web@develop", "lummeco/other": f"{tmp_path}/other"},
    )
    assert list(found) == ["web"]
    assert found["web"].startswith(f"(cd {tmp_path}/web && ")
    assert "git show --binary rfa/a-task | git apply -3" in found["web"]


def test_the_snapshot_reports_a_task_as_archived_in_its_stage(tmp_path, monkeypatch):
    """Archiving is a flag the page can draw, not a move: the card is still in its stage."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    task = tasks.create("an idea")
    was = task.path
    tasks.save(task, archived=True)
    card = board.snapshot()["tasks"][0]
    assert card["archived"] is True
    assert card["sentry"] is False
    assert card["stage"] == "draft"
    assert task.path == was  # the file never left its stage folder


def test_analytics_counts_a_run_with_its_lines(tmp_path, monkeypatch):
    """One run and its patch's added lines."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    run = tmp_path / "var" / "runs" / "20260921-120000-a-task"
    run.mkdir(parents=True)
    (run / "web.patch").write_text(
        "diff --git a/a.js b/a.js\n"
        "index 123..456 100644\n"
        "--- a/a.js\n"
        "+++ b/a.js\n"
        "@@ -1,2 +1,4 @@\n"
        " context\n"
        "+added\n"
        "+added again\n"
        "-gone\n"
    )
    tasks.log(type="run_started", id="20260921-120000-a-task")
    tasks.log(type="run_finished", id="20260921-120000-a-task", shipped=True, rounds=1, landed={"web": "rfa/a-task"})
    found = board.analytics("all")
    assert found["runs"] == 1
    assert found["loc"] == 2  # the two `+` lines; the `+++` header and the `-` line are not code


def test_a_run_is_counted_by_when_it_finished(tmp_path, monkeypatch):
    """Finished twenty days ago: outside the short window, inside the long one, inside all time."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    finished = datetime.now(timezone.utc) - timedelta(days=20)
    started = finished - timedelta(seconds=90)

    def stamp(t):
        return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    events = tmp_path / "var" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        json.dumps({"type": "run_started", "id": "20260921-120000-a-task", "ts": stamp(started)})
        + "\n"
        + json.dumps({"type": "run_finished", "id": "20260921-120000-a-task", "shipped": True, "ts": stamp(finished)})
        + "\n"
    )
    assert board.analytics("7d")["runs"] == 0
    assert board.analytics("30d")["runs"] == 1
    assert board.analytics("all")["runs"] == 1


def test_an_error_is_a_run_with_its_span(tmp_path, monkeypatch):
    """No `run_finished` does not drop the run: its start-to-end span is the runtime."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    finished = datetime.now(timezone.utc)

    def stamp(t):
        return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    events = tmp_path / "var" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        json.dumps(
            {"type": "run_started", "id": "20260921-120000-a-task", "ts": stamp(finished - timedelta(seconds=90))}
        )
        + "\n"
        + json.dumps(
            {"type": "run_error", "id": "20260921-120000-a-task", "error": "docker down", "ts": stamp(finished)}
        )
        + "\n"
    )
    found = board.analytics("7d")
    assert found["runs"] == 1
    assert found["runtime"] == pytest.approx(90)


def test_a_run_that_is_still_going_has_no_finish_and_is_not_counted(tmp_path, monkeypatch):
    """Counted by when it finished, so one that has not finished is in no metric yet."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.log(type="run_started", id="20260921-120000-a-task")
    assert board.analytics("all")["runs"] == 0


def test_an_empty_workspace_is_zero_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    assert board.analytics("7d") == {
        "runs": 0,
        "runtime": 0,
        "loc": 0,
        "shipped": 0,
        "trashed": 0,
        "built": 0,
        "failed": 0,
        "rate": None,
    }


def test_a_paused_run_ends_its_span(tmp_path, monkeypatch):
    """Without its end, the paused start would pair with the next run's finish and double its runtime."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    for type in ("run_started", "run_paused", "run_started"):
        tasks.log(type=type, id="20260921-120000-a-task")
    tasks.log(type="run_finished", id="20260921-120000-a-task", shipped=True)
    assert (board.analytics("all")["runs"], board.analytics("all")["loc"]) == (1, 0)


def test_only_built_code_in_done_takes_a_verdict_and_a_new_run_clears_it(tmp_path, monkeypatch):
    """Shipped and trashed are the human's word on built code; the next run starts without one."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    task = tasks.move(tasks.move(tasks.move(tasks.create("an idea"), "todo"), "under-work"), "done", actor="worker")
    assert (task.status, board.snapshot()["tasks"][0]["verdict"]) == ("built", None)
    assert board.judge(task.id, "trashed").status == "trashed"
    assert board.judge(task.id, None).meta["verdict"] is None
    assert board.judge(task.id, "shipped").status == "shipped"
    with pytest.raises(ValueError, match="no such verdict"):
        board.judge(task.id, "great")
    again = tasks.move(tasks.find(task.id), "todo")
    assert "verdict" not in tasks.read(again.path).meta
    with pytest.raises(ValueError, match="not built code"):
        board.judge(task.id, "shipped")
    failed = tasks.move(tasks.move(again, "under-work"), "done", actor="worker", status="failed")
    with pytest.raises(ValueError, match="not built code"):
        board.judge(failed.id, "shipped")


def test_outcomes_are_per_card_by_when_it_reached_done_and_the_rate_leaves_waiting_out(tmp_path, monkeypatch):
    """Shipped out of shipped, trashed and failed; built is still waiting, and an old card is outside 7d."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()

    def done(idea: str, verdict: str | None = None, **meta) -> tasks.Task:
        task = tasks.move(
            tasks.move(tasks.move(tasks.create(idea), "todo"), "under-work"), "done", actor="worker", **meta
        )
        return board.judge(task.id, verdict) if verdict else task

    done("good", "shipped", finished_at=tasks.now())
    done("also good", "shipped", finished_at=tasks.now())
    done("bad", "trashed", finished_at=tasks.now())
    done("broke", status="failed", finished_at=tasks.now())
    done("waiting", finished_at=tasks.now())
    done("legacy", status="shipped", finished_at=tasks.now())  # the machine's word, not yours: still waiting
    done("long ago", "shipped", finished_at="2020-01-01T00:00:00.000Z")
    found = board.analytics("7d")
    assert [found[k] for k in ("shipped", "trashed", "built", "failed")] == [2, 1, 2, 1]
    assert found["rate"] == pytest.approx(0.5)
    assert board.analytics("all")["shipped"] == 3


def planned_ready(tmp_path, monkeypatch) -> tasks.Task:
    """A planning card the planner has finished: its body is the rendered packet, and it waits to be read."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    packet = Packet(
        title="Add duplicate invoice line functionality.",
        goal="Allow a user to duplicate an existing invoice line.",
        current_behavior="Lines can be added, edited and deleted, but not duplicated.",
        constraints=["Keep it local.", "Leave:\n  - product\n  - VAT"],
        acceptance_criteria=["Clicking Duplicate creates exactly one new line."],
        non_goals=["Bulk duplication."],
        complexity=2,
        complexity_reason="A normal feature following an existing pattern.",
    )
    task = tasks.move(tasks.create("an idea"), "planning", actor="human")
    task.body = "\n" + packet.render()
    return tasks.save(
        task, status="ready", title=packet.title, complexity=2, open_questions=["Which line to copy from?"]
    )


def test_the_snapshot_exposes_a_ready_packets_sections(tmp_path, monkeypatch):
    """The detail view edits the packet section by section, so the snapshot reads it back out of the body."""
    planned_ready(tmp_path, monkeypatch)
    card = board.snapshot()["tasks"][0]
    assert card["packet"]["goal"] == "Allow a user to duplicate an existing invoice line."
    assert card["packet"]["acceptance_criteria"] == ["Clicking Duplicate creates exactly one new line."]
    assert card["packet"]["constraints"] == ["Keep it local.", "Leave:\n  - product\n  - VAT"]
    assert card["open_questions"] == ["Which line to copy from?"]


def test_the_snapshot_does_not_expose_a_packet_on_any_other_card(tmp_path, monkeypatch):
    """Only a planning card that is ready is one; the rest are edited as raw body text."""
    task = planned_ready(tmp_path, monkeypatch)
    assert board.snapshot()["tasks"][0]["packet"] is not None
    tasks.move(task, "todo", actor="human")
    assert board.snapshot()["tasks"][0]["packet"] is None


def test_editing_a_packets_sections_rewrites_the_body_and_keeps_the_rest(tmp_path, monkeypatch):
    """The body is the source of truth the coder reads: the sections the owner did not touch
    come back exactly, nested bullets and all, and the shape is render()'s."""
    task = planned_ready(tmp_path, monkeypatch)
    edited = board.edit(
        {
            "id": task.id,
            "goal": "Allow a user to duplicate an invoice line, with or without its VAT.",
            "acceptance_criteria": ["Clicking Duplicate creates exactly one new line.", "The copy is editable."],
            "open_questions": ["Which line to copy from?", "And why?"],
        }
    )
    body = tasks.read(edited.path).body
    assert "## Goal\nAllow a user to duplicate an invoice line, with or without its VAT." in body
    assert "## Acceptance criteria\n1. Clicking Duplicate creates exactly one new line.\n2. The copy is editable." in body
    assert "## Constraints\n- Keep it local.\n- Leave:\n  - product\n  - VAT" in body
    assert "## Non-goals\n- Bulk duplication." in body
    # open_questions stays on the meta, not in the body.
    assert tasks.read(edited.path).meta["open_questions"] == ["Which line to copy from?", "And why?"]
    assert "Open questions" not in body


def test_an_edited_packet_survives_a_reload(tmp_path, monkeypatch):
    """The body is the source of truth, so the snapshot reads back whatever the owner saved."""
    task = planned_ready(tmp_path, monkeypatch)
    board.edit({"id": task.id, "current_behavior": "Lines can be added, but not duplicated.", "non_goals": []})
    card = board.snapshot()["tasks"][0]
    assert card["packet"]["current_behavior"] == "Lines can be added, but not duplicated."
    assert card["packet"]["non_goals"] == []
    assert "## Non-goals" not in tasks.read(task.path).body


def test_a_packet_is_only_edited_on_a_planning_card(tmp_path, monkeypatch):
    """Past planning, the body carries the run's notes; rewriting it from the packet would drop them."""
    task = planned_ready(tmp_path, monkeypatch)
    tasks.move(task, "todo", actor="human")
    with pytest.raises(ValueError, match="planning card"):
        board.edit({"id": task.id, "goal": "Something else."})


def test_a_card_the_daemon_gave_up_on_is_retried_or_sent_back_to_planned(tmp_path, monkeypatch):
    """Out of attempts, a to-do card sits there for good unless you hand them back or re-approve it."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    task = tasks.save(tasks.move(tasks.create("an idea"), "todo"), attempts=3, error="pip could not reach PyPI", paused=True)
    retried = tasks.find(board.retry(task.id).id)
    assert (retried.stage, retried.status, retried.attempts, retried.paused) == ("todo", "todo", 0, False)
    assert "error" not in retried.meta
    tasks.save(retried, attempts=3, error="again")
    planned = tasks.find(tasks.move(tasks.find(task.id), "planning", status="ready").id)
    assert (planned.stage, planned.status, planned.attempts) == ("planning", "ready", 0)
    assert "error" not in planned.meta
    with pytest.raises(ValueError, match="only a to-do card"):
        board.retry(task.id)
