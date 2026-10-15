"""planning -> planning: the tests, written from the packet before any code is.

The second half of planning. Once the packet stands -- no question left open -- a tester writes the
tests that say what the packet means: in the repository's own test suite, failing now, for the
reason the packet gives. You read them beside the packet before you approve either, and the coder
then works until they pass, without changing them.

It runs in the coder's container, with the repository set up the way the coder will find it,
because a test nobody has run is only a guess. The host runs the suite on every submit and accepts
the tests only when they are test files alone, run in the suite, fail now, and leave every test that
passed before still passing. A tester that finds the packet too open to test asks instead, and the
card waits for your answers the way it waits on the planner's.
"""

import json
import shutil

from minisweagent.environments import get_environment
from minisweagent.models import get_model
from rfa import junit, prepare, settings, tasks
from rfa.planner import REPOS_DIR, conventions, equip, fetched, pin, seed, tests_dir
from rfa.tasks import Task
from rfa.worker import (
    REASONS,
    CoderAgent,
    diffs,
    environment,
    make_git_repos,
    new_problems,
    report,
    run_lint,
    run_tests,
)

TESTS_REJECTED = (
    "You submitted, but the host cannot accept these tests yet:\n\n{report}\n\n"
    "Fix that and submit again. This was round {round} of {rounds}."
)

NO_TEST_COMMAND = (
    "No tests were written first: `{repo}` has no `test:` under `containers:` in rfa.yaml, so the host "
    "could not run them. The coder works from the packet alone."
)

NO_REPORT = (
    "The repository's `test:` command wrote no JUnit report before any test was written, so no test "
    "could be judged. It must write one to `$RFA_JUNIT`; fix `test:` in rfa.yaml and plan the card again."
    "\n\n{output}"
)


def added(tests: dict, baseline: dict) -> list[dict]:
    """The cases in this run's report that were not in the report before the tester started."""
    before = baseline.get("outcomes", {})
    return [c for c in tests["cases"] or [] if c["id"] not in before]


def questions(written: dict) -> list[str]:
    return [str(q).strip() for q in written.get("questions") or [] if str(q).strip()]


class TesterAgent(CoderAgent):
    """The coder's loop, judged by another rule: tests only, in the suite, red now, nothing else broken."""

    rejected = TESTS_REJECTED

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asked: list[str] = []

    def run_checks(self) -> list[dict]:
        self.asked = questions(self.summary())
        return super().run_checks()

    def problem(self, text: str) -> dict:
        return {"command": self.test, "ok": False, "returncode": 1, "output": text}

    def regressions(self) -> list[dict]:
        """Why the host turns these tests down. Questions instead of tests are an answer, not a failure."""
        if self.asked:
            return []
        found = []
        if outside := [p for p in self.changed if not junit.TEST_FILE.search(p.partition("/")[2])]:
            found.append(
                self.problem(
                    f"Only tests may change before the owner approves them, and these are not test files: "
                    f"{', '.join(outside)}. Put them back; the code is the coder's to write."
                )
            )
        if self.tests is None or self.tests["cases"] is None:
            found.append(
                self.problem(f"It wrote no JUnit report to $RFA_JUNIT.\n\n{(self.tests or {}).get('output', '')}")
            )
            return found
        new = added(self.tests, self.test_baseline)
        if not new:
            found.append(
                self.problem(
                    "The suite ran no test that was not there before you started: write them where it runs them."
                )
            )
        elif not any(c["outcome"] in junit.FAILED for c in new):
            found.append(
                self.problem(
                    "Every test you added passes on the code as it is, so none of them can tell whether the "
                    "change was made. A test written first fails now, for the reason the packet gives."
                )
            )
        before = self.test_baseline.get("outcomes", {})
        if broke := [
            c for c in self.tests["cases"] if c["outcome"] in junit.FAILED and before.get(c["id"]) == "passed"
        ]:
            found.append(
                self.problem(
                    "Tests that passed before you started fail now:\n\n"
                    + "\n\n".join(f"FAILED {c['id']}\n{c['message']}".strip() for c in broke[:20])
                )
            )
        if self.linted and (lint := new_problems(self.linted, self.lint_baseline)):
            found.append({"command": self.lint, "ok": False, "returncode": self.linted["returncode"], "output": lint})
        return found


def describe(agent: TesterAgent, written: dict, repo: str) -> dict:
    """What the board shows you of the tests: each new one, what it checks in words, and why it fails now."""
    notes = [t for t in written.get("tests") or [] if isinstance(t, dict) and t.get("name")]
    cases = []
    for case in added(agent.tests, agent.test_baseline):
        note = next((n for n in notes if junit.named(case, str(n["name"]))), {})
        cases.append(case | {"checks": str(note.get("checks") or ""), "criterion": note.get("criterion")})
    return {
        "repo": repo,
        "command": agent.test,
        "summary": str(written.get("summary") or "")[:1000],
        "tests": cases,
    }


