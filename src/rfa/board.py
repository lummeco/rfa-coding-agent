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
import re
import secrets
import shlex
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rfa import daemon, github, settings, tasks

PAGE = Path(__file__).parent / "board.html"


def copy_command(repo: Path, branch: str) -> str:
    """The one line that puts a landed branch into whatever you have checked out, uncommitted.

    `show` rather than a diff against the base: the branch is one commit, so the commit itself is
    already exactly the coder's work and nothing has to remember where it started.

    `-3` because by the time you press this your branch has usually moved: a plain `git apply` wants
    the context lines it was cut from and refuses the moment anything around them changed, while a
    three-way apply merges against the blobs the patch names and only stops at a real clash, which
    it marks in the file. What it costs is that `-3` implies `--index`, so it would both read and
    write the staging area -- which is why it runs against a throwaway index instead. That index is
    `git add -A` of your working tree, so uncommitted work is what the patch merges into rather than
    something in its way, and it is thrown away after, leaving every change unstaged and whatever
    you had staged before untouched.
    """
    return (
        f"(cd {shlex.quote(str(repo))} && export GIT_INDEX_FILE=\"$(git rev-parse --git-path rfa-apply-index)\""
        f" && git add -A && git show --binary {shlex.quote(branch)} | git apply -3;"
        ' s=$?; rm -f "$GIT_INDEX_FILE"; exit $s)'
    )


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
                "archived": task.archived,
                "paused": task.paused,
                "sentry": bool(task.meta.get("sentry")),
                "given_title": task.meta.get("title"),
                "checks": task.meta.get("checks") or [],
                "planned": bool(task.meta.get("planned_at")),
                "repos": task.meta.get("repos") or [],
                "complexity": task.meta.get("complexity"),
                "branches": task.meta.get("branches") or [],
                "context_branches": task.meta.get("context_branches") or [],
                "model": task.meta.get("model"),
                "reasoning": task.meta.get("reasoning"),
                "created": task.meta.get("created"),
                "error": task.meta.get("error"),
                "landed": task.meta.get("landed") or {},
                "shots": task.meta.get("shots") or [],
                "copy": copy_commands(task.meta.get("landed") or {}, configured),
                "prs": task.meta.get("prs") or {},
                "link": task.meta.get("link"),
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


def progress(id: str, of: str = "") -> dict:
    """What a run has done so far: every command it ran, and what came back.

    Live by accident of good design -- mini rewrites the trajectory in a `finally` after each step,
    so the file on disk is at most one model call behind. It is rewritten in place rather than
    swapped, so a read can land mid-write; a half-written file is simply not ready yet.

    `of=review` is the reviewer's own session, which lives one folder deeper so that reviewing a
    card does not write over the coding run it is judging.
    """
    if not tasks.ID_RE.fullmatch(id):
        return {"steps": []}
    run = tasks.home() / "var" / "runs" / id
    path = (run / "review" / "trajectory.json") if of == "review" else (run / "trajectory.json")
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


SHOT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.png")


def screenshot(id: str, name: str) -> bytes | None:
    """One of a review's screenshots, by name.

    Both halves of the path are checked against a pattern rather than cleaned up: this reads files
    off disk for whoever holds the board's token, and a name that has to match is a much shorter
    argument than a name that has been made safe.
    """
    if not tasks.ID_RE.fullmatch(id) or not SHOT_RE.fullmatch(name):
        return None
    path = tasks.home() / "var" / "runs" / id / "review" / name
    return path.read_bytes() if path.is_file() else None


