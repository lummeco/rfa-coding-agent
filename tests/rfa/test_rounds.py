"""Rounds: what each coding round did, the comments left on it, and the tests the host saw pass."""

import json
import subprocess

import pytest

from rfa import board, junit, rounds, tasks

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.rfa.test_a" name="test_passes" time="0.1"/>
  <testcase classname="tests.rfa.test_a" name="test_breaks"><failure message="assert 1 == 2">trace</failure></testcase>
  <testcase classname="tests.rfa.test_a" name="test_param[x]"><skipped message="not here"/></testcase>
  <testcase classname="tests.rfa.test_a" name="test_param[y]"/>
  <testcase classname="tests.rfa.test_a.TestThing" name="test_crashes"><error message="fixture blew up"/></testcase>
  <testcase classname="tests.rfa.test_b" name="test_skipped"><skipped/></testcase>
</testsuite></testsuites>"""

PATCH = """diff --git a/tests/rfa/test_a.py b/tests/rfa/test_a.py
--- a/tests/rfa/test_a.py
+++ b/tests/rfa/test_a.py
@@ -10,3 +10,15 @@ import pytest
 def test_passes():
     assert True
-def test_old():
+def test_breaks():
+    assert 1 == 2
+
+@pytest.mark.parametrize("v", ["x", "y"])
+def test_param(v):
+    pass
+
+def test_never_collected():
+    pass
+
+    def test_crashes(self):
+        pass
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 x = 1
+def test_helper_in_app_code(): pass
"""


def test_the_report_gives_every_test_its_worst_outcome():
    cases = junit.parse(REPORT)
    assert [(c["id"], c["outcome"]) for c in cases] == [
        ("tests.rfa.test_a::test_passes", "passed"),
        ("tests.rfa.test_a::test_breaks", "failed"),
        ("tests.rfa.test_a::test_param[x]", "skipped"),
        ("tests.rfa.test_a::test_param[y]", "passed"),
        ("tests.rfa.test_a.TestThing::test_crashes", "error"),
        ("tests.rfa.test_b::test_skipped", "skipped"),
    ]
    assert cases[1]["message"] == "assert 1 == 2" and junit.parse("not xml") is None


def test_every_test_the_diff_shows_is_marked_with_how_it_ran_and_one_that_never_ran_is_missing():
    """The point of the marks: a test that never ran would otherwise look exactly like a pass."""
    marks = junit.marks(PATCH, junit.parse(REPORT))
    assert {line: (m["name"], m["outcome"]) for line, m in marks["tests/rfa/test_a.py"].items()} == {
        10: ("test_passes", "passed"),
        12: ("test_breaks", "failed"),
        16: ("test_param", "passed"),  # one param skipped, one passed: it ran and passed
        19: ("test_never_collected", "missing"),
        22: ("test_crashes", "failed"),
    }
    assert marks["tests/rfa/test_a.py"][12]["message"] == "assert 1 == 2"
    assert "src/app.py" not in marks  # a `def test_` outside a test file is not a test


@pytest.mark.parametrize(
    ("case", "path", "expected"),
    [
        ({"file": "", "classname": "tests.test_x"}, "lummeco-base/tests/test_x.py", True),
        ({"file": "", "classname": "tests.test_x.TestY"}, "tests/test_x.py", True),
        ({"file": "", "classname": "tests.test_y"}, "tests/test_x.py", False),
        ({"file": "./src/a.test.ts", "classname": "a works"}, "web/src/a.test.ts", True),
        ({"file": "src/b.test.ts", "classname": "b"}, "src/a.test.ts", False),
    ],
)
def test_a_result_is_placed_in_its_file_by_path_or_by_module(case, path, expected):
    assert junit.belongs(case, path) is expected


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ("def test_a(x):", "test_a"),
        ("    async def test_b():", "test_b"),
        ("  it('renders the list', () => {", "renders the list"),
        ('test.only("adds", async () => {', "adds"),
        ("func TestParse(t *testing.T) {", "TestParse"),
        ("def helper():", ""),
    ],
)
def test_a_test_definition_is_found_in_each_language(line, name):
    assert junit.defined(line) == name


@pytest.fixture
def done_card(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    task = tasks.move(tasks.move(tasks.create("an idea", ["a/web"]), "planning"), "todo")
    task = tasks.move(tasks.move(task, "under-work", actor="worker"), "done", actor="worker", attempts=3)
    task.body = "\n# Task\n\nthe packet\n\n## Result\n\nit went fine\n"
    return tasks.save(task, landed={"web": f"rfa/{task.id}"})


def comment(id: str, **fields) -> dict:
    return rounds.comment(id, {"repo": "web", "file": "a.py", "line": 3, "text": "rename this"} | fields)


def test_fixing_comments_sends_the_ticket_back_alone_with_its_attempts_given_back(done_card):
    with pytest.raises(ValueError, match="no comments"):
        board.fix(done_card.id)
    comment(done_card.id)
    task = board.fix(done_card.id)
    assert (task.stage, task.status, task.attempts) == ("todo", "todo", 0)
    assert "## Result" not in task.body and "the packet" in task.body
    with pytest.raises(ValueError, match="only a done card"):
        board.fix(done_card.id)


def test_a_comment_needs_words_and_can_be_taken_back(done_card):
    with pytest.raises(ValueError):
        comment(done_card.id, text="  ")
    first = comment(done_card.id)["pending"][0]
    assert (first["side"], first["line"], first["text"]) == ("new", 3, "rename this")
    comment(done_card.id, line=9, side="old")
    assert [c["line"] for c in rounds.comment(done_card.id, {"delete": first["id"]})["pending"]] == [9]


def test_a_comment_on_the_whole_round_has_no_place(done_card):
    whole = rounds.comment(done_card.id, {"text": "the planner prompt changes too", "line": "", "file": ""})["pending"][0]
    assert whole.keys() == {"id", "text"} and whole["text"] == "the planner prompt changes too"


def test_only_a_round_that_landed_clears_the_comments_it_answered(done_card):
    """A fix that failed leaves the comments there to try again."""
    ids = [c["id"] for c in comment(done_card.id)["pending"]]
    assert rounds.record(done_card.id, {"n": 1, "landed": False}, answered=[])["pending"]
    state = rounds.record(done_card.id, {"n": 2, "landed": True}, answered=ids)
    assert state["pending"] == [] and [r["n"] for r in state["rounds"]] == [1, 2]


def test_a_card_from_before_rounds_shows_its_one_patch_as_the_original_round(done_card, tmp_path):
    run = rounds.run_dir(done_card.id)
    run.mkdir(parents=True)
    (run / "web.patch").write_text(PATCH)
    assert rounds.view(done_card)["rounds"] == [
        {"n": 0, "kind": "original", "plan": "\n# Task\n\nthe packet\n", "landed": True}
    ]
    assert rounds.diff(done_card.id, 0)["repos"][0]["patch"] == PATCH
    assert rounds.diff(done_card.id, 1) is None
    # Its first fix round keeps it as round 0 rather than taking its place.
    state = rounds.record(done_card.id, {"n": 1, "kind": "fix"}, [], earlier=rounds.view(done_card)["rounds"])
    assert [(r["n"], r["kind"]) for r in state["rounds"]] == [(0, "original"), (1, "fix")]
    assert rounds.diff(done_card.id, 0)["repos"][0]["patch"] == PATCH


def test_a_round_diff_carries_its_tests_on_the_repository_they_ran_in(done_card):
    folder = rounds.round_dir(done_card.id, 0)
    folder.mkdir(parents=True)
    (folder / "web.patch").write_text(PATCH)
    (folder / "api.patch").write_text(PATCH)
    (folder / "tests.json").write_text(json.dumps({"repo": "web", "cases": junit.parse(REPORT)}))
    found = {r["repo"]: r["marks"] for r in rounds.diff(done_card.id, 0)["repos"]}
    assert found["api"] == {} and found["web"]["tests/rfa/test_a.py"][19]["outcome"] == "missing"


def test_the_copy_line_brings_every_round_of_a_branch_not_just_the_last(tmp_path):
    repo = tmp_path / "web"
    repo.mkdir()

    def run(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout

    def commit(message):
        run("add", "-A")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)

    run("init", "-qb", "main")
    (repo / "a.txt").write_text("one\n")
    commit("base")
    base = run("rev-parse", "HEAD").strip()
    run("checkout", "-qb", "rfa/t")
    for line in ("two\n", "three\n"):
        (repo / "a.txt").write_text((repo / "a.txt").read_text() + line)
        commit(line)
    run("checkout", "-q", "main")

    state = {"rounds": [{"commits": {"web": {"base": base}}}, {"commits": {"web": {"base": "later"}}}]}
    command = board.copy_commands({"web": "rfa/t"}, {"a/web": str(repo)}, state)["web"]
    assert subprocess.run(command, shell=True, capture_output=True).returncode == 0, command
    assert (repo / "a.txt").read_text() == "one\ntwo\nthree\n"
