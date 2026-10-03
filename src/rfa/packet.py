"""The execution packet: the engineering ticket the planner writes and the coder works from.

It is deliberately a short design to review rather than an implementation plan: what the code does
today, the decisions the planner took (assumptions and constraints included) and how the work will be
judged -- the expensive choices a human should see before any code exists. The steps are the coder's.

`files` and `complexity` are the exception: they never reach the coder's prompt. The host uses
`files` to reject a packet whose paths it cannot find (a hallucinated area otherwise costs a whole
coding run) and to tell the reviewer what was in scope, and `complexity` to pick the coder's tier.
"""

import re
from typing import Literal

from jinja2 import StrictUndefined, Template
from pydantic import BaseModel

MAX_DECISIONS = 10
MAX_CRITERIA = 5

# Paths the export gate refuses to carry out of the sandbox, so a packet must never aim at one.
FORBIDDEN_PATHS = [
    re.compile(r"^\.github/"),
    re.compile(r"^\.gitmodules$"),
    re.compile(r"^\.gitattributes$"),
    re.compile(r"(^|/)\.gitlab-ci\.yml$"),
    re.compile(r"(^|/)(Jenkinsfile|\.travis\.yml|azure-pipelines\.yml)$"),
]

PACKET_TEMPLATE = """\
# Task
{{ p.title }}

## Goal
{{ p.goal }}

## Current behavior
{{ p.current_behavior }}
{% if p.decisions %}

## Decisions
{% for item in p.decisions %}
- {{ item }}
{% endfor %}
{% endif %}

## Acceptance criteria
{% for item in p.acceptance_criteria %}
{{ loop.index }}. {{ item }}
{% endfor %}
"""


class PacketFile(BaseModel):
    path: str
    """`<repo name>/<path>`, as the file sits under /work/repos."""
    why: str


class Packet(BaseModel):
    """What the planner returns. The coder sees `render()`; the rest is the host's."""

    title: str
    goal: str
    current_behavior: str
    decisions: list[str] = []
    acceptance_criteria: list[str]
    files: list[PacketFile] = []
    open_questions: list[str] = []
    complexity: Literal[1, 2, 3, 4, 5]
    complexity_reason: str

    def render(self) -> str:
        """The ticket as markdown: the packet's whole contract with the coder."""
        body = Template(PACKET_TEMPLATE, undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True).render(p=self)
        return re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"

    def paths(self) -> list[str]:
        return [f.path.strip().strip("`") for f in self.files]


def parse(body: str) -> dict:
    """A rendered packet back into the fields the body carries.

    The inverse of `render()`, for the sections that render: `files`, `complexity` and
    `open_questions` never appear in the body, so they are not in the result -- the host keeps
    them on the card's meta and puts them back when it re-renders. A body that is not a rendered
    packet has no sections to read back, and says so.
    """
    sections: dict[str, list[str]] = {}
    current = None
    title = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is None and title is None:
            # `# Task` is the header; the title is the first line under it.
            if line.strip() and not line.startswith("#"):
                title = line.strip()
        elif current is not None:
            sections[current].append(line)

    def items(name: str, pattern: str) -> list[str]:
        out: list[str] = []
        for line in sections.get(name, []):
            if match := re.match(pattern, line):
                out.append(line[match.end() :].strip())
            elif line[:1] in (" ", "\t") and out:
                # An indented line is the item above it -- a nested bullet, not a new one.
                out[-1] += "\n" + line
        return out

    fields = {
        "title": title or "",
        "goal": "\n".join(sections.get("Goal", [])).strip(),
        "current_behavior": "\n".join(sections.get("Current behavior", [])).strip(),
        # Packets planned before decisions existed kept constraints and non-goals; they are decisions now.
        "decisions": items("Decisions", r"- ")
        + items("Constraints", r"- ")
        + [f"Not in scope: {item}" for item in items("Non-goals", r"- ")],
        "acceptance_criteria": items("Acceptance criteria", r"\d+\.\s+"),
    }
    if not all(fields[key] for key in ("title", "goal", "current_behavior", "acceptance_criteria")):
        raise ValueError("not a rendered packet")
    return fields


def normalize_path(path: str) -> str:
    return path.strip().strip("`").lstrip("/").removeprefix("work/repos/")


def problems(packet: Packet, repo_files: dict[str, set[str]], tool_calls: int, min_tool_calls: int) -> list[str]:
    """Why the host sends a schema-valid packet back: not grounded in the repositories, or aimed at a
    file the export gate would throw away. Told now, while a retry still costs only one attempt."""
    found = []
    if tool_calls < min_tool_calls:
        found.append(
            f"You made {tool_calls} tool calls; read the code you are writing the packet about "
            f"(at least {min_tool_calls} tool calls) instead of writing it from the idea alone."
        )
    if len(packet.decisions) > MAX_DECISIONS:
        found.append(f"{len(packet.decisions)} decisions; keep the {MAX_DECISIONS} the owner most needs to see.")
    if len(packet.acceptance_criteria) > MAX_CRITERIA:
        found.append(
            f"{len(packet.acceptance_criteria)} acceptance criteria; keep the {MAX_CRITERIA} that best tell a "
            "correct implementation from a plausible wrong one."
        )
    folders = {name: {p.rpartition("/")[0] for p in paths} for name, paths in repo_files.items()}
    for path in packet.paths():
        name, _, rel = normalize_path(path).partition("/")
        if name not in repo_files:
            found.append(f"`{path}`: paths must start with a repository name ({', '.join(repo_files)}).")
        elif any(rx.search(rel) for rx in FORBIDDEN_PATHS):
            found.append(
                f"`{path}`: the host rejects any work bundle that changes this file, so the coder can "
                f"never land it. Leave it out, and say so as a step for the owner if the task needs it."
            )
        elif rel not in repo_files[name] and rel.rpartition("/")[0] not in folders[name]:
            found.append(f"`{path}` does not exist, and neither does its folder.")
    return found
