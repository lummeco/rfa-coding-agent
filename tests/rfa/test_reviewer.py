"""The reviewer: the verdict the host will accept, and what a card carries back when it fails."""

import pytest
from jinja2 import StrictUndefined, Template

from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output
from rfa import board, daemon, rounds, settings, tasks, up
from rfa.reviewer import (
    CodeReview,
    CodeReviewerAgent,
    Finding,
    Judgement,
    Review,
    ReviewerAgent,
    code_problems,
    comment,
    criteria,
    killed,
    last_round,
    packet_only,
    playwright_pin,
    problems,
    question,
    review_model,
    review_note,
    send_back,
    vm_note,
)

PACKET = """
# Task
Keep the scroll position across a refresh

## Goal
The board must stop yanking the reader back to the top.

## Acceptance criteria
1. Scrolling a column and waiting past a poll leaves it where it was.
2. Opening a different card starts that card at the top.

## Non-goals
- Changing how often the board polls.
"""


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    return tmp_path


def submit() -> dict:
    return make_output("submitting", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])


def judged(*met: bool) -> list[Judgement]:
    return [Judgement(criterion=f"c{i}", met=m, evidence="clicked it") for i, m in enumerate(met)]


def test_criteria_are_read_off_the_packet_and_stop_at_the_next_heading():
    """The board lets you edit a packet, so the body is the contract -- not the front matter."""
    assert criteria(PACKET) == [
        "Scrolling a column and waiting past a poll leaves it where it was.",
        "Opening a different card starts that card at the top.",
    ]
    assert criteria("# Task\n\nno criteria here\n") == []


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("mcr.microsoft.com/playwright/python:v1.55.0-noble", "1.55.0"),
        ("mcr.microsoft.com/playwright/python:v1.55.0", "1.55.0"),
        ("mcr.microsoft.com/playwright/python:next", ""),  # unversioned: pip picks, and may not match
        ("node:22-bookworm", ""),
    ],
)
def test_the_playwright_installed_is_the_one_the_image_baked_browsers_for(image, expected):
    """A package newer than the image expects its own browser build, which is not the one in there."""
    assert playwright_pin(image) == expected


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (137, "", True),
        (1, "Creating an optimized production build ...\nKilled\n", True),  # `|| exit 1` hid the 137
        (1, "Type error: 'Killed' is not assignable\nFailed to compile.", False),
        (1, "Failed to compile.", False),
    ],
)
def test_an_oom_kill_is_the_machines_problem_not_the_coders(returncode, output, expected):
    assert killed(returncode, output) == expected


def test_an_oom_kill_names_dockers_vm_size_as_the_ceiling():
    """12g of `--memory` in a 5.5 GiB VM: the note must say where the real limit is set."""
    assert "5.5 GiB" in vm_note(5632 * 2**20) and "Settings > Resources > Memory" in vm_note(5632 * 2**20)
    assert vm_note(0) == ""


def test_previous_rounds_are_cut_off_before_the_card_goes_back():
    """A card that comes back must arrive as the ticket it started as, however many rounds it took."""
    grown = PACKET + "\n## Result\n\n- landed on rfa/x\n\n## Review\n\n**fail**\n"
    assert packet_only(grown).endswith("- Changing how often the board polls.\n")
    assert "## Result" not in packet_only(grown) and "## Review" not in packet_only(grown)
    assert criteria(packet_only(grown)) == criteria(PACKET)  # the contract survives the pruning


@pytest.mark.parametrize(
    ("review", "shots", "expected"),
    [
        (Review(verdict="pass", judgements=judged(True, True)), 2, []),
        (Review(verdict="pass", judgements=judged(True, True)), 0, ["screenshots"]),
        (Review(verdict="pass", judgements=judged(True)), 2, ["acceptance criteria"]),
        (Review(verdict="pass", judgements=judged(True, False)), 2, ["criteria unmet"]),
        (Review(verdict="fail", judgements=judged(True, False)), 2, ["`problems`"]),
        (Review(verdict="fail", judgements=judged(True, False), problems=["the button throws"]), 2, []),
    ],
)
def test_the_host_reads_the_verdict_rather_than_trusting_it(review, shots, expected):
    """Every rejection here is a review that would otherwise have moved a card on a lie: one that
    never opened the app, judged something else, contradicted itself, or failed without a reason."""
    found = problems(review, ["one", "two"], shots)
    assert len(found) == len(expected)
    assert all(word in " ".join(found) for word in expected)


