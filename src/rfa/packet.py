"""The execution packet: the engineering ticket the planner writes and the coder works from.

It is deliberately a specification rather than an implementation plan. It says what the behavior
must become and how that will be judged, and leaves the how to the coder -- `areas` are named as
hints precisely so the coder inspects the repository instead of trusting them.

`files` and `complexity` are the exception: they never reach the coder's prompt. The host uses
`files` to reject a packet whose paths it cannot find (a hallucinated area otherwise costs a whole
coding run) and to tell the reviewer what was in scope, and `complexity` to pick the coder's tier.
"""

import re
from typing import Literal

from jinja2 import StrictUndefined, Template
from pydantic import BaseModel

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

## Required behavior
{% for item in p.required_behavior %}
- {{ item }}
{% endfor %}
{% if p.constraints %}

## Constraints
{% for item in p.constraints %}
- {{ item }}
{% endfor %}
{% endif %}
{% if p.areas %}

## Relevant areas
Likely relevant:
{% for area in p.areas %}
- {{ area }}
{% endfor %}

These are hints. Inspect the repository before deciding what needs changing.
{% endif %}

## Acceptance criteria
{% for item in p.acceptance_criteria %}
{{ loop.index }}. {{ item }}
{% endfor %}
{% if p.verification.commands or p.verification.manual %}

## Verification
{% if p.verification.commands %}
```bash
{% for command in p.verification.commands %}
{{ command }}
{% endfor %}
```
{% endif %}
{% if p.verification.manual %}
{% if p.verification.commands %}
Manual:
{% endif %}
{% for step in p.verification.manual %}
{% if p.verification.manual | length > 1 or p.verification.commands %}
{{ loop.index }}. {{ step }}
{% else %}
{{ step }}
{% endif %}
{% endfor %}
{% endif %}
{% endif %}
{% if p.non_goals %}

## Non-goals
{% for item in p.non_goals %}
- {{ item }}
{% endfor %}
{% endif %}
"""


class PacketFile(BaseModel):
    path: str
    """`<repo name>/<path>`, as the file sits under /work/repos."""
    why: str


class Verification(BaseModel):
    commands: list[str] = []
    manual: list[str] = []


class Packet(BaseModel):
    """What the planner returns. The coder sees `render()`; the rest is the host's."""

    title: str
    goal: str
    current_behavior: str
    required_behavior: list[str]
    constraints: list[str] = []
    areas: list[str] = []
    acceptance_criteria: list[str]
    verification: Verification = Verification()
    non_goals: list[str] = []
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


NUMBERED = re.compile(r"^\d+\.\s+")


def parse(body: str) -> dict:
    """A rendered packet back into the fields the body carries.

    The inverse of `render()`, for the sections that render: `files`, `complexity` and
    `open_questions` never appear in the body, so they are not in the result -- the host keeps
    them on the card's meta and puts them back when it re-renders. A body that is not a rendered
    packet has no sections to read back, and says so.
    """
    fields: dict = {
        "title": "",
        "goal": "",
        "current_behavior": "",
        "required_behavior": [],
        "constraints": [],
        "areas": [],
        "acceptance_criteria": [],
        "verification": {"commands": [], "manual": []},
        "non_goals": [],
    }
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
    fields["title"] = title or ""

    def items(name: str, marker: str) -> list[str]:
        out: list[str] = []
        for line in sections.get(name, []):
            if line.startswith(marker):
                out.append(line[len(marker):].strip())
            elif line[:1] in (" ", "\t") and out:
                # An indented line is the item above it -- a nested bullet, not a new one.
                out[-1] += "\n" + line
        return out

    def numbered_items(name: str) -> list[str]:
        out: list[str] = []
        for line in sections.get(name, []):
            if (match := NUMBERED.match(line)):
                out.append(line[match.end():].strip())
            elif line[:1] in (" ", "\t") and out:
                out[-1] += "\n" + line
        return out

    fields["goal"] = "\n".join(sections.get("Goal", [])).strip()
    fields["current_behavior"] = "\n".join(sections.get("Current behavior", [])).strip()
    fields["required_behavior"] = items("Required behavior", "- ")
    fields["constraints"] = items("Constraints", "- ")
    fields["areas"] = items("Relevant areas", "- ")
    fields["acceptance_criteria"] = numbered_items("Acceptance criteria")
    fields["non_goals"] = items("Non-goals", "- ")

    commands: list[str] = []
    manual: list[str] = []
    in_block = False
    for line in sections.get("Verification", []):
        stripped = line.strip()
        if in_block:
            if stripped == "```":
                in_block = False
            else:
                commands.append(stripped)
        elif stripped == "```bash":
            in_block = True
        elif stripped and stripped != "Manual:":
            manual.append(NUMBERED.sub("", line).strip())
    fields["verification"] = {"commands": commands, "manual": manual}

    if not all(fields[key] for key in ("title", "goal", "current_behavior", "required_behavior", "acceptance_criteria")):
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
    for command in packet.verification.commands:
        if re.search(r"\b(docker|docker-compose|podman)\b", command):
            found.append(
                f"`{command}`: checks run in a plain container at the repository root, where there is no "
                f"docker. Unwrap it: `docker compose exec app pytest tests/x` is `cd app && pytest tests/x`."
            )
    return found
