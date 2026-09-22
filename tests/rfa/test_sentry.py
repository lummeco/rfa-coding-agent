"""Sentry issues onto the board: real files, and a real HTTP server standing in for Sentry."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from rfa import sentry, tasks, up
from rfa.daemon import Daemon, DaemonConfig

ISSUES = [
    {
        "id": "101",
        "shortId": "WEB-1A",
        "title": "TypeError: Cannot read properties of undefined",
        "culprit": "InvoiceEditor(src/invoice.tsx)",
        "permalink": "https://acme.sentry.io/issues/101/",
        "level": "error",
        "count": "42",
        "userCount": 7,
        "firstSeen": "2026-09-20T06:07:10Z",
        "lastSeen": "2026-09-22T08:00:00Z",
    },
    {
        "id": "102",
        "shortId": "WEB-1B",
        "title": "ZeroDivisionError: division by zero",
        "culprit": "pricing.total",
        "permalink": "https://acme.sentry.io/issues/102/",
        "level": "warning",
        "count": "1",
        "userCount": 1,
        "firstSeen": "2026-09-22T07:00:00Z",
        "lastSeen": "2026-09-22T07:00:00Z",
    },
]
FRAMES = [
    {"filename": "node_modules/react/index.js", "lineNo": 1, "function": "render", "inApp": False},
    {"filename": "src/invoice.tsx", "lineNo": 42, "function": "InvoiceEditor", "inApp": True},
]
EVENTS = {
    "101": {
        "entries": [
            {"type": "breadcrumbs", "data": {}},
            {"type": "exception", "data": {"values": [{"stacktrace": {"frames": FRAMES}}]}},
        ]
    },
    "102": {"entries": [{"type": "message", "data": {"formatted": "boom"}}]},  # a message, no exception
}


class FakeSentry(BaseHTTPRequestHandler):
    """The two endpoints a poll uses, and a record of what was asked, so the bearer and the query can be checked."""

    asked: list[tuple[str, str]] = []

    def do_GET(self):
        FakeSentry.asked.append((self.path, self.headers.get("Authorization", "")))
        if self.path.startswith("/api/0/projects/acme/web/issues/"):
            body = ISSUES
        elif self.path.startswith("/api/0/organizations/acme/issues/") and self.path.endswith("/events/latest/"):
            body = EVENTS[self.path.split("/")[-4]]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        """Quiet: the test output is not an access log."""


@pytest.fixture
def server():
    FakeSentry.asked = []
    httpd = HTTPServer(("127.0.0.1", 0), FakeSentry)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch, server):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    (tmp_path / "rfa.yaml").write_text(
        yaml.safe_dump(
            {
                "repos": {"lummeco/web": "~/dev/web@main"},
                "sentry": {
                    "org": "acme",
                    "url": server,
                    "projects": {"web": "lummeco/web"},
                    "keychain_service": "rfa-sentry-token-that-does-not-exist",
                },
            }
        )
    )
    return tmp_path


def test_every_new_issue_becomes_one_draft_and_only_once():
    """The whole look: the bearer goes out with the query, each issue comes back as a draft naming
    the repository its project maps to, carrying the in-app frames the planner could not fetch
    itself. A second look, even after a card has moved on, adds nothing: one card per issue, ever."""
    config = sentry.SentryConfig.load()
    drafted = sentry.pull(config, "t0k3n")
    assert [(t.stage, t.meta["sentry"], t.meta["link"]) for t in drafted] == [
        ("draft", "101", "https://acme.sentry.io/issues/101/"),
        ("draft", "102", "https://acme.sentry.io/issues/102/"),
    ]
    assert drafted[0].meta["repos"] == ["lummeco/web"]
    assert drafted[0].title == "Fix the Sentry issue WEB-1A: TypeError: Cannot read properties of undefined"
    assert "src/invoice.tsx:42 in InvoiceEditor" in drafted[0].body and "node_modules" not in drafted[0].body
    assert "https://acme.sentry.io/issues/101/" in drafted[0].body and "seen 42 times by 7 users" in drafted[0].body
    assert "```" not in drafted[1].body  # nothing to show for an event without an exception
    assert (
        all(auth == "Bearer t0k3n" for _, auth in FakeSentry.asked)
        and "query=is%3Aunresolved&statsPeriod=7d" in FakeSentry.asked[0][0]
    )
    tasks.move(drafted[0], "planning")
    assert sentry.pull(config, "t0k3n") == [] and len(tasks.tasks()) == 2


def test_two_polls_at_once_cannot_both_make_a_card():
    """The daemon's tick and `rfa sentry` by hand, in the same second, both saw an empty board. The
    id comes from the issue, not the clock, so the second `create` hits the first card and refuses."""
    assert [t.id for t in sentry.pull(sentry.SentryConfig.load(), "t0k3n")] == [
        "20260920-060710-fix-the-sentry-issue-web-1a-typeerror-cannot-read-",
        "20260922-070000-fix-the-sentry-issue-web-1b-zerodivisionerror-divi",
    ]
    with pytest.raises(FileExistsError, match="already in draft"):
        sentry.capture(sentry.SentryConfig.load(), "t0k3n", "web", ISSUES[0])
    tasks.move(tasks.tasks("draft")[0], "todo")
    with pytest.raises(FileExistsError, match="already in todo"):
        sentry.capture(sentry.SentryConfig.load(), "t0k3n", "web", ISSUES[0])
    assert len(tasks.tasks()) == 2


def test_what_sentry_refuses_is_an_error_with_the_status_in_it():
    with pytest.raises(sentry.SentryError, match="404"):
        sentry.api(sentry.SentryConfig.load(), "t0k3n", "/nowhere/")


def test_the_daemon_asks_once_per_interval_and_outlives_a_look_that_failed():
    """No token in the Keychain: the daemon says so, goes on to the board as usual, and does not ask
    again until the interval has passed -- an expired token every fifteen seconds helps nobody."""
    daemon = Daemon(config=DaemonConfig())
    daemon.tick()
    assert [e["type"] for e in tasks.events()] == ["sentry_error"]
    assert "rfa-sentry-token-that-does-not-exist" in tasks.events()[0]["error"]
    assert daemon.said == "nothing to do" and daemon.sentry_due > 0
    daemon.tick()
    assert len(tasks.events()) == 1


def test_without_a_sentry_block_the_daemon_never_asks(workspace):
    (workspace / "rfa.yaml").write_text("repos: {}\n")
    daemon = Daemon(config=DaemonConfig())
    daemon.tick()
    assert tasks.events() == [] and daemon.sentry_due == 0


@pytest.mark.parametrize(
    ("config", "ok", "said"),
    [
        ({}, True, "not configured"),
        ({"repos": {}, "sentry": {"org": "acme", "projects": {"web": "lummeco/web"}}}, False, "lummeco/web"),
        (
            {
                "repos": {"lummeco/web": "~/dev/web"},
                "sentry": {
                    "org": "acme",
                    "projects": {"web": "lummeco/web"},
                    "keychain_service": "rfa-sentry-token-that-does-not-exist",
                },
            },
            False,
            "security add-generic-password -s rfa-sentry-token-that-does-not-exist -a rfa -w",
        ),
    ],
)
def test_rfa_up_checks_the_mapping_and_the_token_before_the_daemon_finds_out(config, ok, said):
    """A project mapped to a repository nobody listed, or no token at all, is refused with the fix
    now, rather than found out from a line in the daemon's log every half hour."""
    assert (up.sentry(config).ok, said in up.sentry(config).detail) == (ok, True)
