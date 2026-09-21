"""Draft -> execution packet.

A read-only agent reads the repositories in a throwaway container with no network and writes the
packet to `/out/packet.json`. The host does not trust what comes back: the packet must parse, match
the schema, and name only paths the repository actually has, or it goes back into the same session
with the exact problems -- while a retry still costs one attempt rather than a whole coding run.

The model call is made from the host, so the container never needs egress.
"""

import json
import subprocess
from pathlib import Path

from pydantic import ValidationError

from minisweagent import Environment, Model
from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.environments import get_environment
from minisweagent.exceptions import InterruptAgentFlow, Submitted
from minisweagent.models import get_model
from rfa import settings, tasks
from rfa.packet import Packet, problems
from rfa.tasks import Task

REPOS_DIR = "/work/repos"
RETRY_NOTE = (
    "The host rejected the packet you wrote:\n\n{problems}\n\n"
    "Read whatever you need to settle these, write the corrected packet to {path}, and submit again."
)


class PlannerConfig(AgentConfig):
    attempts: int = 3
    """How many packets the host will reject before giving up on the draft."""
    min_tool_calls: int = 3
    """Below this, the packet was written from the idea rather than from the code."""
    packet_path: str = "/out/packet.json"
    """Where in the container the planner leaves the packet. The only thing that leaves it."""


class PlannerAgent(DefaultAgent):
    """Submitting is a proposal, not the end: an unusable packet resumes the same session."""

    def __init__(self, model: Model, env: Environment, *, repo_files: dict[str, set[str]], **kwargs):
        super().__init__(model, env, config_class=PlannerConfig, **kwargs)
        self.repo_files = repo_files
        self.packet: Packet | None = None
        self.n_actions = 0
        self.attempt = 0
        env.execute({"command": f"mkdir -p {Path(self.config.packet_path).parent}"})

    def execute_actions(self, message: dict) -> list[dict]:
        try:
            observations = super().execute_actions(message)
        except Submitted:
            if not (found := self.check_packet()):
                raise
            self.attempt += 1
            if self.attempt >= self.config.attempts:
                raise InterruptAgentFlow(
                    {
                        "role": "exit",
                        "content": "\n".join(found),
                        "extra": {"exit_status": "PacketRejected", "submission": ""},
                    }
                ) from None
            # The rejection goes back as the submitting call's own result, so the session keeps its
            # context and -- on a toolcall model -- every tool call still has its response.
            return self.add_messages(
                *self.model.format_observation_messages(
                    message,
                    [
                        {
                            "output": RETRY_NOTE.format(
                                problems="\n".join(f"- {p}" for p in found), path=self.config.packet_path
                            ),
                            "returncode": 1,
                            "exception_info": "",
                            "extra": {"interrupt_type": "PacketRejected"},
                        }
                    ],
                    self.get_template_vars(),
                )
            )
        # Only actions the agent actually got an observation from count as reading the code: the
        # submitting call raises above and never lands here.
        self.n_actions += len(message.get("extra", {}).get("actions", []))
        return observations

    def check_packet(self) -> list[str]:
        """Why the host rejects what the agent wrote. An empty list accepts it into `self.packet`."""
        self.packet = None
        raw = self.env.execute({"command": f"cat {self.config.packet_path}"})
        if raw["returncode"] != 0:
            return [f"`{self.config.packet_path}` is not there. Write the packet to it before submitting."]
        try:
            data = json.loads(raw["output"])
        except json.JSONDecodeError as e:
            return [f"`{self.config.packet_path}` is not valid JSON: {e}."]
        try:
            self.packet = Packet.model_validate(data)
        except ValidationError as e:
            return [f"`{self.config.packet_path}` does not match the schema: {_errors(e)}"]
        return problems(self.packet, self.repo_files, self.n_actions, self.config.min_tool_calls)


def _errors(e: ValidationError) -> str:
    return "; ".join(f"`{'.'.join(str(p) for p in err['loc'])}`: {err['msg']}" for err in e.errors()[:5])


def tracked_files(repo: Path, ref: str) -> set[str]:
    """Every file the repository has at `ref` -- what the packet's paths are checked against."""
    return set(git(repo, "ls-tree", "-r", "--name-only", ref).splitlines())


