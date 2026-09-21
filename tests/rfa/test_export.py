"""The export gate: what a coder's diff is allowed to carry out of the container."""

import subprocess

import pytest

from minisweagent.environments.local import LocalEnvironment
from rfa import planner, worker


def repo(path, **files) -> None:
    """A git repository with one commit, then `files` written on top of it -- what a run looks like
    the moment the coder submits: a base commit, and working-tree changes that are not committed."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "app.js").write_text("start\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], check=True
    )
    for name, body in files.items():
        (path / name.replace("|", "/")).parent.mkdir(parents=True, exist_ok=True)
        (path / name.replace("|", "/")).write_text(body)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """`diffs` reads /work/repos/<name>; a LocalEnvironment makes that this folder instead."""
    monkeypatch.setattr(planner, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "REPOS_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize(
    "path",
    [
        ".github|workflows|release.yml",
        "sub|.gitlab-ci.yml",
        "Jenkinsfile",
        ".gitattributes",
    ],
)
def test_ci_configuration_never_leaves_the_container(workspace, path):
    """A workflow the coder wrote runs the moment the branch is pushed, with nobody reading it first.
    The packet already refuses to aim at these; this is the same rule at the coder's end."""
    repo(workspace / "web", **{path: "whatever\n", "app.js": "start\nreal work\n"})
    patches, dropped = worker.diffs(LocalEnvironment(), ["web"])
    assert dropped == {"web": [path.replace("|", "/")]}
    assert "real work" in patches["web"]  # the rest of the diff is untouched
    assert path.split("|")[-1] not in patches["web"]


def test_a_run_that_only_touched_ci_exports_nothing_at_all(workspace):
    repo(workspace / "web", **{".github|workflows|x.yml": "on: [push]\n"})
    assert worker.diffs(LocalEnvironment(), ["web"]) == ({}, {"web": [".github/workflows/x.yml"]})


def test_an_ordinary_diff_is_carried_whole_and_reports_nothing_dropped(workspace):
    repo(workspace / "web", **{"app.js": "start\nreal work\n", "src|new.js": "export const x = 1\n"})
    patches, dropped = worker.diffs(LocalEnvironment(), ["web"])
    assert dropped == {}
    assert "real work" in patches["web"] and "src/new.js" in patches["web"]


def test_the_card_says_what_was_dropped():
    """A silent drop is a lie: you would push a branch believing it held work it does not."""
    note = worker.dropped_note({"web": [".github/workflows/x.yml"]})
    assert ".github/workflows/x.yml" in note and "not" in note
    assert worker.dropped_note({}) == ""
