"""The board: one static page over the task folders, and the endpoints to read and move them.

There is no state here. Every request reads the folders, so anything you do with `mv`, an editor or
the CLI shows up on the next refresh, and the board going down loses nothing. `/api/stream` is how
the page learns a refresh is worth doing: it says "changed" whenever a file the board draws from
does, and the page redraws only what that change touched.

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
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rfa import daemon, github, packet, rounds, settings, tasks

PAGE = Path(__file__).parent / "board.html"


def copy_command(repo: Path, branch: str, base: str = "") -> str:
    """The one line that puts a landed branch into whatever you have checked out, uncommitted.

    A diff from the commit the first round started at, because a branch with fix rounds on it is
    more than one commit. A branch landed before rounds were recorded is one commit with no base
    written down, so the commit itself is exactly the coder's work: `show` it.

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
        f'(cd {shlex.quote(str(repo))} && export GIT_INDEX_FILE="$(git rev-parse --git-path rfa-apply-index)"'
        f" && git add -A && {f'git diff --binary {shlex.quote(base)} ' if base else 'git show --binary '}"
        f"{shlex.quote(branch)} | git apply -3;"
        ' s=$?; rm -f "$GIT_INDEX_FILE"; exit $s)'
    )


def copy_commands(landed: dict, configured: dict, state: dict | None = None) -> dict[str, str]:
    """One command per landed branch, keyed the way `landed` is -- by the repository's short name.

    A repository that has since left `repos:` is left out rather than guessed at: without its
    checkout there is no path to run the command in.
    """
    full = {key.rpartition("/")[2]: key for key in configured}
    bases = {}
    for entry in reversed((state or {}).get("rounds") or []):
        bases |= {name: c["base"] for name, c in (entry.get("commits") or {}).items()}
    return {
        name: copy_command(Path(str(configured[key]).partition("@")[0]).expanduser(), branch, bases.get(name, ""))
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
                **(state := rounds.view(task)),
                "id": task.id,
                "stage": task.stage,
                "status": task.status,
                "title": task.title,
                "attempts": task.attempts,
                "archived": task.archived,
                "paused": task.paused,
                "sentry": bool(task.meta.get("sentry")),
                "verdict": task.meta.get("verdict"),
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
                "copy": copy_commands(task.meta.get("landed") or {}, configured, state),
                "prs": task.meta.get("prs") or {},
                "link": task.meta.get("link"),
                "open_questions": task.meta.get("open_questions") or [],
                "body": task.body,
                "packet": planned_packet(task),
            }
            for task in tasks.tasks()
        ],
        "events": tasks.events()[-500:],
        "repos": sorted(configured),
        "system": daemon.report(),
    }


def planned_packet(task: tasks.Task) -> dict | None:
    """The packet a card is ready with, read back out of its body.

    The body is the source of truth, so this is parse, not a copy of what the planner saved:
    whatever the owner edited last is what the detail view edits again.
    """
    if task.stage != "planning" or task.status != "ready":
        return None
    try:
        return packet.parse(task.body)
    except ValueError:
        return None


def tail(message: dict) -> str:
    """As much of an observation as is worth showing: the end, where the answer usually is."""
    return str(message.get("content") or "")[-1500:]


