"""todo -> under-work -> done.

The coding itself is mini's agent, unchanged. This module is only the parts mini has no opinion
about: claiming the card, giving the container a git repository to diff against, running the
packet's own checks, and writing down what came out.

A failed check is not the end of the run -- it goes back to the coder as the result of its own
submit, which is what the board counts as a retry.
"""

import json
from pathlib import Path

from minisweagent import Environment, Model
from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.environments import get_environment
from minisweagent.exceptions import FormatError, LimitsExceeded, Submitted
from minisweagent.models import get_model
from minisweagent.utils.serialize import recursive_merge
from rfa import settings, tasks
from rfa.packet import FORBIDDEN_PATHS
from rfa.planner import REPOS_DIR, git, pin, seed
from rfa.tasks import Task

CHECKS_FAILED = (
    "You submitted, but checks that passed before you started are failing now:\n\n{report}\n\n"
    "Fix what you broke and submit again. This was round {round} of {rounds}."
)

WRAP_UP = (
    "\n\n[{deadline}, and when they run out the run stops where it is: the host runs the checks on "
    "whatever you have changed, and lands it only if they pass. Finish the edit you are on, run the "
    "checks and submit.]"
)

UNJUDGEABLE = (
    "None of the packet's checks run in the container, so nothing the coder did could have been "
    "judged, and the run did not start. Fix the checks on the card -- they run in a plain container at "
    "the repository root, not through docker compose -- or the `containers:` block for the repository "
    "in rfa.yaml, and move the card back to todo.\n\n{report}"
)

REASONS = {
    "Submitted": "it said it was finished",
    "ContextExceeded": "it filled the model's context window",
    "LimitsExceeded": "it ran out of steps or budget",
    "AutoSubmitted": "it was cut off, and the host submitted what it had",
    "TimeExceeded": "it ran out of time",
    "RepeatedFormatError": "the model stopped answering with commands the harness could run",
}
"""Why a run ended, in words. The status on its own tells the person reading the card nothing."""

FULL = 0.99
"""How close to the window counts as full. A provider stops a token or two short of the window it
was given, and which token is its business rather than something to match exactly."""


class Unjudgeable(Exception):
    """Every check the packet named was already failing before the coder started."""


class CoderConfig(AgentConfig):
    rounds: int = 2
    """How many times the coder may submit before the run is called failed."""
    step_limit: int = 0
    """Steps the coder gets from its first edit. Reading and setting up before that cost context,
    not budget: a repository that takes sixty steps to understand is not a run going badly."""
    context_window: int = 0
    """Tokens the model is served with (`num_ctx` in rfa.yaml). 0 means the run does not watch it."""
    wrap_up_at: float = 0.8
    """The share of the window, or of the step limit, past which every observation tells the coder
    to finish and submit."""