BAD = '{"verdict":"fail","judgements":[]}'
GOOD = (
    '{"verdict":"fail","judgements":['
    '{"criterion":"one","met":false,"evidence":"the button threw"},'
    '{"criterion":"two","met":true,"evidence":"the list stayed put"}],'
    '"problems":["the button throws"]}'
)


def test_a_rejected_review_goes_back_into_the_same_session(tmp_path):
    """The rejection arrives as the submitting call's own result, so the agent keeps its context and
    fixes the review it already wrote rather than starting the whole thing again."""
    review, shots = tmp_path / "review.json", tmp_path / "shots"
    agent = ReviewerAgent(
        DeterministicModel(
            outputs=[
                make_output("looking", [{"command": f"touch {shots}/a.png && cat > {review} <<'EOF'\n{BAD}\nEOF"}]),
                submit(),
                make_output("fixing", [{"command": f"cat > {review} <<'EOF'\n{GOOD}\nEOF"}]),
                submit(),
            ]
        ),
        LocalEnvironment(cwd=str(tmp_path)),
        expected=["one", "two"],
        system_template="reviewer",
        instance_template="{{task}}",
        cost_limit=0,
        review_path=str(review),
        shots_path=str(shots),
    )
    agent.run("the packet")
    assert agent.attempt == 1  # one rejection, then the corrected review was taken
    assert [m for m in agent.messages if "rejected the review" in str(m.get("content", ""))]
    assert agent.review.verdict == "fail" and agent.review.problems == ["the button throws"]


def test_a_failed_review_sends_the_card_back_as_the_ticket_it_was(workspace):
    """`todo`, not `done`: the coder picks it up again, and what it reads is the packet plus why."""
    task = tasks.create("scroll", ["lummeco/rfa-coding-agent"])
    task.body = PACKET
    tasks.save(task)
    task = tasks.move(task, "todo")
    task = tasks.move(task, "under-work", actor="worker")
    task = tasks.move(task, "review", actor="worker", landed={"rfa-coding-agent": "rfa/scroll"})

    task.body += "\n## Result\n\n- landed on rfa/scroll\n"
    found = [{"text": "the column still jumps to the top"}, {"repo": "web", "file": "a.ts", "line": 3, "text": "x"}]
    sent = send_back(task, found, workspace / "var")
    assert (sent.stage, sent.status) == ("todo", "todo")
    # What was found reaches the coder as comments it must answer, not as a note under the ticket.
    pending = rounds.load(task.id)["pending"]
    assert [(c["text"], c.get("file"), c["by"]) for c in pending] == [
        ("the column still jumps to the top", None, "reviewer"),
        ("x", "a.ts", "reviewer"),
    ]
    assert sent.body == packet_only(PACKET) and criteria(sent.body) == criteria(PACKET)
    # The branch really is in the checkout, so the card keeps saying so even though it was rejected.
    assert sent.meta["landed"] == {"rfa-coding-agent": "rfa/scroll"}


def test_review_note_says_which_criteria_failed():
    note = review_note(Review(verdict="fail", judgements=judged(True, False), problems=["it throws"]), ["a.png"])
    assert "✓ c0" in note and "✗ c1" in note and "it throws" in note and "a.png" in note


