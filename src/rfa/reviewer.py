"""under-work -> review -> done, or back to todo.

The third agent. The coder says it is finished and the checks agree; this one starts the app the
coder changed, drives it through a browser, and says whether the packet's acceptance criteria are
actually true of the running thing. Work that fails goes back to `todo` on its own -- the only edge
in the pipeline an agent may take without you, and the reason the stage exists.

The container is the coder's, one stage later: the same seeded copy of the repositories, except at
the branch the work landed on, out of an image with the browsers baked into it. The app runs in
there too, so "localhost" means the container and nothing the review does can reach this Mac. What
comes out is a verdict and a folder of screenshots.

The reviewer reads pages, not pictures: a local text model cannot look at a screenshot. It drives
the browser and reads back the DOM, the console and the failed requests (see `rfa.drive`). The
screenshots are written for you, and the board shows them on the card.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from minisweagent import Environment, Model
from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.environments import get_environment
from minisweagent.exceptions import InterruptAgentFlow, Submitted
from minisweagent.models import get_model
from rfa import settings, tasks
from rfa.planner import REPOS_DIR, _errors, seed
from rfa.rounds import packet_only
from rfa.tasks import Task

DRIVE = "/work/drive.py"
SHOTS = "/out/shots"
SERVE_LOG = "/out/serve.log"
RETRY_NOTE = (
    "The host rejected the review you wrote:\n\n{problems}\n\n"
    "Settle these, write the corrected review to {path}, and submit again."
)

CRITERIA_RE = re.compile(r"^##+ +Acceptance criteria *$(.*?)(?=^##+ |\Z)", re.MULTILINE | re.DOTALL)
NUMBERED_RE = re.compile(r"^ *\d+\. +(.*)$", re.MULTILINE)


class Judgement(BaseModel):
    criterion: str
    met: bool
    evidence: str
    """What was done and what the app did. "It works" is not evidence."""


class Review(BaseModel):
    verdict: Literal["pass", "fail"]
    judgements: list[Judgement]
    problems: list[str] = []
    """What is wrong, in words the coder can act on. This is the whole message a failed card carries
    back to `todo`, so a problem nobody could act on costs the card one of its attempts for nothing."""
    notes: str = ""


class ReviewerConfig(AgentConfig):
    attempts: int = 3
    """How many reviews the host will reject before giving up on the card."""
    review_path: str = "/out/review.json"
    """Where in the container the reviewer leaves its verdict. The only judgement that leaves it."""
    shots_path: str = SHOTS
    """Where its screenshots go. Read back rather than assumed: an empty one is a review that never
    opened the app, and that is the one thing a verdict cannot be trusted about."""


def criteria(body: str) -> list[str]:
    """The numbered acceptance criteria out of the packet: the contract this stage judges against.

    Read out of the body rather than the front matter because the board lets you edit a packet, and
    what you edited is what the card now promises.
    """
    if not (section := CRITERIA_RE.search(body)):
        return []
    return [m.group(1).strip() for m in NUMBERED_RE.finditer(section.group(1))]


def problems(review: Review, expected: list[str], shots: int) -> list[str]:
    """Why the host sends a schema-valid review back: it judged something other than the packet, it
    contradicts itself, or it never opened the app."""
    found = []
    if not shots:
        found.append(
            "You took no screenshots. Drive the app with `drive.page` and judge what it actually "
            "did, rather than reading the diff and deciding from that."
        )
    if len(review.judgements) != len(expected):
        found.append(
            f"There are {len(expected)} acceptance criteria and you judged {len(review.judgements)}. "
            f"Judge every one of them, in order."
        )
    if review.verdict == "fail" and not review.problems:
        found.append("A `fail` has to say what is wrong: `problems` is what goes back to the coder.")
    if review.verdict == "pass" and (unmet := [j.criterion for j in review.judgements if not j.met]):
        found.append(f"You passed the card with {len(unmet)} criteria unmet: {'; '.join(unmet[:3])}.")
    return found


class ReviewerAgent(tasks.Pausable, DefaultAgent):
    """mini's agent, with the host's reading of the verdict standing between submitting and done."""

    def __init__(self, model: Model, env: Environment, *, expected: list[str], **kwargs):
        super().__init__(model, env, config_class=ReviewerConfig, **kwargs)
        self.expected = expected
        self.review: Review | None = None
        self.attempt = 0
        env.execute({"command": f"mkdir -p {Path(self.config.review_path).parent} {self.config.shots_path}"})

    def execute_actions(self, message: dict) -> list[dict]:
        try:
            return super().execute_actions(message)
        except Submitted:
            if not (found := self.check_review()):
                raise
            self.attempt += 1
            if self.attempt >= self.config.attempts:
                raise InterruptAgentFlow(
                    {
                        "role": "exit",
                        "content": "\n".join(found),
                        "extra": {"exit_status": "ReviewRejected", "submission": ""},
                    }
                ) from None
            return self.add_messages(
                *self.model.format_observation_messages(
                    message,
                    [
                        {
                            "output": RETRY_NOTE.format(
                                problems="\n".join(f"- {p}" for p in found), path=self.config.review_path
                            ),
                            "returncode": 1,
                            "exception_info": "",
                            "extra": {"interrupt_type": "ReviewRejected"},
                        }
                    ],
                    self.get_template_vars(),
                )
            )

    def check_review(self) -> list[str]:
        """Why the host rejects what the agent wrote. An empty list accepts it into `self.review`."""
        self.review = None
        raw = self.env.execute({"command": f"cat {self.config.review_path}"})
        if raw["returncode"] != 0:
            return [f"`{self.config.review_path}` is not there. Write the review to it before submitting."]
        try:
            data = json.loads(raw["output"])
        except json.JSONDecodeError as e:
            return [f"`{self.config.review_path}` is not valid JSON: {e}."]
        try:
            self.review = Review.model_validate(data)
        except ValidationError as e:
            return [f"`{self.config.review_path}` does not match the schema: {_errors(e)}"]
        return problems(self.review, self.expected, self.shots())

    def shots(self) -> int:
        return len(self.env.execute({"command": f"ls -1 {self.config.shots_path} 2>/dev/null"})["output"].split())


class AppFailed(Exception):
    """The app under review would not start. The card's problem, not the machine's."""


def playwright_pin(image: str) -> str:
    """The playwright `drive.py` imports, read off the image tag it has to match.

    The image bakes the browsers and not the package -- it builds playwright into a virtualenv it
    then deletes -- so the version installed here is the one those baked browsers belong to. An
    image with no version in its tag gets whatever pip offers, and may not match.
    """
    return m.group(1) if (m := re.search(r":v(\d[\w.]*?)(-|$)", image)) else ""


def install_drive(env: Environment) -> None:
    """Put `rfa.drive`, and the playwright it imports, in the container. The host never imports it."""
    subprocess.run(
        [env.config.executable, "cp", str(Path(__file__).parent / "drive.py"), f"{env.container_id}:{DRIVE}"],
        check=True,
        capture_output=True,
    )
    pin = playwright_pin(env.config.image)
    result = env.execute({"command": f"pip install --quiet playwright{f'=={pin}' if pin else ''}"}, timeout=600)
    if result["returncode"] != 0:
        raise RuntimeError(f"playwright would not install in the review container:\n\n{result['output'][-2000:]}")


def url(spec: dict) -> str:
    return f"http://127.0.0.1:{spec['port']}{spec.get('path', '/')}"


def start_app(env: Environment, repo: str, spec: dict) -> None:
    """Run the `apps:` block: install what the app needs, leave it serving, wait for the port.

    Deliberately the host's job rather than the agent's. An agent that has to work out how to start
    the app spends its steps on that and reviews nothing, and it fails a different way every run.

    A start that never comes up is the coder's failure, not the machine's: the app is at the branch
    the coder wrote, and an app that will not boot is exactly what this stage is for. The serve log
    goes back as the problem, so `apps:` being wrong reads the same way -- on the first card, once.
    """
    for command in spec.get("setup") or []:
        result = env.execute({"command": command}, cwd=f"{REPOS_DIR}/{repo}", timeout=spec.get("setup_timeout", 900))
        if result["returncode"] != 0:
            raise AppFailed(f"`{command}` failed:\n\n{result['output'][-3000:]}")
    env.execute(
        {"command": f"mkdir -p {Path(SERVE_LOG).parent} && nohup {spec['serve']} > {SERVE_LOG} 2>&1 & echo started"},
        cwd=f"{REPOS_DIR}/{repo}",
    )
    ready = (
        f"for _ in $(seq {spec.get('ready', 120)}); do "
        f"(exec 3<>/dev/tcp/127.0.0.1/{spec['port']}) 2>/dev/null && exit 0; sleep 1; done; exit 1"
    )
    if env.execute({"command": ready}, timeout=spec.get("ready", 120) + 30)["returncode"] != 0:
        log = env.execute({"command": f"cat {SERVE_LOG}"})["output"][-3000:]
        raise AppFailed(f"`{spec['serve']}` never answered on port {spec['port']}:\n\n{log}")


def collect(env: Environment, into: Path, shots: str = SHOTS) -> list[str]:
    """The screenshots, out of the container and beside the trajectory. Returns what arrived."""
    subprocess.run(
        [env.config.executable, "cp", f"{env.container_id}:{shots}/.", str(into)], check=False, capture_output=True
    )
    return sorted(p.name for p in into.glob("*.png"))


def review_note(review: Review, shots: list[str]) -> str:
    """What the review says, on the card, where you read it."""
    lines = ["\n## Review\n", f"**{review.verdict}**" + (f" — {review.notes}" if review.notes else "") + "\n"]
    lines += [f"{'✓' if j.met else '✗'} {j.criterion}\n  {j.evidence}" for j in review.judgements]
    if review.problems:
        lines.append("\n### Problems\n")
        lines += [f"- {p}" for p in review.problems]
    if shots:
        lines.append(f"\nScreenshots: {', '.join(shots)}\n")
    return "\n".join(lines) + "\n"


def review_task(task: Task, config: dict, model: str = "", reasoning: str = "") -> Task:
    """Review one card: the app goes up, the browser goes through it, and the card moves itself on.

    Pass sends it to `done`. Fail sends it back to `todo` as the ticket it was, with the problems
    under it, and the coder's own `attempts` is what stops the two of them going round forever.
    A review that cannot be run at all leaves the card here, failed, for you -- the same way an
    unplannable draft stays in `planning`.
    """
    if task.stage != "review":
        raise ValueError(f"{task.id} is in `{task.stage}`, not `review`")
    landed = task.meta.get("landed") or {}
    specs = settings.app_specs(config, task.meta.get("repos") or [])
    if not (under_test := next((name for name in landed if name in specs), "")):
        raise ValueError(f"{task.id} landed nothing in a repository with an `apps:` block")
    if not (expected := criteria(task.body)):
        raise ValueError(f"{task.id} has no acceptance criteria to judge")

    repos = settings.repo_paths(config, task.meta.get("repos") or [], settings.start_branches(task.meta))
    output = tasks.home() / "var" / "runs" / task.id / "review"
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    chosen, level = settings.for_task(config, task.meta, model, reasoning)
    tasks.save(task, status="reviewing", model=chosen, reasoning=level or None)
    tasks.log(type="review_started", id=task.id, repo=under_test, model=chosen)

    env, spec = None, specs[under_test]
    try:
        env = get_environment(config.get("environment", {}), default_type="docker")
        # The branch, not the base: what is reviewed is what the coder actually landed.
        seed(env, {name: (path, landed.get(name) or ref) for name, (path, ref) in repos.items()})
        install_drive(env)
        start_app(env, under_test, spec)
        agent = ReviewerAgent(
            get_model(config=settings.model_config(config, chosen, level)),
            env,
            expected=expected,
            output_path=output / "trajectory.json",
            task_id=task.id,
            **config.get("agent", {}),
        )
        agent.run(
            packet_only(task.body),
            criteria=expected,
            url=url(spec),
            repo=under_test,
            serve_log=SERVE_LOG,
            shots=agent.config.shots_path,
        )
        shots = collect(env, output, agent.config.shots_path)
    except AppFailed as e:
        tasks.log(type="review_failed", id=task.id, error=str(e))
        return send_back(task, [str(e)], output)
    except tasks.Paused:
        tasks.log(type="review_paused", id=task.id)
        return tasks.save(task, status="queued", paused=True)
    except Exception as e:
        tasks.log(type="review_error", id=task.id, error=str(e))
        return tasks.save(task, status="failed", error=str(e))
    finally:
        if env is not None:
            env.cleanup()

    if agent.review is None:
        note = agent.messages[-1].get("content", "the reviewer gave up")
        tasks.log(type="review_error", id=task.id, error=note)
        return tasks.save(task, status="failed", error=str(note))
    (output / "review.json").write_text(json.dumps(agent.review.model_dump(), indent=2))
    tasks.log(type="reviewed", id=task.id, verdict=agent.review.verdict, shots=len(shots))
    if agent.review.verdict == "fail":
        return send_back(task, agent.review.problems, output, review_note(agent.review, shots))
    task.body += review_note(agent.review, shots)
    return tasks.move(
        task,
        "done",
        actor="reviewer",
        status="built",
        finished_at=tasks.now(),
        review=str(output),
        shots=shots or None,
        error=None,
    )


def send_back(task: Task, found: list[str], output: Path, note: str = "") -> Task:
    """To `todo`, as the ticket it was, with what the reviewer found under it for the next coder.

    `landed` is left alone on purpose: that branch really is in your checkout, and saying otherwise
    because the work was rejected would hide it. The next coding round lands beside it.
    """
    task.body = packet_only(task.body) + (note or "\n## Review\n\n**fail**\n\n" + "".join(f"- {p}\n" for p in found))
    tasks.log(type="sent_back", id=task.id, problems=found[:5])
    return tasks.move(
        task,
        "todo",
        actor="reviewer",
        status="todo",
        review=str(output),
        shots=sorted(p.name for p in output.glob("*.png")) or None,
        error="; ".join(found)[:500],
    )