class CoderAgent(tasks.Pausable, DefaultAgent):
    """mini's agent, with the packet's checks standing between submitting and being done."""

    def __init__(
        self,
        model: Model,
        env: Environment,
        *,
        checks: list[str],
        cwd: str,
        baseline: dict[str, bool] | None = None,
        root: str = REPOS_DIR,
        **kwargs,
    ):
        super().__init__(model, env, config_class=CoderConfig, **kwargs)
        self.checks = checks
        self.cwd = cwd
        self.root = root
        self.baseline = baseline or {}
        self.results: list[dict] = []
        self.round = 0
        self.prompt_tokens = 0
        self.edited_at: int | None = None
        # mini counts from the first call; the budget here counts from the first edit, in `query`.
        self.budget, self.config.step_limit = self.config.step_limit, 0

    def get_template_vars(self, **kwargs) -> dict:
        return super().get_template_vars(step_limit=self.budget, **kwargs)

    def steps(self) -> int:
        """Steps spent since the first edit, which is where the budget starts."""
        return self.n_calls - self.edited_at if self.edited_at is not None else 0

    def edited(self) -> bool:
        """Has any repository under the work root changed since its base commit?"""
        command = f'for d in {self.root}/*/; do git -C "$d" status --porcelain 2>/dev/null; done | head -c 1'
        return bool(self.env.execute({"command": command})["output"].strip())

    def regressions(self) -> list[dict]:
        """Failures this run is answerable for.

        A check that was already failing before the coder touched anything is the repository's
        problem, not the run's: blaming it burns both rounds on something the packet never asked
        about, and makes every failure report untrustworthy.
        """
        return [r for r in self.results if not r["ok"] and self.baseline.get(r["command"], True)]

    def exit_status(self) -> str:
        return self.messages[-1].get("extra", {}).get("exit_status", "") if self.messages else ""

    def submitted(self) -> bool:
        """Did the run end on a submit -- the coder's own, or the host's on its behalf?"""
        return self.exit_status() in ("Submitted", "AutoSubmitted")

    def step(self) -> list[dict]:
        """mini's step, except that a run cut off with a diff in it is submitted rather than lost.

        Out of steps or out of context, the diff in the container is still a diff, and the checks are
        the host's to run either way. A run that was cut off with passing checks lands, and one that
        was cut off with a regression fails on that regression rather than on the cut-off -- the only
        run this loses is one that changed nothing, and the checks are not worth running on that.
        """
        try:
            return super().step()
        except LimitsExceeded as e:
            if self.edited_at is None:
                raise
            self.results = self.run_checks()
            if self.regressions():
                raise
            raise Submitted(
                {
                    "role": "exit",
                    "content": "AutoSubmitted",
                    "extra": {"exit_status": "AutoSubmitted", "submission": "", "cut_off": e.messages[0]["content"]},
                }
            ) from e

    def query(self) -> dict:
        """mini's query, except that a window with no room left ends the run then and there.

        A response cut off at `length` with the window already full is not a format mistake the
        model can correct: the retry that asks it to be brief is one more message in the same
        window, so the three attempts mini allows are three ways to spend the end of the run on the
        same wall -- and the work is still inside the container when it stops.
        """
        if 0 < self.budget <= self.steps():
            raise LimitsExceeded(
                {
                    "role": "exit",
                    "content": "LimitsExceeded",
                    "extra": {"exit_status": "LimitsExceeded", "submission": ""},
                }
            )
        try:
            message = super().query()
        except FormatError as e:
            if not filled_window(e.messages[0], self.config.context_window):
                raise
            # The call was billed before parsing failed, and nothing downstream will charge it now.
            self.cost += e.messages[0].get("extra", {}).get("cost", 0.0)
            raise LimitsExceeded(
                {
                    "role": "exit",
                    "content": "ContextExceeded",
                    "extra": {"exit_status": "ContextExceeded", "submission": ""},
                }
            ) from e
        self.prompt_tokens = usage(message).get("prompt_tokens", 0)
        return message

    def deadline(self) -> str:
        """What is about to end this run, in numbers the coder can act on."""
        if 0 < (window := self.config.context_window) * self.config.wrap_up_at <= self.prompt_tokens:
            return f"{self.prompt_tokens} of the model's {window} tokens of context are used"
        if 0 < self.budget * self.config.wrap_up_at <= self.steps():
            return f"{self.steps()} of the {self.budget} steps since your first edit are used"
        return ""

    def warn(self, messages: list[dict]) -> list[dict]:
        """Put the deadline on the observation the coder is about to read.

        Neither limit announces itself: the run stops mid-edit, the diff stays inside the container
        and the card says the coder changed nothing. The way out is a submit before that, and the
        coder can only choose one if it can see what is coming.
        """
        if messages and (deadline := self.deadline()) and isinstance(messages[-1].get("content"), str):
            messages[-1]["content"] += WRAP_UP.format(deadline=deadline)
        return messages

    def execute_actions(self, message: dict) -> list[dict]:
        try:
            observations = super().execute_actions(message)
        except Submitted:
            self.round += 1
            self.results = self.run_checks()
            if not self.regressions() or self.round >= self.config.rounds:
                raise
            return self.add_messages(
                *self.model.format_observation_messages(
                    message,
                    [
                        {
                            "output": CHECKS_FAILED.format(
                                report=report(self.regressions()), round=self.round, rounds=self.config.rounds
                            ),
                            "returncode": 1,
                            "exception_info": "",
                            "extra": {"interrupt_type": "ChecksFailed"},
                        }
                    ],
                    self.get_template_vars(),
                )
            )
        if self.edited_at is None and self.edited():
            self.edited_at = self.n_calls
        return self.warn(observations)

    def run_checks(self) -> list[dict]:
        return run_checks(self.env, self.checks, self.cwd)


def usage(message: dict) -> dict:
    """What the provider said the call spent, or nothing when it did not say."""
    response = message.get("extra", {}).get("response")
    return (response.get("usage") or {}) if isinstance(response, dict) else {}


