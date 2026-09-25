"""Per-test results out of a JUnit XML report, and where each test sits in a diff.

JUnit XML because every runner can write it -- pytest `--junitxml`, jest-junit, vitest, gotestsum,
cargo2junit -- so one parser covers every repository, and what the board shows is what the runner
said rather than what the coder claimed.
"""

import re
import xml.etree.ElementTree as ET

FAILED = ("failed", "error")
OUTCOMES = {"failure": "failed", "error": "error", "skipped": "skipped"}
RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}

DEFS = [
    re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\("),
    re.compile(r"""^\s*(?:it|test)(?:\.\w+)?\(\s*(['"`])(?P<name>.+?)\1"""),
    re.compile(r"^func\s+(Test\w+)\s*\("),
]
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse(xml: str) -> list[dict] | None:
    """Every test case in the report, or None when there is no report to read."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    cases = []
    for case in root.iter("testcase"):
        outcome, message = "passed", ""
        for child in case:
            if (found := OUTCOMES.get(child.tag)) and RANK[found] > RANK[outcome]:
                outcome, message = found, (child.get("message") or child.text or "").strip()[:2000]
        classname, name = case.get("classname") or "", case.get("name") or ""
        cases.append(
            {
                "id": f"{classname}::{name}",
                "classname": classname,
                "name": name,
                "file": case.get("file") or "",
                "outcome": outcome,
                "message": message,
            }
        )
    return cases


def broken(cases: list[dict], baseline: dict[str, str]) -> list[dict]:
    """Failing tests this run is answerable for: red now, and not red before the coder started."""
    return [c for c in cases if c["outcome"] in FAILED and baseline.get(c["id"]) not in FAILED]


def defined(line: str) -> str:
    """The test a line of code defines, or nothing."""
    for rx in DEFS:
        if m := rx.match(line):
            return m.group("name") if "name" in rx.groupindex else m.group(1)
    return ""


def belongs(case: dict, path: str) -> bool:
    """Did this case come from this file? By its `file` when the runner wrote one, else by module path."""
    if case["file"]:
        file = case["file"].removeprefix("./")
        return path.endswith(file) or file.endswith(path)
    module = re.sub(r"\.\w+$", "", path).replace("/", ".")
    parts = case["classname"].split(".")
    return any(module == p or module.endswith("." + p) for p in (".".join(parts[:i]) for i in range(len(parts), 0, -1)))


def named(case: dict, name: str) -> bool:
    """`test_x` is `test_x[param]` too, a Go test its subtests, and a jest `it` is the end of its full name."""
    n = case["name"]
    return n == name or n.startswith((f"{name}[", f"{name}/")) or n.endswith(f" {name}")


def outcome(cases: list[dict]) -> str:
    if not cases:
        return "missing"
    if any(c["outcome"] in FAILED for c in cases):
        return "failed"
    return "skipped" if all(c["outcome"] == "skipped" for c in cases) else "passed"


def marks(patch: str, cases: list[dict]) -> dict[str, dict[int, dict]]:
    """Each test a diff defines or shows, by file and new-side line, with how it did.

    A test the diff shows that is not in the report at all is `missing`: it never ran, which is the
    one result that looks the same as a pass unless something says otherwise. When the report can
    place tests in files, a file with any of its tests in the report is matched by file; one with
    none falls back to matching by name alone.
    """
    found: dict[str, dict[int, dict]] = {}
    path, line = "", 0
    for text in patch.splitlines():
        if text.startswith("+++ "):
            path = text[4:].removeprefix("b/") if text[4:] != "/dev/null" else ""
        elif m := HUNK.match(text):
            line = int(m.group(1))
        elif text.startswith(("+", " ")) and path:
            if TEST_FILE.search(path) and (name := defined(text[1:])):
                mine = [c for c in cases if belongs(c, path)]
                hits = [c for c in (mine or cases) if named(c, name)]
                result = outcome(hits)
                found.setdefault(path, {})[line] = {
                    "name": name,
                    "outcome": result,
                    "message": next((c["message"] for c in hits if c["outcome"] in FAILED), ""),
                    "before": next((c.get("before", "") for c in hits if c.get("before")), ""),
                }
            line += 1
    return found