def progress(id: str, of: str = "") -> dict:
    """What a run has done so far: every command it ran, and what came back.

    Live by accident of good design -- mini rewrites the trajectory in a `finally` after each step,
    so the file on disk is at most one model call behind. It is rewritten in place rather than
    swapped, so a read can land mid-write; a half-written file is simply not ready yet.

    `of=review` is the reviewer's own session, which lives one folder deeper so that reviewing a
    card does not write over the coding run it is judging. Otherwise it is the latest round's.
    """
    if not tasks.ID_RE.fullmatch(id):
        return {"steps": []}
    run = rounds.run_dir(id)
    coded = sorted(run.glob("round-*/trajectory.json"), key=lambda p: int(p.parent.name.partition("-")[2]))
    path = (run / "review" / "trajectory.json") if of == "review" else (coded or [run / "trajectory.json"])[-1]
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
    """The lines the coder added in a card's patches: the `+` lines, minus the `+++` file headers.

    The headers name the file rather than add a line, and everything else in a patch -- `diff`,
    `index`, `@@`, context and `-` lines -- is not generated code. Every round's patch counts, and
    a card from before rounds has its one patch at the top of its run folder.
    """
    if not tasks.ID_RE.fullmatch(id):
        return 0
    run = rounds.run_dir(id)
    if not run.is_dir():
        return 0
    return sum(
        1
        for patch in sorted([*run.glob("*.patch"), *run.glob("round-*/*.patch")])
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

    Outcomes are per card, not per run: each card in done, by when it got there, is failed, built
    (waiting for your verdict), trashed or shipped. The rate is shipped out of everything that has
    an outcome; built is left out of it, because that is still waiting on you.
    """
    by_id: dict[str, list[dict]] = {}
    for event in tasks.events():
        if event.get("type") in ("run_started", "run_finished", "run_error", "run_paused"):
            by_id.setdefault(str(event.get("id") or ""), []).append(event)

    match = re.fullmatch(r"([1-9]\d*)d", window) if isinstance(window, str) and window != "all" else None
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(match[1])) if match else None

    total = {"runs": 0, "runtime": 0.0, "loc": 0, "shipped": 0, "trashed": 0, "built": 0, "failed": 0}
    for id, events in by_id.items():
        starts = [e for e in events if e.get("type") == "run_started"]
        ends = [e for e in events if e.get("type") in ("run_finished", "run_error", "run_paused")]
        counted = False
        for start, end in zip(starts, ends):
            started, finished = _when(start.get("ts")), _when(end.get("ts"))
            outside = cutoff is not None and finished is not None and finished < cutoff
            if started is None or finished is None or outside or end.get("type") == "run_paused":
                continue
            total["runs"] += 1
            total["runtime"] += (finished - started).total_seconds()
            # Every round's patch is kept now, so the card's lines are counted once, not once per run.
            total["loc"] += 0 if counted else added_lines(id)
            counted = True
    for task in tasks.tasks("done"):
        reached = _when(task.meta.get("finished_at") or task.meta.get("created"))
        if cutoff is None or (reached is not None and reached >= cutoff):
            total["failed" if task.status == "failed" else task.meta.get("verdict") or "built"] += 1
    judged = total["shipped"] + total["trashed"] + total["failed"]
    return {**total, "rate": total["shipped"] / judged if judged else None}


EDITABLE = ("title", "repos", "branches", "context_branches", "model", "reasoning", "checks")
PACKET_SECTIONS = ("goal", "current_behavior", "acceptance_criteria", "constraints", "non_goals")


def edit(payload: dict) -> tasks.Task:
    """Rewrite a card's body, and whichever of its EDITABLE fields the payload carries.

    An empty field is removed rather than written empty, so a card goes back to whatever the
    workspace or the body says -- a blank title is the idea's first line again.

    On a planned card the payload may instead carry the packet's sections: then the body is the
    packet, so it is parsed, the sections carried are applied, and it is rendered again -- the
    sections left out come back exactly as the planner wrote them. `open_questions` is not part
    of the body; editing it updates the card's meta.
    """
    task = tasks.find(payload["id"])
    fields = {key: payload[key] or None for key in EDITABLE if key in payload}
    if "context_branches" in fields:
        fields["context_branches"] = (fields["context_branches"] or [])[: settings.MAX_CONTEXT] or None
    if "model" in fields or "reasoning" in fields:
        settings.validate(settings.load(), fields.get("model") or "", fields.get("reasoning") or "")
    if "open_questions" in payload:
        fields["open_questions"] = [q.strip() for q in (payload["open_questions"] or []) if str(q).strip()] or None
    if any(key in payload for key in PACKET_SECTIONS):
        if task.stage != "planning":
            raise ValueError(f"{task.id} is in `{task.stage}`; the packet is edited on a planning card")
        task.body = rendered_packet(task, payload)
    else:
        task.body = str(payload.get("body", task.body))
    return tasks.save(task, **fields)


def rendered_packet(task: tasks.Task, payload: dict) -> str:
    """The card's body with its editable sections rewritten, by the Packet's own template.

    The body is what the coder reads, so the round-trip is parse -> edit -> render(): the
    sections the owner did not touch survive exactly, and the shape is `render()`'s, so the
    downstream flow sees no difference. `files`, `complexity` and `open_questions` never render;
    the host keeps them on the card's meta.
    """
    fields = packet.parse(task.body)
    for key in PACKET_SECTIONS:
        if key not in payload:
            continue
        value = payload[key]
        fields[key] = (
            [str(item).strip() for item in value if str(item).strip()]
            if isinstance(value, list)
            else str(value).strip()
        )
    return "\n" + packet.Packet(
        **fields,
        complexity=task.meta.get("complexity") or 3,
        complexity_reason=task.meta.get("complexity_reason") or "",
    ).render()


def fix(id: str) -> tasks.Task:
    """Send a done card back to `todo` for a fix round on the comments waiting on it.

    The body goes back to the ticket alone, the comments reach the coder through its prompt, and the
    attempts start over: this round is yours to ask for, not a retry the daemon should cap.
    """
    task = tasks.find(id)
    if task.stage != "done":
        raise ValueError(f"{task.id} is in `{task.stage}`; only a done card can be sent back to fix comments")
    if not rounds.load(task.id)["pending"]:
        raise ValueError("there are no comments to fix; add one on the latest round's diff first")
    task.body = rounds.packet_only(task.body)
    tasks.log(type="fix_requested", id=task.id)
    return tasks.move(task, "todo", actor="human", status="todo")


def pause(id: str, paused: bool) -> tasks.Task:
    """Hold a card, or let it go again. A run already on it stops before its next model call."""
    task = tasks.save(tasks.find(id), paused=paused or None)
    tasks.log(type="paused" if paused else "resumed", id=task.id)
    return task


def retry(id: str) -> tasks.Task:
    """Give a to-do card its attempts back, so the daemon picks it up again after it gave up."""
    if (task := tasks.find(id)).stage != "todo":
        raise ValueError(f"{id} is in {task.stage}; only a to-do card is retried")
    tasks.log(type="retried", id=task.id)
    return tasks.save(task, status="todo", attempts=None, error=None, paused=None)


def reorder(payload: dict) -> tasks.Task:
    """Move a card to a new position in its own column.

    Not a move: stage, status and attempts are untouched, and no transition is involved -- the
    order is written into the column's cards, so the board, the daemon and `rfa plan`/`rfa work`
    all read the same queue order out of the files it came from."""
    task = tasks.find(payload["id"])
    column = tasks.tasks(task.stage)
    try:
        index = int(payload["index"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("a position is needed")
    if not 0 <= index < len(column):
        raise ValueError(f"position {index} is out of range for a column of {len(column)}")
    placed = [t for t in column if t.id != task.id]
    placed.insert(index, task)
    for position, card in enumerate(placed):
        tasks.save(card, order=position + 1)
    tasks.log(type="reordered", id=task.id, index=index)
    return task


def judge(id: str, verdict: str | None) -> tasks.Task:
    """Your call on built code: shipped, trashed, or back to waiting. Only yours, and only in done."""
    task = tasks.find(id)
    if verdict not in ("shipped", "trashed", None):
        raise ValueError(f"no such verdict: {verdict}")
    if task.stage != "done" or task.status == "failed":
        raise ValueError(f"{task.id} is {task.stage}/{task.status}, not built code waiting for a verdict")
    tasks.log(type="judged", id=task.id, verdict=verdict)
    return tasks.save(task, verdict=verdict, status=verdict or "built")


def fingerprint() -> list[tuple[str, int, int]]:
    """Every file the snapshot is read from, by when it was last written and how long it is.

    A stat per file rather than a snapshot per tick: the snapshot runs the gates, and those shell
    out. What this misses is only what no file records -- memory or power drifting across a gate --
    and that waits for the next change, or R.
    """
    home = tasks.home()
    paths = [
        *(home / "tasks").glob("*/*.md"),
        *(home / "var" / "runs").glob("*/rounds.json"),
        *(home / "var").glob("*.pid"),
        *(home / "var" / name for name in ("events.jsonl", "model", "reasoning")),
        settings.path(),
    ]
    found = []
    for path in sorted(paths):
        try:
            stat = path.stat()
        except FileNotFoundError:  # moved between the glob and the stat: the next tick sees where it went
            continue
        found.append((str(path), stat.st_mtime_ns, stat.st_size))
    return found


def stream(write, every: float = 1.0, ping: float = 15.0) -> None:
    """Say "changed" each time the fingerprint moves, until the page goes away.

    The page's own fetch holds this open, token header and all -- an `EventSource` cannot send a
    header, and the token is the one thing that keeps other sites out. The ping is how a closed tab
    is noticed: writing to it is what fails.
    """
    seen, quiet = fingerprint(), 0.0
    while True:
        time.sleep(every)
        if (now := fingerprint()) != seen:
            seen, quiet = now, 0.0
            write(b"data: changed\n\n")
        elif (quiet := quiet + every) >= ping:
            quiet = 0.0
            write(b": ping\n\n")


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
        elif self.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                stream(lambda chunk: (self.wfile.write(chunk), self.wfile.flush()))
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path.startswith("/api/run"):
            query = parse_qs(urlparse(self.path).query)
            self._json(200, progress(query.get("id", [""])[0], query.get("of", [""])[0]))
        elif self.path.startswith("/api/shot"):
            query = parse_qs(urlparse(self.path).query)
            if (shot := screenshot(query.get("id", [""])[0], query.get("name", [""])[0])) is None:
                self._json(404, {"error": "no such screenshot"})
            else:
                self._send(200, shot, "image/png")
        elif self.path.startswith("/api/diff"):
            query = parse_qs(urlparse(self.path).query)
            id, n = query.get("id", [""])[0], query.get("round", [""])[0]
            found = rounds.diff(id, int(n)) if tasks.ID_RE.fullmatch(id) and n.isdigit() else None
            self._json(*((200, found) if found else (404, {"error": "no such round"})))
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
        posts = (
            "/api/move",
            "/api/new",
            "/api/pr",
            "/api/edit",
            "/api/reorder",
            "/api/archive",
            "/api/pause",
            "/api/retry",
            "/api/verdict",
            "/api/comment",
            "/api/fix",
        )
        if self.path not in posts:
            self._json(404, {"error": "not found"})
            return
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path in ("/api/comment", "/api/fix"):
            try:
                if self.path == "/api/fix":
                    task = fix(payload["id"])
                    self._json(200, {"id": task.id, "stage": task.stage, "status": task.status})
                elif (task := tasks.find(payload["id"])).stage != "done":
                    self._json(400, {"error": "comments go on a done card's latest round"})
                else:
                    self._json(200, {"pending": rounds.comment(task.id, payload)["pending"]})
            except (FileNotFoundError, KeyError, ValueError, tasks.TransitionError) as e:
                self._json(400, {"error": str(e).strip("'")})
            return
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
        if self.path in ("/api/edit", "/api/pause", "/api/retry", "/api/verdict", "/api/reorder"):
            try:
                if self.path == "/api/edit":
                    task = edit(payload)
                elif self.path == "/api/pause":
                    task = pause(payload["id"], bool(payload.get("paused")))
                elif self.path == "/api/retry":
                    task = retry(payload["id"])
                elif self.path == "/api/reorder":
                    task = reorder(payload)
                else:
                    task = judge(payload["id"], payload.get("verdict") or None)
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
    # Threaded, because every open page holds `/api/stream` for as long as it is open.
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
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
