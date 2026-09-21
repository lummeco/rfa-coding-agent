"""The two things `rfa up` leaves running: the daemon and the board.

Each is a child process with a pid file in `var/`, started in its own session so it outlives the
terminal that started it, with its output appended to `var/<name>.log`. That is the whole supervisor
-- no launchd plist to install, nothing that comes back after a reboot, and `rfa down` really does
stop it.

A pid file is a claim, not proof: the process it names is checked before anything believes it, so a
file left behind by a crash reads as "not running" rather than wedging the pipeline.
"""

import contextlib
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from rfa import tasks

DEFAULT_PORT = 4380


@dataclass
class Service:
    name: str
    args: list[str]
    """What to run, after `python -m rfa`."""

    @property
    def pid_file(self) -> Path:
        return tasks.home() / "var" / f"{self.name}.pid"

    @property
    def log(self) -> Path:
        return tasks.home() / "var" / f"{self.name}.log"

    def pid(self) -> int | None:
        """The live process, or None.

        Two questions, because a pid file is a claim: is that process still alive, and is it still
        ours? A pid the kernel has since handed to something else must not be what `rfa down` kills.
        """
        if not self.pid_file.exists() or not (text := self.pid_file.read_text().strip()).isdigit():
            return None
        try:
            os.kill(pid := int(text), 0)
        except OSError:
            return None  # gone, or someone else's entirely
        return pid if (listed := command(pid)) is None or "rfa" in listed.split() else None

    def start(self) -> bool:
        """Launch it unless it is already up. True when this call launched something.

        A child that dies at once -- a port already taken, a config it cannot read -- must not leave
        a pid file pointing at nothing, so the launch is given a moment to fail before it is
        believed. What it printed on the way out is in its log, and `pid()` then says it is not up.
        """
        if self.pid():
            return False
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "a") as out:
            child = subprocess.Popen(
                [sys.executable, "-u", "-m", "rfa", *self.args],
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=tasks.home(),
                # The child must agree with us about which workspace this is, however `rfa` was found.
                env={**os.environ, "RFA_HOME": str(tasks.home()), "PYTHONPATH": pythonpath()},
            )
        self.pid_file.write_text(str(child.pid))
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=1.0)
            self.pid_file.unlink(missing_ok=True)
        return True

    def stop(self, grace: float = 20.0) -> bool:
        """Stop it and wait for it to go. True when there was something to stop.

        The whole process group, because a run holds a `docker` of its own; and SIGTERM first,
        because the daemon answers it by unwinding -- which is what removes the container.
        """
        if (pid := self.pid()) is None:
            self.pid_file.unlink(missing_ok=True)
            return False
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if self.pid() is None:
                break
            time.sleep(0.2)
        else:
            os.killpg(pid, signal.SIGKILL)
        self.pid_file.unlink(missing_ok=True)
        return True


def command(pid: int) -> str | None:
    """A process's command line, or None when we could not ask -- then being alive has to do."""
    try:
        listed = ["ps", "-p", str(pid), "-o", "command="]
        return subprocess.run(listed, capture_output=True, text=True, check=False).stdout
    except OSError:
        return None


def pythonpath() -> str:
    """Keep whatever put `rfa` on this interpreter's path reachable from the child."""
    here = str(Path(__file__).resolve().parent.parent)
    return os.pathsep.join(filter(None, [here, os.environ.get("PYTHONPATH", "")]))


def services(port: int = DEFAULT_PORT) -> list[Service]:
    """The daemon first: the board is a window onto it, not the other way round."""
    return [Service("daemon", ["daemon"]), Service("board", ["board", "--no-open", "--port", str(port)])]


def url() -> str:
    """Where the board is, with this start's token. The board itself writes it; nobody can guess it.

    Empty when the board is not up, which is the truth: without a live token there is no address
    that would get you in.
    """
    path = tasks.home() / "var" / "board.url"
    return path.read_text().strip() if path.exists() else ""


def forget() -> None:
    """Drop the address. The token in it dies with the board that minted it."""
    (tasks.home() / "var" / "board.url").unlink(missing_ok=True)