def test_every_landed_card_is_reviewed_and_only_apps_are_driven(workspace, monkeypatch):
    """The code is read on every card; an `apps:` block only adds the drive through the app."""
    config = {"apps": {"lummeco/rfa-coding-agent": {"serve": "x", "port": 1}}}
    assert set(settings.app_specs(config, ["lummeco/rfa-coding-agent"])) == {"rfa-coding-agent"}
    assert settings.app_specs(config, ["lummeco/customer-app"]) == {}

    task = tasks.create("scroll", ["lummeco/customer-app"])
    task = tasks.move(tasks.move(tasks.move(task, "todo"), "under-work", actor="worker"), "review", actor="worker")
    monkeypatch.setattr(settings, "load", lambda stage="planner": config)
    # A card moved here by hand with nothing landed must not be picked up every fifteen seconds.
    assert not daemon.reviewable(task)
    assert daemon.next_job(daemon.DaemonConfig()) is None
    tasks.save(task, landed={"customer-app": "rfa/scroll"})
    assert daemon.reviewable(task)
    assert daemon.next_job(daemon.DaemonConfig())[0] == "review"
    # Waiting on your answer is not waiting on the reviewer.
    tasks.save(task, status="question")
    assert daemon.next_job(daemon.DaemonConfig()) is None


def test_the_daemon_finishes_a_review_before_starting_another_run(workspace, monkeypatch):
    """A card in review is work already done; another coding run ahead of it answers nothing."""
    config = {"apps": {"lummeco/rfa-coding-agent": {"serve": "x", "port": 1}}}
    monkeypatch.setattr(settings, "load", lambda stage="planner": config)
    waiting = tasks.create("code me", ["lummeco/rfa-coding-agent"])
    tasks.move(waiting, "todo")
    reviewing = tasks.create("judge me", ["lummeco/rfa-coding-agent"])
    reviewing = tasks.move(tasks.move(reviewing, "todo"), "under-work", actor="worker")
    tasks.move(reviewing, "review", actor="worker", landed={"rfa-coding-agent": "rfa/judge-me"})

    kind, picked = daemon.next_job(daemon.DaemonConfig())
    assert (kind, picked.id) == ("review", reviewing.id)


def test_the_reviewer_is_a_stage_like_the_other_two(workspace):
    """Its own packaged config, its own block in rfa.yaml, and neither leaking into the others."""
    (workspace / "rfa.yaml").write_text(
        "apps:\n  lummeco/rfa-coding-agent:\n    serve: run\n    port: 4380\n"
        "reviewer:\n  environment:\n    image: my/own:tag\n"
    )
    reviewer = settings.load("reviewer")
    assert reviewer["environment"]["image"] == "my/own:tag"  # mine wins over the packaged default
    assert reviewer["agent"]["step_limit"] == 120  # and the rest of the packaged config survives
    assert "reviewer" not in settings.load("coder")
    # The browser image is the size of a browser: only worth pulling once there is an app to drive.
    assert "my/own:tag" in up.images([settings.load("planner"), settings.load("coder"), reviewer])
    assert "my/own:tag" not in up.images([settings.load("planner"), settings.load("coder"), None])


@pytest.mark.parametrize(
    ("id", "name", "found"),
    [
        ("20260921-120000-a-task", "board.png", True),
        ("20260921-120000-a-task", "../../../../etc/passwd", False),
        ("20260921-120000-a-task", "board.png.sh", False),
        ("../evil", "board.png", False),
    ],
)
def test_the_board_serves_a_screenshot_only_where_both_halves_are_one(workspace, id, name, found):
    """This reads files off disk for whoever holds the token, so a name that has to match a pattern
    is a shorter argument than a name that has been made safe."""
    shots = workspace / "var" / "runs" / "20260921-120000-a-task" / "review"
    shots.mkdir(parents=True)
    (shots / "board.png").write_bytes(b"\x89PNG")
    assert (board.screenshot(id, name) == b"\x89PNG") is found


