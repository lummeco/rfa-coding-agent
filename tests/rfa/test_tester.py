"""Tests written before the code: the tester's loop, the host's rule for accepting them, and the coder held to them."""

import json

import pytest
from jinja2 import StrictUndefined, Template

from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output
from rfa import board, planner, settings, tasks
from rfa.daemon import DaemonConfig, next_job
from rfa.planner import git
from rfa.tester import TesterAgent, describe
from rfa.worker import CoderAgent, apply_tests, changed_paths

# The suite: `test_old` always passes; `test_new` runs once tests/test_new.py exists, and passes only
# if that file says "green" or the code has been built.
SUITE = """\
cases='<testcase classname="tests.test_old" name="test_old"/>'
if [ -f tests/test_new.py ]; then
  if grep -q green tests/test_new.py || [ -f built ]; then r=''; else r='<failure message="duplicate() does not exist yet"/>'; fi
  cases="$cases<testcase classname=\\"tests.test_new\\" name=\\"test_new\\">$r</testcase>"
fi
if [ -f broke ]; then cases='<testcase classname="tests.test_old" name="test_old"><failure message="x"/></testcase>'; fi
printf '<testsuite>%s</testsuite>' "$cases" > "$RFA_JUNIT"
"""


def act(command: str) -> dict:
    return make_output("working", [{"command": command}])


def submit() -> dict:
    return act("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")


