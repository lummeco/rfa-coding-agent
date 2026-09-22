"""The reviewer: the verdict the host will accept, and what a card carries back when it fails."""

import pytest

from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output
from rfa import board, daemon, settings, tasks, up
from rfa.reviewer import Judgement, Review, ReviewerAgent, criteria, packet_only, problems, review_note, send_back

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

    sent = send_back(task, ["the column still jumps to the top"], workspace / "var")
    assert (sent.stage, sent.status) == ("todo", "todo")
    assert "the column still jumps to the top" in sent.body
    assert "## Result" not in sent.body
    assert criteria(sent.body) == criteria(PACKET)
    # The branch really is in the checkout, so the card keeps saying so even though it was rejected.
    assert sent.meta["landed"] == {"rfa-coding-agent": "rfa/scroll"}


def test_review_note_says_which_criteria_failed():
    note = review_note(Review(verdict="fail", judgements=judged(True, False), problems=["it throws"]), ["a.png"])
    assert "✓ c0" in note and "✗ c1" in note and "it throws" in note and "a.png" in note


def test_only_repos_with_an_apps_block_are_reviewable(workspace, monkeypatch):
    """This is what keeps the stage opt-in: no app to start, no stage."""
    config = {"apps": {"lummeco/rfa-coding-agent": {"serve": "x", "port": 1}}}
    assert set(settings.app_specs(config, ["lummeco/rfa-coding-agent"])) == {"rfa-coding-agent"}
    assert settings.app_specs(config, ["lummeco/customer-app"]) == {}

    task = tasks.create("scroll", ["lummeco/customer-app"])
    task = tasks.move(tasks.move(tasks.move(task, "todo"), "under-work", actor="worker"), "review", actor="worker")
    monkeypatch.setattr(settings, "load", lambda stage="planner": config)
    # A card moved here by hand with nothing to drive must not be picked up every fifteen seconds.
    assert not daemon.reviewable(task)
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
    assert reviewer["agent"]["step_limit"] == 60  # and the rest of the packaged config survives
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
