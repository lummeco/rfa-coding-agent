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
from minisweagent.exceptions import Submitted
from minisweagent.models import get_model
from rfa import settings, tasks
from rfa.packet import FORBIDDEN_PATHS
from rfa.planner import REPOS_DIR, git, pin, seed
from rfa.tasks import Task

CHECKS_FAILED = (
    "You submitted, but checks that passed before you started are failing now:\n\n{report}\n\n"
    "Fix what you broke and submit again. This was round {round} of {rounds}."
)


class CoderConfig(AgentConfig):
    rounds: int = 2
    """How many times the coder may submit before the run is called failed."""


class CoderAgent(DefaultAgent):
    """mini's agent, with the packet's checks standing between submitting and being done."""

    def __init__(
        self,
        model: Model,
        env: Environment,
        *,
        checks: list[str],
        cwd: str,
        baseline: dict[str, bool] | None = None,
        **kwargs,
    ):
        super().__init__(model, env, config_class=CoderConfig, **kwargs)
        self.checks = checks
        self.cwd = cwd
        self.baseline = baseline or {}
        self.results: list[dict] = []
        self.round = 0

    def regressions(self) -> list[dict]:
        """Failures this run is answerable for.

        A check that was already failing before the coder touched anything is the repository's
        problem, not the run's: blaming it burns both rounds on something the packet never asked
        about, and makes every failure report untrustworthy.
        """
        return [r for r in self.results if not r["ok"] and self.baseline.get(r["command"], True)]

    def execute_actions(self, message: dict) -> list[dict]:
        try:
            return super().execute_actions(message)
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

    def run_checks(self) -> list[dict]:
        return run_checks(self.env, self.checks, self.cwd)


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
    env = None
    try:
        env = get_environment(config.get("environment", {}), default_type="docker")
        # Pinned here rather than before the move: a fetch needs the network, and a card that fails
        # on it belongs back in the queue with the rest of the infrastructure failures.
        repos = pin(repos)
        reference = pin(settings.reference_paths(config, task.meta))
        seed(env, repos, reference)
        make_git_repos(env, names)
        checks, cwd = task.meta.get("checks") or [], f"{REPOS_DIR}/{names[0]}"
        # Measure the repository before the coder touches it, so a test that was already red is not
        # counted against the run -- and tell the coder, so it does not go chasing it either.
        baseline = {r["command"]: r["ok"] for r in run_checks(env, checks, cwd)}
        tasks.log(type="baseline", id=task.id, failing=[c for c, ok in baseline.items() if not ok])
        agent = CoderAgent(
            get_model(config=settings.model_config(config, chosen, level)),
            env,
            checks=checks,
            cwd=cwd,
            baseline=baseline,
            output_path=output / "trajectory.json",
            **config.get("agent", {}),
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

    shipped = bool(patches) and not agent.regressions()
    # `landed`, not `branches`: the card's own `branches:` is where its work started from, and
    # these are where it ended up. One name for both would quietly overwrite the first with the second.
    landed = {}
    if shipped:
        for name, (repo, ref) in repos.items():
            if name in patches:
                landed[name] = land(repo, ref, f"rfa/{task.id}", output / f"{name}.patch", task.title)
    tasks.log(
        type="run_finished", id=task.id, shipped=shipped, rounds=agent.round, landed=landed, dropped=dropped
    )
    task.body += changed_note(landed, repos, output) if shipped else failure_note(agent, patches)
    task.body += dropped_note(dropped)
    return tasks.move(
        task,
        "done",
        actor="worker",
        status="shipped" if shipped else "failed",
        finished_at=tasks.now(),
        run=str(output),
        landed=landed or None,
        error=None if shipped else summarize(agent, patches),
    )


def summarize(agent: CoderAgent, patches: dict[str, str]) -> str:
    if not patches:
        return f"The coder changed nothing ({agent.messages[-1].get('extra', {}).get('exit_status', 'unknown')})."
    if broke := [r["command"] for r in agent.regressions()]:
        return f"Broke after {agent.round} round(s): {', '.join(broke)}"
    return "The run did not finish."


def changed_note(landed: dict[str, str], repos: dict[str, tuple[Path, str]], output: Path) -> str:
    """Where the work went. A local branch in your own checkout -- nothing was pushed."""
    lines = ["\n## Result\n"]
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


def failure_note(agent: CoderAgent, patches: dict[str, str]) -> str:
    lines = [f"\n## Result\n\n{summarize(agent, patches)}\n"]
    if failures := report(agent.results):
        lines.append(f"```\n{failures[:4000]}\n```\n")
    return "\n".join(lines)
