"""Pull requests over the GitHub API, with a token that is only what this needs.

`gh pr create` would be less code, but it carries whatever `gh auth login` was granted -- `repo`,
`read:org`, often `workflow`, across every repository the account can see. What opens these pull
requests instead is a fine-grained PAT in the login Keychain: repository access limited to the
repos in `repos:`, permissions Contents and Pull requests read/write, and nothing else. A coding
agent that pushes branches on its own should not hold more than that.

The token never reaches disk or a credential helper. Git is handed it as a per-invocation header
and the API as a bearer, each for the length of one call.
"""

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
KEYCHAIN_SERVICE = "rfa-github-pat"


class PublishError(RuntimeError):
    """A push or a pull request did not happen; the reason is for whoever pressed the button."""


def token(config: dict) -> str:
    service = (config.get("github") or {}).get("keychain_service", KEYCHAIN_SERVICE)
    found = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True)
    if found.returncode or not found.stdout.strip():
        raise PublishError(
            "no GitHub token in the Keychain. Create a fine-grained PAT -- repository access: only the "
            "repos in `repos:`; permissions: Contents and Pull requests, read and write; nothing else -- "
            f"and add it with:\n  security add-generic-password -s {service} -a rfa -w")
    return found.stdout.strip()


def push(repo_path: Path, branch: str, secret: str) -> None:
    """Push one branch to origin. Not forced: a diverged remote is something to look at, not overwrite."""
    done = subprocess.run(
        ["git", "-C", str(repo_path), "push", "--quiet", "origin", branch],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
             "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
             "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic "
                                   + base64.b64encode(f"x-access-token:{secret}".encode()).decode()})
    if done.returncode:
        raise PublishError(f"push of {branch} failed: {done.stderr.strip()[-500:]}")


def api(secret: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict | list]:
    request = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {secret}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}
    except OSError as exc:
        raise PublishError(f"GitHub API unreachable: {exc}") from exc


def open_pr(secret: str, repo: str, head: str, base: str, title: str) -> str:
    """Open the pull request and return its URL, or the URL of the one already open for this branch.

    `repo` is the key from `repos:` -- `lummeco/rfa-coding-agent` -- which is also the path the API
    wants, so nothing has to be configured twice.
    """
    status, data = api(secret, "POST", f"/repos/{repo}/pulls", {"title": title, "head": head, "base": base})
    if status == 201 and isinstance(data, dict):
        return str(data["html_url"])
    if status == 422:  # one is already open for this head: pressing the button twice is not an error
        found_status, found = api(secret, "GET", f"/repos/{repo}/pulls?head={repo.partition('/')[0]}:{head}&state=open")
        if found_status == 200 and isinstance(found, list) and found:
            return str(found[0]["html_url"])
    raise PublishError(f"opening the PR on {repo} failed: HTTP {status} "
                       f"{data.get('message', '') if isinstance(data, dict) else ''}".strip())
