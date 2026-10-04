"""under-work -> review -> done, back to todo, or held for your answer.

The third agent, in two passes. The coder says it is finished and the checks agree. First, always,
the reviewer reads what the card changed against its packet: is every criterion implemented, does
the code keep to the decisions you approved, does it make an expensive choice nobody approved, is it
built well enough to keep. Then, for a repository with an `apps:` block and only once the code
holds, it starts the app the coder changed, drives it through a browser, and says whether the
acceptance criteria are actually true of the running thing.

What it finds goes back to `todo` on its own, as comments on the diff the next coder must answer --
the only edge in the pipeline an agent may take without you, and the reason the stage exists. A
choice only you can make holds the card here as a question, and your answer joins the packet.

The reviewer should run on another model than the coder (`review_model:` in rfa.yaml): a model
reviewing its own work shares its blind spots, and the second look is only worth what it can see
that the first could not.

Both passes run in containers holding the repositories at the branch the work landed on. The app
runs in its container too, so "localhost" means the container and nothing the review does can reach
this Mac. What comes out is a verdict and, from the app, a folder of screenshots.

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
from minisweagent.utils.serialize import recursive_merge
from rfa import prepare, rounds, settings, tasks
from rfa.planner import REPOS_DIR, _errors, git, seed
from rfa.rounds import packet_only
from rfa.tasks import Task

DRIVE = "/work/drive.py"
SHOTS = "/out/shots"
SERVE_LOG = "/out/serve.log"
DIFFS = "/out/diff"
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


class Finding(BaseModel):
    route: Literal["fix", "ask", "note"]
    """`fix` goes back to the coder, `ask` waits for the owner, `note` is only said on the card."""
    text: str
    """For `fix`, what is wrong and what it should be instead; for `ask`, the question the owner answers."""
    basis: str = ""
    """The decision or criterion it breaks, or the defect itself. A finding without one is a taste."""
    repo: str = ""
    file: str = ""
    line: int = 0
    previous: int = 0
    """After a fix round: the number of the earlier comment this one says was not dealt with."""


class CodeReview(BaseModel):
    judgements: list[Judgement]
    findings: list[Finding] = []
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


def code_problems(
    review: CodeReview,
    expected: list[str],
    files: dict[str, set[str]],
    latest: dict[str, set[str]] | None = None,
    previous: int = 0,
) -> list[str]:
    """Why the host sends a schema-valid code review back: it judged something other than the packet,
    left an unmet criterion with nothing to do about it, found something it cannot say why, pointed
    at nothing -- or, after a fix round, moved the goalposts.

    `latest` is what the fix round under review changed, None after an original round. A second
    review judges whether the comments were dealt with and what the fix touched; one that reviews the
    whole branch afresh finds something new every time, and the two agents go round until the
    attempts run out.
    """
    found = []
    if len(review.judgements) != len(expected):
        found.append(
            f"There are {len(expected)} acceptance criteria and you judged {len(review.judgements)}. "
            f"Judge every one of them, in order."
        )
    unmet = [j.criterion for j in review.judgements if not j.met]
    if unmet and not any(f.route != "note" for f in review.findings):
        found.append(f"{len(unmet)} criteria are unmet and no `fix` or `ask` says what to do: {'; '.join(unmet[:3])}.")
    for n, f in enumerate(review.findings, 1):
        if f.route == "note":
            continue
        if not f.basis.strip():
            found.append(
                f"Finding {n} has no `basis`. Name the decision or criterion it breaks, or the defect; "
                "a preference is a `note`."
            )
        if f.file and (f.file not in files.get(f.repo, set()) or f.line < 1):
            found.append(
                f"Finding {n} points at `{f.repo}/{f.file}` line {f.line}, which is not a file in the branch. "
                f"`repo` is the folder under {REPOS_DIR}, `file` the path inside it, and lines start at 1."
            )
        if not 0 <= f.previous <= previous:
            found.append(f"Finding {n} repeats earlier comment {f.previous}, but there were {previous}.")
        elif latest is not None and not f.previous and f.file not in latest.get(f.repo, set()):
            found.append(
                f"Finding {n} is new, and not on a file this fix round changed. Judge the earlier comments and "
                "what the fix touched: give a comment that was not dealt with as `previous`, or make this a `note`."
            )
    return found


class ReviewerAgent(tasks.Pausable, tasks.Unlooping, DefaultAgent):
    """mini's agent, with the host's reading of the verdict standing between submitting and done."""

    schema: type[BaseModel] = Review

    def __init__(self, model: Model, env: Environment, *, expected: list[str], **kwargs):
        super().__init__(model, env, config_class=ReviewerConfig, **kwargs)
        self.expected = expected
        self.review = None
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
            self.review = self.schema.model_validate(data)
        except ValidationError as e:
            return [f"`{self.config.review_path}` does not match the schema: {_errors(e)}"]
        return self.judge(self.review)

    def judge(self, review: Review) -> list[str]:
        return problems(review, self.expected, self.shots())

    def shots(self) -> int:
        return len(self.env.execute({"command": f"ls -1 {self.config.shots_path} 2>/dev/null"})["output"].split())


class CodeReviewerAgent(ReviewerAgent):
    """The same loop, reading a diff rather than driving an app."""

    schema = CodeReview

    def __init__(
        self,
        model: Model,
        env: Environment,
        *,
        files: dict[str, set[str]],
        latest: dict[str, set[str]] | None = None,
        previous: int = 0,
        **kwargs,
    ):
        super().__init__(model, env, **kwargs)
        self.files, self.latest, self.previous = files, latest, previous

    def judge(self, review: CodeReview) -> list[str]:
        return code_problems(review, self.expected, self.files, self.latest, self.previous)


class AppFailed(Exception):
    """The app under review would not start. The card's problem, not the machine's."""


class AppKilled(RuntimeError):
    """The kernel killed the app's setup or server: the container ran out of memory. The machine's
    problem, not the card's, so it leaves the card in `review` rather than costing it an attempt."""