def seed(env: Environment, repos: dict[str, tuple[Path, str]]) -> dict[str, set[str]]:
    """Unpack each repository's tree at its ref into the container, and return its tracked files.

    `git archive` into `docker cp` gives the container the files alone -- no history, no other refs,
    no remote, nothing the coder could later push from. Docker environments only.
    """
    env.execute({"command": f"mkdir -p {REPOS_DIR}"})
    files = {}
    for name, (path, ref) in repos.items():
        env.execute({"command": f"mkdir -p {REPOS_DIR}/{name}"})
        subprocess.run(
            [env.config.executable, "cp", "-", f"{env.container_id}:{REPOS_DIR}/{name}"],
            input=git(path, "archive", "--format=tar", ref, text=False),
            check=True,
            capture_output=True,
        )
        files[name] = tracked_files(path, ref)
    return files


def git(repo: Path, *args: str, text: bool = True):
    """Run git in `repo` and return its stdout. Shared with the worker, which lands the diff."""
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=text).stdout


def render_map(repo_files: dict[str, set[str]], max_files: int = 300) -> str:
    """What the planner is told the repositories hold. Every path in the packet is checked against
    this, so a repository too big to list is shown as its folders and the packet is grounded there."""
    blocks = []
    for name, files in repo_files.items():
        listing = sorted(files)
        if len(listing) > max_files:
            listing = sorted({f"{f.rpartition('/')[0]}/" for f in files if "/" in f})
            blocks.append(f"### {name}\n\n{len(files)} files; folders only:\n\n" + "\n".join(listing))
        else:
            blocks.append(f"### {name}\n\n" + "\n".join(listing))
    return "\n\n".join(blocks)


def plan(idea: str, repos: dict[str, tuple[Path, str]], model: Model, env: Environment, **kwargs) -> PlannerAgent:
    """Run the planner over `idea`. The packet, when there is one, is on the returned agent."""
    repo_files = seed(env, repos)
    agent = PlannerAgent(model, env, repo_files=repo_files, **kwargs)
    agent.run(idea, repo_map=render_map(repo_files))
    return agent


def write_packet(agent: PlannerAgent, path: Path) -> None:
    """The rendered ticket for the board, with the host's own fields beside it."""
    if agent.packet is None:
        raise ValueError("the planner produced no usable packet")
    path.write_text(agent.packet.render())
    path.with_suffix(".json").write_text(json.dumps(agent.packet.model_dump(), indent=2))


def plan_task(task: Task, config: dict, model: str = "", reasoning: str = "") -> Task:
    """Plan one card in place: the packet becomes its body and it waits in `planning` for a human.

    Both ends of this stage are yours. You move a draft into `planning` when you want it planned,
    and you move the packet on to `todo` when you have read it -- the planner only ever works on
    cards that are already there, and never moves one out.

    A card that cannot be planned says so and stays put. It is never re-planned on its own -- a
    second identical attempt fails the same way -- so the next move is yours too.
    """
    if task.stage != "planning":
        raise ValueError(f"{task.id} is in `{task.stage}`; move it to `planning` first (rfa mv {task.id} planning)")
    repos = settings.repo_paths(config, task.meta.get("repos") or [])
    # The card's own `model:` is what the board and `rfa new -m` set; the command still wins.
    chosen = settings.pick(config, model or str(task.meta.get("model") or ""))
    tasks.save(task, status="planning", model=chosen)
    env = get_environment(config.get("environment", {}), default_type="docker")
    try:
        agent = plan(
            task.body,
            repos,
            get_model(config=settings.model_config(config, chosen, reasoning)),
            env,
            **config.get("agent", {}),
        )
    except Exception as e:
        tasks.log(type="plan_failed", id=task.id, error=str(e))
        return tasks.save(task, status="failed", error=str(e))
    finally:
        env.cleanup()
    if agent.packet is None:
        note = agent.messages[-1].get("content", "the planner gave up")
        tasks.log(type="plan_failed", id=task.id, error=note)
        return tasks.save(task, status="failed", error=note)
    tasks.log(type="planned", id=task.id, complexity=agent.packet.complexity, steps=agent.n_actions, model=chosen)
    task.body = "\n" + agent.packet.render()
    return tasks.save(
        task,
        status="ready",
        title=agent.packet.title,
        planned_at=tasks.now(),
        complexity=agent.packet.complexity,
        packet_files=agent.packet.paths(),
        open_questions=agent.packet.open_questions,
        checks=agent.packet.verification.commands,
        error=None,
    )