def _when(value: object) -> datetime | None:
    """An event's `ts` as a time, or nothing if it is not a time at all."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def added_lines(id: str) -> int:
    """The lines the coder added in one run's patches: the `+` lines, minus the `+++` file headers.

    The headers name the file rather than add a line, and everything else in a patch -- `diff`,
    `index`, `@@`, context and `-` lines -- is not generated code.
    """
    if not tasks.ID_RE.fullmatch(id):
        return 0
    run = tasks.home() / "var" / "runs" / id
    if not run.is_dir():
        return 0
    return sum(
        1
        for patch in sorted(run.glob("*.patch"))
        for line in patch.read_text(errors="replace").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def analytics(window: str = "all") -> dict:
    """How much of the pipeline has been used, for the window ending now.

    A run is a `run_started` to its `run_finished` or `run_error`, and it is counted by when it
    finished: a run that finished in the window is in every metric, one that finished outside it is
    in none, and one that is still going has no finish and is not counted. Runtime is that span,
    not the card's whole life; the lines are whatever the run's patches add. Read-only over the
    records and recomputed on every call, so the board stays stateless. A run you paused ends there
    too, but it is neither a success nor a failure, so it is left out of every count.
    """
    by_id: dict[str, list[dict]] = {}
    for event in tasks.events():
        if event.get("type") in ("run_started", "run_finished", "run_error", "run_paused"):
            by_id.setdefault(str(event.get("id") or ""), []).append(event)

    match = re.fullmatch(r"([1-9]\d*)d", window) if isinstance(window, str) and window != "all" else None
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(match[1])) if match else None

    total = {"runs": 0, "runtime": 0.0, "loc": 0, "shipped": 0, "failed": 0}
    for id, events in by_id.items():
        starts = [e for e in events if e.get("type") == "run_started"]
        ends = [e for e in events if e.get("type") in ("run_finished", "run_error", "run_paused")]
        for start, end in zip(starts, ends):
            started, finished = _when(start.get("ts")), _when(end.get("ts"))
            outside = cutoff is not None and finished is not None and finished < cutoff
            if started is None or finished is None or outside or end.get("type") == "run_paused":
                continue
            total["runs"] += 1
            total["runtime"] += (finished - started).total_seconds()
            total["loc"] += added_lines(id)
            if end.get("type") == "run_finished" and end.get("shipped"):
                total["shipped"] += 1
            else:
                total["failed"] += 1
    return total


EDITABLE = ("title", "repos", "branches", "context_branches", "model", "reasoning", "checks")


def edit(payload: dict) -> tasks.Task:
    """Rewrite a card's body, and whichever of its EDITABLE fields the payload carries.

    An empty field is removed rather than written empty, so a card goes back to whatever the
    workspace or the body says -- a blank title is the idea's first line again.
    """
    task = tasks.find(payload["id"])
    fields = {key: payload[key] or None for key in EDITABLE if key in payload}
    if "context_branches" in fields:
        fields["context_branches"] = (fields["context_branches"] or [])[: settings.MAX_CONTEXT] or None
    if "model" in fields or "reasoning" in fields:
        settings.validate(settings.load(), fields.get("model") or "", fields.get("reasoning") or "")
    task.body = str(payload.get("body", task.body))
    return tasks.save(task, **fields)


def pause(id: str, paused: bool) -> tasks.Task:
    """Hold a card, or let it go again. A run already on it stops before its next model call."""
    task = tasks.save(tasks.find(id), paused=paused or None)
    tasks.log(type="paused" if paused else "resumed", id=task.id)
    return task


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
            query = parse_qs(urlparse(self.path).query)
            self._json(200, progress(query.get("id", [""])[0], query.get("of", [""])[0]))
        elif self.path.startswith("/api/shot"):
            query = parse_qs(urlparse(self.path).query)
            if (shot := screenshot(query.get("id", [""])[0], query.get("name", [""])[0])) is None:
                self._json(404, {"error": "no such screenshot"})
            else:
                self._send(200, shot, "image/png")
        elif self.path.startswith("/api/analytics"):
            window = parse_qs(urlparse(self.path).query).get("window", ["all"])[0]
            self._json(200, analytics(window))
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
        if self.path not in ("/api/move", "/api/new", "/api/pr", "/api/edit", "/api/archive", "/api/pause"):
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
        if self.path in ("/api/edit", "/api/pause"):
            try:
                task = edit(payload) if self.path == "/api/edit" else pause(payload["id"], bool(payload.get("paused")))
            except (FileNotFoundError, KeyError, ValueError) as e:
                self._json(400, {"error": str(e).strip("'")})
                return
            self._json(200, {"id": task.id, "stage": task.stage, "status": task.status, "paused": task.paused})
            return
        if self.path == "/api/archive":
            try:
                task = tasks.find(payload["id"])
            except (FileNotFoundError, KeyError) as e:
                self._json(400, {"error": f"{type(e).__name__}: {e}"})
                return
            # A flag on the task, not a move: the file stays in its stage folder.
            tasks.save(task, archived=bool(payload.get("archived")))
            self._json(200, {"id": task.id, "stage": task.stage, "status": task.status, "archived": task.archived})
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