KILLED_RE = re.compile(r"^Killed\b", re.MULTILINE)


def killed(returncode: int, output: str) -> bool:
    """SIGKILL, which in a memory-capped container is the OOM killer. A wrapped command's own exit
    code is often lost (`|| exit 1`), so the shell's `Killed` line counts as well."""
    return returncode == 137 or bool(KILLED_RE.search(output))


def vm_note(total: int) -> str:
    """Docker Desktop runs every container in one VM, and the VM's memory is the real ceiling: a
    container's `--memory` above it is never reached. Said on the card, because an OOM kill with
    free memory on the Mac otherwise looks like it cannot be happening."""
    if not total:
        return ""
    return (
        f"\n\nDocker's VM has {total / 2**30:.1f} GiB in all, shared by every container, whatever "
        "`--memory` in reviewer.yaml says. Raise it in Docker Desktop: Settings > Resources > Memory."
    )


def docker_memory(executable: str) -> int:
    found = subprocess.run([executable, "info", "--format", "{{.MemTotal}}"], capture_output=True, text=True)
    return int(found.stdout.strip()) if found.stdout.strip().isdigit() else 0


def playwright_pin(image: str) -> str:
    """The playwright `drive.py` imports, read off the image tag it has to match.

    The image bakes the browsers and not the package -- it builds playwright into a virtualenv it
    then deletes -- so the version installed here is the one those baked browsers belong to. An
    image with no version in its tag gets whatever pip offers, and may not match.
    """
    return m.group(1) if (m := re.search(r":v(\d[\w.]*?)(-|$)", image)) else ""


def install_playwright(image: str) -> str:
    """The setup command for the playwright `rfa.drive` imports: the first thing in the saved image."""
    return f"pip install --quiet playwright{f'=={pin}' if (pin := playwright_pin(image)) else ''}"


def put(env: Environment, source: Path, dest: str) -> None:
    """A file, or a folder that is not there yet, into the container."""
    subprocess.run(
        [env.config.executable, "cp", str(source), f"{env.container_id}:{dest}"], check=True, capture_output=True
    )


def install_drive(env: Environment) -> None:
    """Put `rfa.drive` in the container. The host never imports it."""
    put(env, Path(__file__).parent / "drive.py", DRIVE)


