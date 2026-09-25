"""The coder loop: mini's agent, with the packet's checks standing between submitting and done."""

import subprocess

import pytest

from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError
from minisweagent.models.test_models import DeterministicModel, make_output
from rfa.planner import git
from rfa.worker import (
    UNJUDGEABLE,
    CoderAgent,
    changed_files,
    changed_note,
    environment,
    free_branch,
    land,
    landable,
    outside,
    record_tests,
    report,
    summarize,
)


def act(command: str, prompt_tokens: int = 0) -> dict:
    output = make_output("working", [{"command": command}])
    if prompt_tokens:
        output["extra"]["response"] = {"usage": {"prompt_tokens": prompt_tokens}}
    return output


def cut_off(total_tokens: int) -> dict:
    """A response the provider stopped at `length` -- what both kinds of truncation look like here."""
    return {"_cut_off": {"choices": [{"finish_reason": "length"}], "usage": {"total_tokens": total_tokens}}}


class CutOffModel(DeterministicModel):
    """A model that raises on a `cut_off` output the way the real one does: a FormatError carrying
    the response that was stopped. The response stays plain data, so the trajectory still saves."""

    def query(self, messages: list[dict], **kwargs) -> dict:
        if response := self.config.outputs[self.current_index + 1].get("_cut_off"):
            self.current_index += 1
            raise FormatError({"role": "user", "content": "no tool calls", "extra": {"response": response}})
        return super().query(messages, **kwargs)


def submit() -> dict:
    return act("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")


def run_coder(outputs: list[dict], tmp_path, checks: list[str], **config) -> CoderAgent:
    """A coder over one repository, `web`, with a base commit: what `edited` looks at in the container."""
    repo = tmp_path / "web"
    repo.mkdir(exist_ok=True)
    for args in (
        ["init", "-q"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base", "--allow-empty"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    agent = CoderAgent(
        CutOffModel(outputs=outputs),
        LocalEnvironment(cwd=str(repo)),
        checks=checks,
        cwd=str(repo),
        root=str(tmp_path),
        baseline=config.pop("baseline", None),
        system_template="coder",
        instance_template="{{task}} {{rounds}} {{broken_checks}} {{step_limit}} steps",
        cost_limit=0,
        **config,
    )
    agent.run("the packet", broken_checks=[])
    return agent


def test_passing_checks_end_the_run(tmp_path):
    agent = run_coder([act("touch fixed"), submit()], tmp_path, ["test -f fixed"])
    assert [r["ok"] for r in agent.results] == [True] and agent.round == 1
    assert agent.messages[-1]["extra"]["exit_status"] == "Submitted"


def test_a_failing_check_goes_back_to_the_coder_and_the_fix_is_accepted(tmp_path):
    """The retry the board counts: the coder keeps its session and sees the real command output."""
    agent = run_coder([submit(), act("touch fixed"), submit()], tmp_path, ["test -f fixed"])
    assert agent.round == 2 and all(r["ok"] for r in agent.results)
    assert agent.messages[-1]["extra"]["exit_status"] == "Submitted"
    assert any("failing now" in m.get("content", "") for m in agent.messages)


def test_the_check_output_itself_reaches_the_coder_not_just_a_verdict(tmp_path):
    agent = run_coder([submit(), submit()], tmp_path, ["echo 'ReferenceError: line is not defined'; exit 1"])
    assert any("ReferenceError: line is not defined" in m.get("content", "") for m in agent.messages)


def test_the_run_gives_up_after_the_configured_rounds(tmp_path):
    agent = run_coder([submit()] * 3, tmp_path, ["false"], rounds=3)
    assert agent.round == 3 and not any(r["ok"] for r in agent.results)
    assert agent.messages[-1]["extra"]["exit_status"] == "Submitted"


def test_a_packet_with_no_checks_submits_straight_through(tmp_path):
    """The planner found no test command; the host has nothing to run and says so honestly."""
    agent = run_coder([submit()], tmp_path, [])
    assert agent.results == [] and agent.round == 1


def test_every_check_runs_so_the_coder_sees_all_the_damage_at_once(tmp_path):
    agent = run_coder([submit(), submit()], tmp_path, ["true", "false", "true"])
    assert [r["ok"] for r in agent.results] == [True, False, True]
    assert report(agent.results).count("$ ") == 1


@pytest.mark.parametrize(
    ("patches", "checks", "expected"),
    [
        ({}, ["false"], "changed nothing"),
        ({"web": "diff"}, ["false"], "Broke after 1 round(s): false"),
    ],
)
def test_the_card_says_why_a_run_failed(patches, checks, expected, tmp_path):
    assert expected in summarize(run_coder([submit()], tmp_path, checks, rounds=1), patches)


def test_a_full_window_ends_the_run_instead_of_spending_it_on_the_same_wall(tmp_path):
    """Three retries into a window with no room left are three ways to lose the rest of the run."""
    agent = run_coder([cut_off(131000)] * 3, tmp_path, [], context_window=131072)
    assert agent.exit_status() == "ContextExceeded" and agent.n_calls == 1
    assert "filled the model's context window" in summarize(agent, {})


def test_a_model_that_only_rambled_is_asked_again_rather_than_cut_off(tmp_path):
    """The same `length` response, with room to spare: being brief is something it can still do."""
    agent = run_coder([cut_off(40000), act("touch fixed"), submit()], tmp_path, ["test -f fixed"], rounds=1)
    assert agent.exit_status() == "Submitted" and all(r["ok"] for r in agent.results)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"context_window": 131072}, "120000 of the model's 131072 tokens"),
        ({}, "2 of the 2 steps since your first edit"),
    ],
)
def test_the_coder_is_told_when_the_run_is_about_to_end_under_it(config, expected, tmp_path):
    """A deadline nobody can see is a run that stops mid-edit with the work still in the container."""
    outputs = [act("touch half", prompt_tokens=120000)] + [act("true", prompt_tokens=120000)] * 2
    agent = run_coder(outputs, tmp_path, ["false"], baseline={"false": True}, step_limit=2, **config)
    warned = [m for m in agent.messages if expected in str(m.get("content"))]
    assert warned and "submit" in warned[-1]["content"]
    assert agent.exit_status() == "LimitsExceeded"