def test_a_review_cannot_write_over_the_run_it_is_judging(workspace):
    """Two trajectories per card -- the coding one and the reviewing one -- and the board can ask
    for either. One folder for both would lose the run the review is about."""
    task = tasks.create("scroll", ["lummeco/rfa-coding-agent"])
    run = workspace / "var" / "runs" / task.id
    (run / "review").mkdir(parents=True)
    (run / "trajectory.json").write_text('{"messages": [{"role": "exit", "content": "the coder"}]}')
    (run / "review" / "trajectory.json").write_text('{"messages": [{"role": "exit", "content": "the reviewer"}]}')
    assert board.progress(task.id)["steps"][0]["output"] == "the coder"
    assert board.progress(task.id, "review")["steps"][0]["output"] == "the reviewer"


@pytest.mark.parametrize(("notes", "shown"), [("sign in with `login(tab)` first", True), ("", False)])
def test_an_apps_notes_reach_the_reviewer_only_when_written(notes, shown):
    """How to sign in is the app's to say; an app that says nothing gets no empty section."""
    template = settings.load("reviewer")["agent"]["instance_template"]
    rendered = Template(template, undefined=StrictUndefined).render(
        task="t", criteria=["c"], url="u", repo="r", serve_log="l", shots="s", step_limit=1, notes=notes
    )
    assert ("## Using this app" in rendered) is shown and (notes in rendered) and "## Driving the browser" in rendered


FILES = {"web": {"src/list.ts", "src/list.test.ts"}}


def fix(**kwargs) -> Finding:
    return Finding(
        **{"route": "fix", "text": "t", "basis": "decision 1", "repo": "web", "file": "src/list.ts", "line": 3} | kwargs
    )


@pytest.mark.parametrize(
    ("findings", "met", "latest", "expected"),
    [
        ([], (True, True), None, []),
        ([], (True,), None, ["acceptance criteria"]),
        ([], (True, False), None, ["no `fix` or `ask`"]),
        ([Finding(route="note", text="I would name it differently")], (True, False), None, ["no `fix` or `ask`"]),
        ([fix()], (True, False), None, []),
        ([fix(basis=" ")], (True, True), None, ["no `basis`"]),
        ([fix(file="src/ghost.ts")], (True, True), None, ["not a file in the branch"]),
        ([fix(repo="api")], (True, True), None, ["not a file in the branch"]),
        ([fix(line=0)], (True, True), None, ["not a file in the branch"]),
        ([fix(file="", line=0)], (True, True), None, []),
        ([Finding(route="ask", text="Add a migration?", basis="the ticket is silent")], (True, True), None, []),
        # After a fix round: new findings only where the fix touched, old ones by number.
        ([fix()], (True, True), {"web": {"src/list.ts"}}, []),
        ([fix(file="src/list.test.ts")], (True, True), {"web": {"src/list.ts"}}, ["not on a file this fix round"]),
        ([fix(file="", line=0)], (True, True), {"web": {"src/list.ts"}}, ["not on a file this fix round"]),
        ([fix(file="src/list.test.ts", previous=2)], (True, True), {"web": {"src/list.ts"}}, []),
        ([fix(previous=3)], (True, True), {"web": {"src/list.ts"}}, ["but there were 2"]),
        ([Finding(route="note", text="anywhere")], (True, True), {}, []),
    ],
)
def test_the_host_reads_the_code_review_rather_than_trusting_it(findings, met, latest, expected):
    """Every rejection is a review that would send the coder chasing something nobody can act on: a
    taste, a place that is not there, or -- after a fix round -- a goalpost that moved."""
    found = code_problems(CodeReview(judgements=judged(*met), findings=findings), ["one", "two"], FILES, latest, 2)
    assert len(found) == len(expected)
    assert all(word in " ".join(found) for word in expected)