def url(spec: dict) -> str:
    return f"http://127.0.0.1:{spec['port']}{spec.get('path', '/')}"


def start_app(env: Environment, repo: str, spec: dict) -> None:
    """Run the `apps:` block's `start:`, leave the app serving, wait for the port. Its `setup:` is
    already in the image (see `rfa.prepare`).

    Deliberately the host's job rather than the agent's. An agent that has to work out how to start
    the app spends its steps on that and reviews nothing, and it fails a different way every run.

    A start that never comes up is the coder's failure, not the machine's: the app is at the branch
    the coder wrote, and an app that will not boot is exactly what this stage is for. The serve log
    goes back as the problem, so `apps:` being wrong reads the same way -- on the first card, once.
    The exception is the OOM killer: no code change fixes a container too small for the build.
    """
    for command in spec.get("start") or []:
        result = env.execute({"command": command}, cwd=f"{REPOS_DIR}/{repo}", timeout=spec.get("setup_timeout", 900))
        if result["returncode"] != 0:
            if killed(result["returncode"], result["output"]):
                raise AppKilled(
                    f"`{command}` was killed, out of memory:\n\n{result['output'][-3000:]}"
                    + vm_note(docker_memory(env.config.executable))
                )
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
        if killed(0, log):
            raise AppKilled(
                f"`{spec['serve']}` was killed, out of memory:\n\n{log}" + vm_note(docker_memory(env.config.executable))
            )
        raise AppFailed(f"`{spec['serve']}` never answered on port {spec['port']}:\n\n{log}")


def collect(env: Environment, into: Path, shots: str = SHOTS) -> list[str]:
    """The screenshots, out of the container and beside the trajectory. Returns what arrived."""
    subprocess.run(
        [env.config.executable, "cp", f"{env.container_id}:{shots}/.", str(into)], check=False, capture_output=True
    )
    return sorted(p.name for p in into.glob("*.png"))


def review_note(review: Review, shots: list[str]) -> str:
    """What the app review says, on the card, under the code review's note."""
    lines = ["\n### App\n", f"**{review.verdict}**" + (f" — {review.notes}" if review.notes else "") + "\n"]
    lines += [f"{'✓' if j.met else '✗'} {j.criterion}\n  {j.evidence}" for j in review.judgements]
    if review.problems:
        lines.append("\n#### Problems\n")
        lines += [f"- {p}" for p in review.problems]
    if shots:
        lines.append(f"\nScreenshots: {', '.join(shots)}\n")
    return "\n".join(lines) + "\n"


def code_note(review: CodeReview) -> str:
    """What the code review says, on the card, where you read it."""
    lines = ["\n## Review\n", "### Code\n"]
    lines += [f"{'✓' if j.met else '✗'} {j.criterion}\n  {j.evidence}" for j in review.judgements]
    if notes := [f"- {f.text}" for f in review.findings if f.route == "note"]:
        lines += ["\n#### Notes\n", *notes]
    if review.notes:
        lines.append(f"\n{review.notes}")
    return "\n".join(lines) + "\n"


def comment(f: Finding) -> dict:
    """A `fix` as a comment on the diff: on its line when it has one, on the whole round otherwise."""
    again = f"Still not dealt with (comment {f.previous}): " if f.previous else ""
    return {"repo": f.repo, "file": f.file, "line": f.line, "text": f"{again}{f.text}\n\nBasis: {f.basis}"}


def question(f: Finding) -> str:
    return f"{f.text} — {f.basis}" + (f" (`{f.repo}/{f.file}:{f.line}`)" if f.file else "")


def review_model(config: dict, meta: dict, model: str = "", reasoning: str = "") -> tuple[str, str]:
    """The reviewer's model: what the command said, the card's `review_model`, then `review_model` in
    rfa.yaml. With none of those it is the card's coding model, as it was before there was a choice."""
    name = model or meta.get("review_model") or config.get("review_model") or meta.get("model") or ""
    return settings.pick(config, str(name)), reasoning or str(meta.get("review_reasoning") or "")


