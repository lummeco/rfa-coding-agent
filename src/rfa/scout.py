"""Finding work when the machine has none: a scout reads one repository and suggests a few small cards.

You ask for it from the board -- a repository and a focus -- and the request is a card like any other:
it waits its turn in `planning`, obeys the gates and can be paused. The scout is the planner's agent
with another prompt and another thing to write: three to five suggestions, each small and each
pointing at code that exists. The host turns them into drafts marked `suggested:`, which you plan,
edit or dismiss; nothing is planned or coded until you say so. A dismissed draft is only archived,
so the next scout is told about it and does not bring it back.
"""

from typing import Literal

from pydantic import BaseModel

from minisweagent.environments import get_environment
from minisweagent.models import get_model
from rfa import rounds, settings, tasks
from rfa.packet import normalize_path
from rfa.planner import PlannerAgent, fetched, pin, plan
from rfa.tasks import Task

FOCUS = {
    "anything": "Whatever is most worth fixing: real bugs first, then small improvements.",
    "bugs": "Bugs: code that does the wrong thing, or fails, for an input or a state it will really meet.",
    "security": "Security: missing checks on who may do what, unvalidated input, secrets, unsafe defaults.",
    "dead code": "Dead code: paths nothing reaches, flags nothing reads, duplicates of what exists elsewhere.",
    "tests": "Tests: behavior that matters and has no test, and tests that cannot fail.",
}
MIN, MAX = 3, 5
MAX_COMPLEXITY = 2
KNOWN = 120
"""How many of a repository's cards the scout is told about. Past this the oldest are left out."""


class Suggestion(BaseModel):
    title: str
    why: str
    evidence: list[str]
    kind: Literal["bug", "improvement"]
    complexity: Literal[1, 2, 3, 4, 5]


class Found(BaseModel):
    """What the scout returns, most worth doing first."""

    suggestions: list[Suggestion]


def problems(found: Found, repo_files: dict[str, set[str]], tool_calls: int, min_tool_calls: int) -> list[str]:
    """Why the host sends the suggestions back: too many or too few, too big, or pointing at nothing."""
    said = []
    if tool_calls < min_tool_calls:
        said.append(f"You ran {tool_calls} commands. Read the code before suggesting changes to it.")
    if not MIN <= len(found.suggestions) <= MAX:
        said.append(f"{len(found.suggestions)} suggestions; give {MIN} to {MAX}, the most worth doing first.")
    for n, s in enumerate(found.suggestions, 1):
        if s.complexity > MAX_COMPLEXITY:
            said.append(f"Suggestion {n} is complexity {s.complexity}; suggest only work of 1 or 2, or a smaller piece of it.")
        if not s.evidence:
            said.append(f"Suggestion {n} has no evidence; name the file it is about.")
        for path in s.evidence:
            name, _, rel = normalize_path(path).partition(":")[0].partition("/")
            if rel not in repo_files.get(name, ()):
                said.append(f"Suggestion {n}: `{path}` is not a file in {', '.join(repo_files)}.")
    return said


class ScoutAgent(PlannerAgent):
    """The planner's loop -- read, write, submit, be sent back -- over suggestions rather than a packet."""

    schema, check = Found, staticmethod(problems)


def pending(repo: str) -> list[Task]:
    """What a repository still has to be decided on: a scout card not yet filed, or suggestions not yet
    planned or dismissed. While there are any, another scout would only pile more on top."""
    return [
        t
        for t in tasks.tasks()
        if not t.archived
        and repo in (t.meta.get("repos") or [])
        and (t.meta.get("scout") or (t.meta.get("suggested") and t.stage == "draft"))
    ]


def request(repo: str, focus: str = "anything") -> Task:
    """Queue a scout of `repo`. Refused while the repository still has suggestions waiting on you."""
    if repo not in (settings.load().get("repos") or {}):
        raise ValueError(f"no such repository: {repo}")
    if focus not in FOCUS:
        raise ValueError(f"no such focus: {focus} ({', '.join(FOCUS)})")
    if waiting := pending(repo):
        raise ValueError(f"{repo} still has {len(waiting)} to decide on: {waiting[0].title}")
    short = repo.rpartition("/")[2]
    return tasks.plan_now(tasks.create(f"Analyze {short}: {focus}", [repo], scout=focus, title=f"Analyze {short} · {focus}"))


def known(repo: str) -> str:
    """The repository's cards as the scout is told them, so it suggests nothing already asked for,
    shipped, trashed or dismissed."""
    cards = sorted(
        (
            t
            for t in tasks.tasks()
            if repo in (t.meta.get("repos") or []) and not t.meta.get("scout") and (not t.archived or t.meta.get("suggested"))
        ),
        key=lambda t: t.id,
    )
    return "\n".join(
        f"- {t.title} ({'dismissed' if t.archived else t.meta.get('verdict') or t.stage})" for t in cards[-KNOWN:]
    )


def file(task: Task, found: Found) -> Task:
    """Turn a scout's suggestions into drafts, in its order, and file the scout card away."""
    made = [
        tasks.create(
            f"{s.title}\n\n{s.why}\n\nWhere:\n" + "\n".join(f"- `{path}`" for path in s.evidence),
            task.meta["repos"],
            title=s.title,
            suggested=task.id,
            kind=s.kind,
        )
        for s in found.suggestions
    ]
    tasks.log(type="scouted", id=task.id, suggestions=len(made))
    return tasks.save(task, status="scouted", archived=True, suggestions=[t.id for t in made], error=None)


def scout_task(task: Task, config: dict) -> Task:
    """Scout one card's repository. A scout that fails says so on its card and is not run again."""
    repo = task.meta["repos"][0]
    repos = settings.repo_paths(config, [repo])
    offline = fetched(repos, {})
    chosen, level = settings.for_task(config, task.meta, "", "", prefix="plan_")
    tasks.save(task, status="planning", plan_model=chosen, plan_reasoning=level or None, offline=offline)
    env = get_environment(config.get("environment", {}), default_type="docker")
    try:
        agent = plan(
            FOCUS[task.meta["scout"]],
            pin(repos),
            get_model(config=settings.model_config(config, chosen, level)),
            env,
            progress=known(repo),
            agent_class=ScoutAgent,
            task_id=task.id,
            **({"output_path": rounds.run_dir(task.id) / "plan" / "trajectory.json"} | config.get("agent", {})),
        )
    except tasks.Paused:
        tasks.log(type="plan_paused", id=task.id)
        return tasks.save(task, status="queued", paused=True)
    except Exception as e:
        tasks.log(type="plan_failed", id=task.id, error=str(e))
        return tasks.save(task, status="failed", error=str(e))
    finally:
        env.cleanup()
    if agent.packet is None:
        note = agent.messages[-1].get("content", "the scout gave up")
        tasks.log(type="plan_failed", id=task.id, error=note)
        return tasks.save(task, status="failed", error=note)
    return file(task, agent.packet)
