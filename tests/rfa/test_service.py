"""The supervisor: real child processes, real pid files, started and stopped the way `rfa up` does."""

import time

import pytest

from rfa import tasks
from rfa.service import Service, forget, services, url


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    return tmp_path


def wait_for(predicate, seconds: float = 20) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.1)
    return predicate()


def test_the_daemon_runs_detached_in_the_workspace_it_was_started_from(workspace):
    """The whole lifecycle: a second `rfa up` starts nothing, the child agrees with us about which
    workspace this is however `rfa` was found, and `rfa down` leaves no pid file behind."""
    daemon = services()[0]
    assert daemon.start() is True
    assert daemon.start() is False
    assert wait_for(lambda: daemon.log.exists() and "daemon up" in daemon.log.read_text())
    assert str(workspace) in daemon.log.read_text()
    assert daemon.pid() == int(daemon.pid_file.read_text())
    assert daemon.stop() is True
    assert (daemon.pid(), daemon.pid_file.exists()) == (None, False)
    assert daemon.stop() is False


@pytest.mark.parametrize("contents", ["", "not a pid", "1"])
def test_a_pid_file_is_a_claim_not_proof(contents):
    """Nothing, nonsense, or pid 1 -- alive since boot and never ours. A file a crash left behind
    must read as stopped, or `rfa up` refuses to start and `rfa down` kills a stranger."""
    daemon = services()[0]
    daemon.pid_file.parent.mkdir(parents=True, exist_ok=True)
    daemon.pid_file.write_text(contents)
    assert daemon.pid() is None
    assert daemon.stop() is False and not daemon.pid_file.exists()


def test_there_is_no_address_without_a_live_board(workspace):
    """The board writes down where it is, token and all, because nobody else can guess the token.
    No file means no board -- and guessing a tokenless URL would only 403."""
    assert url() == ""
    (workspace / "var" / "board.url").write_text("http://127.0.0.1:4380/#token=abc\n")
    assert url() == "http://127.0.0.1:4380/#token=abc"
    forget()
    assert url() == ""


def test_a_child_that_dies_at_once_is_not_a_running_service():
    """A port already taken or a config it cannot read: `rfa up` has to notice, rather than leave a
    pid file pointing at nothing and report a board that was never there."""
    doomed = Service("daemon", ["no-such-command"])
    assert doomed.start() is True
    assert (doomed.pid(), doomed.pid_file.exists()) == (None, False)
