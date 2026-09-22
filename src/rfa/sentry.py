"""Sentry issues become drafts: every `sentry.interval` seconds the daemon asks each project under
`sentry:` for the issues `query` matches -- unresolved ones, by default -- and captures each one it
has not seen before as a draft on the board.

A draft, and only a draft. The card names the repository its project maps to and carries what the
planner cannot fetch for itself, since its container has no network: the exception, where it was
thrown, and the in-app frames of the latest event. Whether it becomes work is still your call.

The token is read-only -- an organization auth token with Issue & Event: Read and nothing else --
and lives in the login Keychain beside the GitHub one, handed to the API as a bearer for the length
of one call and never written down.
"""

import json
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from rfa import settings, tasks
from rfa.tasks import Task

KEYCHAIN_SERVICE = "rfa-sentry-token"


class SentryError(RuntimeError):
    """A poll did not happen; the reason is for the daemon's log."""


@dataclass
class SentryConfig:
    """`sentry:` in rfa.yaml. Absent, and nobody asks."""

    org: str
    projects: dict[str, str] = field(default_factory=dict)
    """Sentry project slug -> the key in `repos:` its issues are drafted against."""
    interval: int = 1800
    query: str = "is:unresolved"
    url: str = "https://sentry.io"
    keychain_service: str = KEYCHAIN_SERVICE

    @classmethod
    def load(cls, config: dict | None = None) -> "SentryConfig | None":
        found = (settings.load() if config is None else config).get("sentry")
        return cls(**found) if found else None


def token(config: SentryConfig) -> str:
    found = subprocess.run(
        ["security", "find-generic-password", "-s", config.keychain_service, "-w"], capture_output=True, text=True
    )
    if found.returncode or not found.stdout.strip():
        raise SentryError(
            "no Sentry token in the Keychain. Create an organization auth token -- Issue & Event: Read, nothing "
            f"else -- and add it with:\n  security add-generic-password -s {config.keychain_service} -a rfa -w"
        )
    return found.stdout.strip()


def api(config: SentryConfig, secret: str, path: str) -> list | dict:
    request = urllib.request.Request(f"{config.url}/api/0{path}", headers={"Authorization": f"Bearer {secret}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except OSError as exc:  # an HTTP error is one too, and says its status
        raise SentryError(f"GET {path}: {exc}") from exc


def issues(config: SentryConfig, secret: str, project: str) -> list[dict]:
    """The first page of what `query` matches, most recently seen first -- Sentry's own order."""
    query = urllib.parse.urlencode({"query": config.query})
    return api(config, secret, f"/projects/{config.org}/{project}/issues/?{query}")


def frames(event: dict) -> list[str]:
    """The in-app frames of the event's exception, innermost last, as `file:line in function`."""
    return [
        f"{frame.get('filename')}:{frame.get('lineNo')} in {frame.get('function')}"
        for entry in event.get("entries") or []
        if entry.get("type") == "exception"
        for value in entry["data"].get("values") or []
        for frame in (value.get("stacktrace") or {}).get("frames") or []
        if frame.get("inApp")
    ][-10:]


def idea(issue: dict, event: dict) -> str:
    """The draft's body: what you would paste from the issue page, with the frames under it."""
    lines = [
        f"Fix the Sentry issue {issue['shortId']}: {issue['title']}",
        "",
        f"- Sentry: {issue['permalink']}",
        f"- Where: {issue.get('culprit') or 'unknown'}",
        f"- {issue.get('level', 'error')}, seen {issue.get('count')} times by {issue.get('userCount')} users; "
        f"first {issue.get('firstSeen')}, last {issue.get('lastSeen')}",
    ]
    if stack := frames(event):
        lines += ["", "In-app frames of the latest event, innermost last:", "", "```", *stack, "```"]
    return "\n".join(lines)


def capture(config: SentryConfig, secret: str, project: str, issue: dict) -> Task:
    event = api(config, secret, f"/organizations/{config.org}/issues/{issue['id']}/events/latest/")
    return tasks.create(
        idea(issue, event), [config.projects[project]], sentry=str(issue["id"]), link=issue["permalink"]
    )


def captured() -> set[str]:
    """The issues already on the board, whatever their stage: one card per issue, ever."""
    return {str(t.meta["sentry"]) for t in tasks.tasks() if t.meta.get("sentry")}


def pull(config: SentryConfig, secret: str) -> list[Task]:
    """One look: every issue `query` matches, in every configured project, that has no card yet."""
    seen = captured()
    return [
        capture(config, secret, project, issue)
        for project in config.projects
        for issue in issues(config, secret, project)
        if str(issue["id"]) not in seen
    ]