def test_reading_and_setting_up_cost_no_steps_until_the_first_edit(tmp_path):
    """Sixty steps spent understanding a repository is not a run going badly. The budget starts at the edit."""
    agent = run_coder([act("ls")] * 5 + [act("touch fixed"), submit()], tmp_path, ["test -f fixed"], step_limit=2)
    assert agent.exit_status() == "Submitted" and agent.n_calls == 7 and agent.edited_at == 6
    assert not any("steps since your first edit" in str(m.get("content")) for m in agent.messages[:-4])
    assert "2 steps" in agent.messages[1]["content"]  # the template still sees the budget, not mini's 0


def test_a_run_cut_off_with_a_clean_diff_is_submitted_by_the_host(tmp_path):
    """Out of steps at step 120 with green tests was the run this exists for: the checks decide, not the cut-off."""
    agent = run_coder([act("touch half")] * 3, tmp_path, ["test -f half"], step_limit=2)
    assert agent.exit_status() == "AutoSubmitted" and agent.submitted() and agent.regressions() == []
    assert agent.messages[-1]["extra"]["cut_off"] == "LimitsExceeded" and agent.n_calls == 3
    assert landable(agent, {"web": "diff"})
    note = changed_note(agent, {"web": "rfa/t"}, {"web": (tmp_path, "abc")}, tmp_path)
    assert "never submitted" in note and "ran out of steps" in note and "unfinished" in note


def test_a_run_cut_off_with_a_regression_fails_on_the_regression(tmp_path):
    agent = run_coder(
        [act("touch half")] * 3, tmp_path, ["test ! -f half"], baseline={"test ! -f half": True}, step_limit=2
    )
    assert agent.exit_status() == "LimitsExceeded" and not landable(agent, {"web": "diff"})
    assert summarize(agent, {"web": "diff"}) == "Broke when it was cut off (LimitsExceeded): test ! -f half"


def test_a_run_that_was_cut_off_before_changing_anything_does_not_run_the_checks(tmp_path):
    """Nothing to judge, and `context_window` is the cut-off that comes with no edit: the window filled reading."""
    agent = run_coder([act("ls", prompt_tokens=1000), cut_off(131000)] * 2, tmp_path, ["exit 3"], context_window=131072)
    assert agent.exit_status() == "ContextExceeded" and agent.results == [] and not agent.submitted()
    assert "changed nothing" in summarize(agent, {})