def test_a_code_review_pointing_at_nothing_goes_back_into_the_same_session(tmp_path):
    review = tmp_path / "review.json"
    bad = '{"judgements":[{"criterion":"one","met":true,"evidence":"x"}],"findings":[{"route":"fix","text":"t","basis":"b","repo":"web","file":"nope.ts","line":1}]}'
    good = '{"judgements":[{"criterion":"one","met":true,"evidence":"src/list.ts keeps the offset"}],"findings":[]}'
    agent = CodeReviewerAgent(
        DeterministicModel(
            outputs=[
                make_output("reading", [{"command": f"cat > {review} <<'EOF'\n{bad}\nEOF"}]),
                submit(),
                make_output("fixing", [{"command": f"cat > {review} <<'EOF'\n{good}\nEOF"}]),
                submit(),
            ]
        ),
        LocalEnvironment(cwd=str(tmp_path)),
        expected=["one"],
        files=FILES,
        system_template="reviewer",
        instance_template="{{task}}",
        cost_limit=0,
        review_path=str(review),
        shots_path=str(tmp_path / "shots"),
    )
    agent.run("the packet")
    # No screenshots, and none asked for: the code review never opens an app.
    assert agent.attempt == 1 and "not a file in the branch" in str(agent.messages)
    assert isinstance(agent.review, CodeReview) and agent.review.findings == []


def test_findings_reach_the_coder_and_the_owner_with_their_reasons():
    assert comment(fix(previous=2)) == {
        "repo": "web",
        "file": "src/list.ts",
        "line": 3,
        "text": "Still not dealt with (comment 2): t\n\nBasis: decision 1",
    }
    asked = Finding(
        route="ask", text="Add a column?", basis="no decision on the schema", repo="web", file="a.sql", line=1
    )
    assert question(asked) == "Add a column? — no decision on the schema (`web/a.sql:1`)"


def test_the_reviewer_runs_on_its_own_model_when_one_is_set():
    config = {"models": {"coder": {}, "judge": {}, "other": {}}}
    assert review_model(config, {"model": "coder"}) == ("coder", "")  # unset: as it was
    assert review_model(config | {"review_model": "judge"}, {"model": "coder"}) == ("judge", "")
    assert review_model(config | {"review_model": "judge"}, {"model": "coder", "review_model": "other"})[0] == "other"
    assert review_model(config | {"review_model": "judge"}, {"model": "coder"}, "other", "high") == ("other", "high")
    with pytest.raises(KeyError):
        review_model(config | {"review_model": "ghost"}, {})


def test_a_review_after_a_fix_round_is_held_to_that_round(workspace):
    task = tasks.create("scroll")
    assert last_round(task.id) == ([], None)
    asked = [{"id": "a1", "text": "keep the offset", "answer": "kept it"}]
    rounds.record(task.id, {"n": 0, "kind": "original", "landed": True}, answered=[])
    assert last_round(task.id) == ([], None)  # an original round: the whole branch is fair game
    rounds.record(task.id, {"n": 1, "kind": "fix", "landed": True, "comments": asked}, answered=[])
    rounds.round_dir(task.id, 1).mkdir(parents=True)
    (rounds.round_dir(task.id, 1) / "web.patch").write_text("diff --git a/src/list.ts b/src/list.ts\n")
    assert last_round(task.id) == (asked, {"web": {"src/list.ts"}})


def test_the_shipped_code_review_prompt_renders_with_what_the_host_passes():
    template = settings.load("reviewer")["code_agent"]["instance_template"]
    render = Template(template, undefined=StrictUndefined).render
    common = {
        "task": "t",
        "criteria": ["c"],
        "repos": ["web"],
        "diffs": "/out/diff",
        "review_path": "r",
        "step_limit": 1,
    }
    first = render(**common, previous=[], latest=None)
    assert "/out/diff/<repository>.diff" in first and "follows a fix round" not in first
    previous = [{"repo": "web", "file": "a.ts", "line": 2, "text": "off by one", "answer": "fixed"}, {"text": "whole"}]
    again = render(**common, previous=previous, latest={"web": ["a.ts"]})
    assert "1. `web/a.ts` line 2: off by one" in again and "Answer: fixed" in again and "Answer: (none)" in again
    assert "- `web/a.ts`" in again and "- nothing" not in again
    assert "- nothing" in render(**common, previous=previous, latest={})