def write_tests(task: Task, config: dict, model: str = "", reasoning: str = "") -> Task:
    """Write the tests for a planned card, and leave it `ready` for you with the packet beside them.

    With the planning model: the tests are the packet made exact, which is the planner's kind of work.
    A repository with no `test:` gets no tests -- the host could not run them -- and the card says so.
    """
    if task.stage != "planning":
        raise ValueError(f"{task.id} is in `{task.stage}`; tests are written for a planning card")
    names_full = task.meta.get("repos") or []
    repos = settings.repo_paths(config, names_full, settings.start_branches(task.meta))
    if not repos:
        raise ValueError(f"{task.id} names no repositories; add `repos:` to the card")
    names = list(repos)
    output = tests_dir(task.id)
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    spec = settings.container(config, names_full[0])
    if not (test := spec.get("test") or ""):
        tasks.log(type="tests_skipped", id=task.id)
        return tasks.save(
            task, status="ready", tests_at=tasks.now(), tests_note=NO_TEST_COMMAND.format(repo=names[0]), error=None
        )

    chosen, level = settings.for_task(config, task.meta, model, reasoning, prefix="plan_")
    task = tasks.save(task, status="writing-tests", tests_model=chosen, tests_note=None)
    tasks.log(type="tests_started", id=task.id, model=chosen)
    reference = settings.reference_paths(config, task.meta)
    offline = fetched(repos, reference)
    repos, reference = pin(repos), pin(reference)
    cwd = f"{REPOS_DIR}/{names[0]}"
    env = None
    try:
        container = environment(config, spec)
        container["image"] = prepare.image(container, spec, repos, names[0], "coder")
        skills = settings.skills(config, task.meta.get("packet_files") or [])
        env = get_environment(container, default_type="docker")
        seed(env, repos, reference)
        equip(env, skills)
        prepare.run(env, spec.get("start") or [], cwd, spec.get("setup_timeout", 900))
        make_git_repos(env, names)
        lint = spec.get("lint") or ""
        linted = run_lint(env, lint, cwd) if lint else None
        if (tested := run_tests(env, test, cwd))["cases"] is None:
            raise RuntimeError(NO_REPORT.format(output=tested["output"]))
        agent = TesterAgent(
            get_model(config=settings.model_config(config, chosen, level)),
            env,
            checks=[],
            cwd=cwd,
            test=test,
            test_baseline={
                "returncode": tested["returncode"],
                "outcomes": {c["id"]: c["outcome"] for c in tested["cases"]},
            },
            lint=lint,
            lint_baseline=linted,
            output_path=output / "trajectory.json",
            task_id=task.id,
            **({"context_window": settings.context_window(config, chosen)} | config.get("agent", {})),
        )
        agent.run(
            task.body,
            repo_list=", ".join(names),
            primary_repo=cwd,
            test=test,
            reference=sorted(reference),
            conventions=conventions(repos),
            skills=sorted(skills),
        )
        patches, _ = diffs(env, names)
        written = agent.summary()
    except tasks.Paused:
        tasks.log(type="tests_paused", id=task.id)
        return tasks.save(task, status="queued", paused=True)
    except Exception as e:
        tasks.log(type="tests_failed", id=task.id, error=str(e))
        return tasks.save(task, status="failed", error=str(e))
    finally:
        if env is not None:
            env.cleanup()

    if agent.asked:
        tasks.log(type="tests_asked", id=task.id, questions=len(agent.asked))
        return tasks.save(task, status="question", open_questions=agent.asked, offline=offline, error=None)
    if not (agent.submitted() and patches and not agent.regressions()):
        why = (
            report(agent.regressions())
            or f"{REASONS.get(agent.exit_status(), 'the run ended')} ({agent.exit_status()})"
        )
        error = f"The tests could not be written: {why}"
        tasks.log(type="tests_failed", id=task.id, error=error)
        return tasks.save(task, status="failed", error=error[:2000], offline=offline)
    for name, patch in patches.items():
        (output / f"{name}.patch").write_text(patch)
    (output / "tests.json").write_text(json.dumps(describe(agent, written, names[0]), indent=2))
    tasks.log(type="tests_written", id=task.id, tests=len(added(agent.tests, agent.test_baseline)))
    return tasks.save(task, status="ready", tests_at=tasks.now(), offline=offline, error=None)
