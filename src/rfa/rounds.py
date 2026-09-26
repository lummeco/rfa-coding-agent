"""A card's rounds: the original coding run and every fix after it, one record each.

The first round codes the packet from the card's start branch. Every round after it starts from the
branch the last one landed, and lands one more commit on it -- so each round's patch is exactly its
own diff against the round before, and the branch stays the one thing a pull request ships.

Comments you leave on the latest round's diff wait in `pending` until a fix round takes them. The
round that takes them records each one with the coder's answer, and only a round that landed clears
them: a fix that failed leaves them there to try again.

All of it lives in var/runs/<id>/rounds.json beside the rounds' own folders, not on the card: the
card stays a ticket you can read and edit, and the history is the host's record of what happened.
"""

import json
import os
import re
import secrets
from pathlib import Path

from rfa import junit, tasks

RESULT_RE = re.compile(r"\n##+ +(Result|Review|Not exported) *\n.*", re.DOTALL)


def packet_only(body: str) -> str:
    """The ticket, without what previous runs wrote under it.

    A card that comes back for another coding round must arrive as the ticket it started as. Left
    alone, the body would grow a result note and a review round after round, and the coder would
    spend its context reading the story of its own failures instead of the task.
    """
    return RESULT_RE.sub("", body).rstrip() + "\n"


def run_dir(id: str) -> Path:
    return tasks.home() / "var" / "runs" / id


def round_dir(id: str, n: int) -> Path:
    return run_dir(id) / f"round-{n}"


def load(id: str) -> dict:
    path = run_dir(id) / "rounds.json"
    return json.loads(path.read_text()) if path.exists() else {"rounds": [], "pending": []}


def save(id: str, state: dict) -> dict:
    path = run_dir(id) / "rounds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".rounds.json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)
    return state


def record(id: str, entry: dict, answered: list[str], earlier: list[dict] | None = None) -> dict:
    """Add a finished round, and clear the pending comments it answered.

    `earlier` is what `view` showed before this round: for a card from before rounds were recorded,
    its one original round, which is written down now rather than lost behind the first fix.
    """
    with tasks._lock():
        state = load(id)
        state["rounds"] = state["rounds"] or list(earlier or [])
        state["rounds"].append(entry)
        state["pending"] = [c for c in state["pending"] if c["id"] not in answered]
        return save(id, state)


def comment(id: str, payload: dict) -> dict:
    """Add a comment on the latest round's diff, or delete one by its `delete` id.

    A comment with no `file` is on the round as a whole: what it asks may reach files the diff
    never touched, so it carries no place and no quoted line.
    """
    with tasks._lock():
        state = load(id)
        if cid := payload.get("delete"):
            state["pending"] = [c for c in state["pending"] if c["id"] != cid]
            return save(id, state)
        if not (text := str(payload.get("text") or "").strip()):
            raise ValueError("a comment needs some words")
        where = payload.get("file") and {
            "repo": str(payload["repo"]),
            "file": str(payload["file"]),
            "side": "old" if payload.get("side") == "old" else "new",
            "line": int(payload["line"]),
            "context": str(payload.get("context") or "")[:2000],
        }
        state["pending"].append({"id": secrets.token_hex(4), **(where or {}), "text": text[:4000]})
        return save(id, state)


def view(task: tasks.Task) -> dict:
    """What the board draws for a card's rounds.

    A card that ran before rounds were recorded has its one patch at the top of its run folder; it
    shows as an original round with no summary rather than as nothing at all.
    """
    state = load(task.id)
    if not state["rounds"] and any(run_dir(task.id).glob("*.patch")):
        state["rounds"] = [{"n": 0, "kind": "original", "plan": packet_only(task.body), "landed": True}]
    return state


def files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/.+? b/(.+)$", patch, re.MULTILINE)


def diff(id: str, n: int) -> dict | None:
    """One round's patches, per repository, with its test results placed on the lines they belong to."""
    folder = round_dir(id, n)
    if not folder.is_dir():
        if n:
            return None
        folder = run_dir(id)
    tests_file = folder / "tests.json"
    tests = json.loads(tests_file.read_text()) if tests_file.exists() else None
    repos = []
    for patch in sorted(folder.glob("*.patch")):
        text = patch.read_text(errors="replace")
        tested = tests and tests.get("repo") == patch.stem and tests.get("cases") is not None
        repos.append({"repo": patch.stem, "patch": text, "marks": junit.marks(text, tests["cases"]) if tested else {}})
    return {"repos": repos, "tests": tests}
