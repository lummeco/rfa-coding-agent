"""The planner loop, driven end to end against a real shell and a scripted model."""

import json
import subprocess

import pytest
import yaml

from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output
from rfa.planner import PlannerAgent, render_map, tracked_files
from rfa.run.plan import DEFAULT_CONFIG, parse_repo

REPO_FILES = {"invoicing": {"src/LineRow.tsx", "tests/editor.test.ts"}}

PACKET = {
    "title": "Add duplicate invoice line functionality.",
    "goal": "Let a user duplicate an invoice line.",
    "current_behavior": "Lines can be added, edited and deleted, but not duplicated.",
    "required_behavior": ["Insert the copy immediately below the original."],
    "constraints": [],
    "areas": ["invoice line row component"],
    "acceptance_criteria": ["Clicking Duplicate creates exactly one new line."],
    "verification": {"commands": ["pnpm test"], "manual": []},
    "non_goals": [],
    "files": [{"path": "invoicing/src/LineRow.tsx", "why": "the row gains the action"}],
    "open_questions": [],
    "complexity": 2,
    "complexity_reason": "A normal feature following an existing pattern.",
}


def write(packet: dict | str, path) -> dict:
    """A model turn that writes `packet` to the packet path, as the real planner is told to."""
    body = packet if isinstance(packet, str) else json.dumps(packet)
    return make_output("writing it", [{"command": f"cat <<'EOF' > {path}\n{body}\nEOF"}])


def submit() -> dict:
    return make_output("done", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])


def read() -> dict:
    return make_output("looking", [{"command": "echo reading the code"}])


DEFAULTS = {
    "system_template": "planner",
    "instance_template": "{{task}} {{repo_map}} {{attempts}} {{packet_path}}",
    "min_tool_calls": 0,
    "cost_limit": 0,
}


def run_planner(outputs: list[dict], tmp_path, **config) -> PlannerAgent:
    agent = PlannerAgent(
        DeterministicModel(outputs=outputs),
        LocalEnvironment(cwd=str(tmp_path)),
        repo_files=REPO_FILES,
        packet_path=str(tmp_path / "packet.json"),
        **(DEFAULTS | config),
    )
    agent.run("duplicate a line", repo_map="invoicing/src/LineRow.tsx")
    return agent


def test_a_good_packet_ends_the_run_on_the_first_submit(tmp_path):
    agent = run_planner([write(PACKET, tmp_path / "packet.json"), submit()], tmp_path)
    assert agent.packet is not None and agent.packet.complexity == 2
    assert agent.messages[-1]["extra"]["exit_status"] == "Submitted"


def test_a_new_file_in_a_folder_that_exists_is_a_groundable_path(tmp_path):
    """The packet may add files; it may not aim at a folder the repository does not have."""
    good = json.dumps(PACKET | {"files": [{"path": "invoicing/src/New.tsx", "why": "new"}]})
    agent = run_planner([write(good, tmp_path / "packet.json"), submit()], tmp_path)
    assert agent.packet is not None and agent.attempt == 0


@pytest.mark.parametrize(
    ("bad", "complaint"),
    [
        ("not json at all", "not valid JSON"),
        (json.dumps(PACKET | {"complexity": 9}), "does not match the schema"),
        (json.dumps(PACKET | {"files": [{"path": "invoicing/nowhere/Gone.tsx", "why": "w"}]}), "does not exist"),
    ],
)
def test_a_rejected_packet_resumes_the_same_session_and_the_fix_is_accepted(bad, complaint, tmp_path):
    """The whole point of validating host-side: the planner keeps its context and corrects itself."""
    packet_path = tmp_path / "packet.json"
    agent = run_planner([write(bad, packet_path), submit(), write(PACKET, packet_path), submit()], tmp_path)
    assert agent.packet is not None and agent.attempt == 1
    assert any(complaint in m.get("content", "") for m in agent.messages)
    assert agent.messages[-1]["extra"]["exit_status"] == "Submitted"


def test_the_draft_is_given_up_on_after_the_configured_attempts(tmp_path):
    packet_path = tmp_path / "packet.json"
    agent = run_planner([write("nope", packet_path), submit()] * 2, tmp_path, attempts=2)
    assert agent.packet is None and agent.attempt == 2
    assert agent.messages[-1]["extra"]["exit_status"] == "PacketRejected"


def test_submitting_without_writing_a_packet_is_rejected_rather_than_crashing(tmp_path):
    agent = run_planner([submit()] * 2, tmp_path, attempts=2)
    assert agent.packet is None
    assert "is not there" in agent.messages[-1]["content"]


def test_a_packet_written_from_the_idea_alone_is_sent_back(tmp_path):
    """`min_tool_calls` counts the actions the agent really ran -- writing the packet is one of them."""
    packet_path = tmp_path / "packet.json"
    agent = run_planner(
        [write(PACKET, packet_path), submit(), read(), write(PACKET, packet_path), submit()],
        tmp_path,
        min_tool_calls=3,
    )
    assert agent.packet is not None and agent.attempt == 1


def test_tracked_files_lists_the_commit_not_the_working_tree(tmp_path):
    """The container is seeded from `ref`, so an untracked file must not become a groundable path."""
    repo = tmp_path / "invoicing"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "LineRow.tsx").write_text("row")
    for args in (["init", "-q"], ["add", "src"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "untracked.txt").write_text("ignore me")

    assert tracked_files(repo, "HEAD") == {"src/LineRow.tsx"}


def test_render_map_falls_back_to_folders_when_the_repository_is_too_big():
    files = {f"src/mod{i}/file.py" for i in range(10)}
    assert "src/mod0/file.py" in render_map({"big": files}, max_files=100)
    folders = render_map({"big": files}, max_files=5)
    assert "10 files; folders only:" in folders and "src/mod0/" in folders and "file.py" not in folders


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("/tmp/invoicing", ("invoicing", "HEAD")),
        ("/tmp/invoicing@v2", ("invoicing", "v2")),
        ("billing=/tmp/invoicing@main", ("billing", "main")),
    ],
)
def test_parse_repo(spec, expected):
    name, (path, ref) = parse_repo(spec)
    assert (name, ref) == expected and path.is_absolute()


def test_the_shipped_planner_config_renders_with_the_agents_own_variables(tmp_path):
    """StrictUndefined means a typo'd variable in the prompt is a crash at run time, not a bad plan."""
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())["agent"]
    agent = run_planner([read(), read(), write(PACKET, tmp_path / "packet.json"), submit()], tmp_path, **config)
    assert agent.packet is not None
    assert "execution packet" in agent.messages[0]["content"]
    assert str(tmp_path / "packet.json") in agent.messages[1]["content"]


def test_the_planner_refuses_a_card_the_human_has_not_moved_into_planning(tmp_path, monkeypatch):
    """Both ends of planning are the owner's: the planner never pulls a draft in by itself."""
    from rfa import tasks
    from rfa.planner import plan_task

    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    with pytest.raises(ValueError, match="move it to `planning` first"):
        plan_task(tasks.create("an idea"), {})
