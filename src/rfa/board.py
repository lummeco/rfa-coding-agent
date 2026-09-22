"""The board: one static page over the task folders, and the endpoints to read and move them.

There is no state here. Every request reads the folders, so anything you do with `mv`, an editor or
the CLI shows up on the next refresh, and the board going down loses nothing.

Binding to localhost is not access control. Every process on this Mac can reach the board, and so
can any web page you happen to have open -- a cross-site form post to 127.0.0.1 needs nobody's
permission. That matters here more than on most local servers, because `POST /api/move` can carry a
card from `planning` to `todo`, which is the one gate between an idea and a machine writing code.
So each start mints a token, `rfa up` prints it in the URL fragment, and the page hands it back in a
header that no cross-site request can set without a preflight this server refuses.
"""

import json
import secrets
import shlex
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rfa import daemon, github, settings, tasks

PAGE = Path(__file__).parent / "board.html"


def copy_command(repo: Path, branch: str) -> str:
    """The one line that puts a landed branch into whatever you have checked out, uncommitted.

    `show` rather than a diff against the base: the branch is one commit, so the commit itself is
    already exactly the coder's work and nothing has to remember where it started. It lands in the
    working tree only -- unstaged, on your current branch, yours to read and stage a hunk at a time.
    """
    where = shlex.quote(str(repo))
    return f"git -C {where} show --binary {shlex.quote(branch)} | git -C {where} apply"


def copy_commands(landed: dict, configured: dict) -> dict[str, str]:
    """One command per landed branch, keyed the way `landed` is -- by the repository's short name.

    A repository that has since left `repos:` is left out rather than guessed at: without its
    checkout there is no path to run the command in.
    """
    full = {key.rpartition("/")[2]: key for key in configured}
    return {
        name: copy_command(Path(str(configured[key]).partition("@")[0]).expanduser(), branch)
        for name, branch in landed.items()
        if (key := full.get(name))
    }


def snapshot() -> dict:
    """Everything the page draws, in one request."""
    configured = settings.load().get("repos") or {}
    return {
        "stages": list(tasks.STAGES),
        "tasks": [
            {
                "id": task.id,
                "stage": task.stage,
                "status": task.status,
                "title": task.title,
                "attempts": task.attempts,
                "repos": task.meta.get("repos") or [],
                "complexity": task.meta.get("complexity"),
                "branches": task.meta.get("branches") or [],
                "context_branches": task.meta.get("context_branches") or [],
                "model": task.meta.get("model"),
                "reasoning": task.meta.get("reasoning"),
                "created": task.meta.get("created"),
                "error": task.meta.get("error"),
                "landed": task.meta.get("landed") or {},
                "copy": copy_commands(task.meta.get("landed") or {}, configured),
                "prs": task.meta.get("prs") or {},
                "open_questions": task.meta.get("open_questions") or [],
                "body": task.body,
            }
            for task in tasks.tasks()
        ],
        "events": tasks.events()[-500:],
        "repos": sorted(configured),
        "system": daemon.report(),
    }


def tail(message: dict) -> str:
    """As much of an observation as is worth showing: the end, where the answer usually is."""
    return str(message.get("content") or "")[-1500:]


def progress(id: str) -> dict:
    """What a run has done so far: every command it ran, and what came back.

    Live by accident of good design -- mini rewrites the trajectory in a `finally` after each step,
    so the file on disk is at most one model call behind. It is rewritten in place rather than
    swapped, so a read can land mid-write; a half-written file is simply not ready yet.
    """
    if not tasks.ID_RE.fullmatch(id):
        return {"steps": []}
    path = tasks.home() / "var" / "runs" / id / "trajectory.json"
    if not path.exists():
        return {"steps": []}
    try:
        messages = json.loads(path.read_text()).get("messages", [])
    except (json.JSONDecodeError, OSError):
        return {"steps": [], "again": True}
    steps, ran = [], None
    for message in messages:
        extra = message.get("extra") or {}
        if message.get("role") == "assistant":
            ran = [action.get("command", "") for action in extra.get("actions", [])]
        elif message.get("role") == "tool" and ran is not None:
            steps.append({"commands": ran, "returncode": extra.get("returncode"), "output": tail(message)})
            ran = None
        elif message.get("role") == "exit":
            steps.append({"commands": [], "returncode": 0, "output": tail(message), "exit": True})
    # The last one has no observation yet: that command is what the run is doing right now.
    return {"steps": steps, "now": ran or []}


def allowed(headers, token: str, origin: str) -> bool:
    """May this request read or move anything?

    Two questions, and both must answer yes. Does it carry this start's token -- which rides in the
    URL fragment, so browsers never send it to a server and it stays out of access logs and
    `Referer`? The page reads it back out of its own address and returns it as a header, and the
    header is the point: setting one makes a cross-site request non-simple, so the browser has to
    preflight it, and the preflight gets nothing back.

    And does it claim to come from somewhere else? A request that says so is believed, and refused.
    """
    from_elsewhere = (sender := headers.get("Origin")) is not None and sender != origin
    return secrets.compare_digest(headers.get("X-RFA-Token", ""), token) and not from_elsewhere