def filled_window(message: dict, window: int) -> bool:
    """Was the response cut off because the window was full, or because the model rambled?

    From here the two look the same -- `finish_reason: length`, no action in it -- and they want
    opposite answers: a model that thought for too long can be asked to be brief, while a full
    window only gets fuller, because the asking is itself another message in it.
    """
    response = message.get("extra", {}).get("response")
    if not window or not isinstance(response, dict):
        return False
    if ((response.get("choices") or [{}])[0]).get("finish_reason") != "length":
        return False
    return usage(message).get("total_tokens", 0) >= window * FULL


def report(results: list[dict]) -> str:
    return "\n\n".join(f"$ {r['command']}\n[exit {r['returncode']}]\n{r['output']}" for r in results if not r["ok"])


def run_checks(env: Environment, checks: list[str], cwd: str) -> list[dict]:
    """The packet's own verification commands, run by the host rather than reported by the model."""
    results = []
    for command in checks:
        output = env.execute({"command": command}, cwd=cwd)
        results.append(
            {
                "command": command,
                "ok": output["returncode"] == 0,
                "returncode": output["returncode"],
                "output": output["output"][-4000:],
            }
        )
    return results


def make_git_repos(env: Environment, names: list[str]) -> None:
    """Give each seeded repository one commit, so `git diff` is exactly the coder's own work.

    There is no remote and no history: nothing to push to, and nothing to rewrite.
    """
    for name in names:
        env.execute(
            {
                "command": f"cd {REPOS_DIR}/{name} && git init -q && git add -A && "
                f"git -c user.email=rfa@local -c user.name=rfa commit -qm base"
            }
        )


def blocked(paths: list[str]) -> list[str]:
    """Paths the export gate will not carry out of the container.

    CI configuration is the one thing a diff can contain that runs somewhere by itself: a workflow
    the coder wrote executes the moment you push the branch, without anyone reading it. The packet
    already refuses to aim at these (packet.FORBIDDEN_PATHS); this is the same rule at the far end,
    where it is the coder's own output rather than the planner's intent.
    """
    return [p for p in paths if any(rx.search(p) for rx in FORBIDDEN_PATHS)]