def test_the_shipped_coder_config_renders_with_the_variables_the_worker_passes(tmp_path):
    """StrictUndefined: a variable the worker forgets to pass is a crash, not a vague prompt."""
    import yaml

    from rfa import settings

    config = yaml.safe_load((settings.CONFIG_DIR / "coder.yaml").read_text())["agent"]
    agent = CoderAgent(
        DeterministicModel(outputs=[act("touch fixed"), submit()]),
        LocalEnvironment(cwd=str(tmp_path)),
        checks=["echo checks-ran"],
        cwd=str(tmp_path),
        **config,
    )
    agent.run(
        "# Task\nthe packet",
        repo_list="web",
        primary_repo="/work/repos/web",
        checks=["echo checks-ran"],
        broken_checks=["pnpm typecheck"],
        reference=[],
        test="pytest -q --junitxml=$RFA_JUNIT",
        comments=[{"repo": "web", "file": "src/a.py", "side": "new", "line": 7, "context": "x = 1", "text": "use 2"}],
    )
    prompt = agent.messages[1]["content"]
    assert "execution packet" in agent.messages[0]["content"]
    assert "120 steps, counted from your first edit" in prompt
    assert "echo checks-ran" in prompt and "/work/repos/web" in prompt
    assert "already failing" in prompt and "pnpm typecheck" in prompt
    assert "RFA_JUNIT=/tmp/rfa-junit.xml pytest -q --junitxml=$RFA_JUNIT" in prompt
    assert "1. `web/src/a.py` line 7: use 2" in prompt and "x = 1" in prompt and '"answers"' in prompt
    assert "/work/summary.json" in prompt
    assert agent.round == 1 and all(r["ok"] for r in agent.results)


def test_settings_merge_a_per_stage_override_over_the_packaged_config(tmp_path, monkeypatch):
    from rfa import settings, tasks

    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    (tmp_path / "rfa.yaml").write_text(
        "repos:\n  a/web: ~/dev/web\nmodel:\n  model_name: ollama_chat/qwen\n"
        "coder:\n  environment:\n    image: node:22-bookworm\n"
    )
    coder, planner = settings.load("coder"), settings.load("planner")
    assert coder["environment"]["image"] == "node:22-bookworm"
    assert planner["environment"]["image"] == "python:3.12-slim-bookworm"  # untouched by the coder block
    assert coder["model"]["model_name"] == planner["model"]["model_name"] == "ollama_chat/qwen"
    assert "coder" not in planner and settings.repo_paths(coder, ["a/web"])["web"][1] == "HEAD"
    assert tasks.home() == tmp_path


def test_infrastructure_trouble_puts_the_card_back_in_the_queue(tmp_path, monkeypatch):
    """A card must never be stranded in under-work with no worker on it."""
    from rfa import tasks
    from rfa.worker import run_task

    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    task = tasks.move(tasks.move(tasks.create("an idea", ["a/web"]), "planning"), "todo")
    config = {"repos": {"a/web": str(tmp_path)}, "environment": {"environment_class": "docker", "image": "nope:nope"}}

    with pytest.raises(Exception):
        run_task(task, config)
    back = tasks.find(task.id)
    assert (back.stage, back.status, back.attempts) == ("todo", "todo", 1)
    assert [e["type"] for e in tasks.events(task.id)][-2:] == ["run_error", "moved"]


def test_a_repository_whose_checks_all_fail_before_the_coder_starts_is_refused_with_the_output():
    """Forty-five minutes of coding that nothing could judge: the card says what to fix instead."""
    note = UNJUDGEABLE.format(
        report=report(
            [
                {
                    "command": "docker compose exec app pytest",
                    "ok": False,
                    "returncode": 127,
                    "output": "docker: not found",
                }
            ]
        )
    )
    assert "did not start" in note and "docker: not found" in note and "containers:" in note


