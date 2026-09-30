"""Branch selection: where the work starts, and what gets read beside it."""

import subprocess

import pytest
import yaml

from rfa import planner, settings, tasks

WORKSPACE = {
    "repos": {
        "lummeco/web": "~/dev/web@main",
        "lummeco/base": "~/dev/base@main",
        "lummeco/other": "~/dev/other",
    }
}


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    (tmp_path / "rfa.yaml").write_text(yaml.safe_dump(WORKSPACE))
    return tmp_path


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"branches": ["lummeco/web@feature-x"]}, {"lummeco/web": "feature-x"}),
        ({"branches": "lummeco/web@feature-x"}, {"lummeco/web": "feature-x"}),  # one, unwrapped
        ({"branches": ["lummeco/web@fix/a-b_c.1"]}, {"lummeco/web": "fix/a-b_c.1"}),  # slashes survive
        ({"branches": ["nonsense", "lummeco/web@ok"]}, {"lummeco/web": "ok"}),  # no @: not a branch
        ({}, {}),
    ],
)
def test_the_card_says_where_each_repositorys_work_starts(meta, expected):
    assert settings.start_branches(meta) == expected


def test_a_named_branch_replaces_the_default_the_workspace_is_pinned_to():
    """Most work starts from `main`; the work that does not would otherwise start in the wrong place."""
    config = settings.load()
    assert settings.repo_paths(config, ["lummeco/web"])["web"][1] == "main"
    assert settings.repo_paths(config, ["lummeco/web"], {"lummeco/web": "feature-x"})["web"][1] == "feature-x"
    # A repository with no `@` in rfa.yaml and no card branch has to start somewhere.
    assert settings.repo_paths(config, ["lummeco/other"])["other"][1] == "HEAD"


def test_reference_branches_are_named_for_the_folder_the_agent_will_see():
    """The agent is told `/work/reference/<name>@<branch>`, so that is the key the seeding uses."""
    meta = {"context_branches": ["lummeco/base@refactor", "lummeco/web@old-ui"]}
    found = settings.reference_paths(settings.load(), meta)
    assert sorted(found) == ["base@refactor", "web@old-ui"]
    assert found["base@refactor"][1] == "refactor"


def test_only_three_reference_branches_come_along():
    """Each one is a whole repository in the context window; past a few they are noise to read past."""
    meta = {"context_branches": [f"lummeco/base@b{n}" for n in range(6)]}
    assert len(settings.pairs(meta, "context_branches")) == 6
    assert len(settings.reference_paths(settings.load(), meta)) == settings.MAX_CONTEXT


def test_the_same_branch_twice_is_one_branch():
    meta = {"context_branches": ["lummeco/base@x", "lummeco/base@x", "lummeco/base@y"]}
    assert settings.pairs(meta, "context_branches") == [("lummeco/base", "x"), ("lummeco/base", "y")]


@pytest.mark.parametrize("key", ["branches", "context_branches"])
def test_a_repository_nobody_configured_is_refused(key):
    """Otherwise a card could point the seeding at any path on this Mac."""
    with pytest.raises(KeyError):
        if key == "branches":
            settings.repo_paths(settings.load(), ["lummeco/ghost"])
        else:
            settings.reference_paths(settings.load(), {"context_branches": ["lummeco/ghost@main"]})


def test_where_the_work_started_and_where_it_landed_stay_separate():
    """The worker writes `landed:` on the card when it finishes. Sharing the name with `branches:`
    would overwrite where the work started, and running the card again would begin somewhere else."""
    meta = {"branches": ["lummeco/web@feature-x"], "landed": {"web": "rfa/20260921-153000-thing"}}
    assert settings.start_branches(meta) == {"lummeco/web": "feature-x"}


def test_a_run_offline_starts_from_the_last_fetch_and_the_card_says_how_old_it_is(tmp_path):
    """Nothing stops for a missing network. But last week's `main` must not pass for today's."""

    def git(at, *args) -> str:
        return subprocess.run(
            ["git", "-C", str(at), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    origin, web, base = tmp_path / "origin", tmp_path / "web", tmp_path / "base"
    git(tmp_path, "init", "-qb", "main", str(origin))
    git(origin, "commit", "-qm", "one", "--allow-empty")
    git(tmp_path, "clone", "-q", "--single-branch", str(origin), str(web))
    git(tmp_path, "clone", "-q", str(origin), str(base))
    git(origin, "commit", "-qm", "two", "--allow-empty")
    git(origin, "branch", "feature")
    repos = {"web": (web, "feature"), "base": (base, "main")}
    # `feature` did not exist when `web` was cloned, single-branch: only the spelled-out refspec finds it.
    assert planner.fetched(repos) is None and planner.stamp(web).exists()
    assert planner.pin(repos) == {
        "web": (web, git(origin, "rev-parse", "feature")),
        "base": (base, git(origin, "rev-parse", "main")),
    }

    planner.stamp(base).unlink()
    origin.rename(tmp_path / "gone")
    note = planner.fetched(repos, {"web@main": (web, "main")})
    assert note.startswith("Could not reach origin, so this started from web as of ")
    assert note.endswith(", base as last fetched outside rfa.") and note.count("web") == 1
    assert planner.pin(repos)["base"][1] == git(tmp_path / "gone", "rev-parse", "main")