def landed_repos(config: dict, task: Task, names: list[str]) -> dict[str, tuple[Path, str]]:
    """The checkouts a review seeds: what the coder landed at its branch, the rest at its start."""
    landed = task.meta.get("landed") or {}
    repos = settings.repo_paths(config, names, settings.start_branches(task.meta))
    return {name: (path, landed.get(name) or ref) for name, (path, ref) in repos.items()}


def write_diffs(task: Task, repos: dict[str, tuple[Path, str]], into: Path) -> None:
    """Everything the card changed, per repository, from where its first round started: the diff a
    pull request would show. A card from before rounds were recorded is one commit."""
    landed, bases = task.meta.get("landed") or {}, rounds.first_bases(rounds.load(task.id))
    into.mkdir(parents=True, exist_ok=True)
    for name in (name for name in landed if name in repos):
        args = ("diff", f"{bases[name]}...{landed[name]}") if name in bases else ("show", "--format=", landed[name])
        (into / f"{name}.diff").write_bytes(git(repos[name][0], *args, text=False))


def last_round(id: str) -> tuple[list[dict], dict[str, set[str]] | None]:
    """The comments the latest round answered and the files it changed, when it was a fix round.
    After an original round there is nothing earlier to hold the review to."""
    entries = rounds.load(id)["rounds"]
    if not entries or entries[-1].get("kind") != "fix":
        return [], None
    patches = rounds.round_dir(id, entries[-1]["n"]).glob("*.patch")
    return entries[-1].get("comments") or [], {
        p.stem: set(rounds.files(p.read_text(errors="replace"))) for p in patches
    }


def code_review(task: Task, config: dict, model: dict, expected: list[str], output: Path) -> CodeReviewerAgent:
    """Read what the card changed against its packet. No app and no browser: the repositories at the
    landed branch, the diff from where the work began, and bash."""
    repos = landed_repos(config, task, task.meta.get("repos") or [])
    write_diffs(task, repos, output / "diff")
    previous, latest = last_round(task.id)
    env = get_environment(config.get("environment", {}), default_type="docker")
    try:
        files = seed(env, repos)
        env.execute({"command": f"mkdir -p {Path(DIFFS).parent}"})
        put(env, output / "diff", DIFFS)
        agent = CodeReviewerAgent(
            get_model(config=model),
            env,
            expected=expected,
            files=files,
            latest=latest,
            previous=len(previous),
            output_path=output / "code-trajectory.json",
            task_id=task.id,
            **recursive_merge(config.get("agent", {}), config.get("code_agent", {})),
        )
        agent.run(
            packet_only(task.body),
            criteria=expected,
            repos=sorted(repos),
            diffs=DIFFS,
            previous=previous,
            latest=None if latest is None else {name: sorted(paths) for name, paths in latest.items()},
        )
        return agent
    finally:
        env.cleanup()


def app_review(
    task: Task, config: dict, model: dict, expected: list[str], under_test: str, spec: dict, output: Path
) -> tuple[ReviewerAgent, list[str]]:
    """Start the app at the landed branch and drive it against the criteria. Returns the screenshots too."""
    # `with:` is what the app cannot run without -- its backend -- seeded beside it at its pinned ref.
    names = list(dict.fromkeys([*(task.meta.get("repos") or []), *(spec.get("with") or [])]))
    repos = landed_repos(config, task, names)
    container = recursive_merge(config.get("environment", {}), {"env": spec.get("env") or {}})
    # A setup that fails is the machine's -- the network, the registry -- not the coder's, so it
    # raises like any other infrastructure trouble instead of sending the card back.
    setup = {"setup": [install_playwright(container["image"]), *(spec.get("setup") or [])]}
    container["image"] = prepare.image(container, spec | setup, repos, under_test, "reviewer")
    env = get_environment(container, default_type="docker")
    try:
        seed(env, repos)
        install_drive(env)
        start_app(env, under_test, spec)
        agent = ReviewerAgent(
            get_model(config=model),
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
            notes=spec.get("notes", ""),
            shots=agent.config.shots_path,
        )
        return agent, collect(env, output, agent.config.shots_path)
    finally:
        env.cleanup()