def test_the_repository_container_block_overrides_the_stage_image_and_adds_variables():
    stage = {"environment_class": "docker", "image": "node:22", "cwd": "/work/repos", "env": {"CI": "1"}}
    spec = {"image": "python:3.11", "env": {"POSTGRES_HOST": "127.0.0.1"}, "setup": ["apt-get install postgresql"]}
    assert environment({"environment": stage}, spec) == {
        "environment_class": "docker",
        "image": "python:3.11",
        "cwd": "/work/repos",
        "env": {"CI": "1", "POSTGRES_HOST": "127.0.0.1"},
    }
    assert environment({"environment": stage}, {}) == stage  # a repository with no block gets the stage's container


def test_a_check_that_was_already_red_is_not_counted_against_the_run(tmp_path):
    """Otherwise a repository with one failing test burns every round on something nobody asked for."""
    agent = run_coder([submit()], tmp_path, ["false"], baseline={"false": False})
    assert agent.regressions() == [] and agent.round == 1
    assert agent.messages[-1]["extra"]["exit_status"] == "Submitted"  # ended, not retried


def test_breaking_a_check_that_was_green_still_costs_a_round(tmp_path):
    agent = run_coder(
        [submit(), act("touch fixed"), submit()], tmp_path, ["test -f fixed"], baseline={"test -f fixed": True}
    )
    assert agent.round == 2 and agent.regressions() == []


def test_a_run_is_judged_on_regressions_only_even_when_other_checks_are_red(tmp_path):
    agent = run_coder([submit()], tmp_path, ["false", "true"], baseline={"false": False, "true": True})
    assert [r["ok"] for r in agent.results] == [False, True] and agent.regressions() == []


def junit_command(tmp_path) -> str:
    """A suite of two tests: `test_new` passes once the coder has made `fixed`, `test_old` is always red."""
    new = 'if [ -f fixed ]; then o=""; else o="<failure message=\\"boom\\"/>"; fi'
    cases = (
        '<testsuite><testcase classname="tests.test_a" name="test_new">%s</testcase>'
        '<testcase classname="tests.test_a" name="test_old"><failure message="was red"/></testcase></testsuite>'
    )
    return f'{new}; printf \'{cases}\' "$o" > "$RFA_JUNIT"'


def test_a_test_the_coder_broke_goes_back_to_it_by_name_and_the_fix_is_accepted(tmp_path):
    """The host runs the suite, not the model; a test already red before the run is not held against it."""
    agent = run_coder(
        [submit(), act("touch fixed"), submit()],
        tmp_path,
        [],
        test=junit_command(tmp_path),
        test_baseline={"returncode": 0, "outcomes": {"tests.test_a::test_old": "failed"}},
        junit_path=str(tmp_path / "junit.xml"),
    )
    told = [m["content"] for m in agent.messages if "failing now" in str(m.get("content"))]
    assert (
        len(told) == 1
        and "FAILED tests.test_a::test_new\nboom" in told[0]
        and "FAILED tests.test_a::test_old" not in told[0]
    )
    assert agent.round == 2 and agent.regressions() == [] and landable(agent, {"web": "diff"})
    assert [c["outcome"] for c in agent.tests["cases"]] == ["passed", "failed"]


def test_a_suite_that_wrote_no_report_passed_nothing(tmp_path):
    """A crash before the report is written looks like a clean exit; it must not land as one."""
    agent = run_coder([submit()], tmp_path, [], test="true", rounds=1, junit_path=str(tmp_path / "junit.xml"))
    assert agent.tests["cases"] is None and not landable(agent, {"web": "diff"})
    assert "wrote no JUnit report" in agent.regressions()[0]["output"]


def test_the_host_keeps_its_own_record_of_every_test_with_how_it_was_before(tmp_path):
    tests = {
        "command": "t",
        "returncode": 1,
        "output": "",
        "cases": [
            {"id": "m::a", "classname": "m", "name": "a", "file": "", "outcome": "failed", "message": "was red"},
            {"id": "m::b", "classname": "m", "name": "b", "file": "", "outcome": "skipped", "message": ""},
        ],
    }
    found = record_tests(tests, {"returncode": 1, "outcomes": {"m::a": "failed"}}, "web", 2)
    assert found["ok"] and found["repo"] == "web" and found["attempts"] == 2
    assert found["counts"] == {"passed": 0, "failed": 0, "known": 1, "skipped": 1}
    assert [c["before"] for c in found["cases"]] == ["failed", ""]
    assert not record_tests(tests, {"returncode": 0, "outcomes": {}}, "web", 1)["ok"]  # a new red test


