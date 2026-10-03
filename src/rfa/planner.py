"""Draft -> execution packet.

A read-only agent reads the repositories in a throwaway container with no network and writes the
packet to `/out/packet.json`. The host does not trust what comes back: the packet must parse, match
the schema, and name only paths the repository actually has, or it goes back into the same session
with the exact problems -- while a retry still costs one attempt rather than a whole coding run.

The model call is made from the host, so the container never needs egress.
"""

import json
import os
import subprocess
import time
from datetime import datetime
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
REFERENCE_DIR = "/work/reference"
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


class PlannerAgent(tasks.Pausable, tasks.Unlooping, DefaultAgent):
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


def pin(repos: dict[str, tuple[Path, str]]) -> dict[str, tuple[Path, str]]:
    """Turn each ref into the commit it means right now, as of the last fetch -- `fetched` first.

    A run lasts half an hour: a branch name would not mean the same commit when the diff lands as it
    did when the files went in, while a sha means one thing forever.
    """
    return {name: (path, commit(path, ref)) for name, (path, ref) in repos.items()}


def commit(repo: Path, ref: str) -> str:
    """The sha `ref` names: origin's as last fetched, then this checkout's own.

    Origin's first, because the branch list you picked from is the remote's -- a branch this
    checkout has never checked out is the ordinary case rather than an error.
    """
    for candidate in [f"refs/remotes/origin/{ref}", ref]:
        found = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{candidate}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if found.returncode == 0:
            return found.stdout.strip()
    raise KeyError(f"{repo.name}: no branch `{ref}`, here or on origin")


def stamp(repo: Path) -> Path:
    """Touched after every fetch that worked. Git's own FETCH_HEAD is no clock: a failed fetch
    rewrites it too."""
    found = subprocess.run(["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"], capture_output=True, text=True)
    return Path(found.stdout.strip() or repo) / "rfa-fetched"


def refresh(repos: dict[str, tuple[Path, str]], timeout: int = 60) -> list[str]:
    """Fetch every branch of every repository from origin at once; the names that could not be.

    What `pin` resolves is the last fetch, so the daemon keeps that recent while the network is
    there, and every run tries once more before it starts. One deadline for all of them: offline
    each fails at once, but a network that only half works would otherwise hold the run for a
    minute per repository. The refspec is spelled out because a single-branch clone fetches only
    its one branch, and a card may start from any.
    """
    fetches = {
        name: subprocess.Popen(
            ["git", "-C", str(path), "fetch", "--quiet", "origin", "+refs/heads/*:refs/remotes/origin/*"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # A credential prompt nobody will ever answer is a fetch that never ends.
            env=os.environ | {"GIT_TERMINAL_PROMPT": "0"},
        )
        for name, (path, _) in repos.items()
    }
    deadline = time.monotonic() + timeout
    for fetch in fetches.values():
        try:
            fetch.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            fetch.kill()
            fetch.wait()
    for name, fetch in fetches.items():
        if fetch.returncode == 0:
            stamp(repos[name][0]).touch()
    return [name for name, fetch in fetches.items() if fetch.returncode != 0]


def fetched(*repos: dict[str, tuple[Path, str]]) -> str | None:
    """Fetch a run's repositories before `pin`; what the card should say if any could not be.

    Nothing stops for it: an offline run starts from the last fetch. But a run that quietly starts
    from last week's `main` reads like one that started from today's, so the card says which.
    """
    # One fetch per checkout: a reference branch is usually a second name for a work repository.
    unique: dict[Path, str] = {}
    for group in repos:
        for name, (path, _) in group.items():
            unique.setdefault(path, name)
    failed = refresh({name: (path, "") for path, name in unique.items()})
    ages = [
        f"{name} as of {datetime.fromtimestamp(found.stat().st_mtime):%-d %b %H:%M}"
        if (found := stamp(path)).exists()
        else f"{name} as last fetched outside rfa"
        for path, name in unique.items()
        if name in failed
    ]
    return f"Could not reach origin, so this started from {', '.join(ages)}." if ages else None


def branches(repo: Path, default: str = "") -> list[str]:
    """The branch names upstream, live, through this checkout's own git credentials.

    `git ls-remote`, not the GitHub API: nothing to authenticate separately, no rate limit, and it
    works for any remote rather than only for GitHub. Offline, the last fetch beats an empty list --
    you can still pick a branch you already have, and `pin` will still find it.
    """
    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", "--heads", "origin"], capture_output=True, text=True, timeout=60
    )
    if listed.returncode == 0:
        names = [line.partition("refs/heads/")[2] for line in listed.stdout.splitlines() if "refs/heads/" in line]
    else:
        seen = git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/").splitlines()
        names = [r.removeprefix("refs/remotes/origin/") for r in seen if not r.endswith("/HEAD")]
    return sorted(set(names), key=lambda n: (n != default, n.lower()))