def review_task(task: Task, config: dict, model: str = "", reasoning: str = "") -> Task:
    """Review one card: the code always, then the app where there is one, and the card moves itself on.

    Pass sends it to `done`. A `fix`, or an app that does not do what was asked, sends it back to
    `todo` as the ticket it was, with what was found waiting as comments on its diff, and the coder's
    own `attempts` is what stops the two of them going round forever. An `ask` holds it here as a
    question for you. A review that cannot be run at all leaves the card here, failed, for you -- the
    same way an unplannable draft stays in `planning`.
    """
    if task.stage != "review":
        raise ValueError(f"{task.id} is in `{task.stage}`, not `review`")
    if not (landed := task.meta.get("landed") or {}):
        raise ValueError(f"{task.id} landed nothing to review")
    expected = criteria(task.body)
    specs = settings.app_specs(config, task.meta.get("repos") or [])
    under_test = next((name for name in landed if name in specs), "")
    output = tasks.home() / "var" / "runs" / task.id / "review"
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    chosen, level = review_model(config, task.meta, model, reasoning)
    # Not `model`: that is the coder's, and the next fix round would otherwise run on the reviewer's.
    tasks.save(task, status="reviewing", reviewed_with=chosen, open_questions=None)
    tasks.log(type="review_started", id=task.id, repo=under_test or None, model=chosen)
    app, shots = None, []
    try:
        model_config = settings.model_config(config, chosen, level)
        code = code_review(task, config, model_config, expected, output)
        # Driving an app whose code is about to change again would judge a build nobody will ship.
        holds = code.review is not None and all(f.route == "note" for f in code.review.findings)
        if holds and under_test and expected:
            app, shots = app_review(task, config, model_config, expected, under_test, specs[under_test], output)
    except AppFailed as e:
        tasks.log(type="review_failed", id=task.id, error=str(e))
        return send_back(task, [{"text": str(e)}], output)
    except tasks.Paused:
        tasks.log(type="review_paused", id=task.id)
        return tasks.save(task, status="queued", paused=True)
    except Exception as e:
        tasks.log(type="review_error", id=task.id, error=str(e))
        return tasks.save(task, status="failed", error=str(e))

    if gave_up := next((a for a in (code, app) if a is not None and a.review is None), None):
        note = gave_up.messages[-1].get("content", "the reviewer gave up")
        tasks.log(type="review_error", id=task.id, error=note)
        return tasks.save(task, status="failed", error=str(note))
    verdict = {"code": code.review.model_dump(), "app": app.review.model_dump() if app else None}
    (output / "review.json").write_text(json.dumps(verdict, indent=2))
    findings = code.review.findings
    tasks.log(type="reviewed", id=task.id, findings=len(findings), app=app and app.review.verdict, shots=len(shots))
    if asks := [f for f in findings if f.route == "ask"]:
        # Before any `fix`: the answer may change what the fix should be.
        tasks.log(type="review_asked", id=task.id, questions=len(asks))
        return tasks.save(task, status="question", open_questions=[question(f) for f in asks], review=str(output))
    if fixes := [f for f in findings if f.route == "fix"]:
        return send_back(task, [comment(f) for f in fixes], output)
    if app and app.review.verdict == "fail":
        return send_back(task, [{"text": p} for p in app.review.problems], output)
    task.body += code_note(code.review) + (review_note(app.review, shots) if app else "")
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


def send_back(task: Task, found: list[dict], output: Path) -> Task:
    """To `todo`, as the ticket it was, with what the review found waiting as comments on its diff.

    Comments rather than a note under the ticket: the coder answers each one by number, the board
    shows what it said, and the next review holds the fix to exactly those.

    `landed` is left alone on purpose: that branch really is in your checkout, and saying otherwise
    because the work was rejected would hide it. The next coding round lands beside it.
    """
    for item in found:
        rounds.comment(task.id, {**item, "by": "reviewer"})
    task.body = packet_only(task.body)
    texts = [item["text"] for item in found]
    tasks.log(type="sent_back", id=task.id, problems=texts[:5])
    return tasks.move(
        task,
        "todo",
        actor="reviewer",
        status="todo",
        review=str(output),
        shots=sorted(p.name for p in output.glob("*.png")) or None,
        error="; ".join(texts)[:500],
    )
