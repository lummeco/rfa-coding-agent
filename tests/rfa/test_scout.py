"""The scout: suggestions held to the code they point at, and drafts you decide on one at a time."""

import json

import pytest
import yaml
from jinja2 import StrictUndefined, Template

from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output
from rfa import scout, settings, tasks
from rfa.scout import Found, ScoutAgent, problems

REPO_FILES = {"web": {"src/price.ts", "src/cart.ts", "README.md"}}


def suggestion(n: int = 1, **fields) -> dict:
    return {
        "title": f"Round the total once, not per line ({n})",
        "why": "Each line is rounded before summing, so totals drift by a cent.",
        "evidence": ["web/src/price.ts:12"],
        "kind": "bug",
        "complexity": 1,
    } | fields


def found(*suggestions: dict) -> Found:
    return Found.model_validate({"suggestions": list(suggestions or [suggestion(n) for n in (1, 2, 3)])})


@pytest.mark.parametrize(
    ("given", "tool_calls", "said"),
    [
        (found(), 5, []),
        (found(), 1, ["You ran 1 commands"]),
        (found(suggestion()), 5, ["1 suggestions; give 3 to 5"]),
        (found(*[suggestion(n) for n in range(6)]), 5, ["6 suggestions; give 3 to 5"]),
        (found(suggestion(1, complexity=3), suggestion(2), suggestion(3)), 5, ["Suggestion 1 is complexity 3"]),
        (found(suggestion(1), suggestion(2, evidence=[]), suggestion(3)), 5, ["Suggestion 2 has no evidence"]),
        (
            found(suggestion(1), suggestion(2), suggestion(3, evidence=["web/src/gone.ts"])),
            5,
            ["`web/src/gone.ts` is not a file"],
        ),
        (
            found(suggestion(1), suggestion(2), suggestion(3, evidence=["src/price.ts"])),
            5,
            ["`src/price.ts` is not a file"],
        ),
        (found(suggestion(1, evidence=["/work/repos/web/src/cart.ts"]), suggestion(2), suggestion(3)), 5, []),
    ],
)
def test_suggestions_are_few_small_and_point_at_real_files(given, tool_calls, said):
    assert [any(want in line for line in problems(given, REPO_FILES, tool_calls, 3)) for want in said] == [True] * len(
        said
    )
    assert len(problems(given, REPO_FILES, tool_calls, 3)) == len(said)


def test_the_scout_is_sent_back_until_its_suggestions_hold(tmp_path):
    """The planner's own loop: a rejection is the submitting call's result, and the session goes on."""
    path = tmp_path / "found.json"

    def write(body: dict) -> dict:
        return make_output("writing", [{"command": f"cat <<'EOF' > {path}\n{json.dumps(body)}\nEOF"}])

    submit = make_output("done", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])
    good = {"suggestions": [suggestion(n) for n in (1, 2, 3)]}
    agent = ScoutAgent(
        DeterministicModel(outputs=[write({"suggestions": [suggestion()]}), submit, write(good), submit]),
        LocalEnvironment(cwd=str(tmp_path)),
        repo_files=REPO_FILES,
        packet_path=str(path),
        system_template="scout",
        instance_template="{{task}} {{progress}}",
        min_tool_calls=0,
        cost_limit=0,
    )
    agent.run("bugs", progress="")
    assert agent.attempt == 1 and [s.title for s in agent.packet.suggestions] == [
        s["title"] for s in good["suggestions"]
    ]
    assert "give 3 to 5" in json.dumps(agent.messages)


def test_the_packaged_prompt_renders_with_what_the_scout_is_given():
    config = settings.load("scout")["agent"]
    assert "Scout stage" in config["system_template"] and config["attempts"] == 3, "the planner's settings carry over"
    said = Template(config["instance_template"], undefined=StrictUndefined).render(
        **config,
        packet_path="/out/packet.json",
        task=scout.FOCUS["bugs"],
        repo_map="web",
        conventions={},
        progress="- Old card (trashed)",
    )
    assert (
        "Bugs: code that does the wrong thing" in said and "- Old card (trashed)" in said and "/out/packet.json" in said
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    settings.path().write_text(yaml.safe_dump({"repos": {"lummeco/web": str(tmp_path), "lummeco/api": str(tmp_path)}}))


def test_a_scout_becomes_drafts_you_decide_on_before_the_next_one(workspace):
    card = scout.request("lummeco/web", "bugs")
    assert (card.stage, card.status, card.title, card.meta["scout"]) == (
        "planning",
        "queued",
        "Analyze web · bugs",
        "bugs",
    )
    # One scout per repository at a time; another repository is its own question.
    with pytest.raises(ValueError, match="still has 1 to decide on: Analyze web"):
        scout.request("lummeco/web")
    assert scout.request("lummeco/api").meta["repos"] == ["lummeco/api"]
    filed = scout.file(card, found())
    drafts = [tasks.find(id) for id in filed.meta["suggestions"]]
    assert (filed.status, filed.archived, len(drafts)) == ("scouted", True, 3)
    assert [(d.stage, d.meta["suggested"], d.meta["kind"], d.meta["repos"]) for d in drafts] == [
        ("draft", card.id, "bug", ["lummeco/web"])
    ] * 3
    assert "Each line is rounded" in drafts[0].body and "- `web/src/price.ts:12`" in drafts[0].body
    with pytest.raises(ValueError, match="still has 3 to decide on"):
        scout.request("lummeco/web")
    # Planned, dismissed, dismissed: nothing is left to decide, and the next scout knows all three.
    tasks.move(drafts[0], "planning")
    for draft in drafts[1:]:
        tasks.save(draft, archived=True)
    assert scout.known("lummeco/web").splitlines() == [
        "- Round the total once, not per line (1) (planning)",
        "- Round the total once, not per line (2) (dismissed)",
        "- Round the total once, not per line (3) (dismissed)",
    ]
    assert scout.request("lummeco/web", "tests").meta["scout"] == "tests"


@pytest.mark.parametrize(
    ("repo", "focus", "why"),
    [("lummeco/nope", "bugs", "no such repository"), ("lummeco/web", "vibes", "no such focus")],
)
def test_a_scout_is_only_of_a_configured_repository_and_a_known_focus(workspace, repo, focus, why):
    with pytest.raises(ValueError, match=why):
        scout.request(repo, focus)