def options(config: dict, names: list[str] | None = None) -> dict:
    """Every branch you could start from, for whoever is drawing a picker.

    Errors come back beside the branches rather than instead of them: one unreachable repository
    should not empty the list for the rest.
    """
    configured = config.get("repos") or {}
    found, errors = [], []
    for name in names or list(configured):
        if name not in configured:
            errors.append(f"{name} is not listed under `repos:`")
            continue
        location, _, default = str(configured[name]).partition("@")
        try:
            listed = branches(Path(location).expanduser().resolve(), default)
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"{name}: {str(e).splitlines()[0]}")
            continue
        found += [{"repo": name, "branch": b, "default": b == default} for b in listed]
    return {"branches": found, "errors": errors}


def unpack(env: Environment, into: str, path: Path, ref: str) -> None:
    """One repository's tree at one commit, into the container.

    `git archive` through `docker cp` gives it the files alone -- no history, no other refs, no
    remote, nothing an agent could later push from. Docker environments only.
    """
    env.execute({"command": f"mkdir -p {into}"})
    subprocess.run(
        [env.config.executable, "cp", "-", f"{env.container_id}:{into}"],
        input=git(path, "archive", "--format=tar", ref, text=False),
        check=True,
        capture_output=True,
    )


def seed(
    env: Environment, repos: dict[str, tuple[Path, str]], reference: dict[str, tuple[Path, str]] | None = None
) -> dict[str, set[str]]:
    """The work repositories, plus any reference branches beside them. Returns the work's files.

    Only the work is returned, because that is what a packet's paths are checked against: reference
    code is there to be read, and a packet that aimed at it would be aiming at nothing.
    """
    files = {}
    for name, (path, ref) in repos.items():
        unpack(env, f"{REPOS_DIR}/{name}", path, ref)
        files[name] = tracked_files(path, ref)
    for label, (path, ref) in (reference or {}).items():
        unpack(env, f"{REFERENCE_DIR}/{label}", path, ref)
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


def plan(
    idea: str,
    repos: dict[str, tuple[Path, str]],
    model: Model,
    env: Environment,
    reference: dict[str, tuple[Path, str]] | None = None,
    **kwargs,
) -> PlannerAgent:
    """Run the planner over `idea`. The packet, when there is one, is on the returned agent."""
    repo_files = seed(env, repos, reference)
    agent = PlannerAgent(model, env, repo_files=repo_files, **kwargs)
    agent.run(idea, repo_map=render_map(repo_files), reference=sorted(reference or {}))
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
    repos = settings.repo_paths(config, task.meta.get("repos") or [], settings.start_branches(task.meta))
    reference = settings.reference_paths(config, task.meta)
    offline = fetched(repos, reference)
    repos, reference = pin(repos), pin(reference)
    # The card's own `plan_model:` is what the board and `rfa new -M` set; the command still wins.
    chosen, level = settings.for_task(config, task.meta, model, reasoning, prefix="plan_")
    tasks.save(task, status="planning", plan_model=chosen, plan_reasoning=level or None, offline=offline)
    env = get_environment(config.get("environment", {}), default_type="docker")
    try:
        agent = plan(
            task.body,
            repos,
            get_model(config=settings.model_config(config, chosen, level)),
            env,
            reference,
            task_id=task.id,
            **config.get("agent", {}),
        )
    except tasks.Paused:
        # Back in the queue, held: resuming plans it again from the start.
        tasks.log(type="plan_paused", id=task.id)
        return tasks.save(task, status="queued", paused=True)
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
        complexity_reason=agent.packet.complexity_reason,
        packet_files=agent.packet.paths(),
        open_questions=agent.packet.open_questions,
        error=None,
    )