def test_a_fix_round_lands_as_one_more_commit_on_the_same_branch(tmp_path):
    repo, run = tmp_path / "web", tmp_path / "run"
    repo.mkdir()
    run.mkdir()
    (repo / "a.txt").write_text("one\n")
    for args in (
        ["init", "-qb", "main"],
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    patch = run / "web.patch"
    patch.write_text("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n one\n+two\n")
    assert land(repo, "main", "rfa/t1", patch, "first") == "rfa/t1"
    first = git(repo, "rev-parse", "rfa/t1").strip()
    patch.write_text("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,3 @@\n one\n two\n+three\n")
    assert land(repo, first, "rfa/t1", patch, "fix", onto=True) == "rfa/t1"
    assert git(repo, "show", "rfa/t1:a.txt") == "one\ntwo\nthree\n"
    assert git(repo, "rev-parse", "rfa/t1~1").strip() == first  # on top of the first round, not beside it
    assert sorted(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()) == ["main", "rfa/t1"]


@pytest.mark.parametrize(
    ("comments", "expected"),
    [
        ([{"repo": "web", "file": "a.py"}], {"web": ["b.py"]}),
        ([{"repo": "web", "file": "a.py"}, {"repo": "web", "file": "b.py"}], {}),
        ([{"text": "the reviewer's problem, on no line"}], {}),
    ],
)
def test_a_fix_round_says_which_files_it_changed_that_no_comment_was_on(comments, expected):
    patches = {"web": "diff --git a/a.py b/a.py\n+x\ndiff --git a/b.py b/b.py\n+y\n"}
    assert outside(comments, patches) == expected
    assert changed_files(patches) == "Changed a.py, b.py."


def test_land_puts_the_patch_on_a_branch_without_disturbing_the_working_tree(tmp_path):
    """The whole point: your half-finished work stays exactly as it was."""
    repo, run = tmp_path / "web", tmp_path / "run"
    (repo / "src").mkdir(parents=True)
    run.mkdir()
    (repo / "src" / "app.js").write_text("export const a = 1;\n")
    for args in (
        ["init", "-qb", "main"],
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "src" / "scratch.js").write_text("my half-finished work\n")  # untracked, must survive

    patch = run / "web.patch"
    patch.write_text(
        "diff --git a/src/app.js b/src/app.js\n--- a/src/app.js\n+++ b/src/app.js\n"
        "@@ -1 +1,2 @@\n export const a = 1;\n+export const b = 2;\n"
    )
    assert land(repo, "main", "rfa/t1", patch, "add b") == "rfa/t1"

    branches = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(branches) == ["main", "rfa/t1"]
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        == "main"
    )
    assert (repo / "src" / "app.js").read_text() == "export const a = 1;\n"  # main untouched
    assert (repo / "src" / "scratch.js").read_text() == "my half-finished work\n"
    landed = subprocess.run(
        ["git", "-C", str(repo), "show", "rfa/t1:src/app.js"], capture_output=True, text=True, check=True
    ).stdout
    assert landed == "export const a = 1;\nexport const b = 2;\n"
    assert not (run / "worktree-web").exists()
    assert free_branch(repo, "rfa/t1") == "rfa/t1-2"


def test_pausing_the_card_stops_the_run_before_its_next_model_call(tmp_path, monkeypatch):
    from rfa import tasks

    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    task = tasks.save(tasks.create("an idea"), paused=True)
    model = DeterministicModel(outputs=[act("touch fixed"), submit()])
    agent = CoderAgent(
        model,
        LocalEnvironment(cwd=str(tmp_path)),
        checks=[],
        cwd=str(tmp_path),
        task_id=task.id,
        system_template="coder",
        instance_template="{{task}}",
        cost_limit=0,
    )
    with pytest.raises(tasks.Paused):
        agent.run("the packet", broken_checks=[])
    assert model.current_index == -1 and not (tmp_path / "fixed").exists()
    assert agent.messages[-1]["extra"]["exit_status"] == "Paused"
