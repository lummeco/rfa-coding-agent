"""The board: one static page over the task folders, and three endpoints to read and move them.

There is no state here. Every request reads the folders, so anything you do with `mv`, an editor or
the CLI shows up on the next refresh, and the board going down loses nothing.
"""

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from rfa import settings, tasks

PAGE = Path(__file__).parent / "board.html"


def snapshot() -> dict:
    """Everything the page draws, in one request."""
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
                "created": task.meta.get("created"),
                "error": task.meta.get("error"),
                "branches": task.meta.get("branches") or {},
                "open_questions": task.meta.get("open_questions") or [],
                "body": task.body,
            }
            for task in tasks.tasks()
        ],
        "events": tasks.events()[-500:],
        "repos": sorted(settings.load().get("repos") or {}),
    }


class Handler(BaseHTTPRequestHandler):
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
        if self.path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/tasks"):
            self._json(200, snapshot())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in ("/api/move", "/api/new"):
            self._json(404, {"error": "not found"})
            return
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/new":
            if not (idea := str(payload.get("idea", "")).strip()):
                self._json(400, {"error": "an idea needs some words"})
                return
            task = tasks.create(idea, list(payload.get("repos") or []))
            self._json(200, {"id": task.id, "stage": task.stage, "status": task.status})
            return
        try:
            task = tasks.move(tasks.find(payload["id"]), payload["to"], actor="human", **payload.get("meta", {}))
        except (FileNotFoundError, KeyError, tasks.TransitionError, FileExistsError) as e:
            self._json(400, {"error": f"{type(e).__name__}: {e}"})
            return
        self._json(200, {"id": task.id, "stage": task.stage, "status": task.status})

    def log_message(self, *args) -> None:
        """The board is a local page, not a service: its access log is noise."""


def serve(host: str = "127.0.0.1", port: int = 4380, open_browser: bool = True) -> None:
    server = HTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"Board on {url}  (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
