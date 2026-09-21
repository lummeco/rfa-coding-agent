"""The board's front door, and what it shows of a run.

Binding to localhost is not access control: every process on this Mac can reach the board, and so
can any page you have open, because a cross-site form post to 127.0.0.1 needs nobody's permission.
`POST /api/move` carries a card from `planning` to `todo`, which is the one gate between an idea and
a machine writing code, so these are the tests that gate depends on.
"""

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