def diffs(env: Environment, names: list[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """What the coder changed, per repository, and what the gate dropped on the way out.

    The diff is the only thing that leaves the container, so it is the only place worth gating.
    """
    changed, dropped = {}, {}
    for name in names:
        where = f"cd {REPOS_DIR}/{name} && git add -A"
        listed = env.execute({"command": f"{where} && git diff --cached --name-only"})
        if listed["returncode"] != 0:
            continue
        if refused := blocked(listed["output"].split()):
            dropped[name] = refused
        # By exact path rather than by pattern: the gate drops what it actually found, and a file
        # whose name merely looks like CI keeps its diff.
        exclude = " ".join(f"':(exclude){path}'" for path in refused)
        output = env.execute({"command": f"{where} && git diff --cached --binary -- . {exclude}"})
        if output["returncode"] == 0 and output["output"].strip():
            changed[name] = output["output"]
    return changed, dropped


def run_dir(id: str) -> Path:
    return tasks.home() / "var" / "runs" / id


def free_branch(repo: Path, name: str) -> str:
    """`name`, or the first `name-2`, `name-3`... the repository does not already have."""
    existing = set(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())
    if name not in existing:
        return name
    return next(f"{name}-{n}" for n in range(2, 1000) if f"{name}-{n}" not in existing)


def land(repo: Path, ref: str, branch: str, patch: Path, message: str) -> str:
    """Put the coder's patch on a branch in your real checkout, and return the branch name.

    Through a throwaway worktree, so the branch appears in the repository while whatever you had
    checked out, staged or half-finished stays exactly as it was. Nothing is pushed: the branch is
    local, and what you do with it is yours.
    """
    branch = free_branch(repo, branch)
    tree = patch.parent / f"worktree-{repo.name}"
    git(repo, "worktree", "add", "-q", "-b", branch, str(tree), ref)
    try:
        git(tree, "apply", str(patch))
        git(tree, "add", "-A")
        git(tree, "-c", "user.email=rfa@local", "-c", "user.name=rfa", "commit", "-qm", message)
    finally:
        git(repo, "worktree", "remove", "--force", str(tree))
    return branch


def run_task(task: Task, config: dict, model: str = "", reasoning: str = "") -> Task:
    """Claim a todo, code it, check it, and put it in done saying how it went."""
    if task.stage != "todo":
        raise ValueError(f"{task.id} is in `{task.stage}`, not `todo`")
    repos = settings.repo_paths(config, task.meta.get("repos") or [], settings.start_branches(task.meta))
    if not repos:
        raise ValueError(f"{task.id} names no repositories; add `repos:` to the card")
    names = list(repos)
    output = run_dir(task.id)
    output.mkdir(parents=True, exist_ok=True)

    # The card's own `model:` is what the board and `rfa new -m` set; the command still wins.
    chosen, level = settings.for_task(config, task.meta, model, reasoning)
    task = tasks.move(
        task,
        "under-work",
        actor="worker",
        status="coding",
        attempts=task.attempts + 1,
        model=chosen,
        reasoning=level or None,
    )
    tasks.log(type="run_started", id=task.id, attempt=task.attempts, model=chosen, reasoning=level)
    spec = settings.container(config, (task.meta.get("repos") or [""])[0])
    cwd = f"{REPOS_DIR}/{names[0]}"
    env = None
    try:
        env = get_environment(environment(config, spec), default_type="docker")
        # Pinned here rather than before the move: a fetch needs the network, and a card that fails
        # on it belongs back in the queue with the rest of the infrastructure failures.
        repos = pin(repos)
        reference = pin(settings.reference_paths(config, task.meta))
        seed(env, repos, reference)
        # Before the base commit, so whatever the setup leaves in the checkout is not the coder's diff.
        setup(env, spec, cwd)
        make_git_repos(env, names)
        checks = task.meta.get("checks") or []
        # Measure the repository before the coder touches it, so a test that was already red is not
        # counted against the run -- and tell the coder, so it does not go chasing it either.
        measured = run_checks(env, checks, cwd)
        baseline = {r["command"]: r["ok"] for r in measured}
        tasks.log(type="baseline", id=task.id, failing=[c for c, ok in baseline.items() if not ok])
        if checks and not any(baseline.values()):
            raise Unjudgeable(UNJUDGEABLE.format(report=report(measured)))
        agent = CoderAgent(
            get_model(config=settings.model_config(config, chosen, level)),
            env,
            checks=checks,
            cwd=cwd,
            baseline=baseline,
            output_path=output / "trajectory.json",
            task_id=task.id,
            # The window the variant is served with, unless the workspace said otherwise: a run
            # that does not know where its ceiling is can only find it by hitting it.
            **({"context_window": settings.context_window(config, chosen)} | config.get("agent", {})),
        )
        agent.run(
            task.body,
            repo_list=", ".join(names),
            primary_repo=cwd,
            checks=checks,
            broken_checks=[c for c, ok in baseline.items() if not ok],
            reference=sorted(reference),
        )
        patches, dropped = diffs(env, names)
    except Unjudgeable as e:
        # A verdict on the packet, not on the machine: retrying would fail the same way in a minute.
        tasks.log(type="run_finished", id=task.id, shipped=False, rounds=0, landed={}, dropped={}, exit="Unjudgeable")
        task.body += f"\n## Result\n\n{e}\n"
        return tasks.move(
            task,
            "done",
            actor="worker",
            status="failed",
            finished_at=tasks.now(),
            run=str(output),
            error=str(e).partition("\n")[0],
        )
    except tasks.Paused:
        # Back to todo, held, and the attempt given back: you stopped it, the run did not fail.
        tasks.log(type="run_paused", id=task.id)
        return tasks.move(task, "todo", actor="worker", status="todo", attempts=task.attempts - 1, paused=True)
    except Exception as e:
        # Docker down, the model unreachable, the image missing: nothing to do with the task itself.
        # Put it back in the queue rather than leaving a card in under-work with no worker on it.
        tasks.log(type="run_error", id=task.id, error=str(e))
        tasks.move(task, "todo", actor="worker", status="todo", error=str(e))
        raise
    finally:
        if env is not None:
            env.cleanup()

    for name, patch in patches.items():
        (output / f"{name}.patch").write_text(patch)
    (output / "checks.json").write_text(json.dumps(agent.results, indent=2))

    shipped = landable(agent, patches)
    # `landed`, not `branches`: the card's own `branches:` is where its work started from, and
    # these are where it ended up. One name for both would quietly overwrite the first with the second.
    landed = {}
    if shipped:
        for name, (repo, ref) in repos.items():
            if name in patches:
                landed[name] = land(repo, ref, f"rfa/{task.id}", output / f"{name}.patch", task.title)
    tasks.log(
        type="run_finished",
        id=task.id,
        shipped=shipped,
        rounds=agent.round,
        landed=landed,
        dropped=dropped,
        exit=agent.exit_status(),
    )
    task.body += changed_note(agent, landed, repos, output) if shipped else failure_note(agent, patches, output)
    task.body += dropped_note(dropped)
    # Work that landed in a repository somebody wrote an `apps:` block for is not done until the
    # reviewer has driven it. Everywhere else there is no app to start, so `done` is the truth.
    if shipped and set(landed) & set(settings.app_specs(config, task.meta.get("repos") or [])):
        return tasks.move(task, "review", actor="worker", run=str(output), landed=landed, error=None)
    return tasks.move(
        task,
        "done",
        actor="worker",
        status="built" if shipped else "failed",
        finished_at=tasks.now(),
        run=str(output),
        landed=landed or None,
        error=None if shipped else summarize(agent, patches),
    )


def environment(config: dict, spec: dict) -> dict:
    """The coder's container, with the repository's own image and variables over the workspace's."""
    return recursive_merge(config.get("environment", {}), {k: spec[k] for k in ("image", "env") if k in spec})


def setup(env: Environment, spec: dict, cwd: str) -> None:
    """The `containers:` block's setup: the dependencies and services the checks need, put there by
    the host. A coder that has to build its own test environment spends its budget on pip and apt,
    and a check that needs a database nobody started is a check that was red before it began."""
    for command in spec.get("setup") or []:
        result = env.execute({"command": command}, cwd=cwd, timeout=spec.get("setup_timeout", 900))
        if result["returncode"] != 0:
            raise RuntimeError(f"`{command}` failed setting up the container:\n\n{result['output'][-3000:]}")


def landable(agent: CoderAgent, patches: dict[str, str]) -> bool:
    """Is this a diff worth putting on a branch?

    Only when the run ended on a submit, with the packet's checks green after it. A run cut off
    before it could submit gets the checks run by the host, so what lands is never a diff nobody
    checked -- but a diff that was checked and passed is the same diff whoever pressed submit.
    """
    return bool(patches) and not agent.regressions() and agent.submitted()


def summarize(agent: CoderAgent, patches: dict[str, str]) -> str:
    """The line the card leads with when a run shipped nothing. It has to say what to do next."""
    reason = f"{REASONS.get(agent.exit_status(), 'the run ended')} ({agent.exit_status() or 'unknown'})"
    if not patches:
        return f"The coder changed nothing: {reason}."
    if broke := [r["command"] for r in agent.regressions()]:
        when = f"after {agent.round} round(s)" if agent.round else f"when it was cut off ({agent.exit_status()})"
        return f"Broke {when}: {', '.join(broke)}"
    return f"Stopped before submitting: {reason}. Its diff was never checked, so nothing was landed."


def changed_note(agent: CoderAgent, landed: dict[str, str], repos: dict[str, tuple[Path, str]], output: Path) -> str:
    """Where the work went. A local branch in your own checkout -- nothing was pushed."""
    lines = ["\n## Result\n"]
    if agent.exit_status() == "AutoSubmitted":
        cut = agent.messages[-1]["extra"]["cut_off"]
        lines.append(
            f"The coder never submitted: {REASONS.get(cut, 'the run ended')} ({cut}). The host ran the "
            "checks on what it had changed and they passed, so it landed -- read it as unfinished work.\n"
        )
    for name, branch in landed.items():
        repo, ref = repos[name]
        lines.append(f"- `{name}` on `{branch}` — `git -C {repo} diff {ref}..{branch}`")
    lines.append(f"\nPatches and the trajectory: `{output}`\n")
    return "\n".join(lines)


def dropped_note(dropped: dict[str, list[str]]) -> str:
    """Say what the gate refused, on the card, where you will read it -- a silent drop is a lie."""
    if not dropped:
        return ""
    lines = ["\n## Not exported\n", "The coder changed CI configuration. It is never carried out of the"]
    lines.append("container, so these are **not** in the branch:\n")
    lines += [f"- `{name}`: {', '.join(f'`{p}`' for p in paths)}" for name, paths in dropped.items()]
    return "\n".join(lines) + "\n"


def failure_note(agent: CoderAgent, patches: dict[str, str], output: Path) -> str:
    lines = [f"\n## Result\n\n{summarize(agent, patches)}\n"]
    if patches:
        # An unlanded diff is still work somebody may want; saying where it is costs a line.
        lines.append(f"Patches and the trajectory: `{output}`\n")
    if failures := report(agent.results):
        lines.append(f"```\n{failures[:4000]}\n```\n")
    return "\n".join(lines)