class Handler(BaseHTTPRequestHandler):
    def allowed(self) -> bool:
        return allowed(self.headers, self.server.token, self.server.origin)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        # The page itself carries no data and needs no token: the token is what it is fetching.
        if self.path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif not self.allowed():
            self._json(403, {"error": "open the board from the link `rfa up` printed"})
        elif self.path.startswith("/api/tasks"):
            self._json(200, snapshot())
        elif self.path.startswith("/api/run"):
            self._json(200, progress(self.path.rpartition("id=")[2]))
        elif self.path.startswith("/api/branches"):
            from rfa.planner import options

            asked = parse_qs(urlparse(self.path).query).get("repos", [""])[0]
            self._json(200, options(settings.load(), [r for r in asked.split(",") if r] or None))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.allowed():
            self._json(403, {"error": "open the board from the link `rfa up` printed"})
            return
        if self.path not in ("/api/move", "/api/new", "/api/pr"):
            self._json(404, {"error": "not found"})
            return
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/new":
            if not (idea := str(payload.get("idea", "")).strip()):
                self._json(400, {"error": "an idea needs some words"})
                return
            model, reasoning = str(payload.get("model") or ""), str(payload.get("reasoning") or "")
            try:
                settings.validate(settings.load(), model, reasoning)
            except (KeyError, ValueError) as e:
                self._json(400, {"error": str(e).strip("'")})
                return
            task = tasks.create(
                idea,
                list(payload.get("repos") or []),
                branches=list(payload.get("branches") or []) or None,
                context_branches=list(payload.get("context_branches") or [])[: settings.MAX_CONTEXT] or None,
                model=model or None,
                reasoning=reasoning or None,
            )
            self._json(200, {"id": task.id, "stage": task.stage, "status": task.status})
            return
        if self.path == "/api/pr":
            try:
                task = tasks.find(payload["id"])
            except (FileNotFoundError, KeyError) as e:
                self._json(400, {"error": f"{type(e).__name__}: {e}"})
                return
            try:
                result = create_pr(task)
            except github.PublishError as e:
                self._json(400, {"error": str(e)})
                return
            if result.get("ok"):
                self._json(200, {"ok": True, "message": result["message"]})
            else:
                self._json(400, {"error": result["message"]})
            return
        try:
            task = tasks.move(tasks.find(payload["id"]), payload["to"], actor="human", **payload.get("meta", {}))
        except (FileNotFoundError, KeyError, tasks.TransitionError, FileExistsError) as e:
            self._json(400, {"error": f"{type(e).__name__}: {e}"})
            return
        self._json(200, {"id": task.id, "stage": task.stage, "status": task.status})

    def log_message(self, *args) -> None:
        """The board is a local page, not a service: its access log is noise."""


def create_pr(task: tasks.Task) -> dict:
    """Push each landed branch to origin and open its pull request.

    `landed` is ``{short_name: branch}``, where the short name is the last segment of the key in
    `repos:` -- "rfa-coding-agent" from "lummeco/rfa-coding-agent". That key is also the path the
    GitHub API wants, so resolving back to it is what makes the pull request addressable.

    One repository failing is reported and the rest still go: a multi-repo task should not lose
    three pull requests because the fourth remote moved.
    """
    config = settings.load()
    configured = config.get("repos") or {}
    if not (landed := task.meta.get("landed") or {}):
        return {"ok": False, "message": "no landed branches to push"}

    secret = github.token(config)
    starts = settings.start_branches(task.meta)
    results, failed = [], False
    opened = dict(task.meta.get("prs") or {})
    full = {key.rpartition("/")[2]: key for key in configured}
    for short_name, branch in landed.items():
        key = full.get(short_name)
        if key is None:
            results.append(f"{short_name}: not listed under `repos:` in {settings.path()}")
            failed = True
            continue
        location, _, pinned = str(configured[key]).partition("@")
        try:
            github.push(Path(location).expanduser().resolve(), branch, secret)
            url = github.open_pr(secret, key, branch, starts.get(key) or pinned or "main", task.title)
        except github.PublishError as e:
            results.append(f"{short_name}: {e}")
            failed = True
            continue
        opened[short_name] = url
        tasks.log(type="pr_opened", id=task.id, repo=short_name, url=url)
        results.append(f"{short_name}: {url}")
    if opened != (task.meta.get("prs") or {}):
        tasks.save(task, prs=opened)
    return {"ok": not failed, "message": "; ".join(results)}


def serve(host: str = "127.0.0.1", port: int = 4380, open_browser: bool = True) -> None:
    server = HTTPServer((host, port), Handler)
    server.token = secrets.token_urlsafe(16)
    server.origin = f"http://{host}:{port}"
    # Written down rather than only printed: `rfa status`, `rfa up` and the menu bar all open the
    # board, and none of them can reach it without this start's token.
    (path := tasks.home() / "var" / "board.url").parent.mkdir(parents=True, exist_ok=True)
    path.write_text(url := f"{server.origin}/#token={server.token}")
    print(f"Board on {url}  (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
