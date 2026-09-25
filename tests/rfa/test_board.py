"""The board's front door, and what it shows of a run.

Binding to localhost is not access control: every process on this Mac can reach the board, and so
can any page you have open, because a cross-site form post to 127.0.0.1 needs nobody's permission.
`POST /api/move` carries a card from `planning` to `todo`, which is the one gate between an idea and
a machine writing code, so these are the tests that gate depends on.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from rfa import board, tasks

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


def test_a_run_reports_every_command_and_what_is_happening_now(tmp_path, monkeypatch):
    """The trajectory is rewritten after each step, so the board can watch a run rather than wait."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    run = tmp_path / "var" / "runs" / "20260921-120000-a-task"
    run.mkdir(parents=True)
    (run / "trajectory.json").write_text(
        '{"messages": ['
        '{"role": "system"}, {"role": "user"},'
        '{"role": "assistant", "extra": {"actions": [{"command": "ls src"}]}},'
        '{"role": "tool", "content": "<output>a.js</output>", "extra": {"returncode": 0}},'
        '{"role": "assistant", "extra": {"actions": [{"command": "npm test"}]}},'
        '{"role": "tool", "content": "1 failing", "extra": {"returncode": 1}},'
        '{"role": "assistant", "extra": {"actions": [{"command": "npm test -- --fix"}]}}'
        "]}"
    )
    found = board.progress("20260921-120000-a-task")
    assert [s["commands"] for s in found["steps"]] == [["ls src"], ["npm test"]]
    assert [s["returncode"] for s in found["steps"]] == [0, 1]
    # The last command has no observation yet: that is what the run is doing this second.
    assert found["now"] == ["npm test -- --fix"]


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
    assert card["stage"] == "draft"
    assert task.path == was  # the file never left its stage folder


def test_analytics_counts_a_shipped_run_with_its_lines(tmp_path, monkeypatch):
    """The numbers the panel shows: one run, its patch's added lines, and it landed a branch."""
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
    assert found["shipped"] == 1
    assert found["failed"] == 0


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


def test_an_error_is_a_run_with_its_span_and_a_failure(tmp_path, monkeypatch):
    """No `run_finished` does not drop the run: its start-to-end span is the runtime, and it failed."""
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
    assert found["shipped"] == 0
    assert found["failed"] == 1


def test_a_run_that_is_still_going_has_no_finish_and_is_not_counted(tmp_path, monkeypatch):
    """Counted by when it finished, so one that has not finished is in no metric yet."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.log(type="run_started", id="20260921-120000-a-task")
    assert board.analytics("all")["runs"] == 0


def test_an_empty_workspace_is_zero_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    assert board.analytics("7d") == {"runs": 0, "runtime": 0, "loc": 0, "shipped": 0, "failed": 0}


def test_a_paused_run_ends_its_span_but_is_neither_shipped_nor_failed(tmp_path, monkeypatch):
    """Without its end, the paused start would pair with the next run's finish and double its runtime."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    for type in ("run_started", "run_paused", "run_started"):
        tasks.log(type=type, id="20260921-120000-a-task")
    tasks.log(type="run_finished", id="20260921-120000-a-task", shipped=True)
    assert {k: v for k, v in board.analytics("all").items() if k != "runtime"} == {
        "runs": 1,
        "loc": 0,
        "shipped": 1,
        "failed": 0,
    }