def repo(tmp_path):
    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    (web / "app.py").write_text("x = 1\n")
    (tmp_path / "suite.sh").write_text(SUITE)
    git(web, "init", "-q")
    git(web, "add", "-A")
    git(web, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return web


def summary(tmp_path, **written) -> dict:
    return act(f"cat <<'EOF' > {tmp_path / 'tests.json'}\n{json.dumps(written)}\nEOF")


def write_test(text: str) -> dict:
    return act(f"mkdir -p tests && printf '{text}\\n' > tests/test_new.py")


def run_tester(outputs: list[dict], tmp_path, **config) -> TesterAgent:
    web = repo(tmp_path)
    agent = TesterAgent(
        DeterministicModel(outputs=outputs),
        LocalEnvironment(cwd=str(web)),
        checks=[],
        cwd=str(web),
        root=str(tmp_path),
        test=f"sh {tmp_path / 'suite.sh'}",
        test_baseline={"returncode": 0, "outcomes": {"tests.test_old::test_old": "passed"}},
        junit_path=str(tmp_path / "junit.xml"),
        summary_path=str(tmp_path / "tests.json"),
        system_template="tester",
        instance_template="{{task}} {{rounds}}",
        cost_limit=0,
        **config,
    )
    agent.run("the packet")
    return agent


def test_a_test_that_already_passes_is_sent_back_and_a_red_one_is_accepted(tmp_path):
    """A test written first must be able to tell that the change was made, so it fails now."""
    note = {"name": "test_new", "checks": "Duplicating a line makes exactly one copy", "criterion": 1}
    agent = run_tester(
        [
            write_test("def test_new(): assert green"),
            submit(),
            write_test("def test_new(): assert duplicate()"),
            summary(tmp_path, summary="pins down duplicate", tests=[note]),
            submit(),
        ],
        tmp_path,
    )
    told = [m["content"] for m in agent.messages if "cannot accept these tests" in str(m.get("content"))]
    assert len(told) == 1 and "passes on the code as it is" in told[0]
    assert agent.round == 2 and agent.submitted() and agent.regressions() == [] and agent.asked == []
    shown = describe(agent, agent.summary(), "web")
    assert shown["summary"] == "pins down duplicate" and len(shown["tests"]) == 1
    assert (
        shown["tests"][0].items()
        >= {
            "id": "tests.test_new::test_new",
            "outcome": "failed",
            "message": "duplicate() does not exist yet",
            "checks": "Duplicating a line makes exactly one copy",
            "criterion": 1,
        }.items()
    )


@pytest.mark.parametrize(
    ("outputs", "expected"),
    [
        (
            [act("echo 'x = 2' > app.py"), write_test("def test_new(): assert duplicate()")],
            "not test files: web/app.py",
        ),
        ([act("touch broke"), write_test("def test_new(): assert duplicate()")], "FAILED tests.test_old::test_old"),
        ([act("echo nothing")], "ran no test that was not there before"),
    ],
)
def test_the_host_turns_down_tests_that_are_not_tests_first(outputs, expected, tmp_path):
    """Code changed along with the tests, an old test broken, or no new test at all: none of them is a spec."""
    agent = run_tester([*outputs, submit()], tmp_path, rounds=1)
    assert agent.regressions() and expected in "\n".join(r["output"] for r in agent.regressions())


def test_a_tester_that_finds_the_packet_too_open_asks_instead_of_guessing(tmp_path):
    """Questions are an answer, not a failure: nothing is judged, and the card waits for the owner."""
    asked = "Is the copy placed under the original or at the end?"
    agent = run_tester([summary(tmp_path, questions=[asked, " "]), submit()], tmp_path)
    assert agent.asked == [asked] and agent.regressions() == [] and agent.round == 1


def test_the_shipped_tester_config_renders_in_the_coders_container(tmp_path, monkeypatch):
    """The tester inherits the coder's environment and rfa.yaml's coder block; its own block wins over both."""
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    (tmp_path / "rfa.yaml").write_text(
        "coder:\n  environment:\n    image: node:22-bookworm\n    timeout: 900\ntester:\n  environment:\n    timeout: 300\n"
    )
    config = settings.load("tester")
    assert config["environment"]["image"] == "node:22-bookworm" and config["environment"]["timeout"] == 300
    assert (
        config["agent"]["summary_path"] == "/work/tests.json"
        and settings.load("coder")["environment"]["timeout"] == 900
    )
    web = repo(tmp_path)
    agent = TesterAgent(
        DeterministicModel(outputs=[submit()]),
        LocalEnvironment(cwd=str(web)),
        checks=[],
        cwd=str(web),
        root=str(tmp_path),
        test="pytest",
        lint="ruff check",
        **(config["agent"] | {"summary_path": str(tmp_path / "tests.json")}),
    )
    prompt = Template(config["agent"]["instance_template"], undefined=StrictUndefined).render(
        **agent.get_template_vars(),
        task="# Task\nthe packet",
        repo_list="web",
        primary_repo="/work/repos/web",
        test="pytest",
        reference=["base@main"],
        conventions={"web/agents.md": "Never use default exports."},
        skills=["ui-ux-pro-max"],
    )
    assert "ruff check\nRFA_JUNIT=/tmp/rfa-junit.xml pytest" in prompt and "3 rounds" in prompt
    assert (
        '"questions": []' in prompt and str(tmp_path / "tests.json") in prompt and "/work/reference/base@main" in prompt
    )


def test_the_approved_tests_are_their_own_commit_and_the_coder_may_not_change_them(tmp_path):
    """The coder's diff stays its own work, so a change to an approved test shows in it and is sent back."""
    web = repo(tmp_path)
    patch = "diff --git a/tests/test_new.py b/tests/test_new.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_new.py\n@@ -0,0 +1 @@\n+def test_new(): assert duplicate()\n"
    env = LocalEnvironment(cwd=str(web))
    apply_tests(env, {"web": patch}, root=str(tmp_path))
    assert git(web, "log", "--format=%s").split() == ["tests", "base"] and changed_paths(env, str(tmp_path)) == []
    agent = CoderAgent(
        DeterministicModel(
            outputs=[
                act("echo 'def test_new(): assert True' > tests/test_new.py && touch built"),
                submit(),
                act("git checkout HEAD -- tests/test_new.py"),
                submit(),
            ]
        ),
        env,
        checks=[],
        cwd=str(web),
        root=str(tmp_path),
        test=f"sh {tmp_path / 'suite.sh'}",
        test_baseline={"returncode": 0, "outcomes": {"tests.test_old::test_old": "passed"}},
        junit_path=str(tmp_path / "junit.xml"),
        protected=["web/tests/test_new.py"],
        system_template="coder",
        instance_template="{{task}}",
        cost_limit=0,
    )
    agent.run("the packet")
    told = [m["content"] for m in agent.messages if "failing now" in str(m.get("content"))]
    assert len(told) == 1 and "You changed tests the owner approved" in told[0] and "web/tests/test_new.py" in told[0]
    assert agent.round == 2 and agent.regressions() == [] and changed_paths(env, str(tmp_path)) == ["web/built"]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    return tmp_path


def test_an_idea_with_a_repository_is_planned_right_away_and_one_without_waits_as_a_draft(workspace):
    assert tasks.plan_now(tasks.create("add a duplicate button", ["lummeco/web"])).stage == "planning"
    assert tasks.find(tasks.tasks("planning")[0].id).status == "queued"
    assert tasks.plan_now(tasks.create("something, somewhere")).stage == "draft"


def test_a_planned_card_has_its_tests_written_before_anything_new_is_planned(workspace):
    """Planning is two runs on a card; the second finishes what is nearly in front of you."""
    fresh = tasks.plan_now(tasks.create("a new idea", ["lummeco/web"]))
    planned = tasks.save(tasks.plan_now(tasks.create("a planned idea", ["lummeco/web"])), planned_at="now")
    assert next_job(DaemonConfig()) == ("tests", tasks.find(planned.id))
    tasks.save(tasks.find(planned.id), status="ready", tests_at="now")
    assert next_job(DaemonConfig()) == ("plan", tasks.find(fresh.id))
    # Dropped back into the queue with its tests already written: planned again from the start.
    tasks.save(tasks.find(fresh.id), status="ready")
    tasks.save(tasks.find(planned.id), status="queued")
    assert next_job(DaemonConfig())[0] == "plan"


def test_the_last_answer_to_a_planning_question_sends_the_card_on_to_its_tests(workspace):
    task = tasks.plan_now(tasks.create("duplicate a line", ["lummeco/web"]))
    task.body = "\n# Task\nDuplicate\n\n## Goal\ng\n\n## Current behavior\nc\n\n## Acceptance criteria\n1. one copy\n"
    tasks.save(task, status="question", planned_at="now", open_questions=["Where does the copy go?", "Copy the VAT?"])
    asked = board.answer(task.id, 1, "Yes, with the VAT")
    assert asked.status == "question" and asked.meta["open_questions"] == ["Where does the copy go?"]
    done = board.answer(task.id, 0, "Under the original")
    assert done.status == "queued" and done.meta.get("open_questions") is None
    assert "- Under the original -- the owner's answer to: Where does the copy go?" in done.body
    assert "- Yes, with the VAT -- the owner's answer to: Copy the VAT?\n- Under the original" in done.body
    assert next_job(DaemonConfig()) == ("tests", tasks.find(task.id))
    with pytest.raises(ValueError):
        board.answer(task.id, 0, "no question is waiting")


def test_writing_the_tests_again_throws_the_old_ones_away_and_queues_the_card(workspace):
    task = tasks.save(
        tasks.plan_now(tasks.create("duplicate a line", ["lummeco/web"])),
        status="ready",
        planned_at="now",
        tests_at="now",
    )
    (planner.tests_dir(task.id)).mkdir(parents=True)
    (planner.tests_dir(task.id) / "tests.json").write_text(
        json.dumps({"tests": [], "command": "pytest", "summary": ""})
    )
    (planner.tests_dir(task.id) / "web.patch").write_text("diff --git a/t b/t\n")
    shown = next(t for t in board.snapshot()["tasks"] if t["id"] == task.id)["written_tests"]
    assert shown["command"] == "pytest" and shown["patches"] == {"web": "diff --git a/t b/t\n"}
    again = board.retest(task.id)
    assert again.status == "queued" and again.meta.get("tests_at") is None and not planner.tests_dir(task.id).exists()
    assert next_job(DaemonConfig()) == ("tests", tasks.find(task.id))
    tasks.move(tasks.find(task.id), "todo")
    with pytest.raises(ValueError):
        board.retest(task.id)
