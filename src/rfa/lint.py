"""Per-problem results out of a linter's JSON report: ruff's `--output-format json`.

A problem is its file, rule and message, never its line: an edit above a problem moves it, and a
problem that moved is not a new one. Counted, so a second copy of an old problem is still new.
"""

import json
import re
from collections import Counter


def parse(output: str) -> Counter | None:
    """Every problem in the report, or None when the output holds no report to read.

    The JSON is looked for at the start of a line: stderr shares the output, and a warning ruff
    prints before the report is not part of it.
    """
    for start in re.finditer(r"(?m)^\[", output):
        try:
            found, _ = json.JSONDecoder().raw_decode(output, start.start())
        except json.JSONDecodeError:
            continue
        if isinstance(found, list):
            return Counter((p.get("filename", ""), p.get("code") or "", p.get("message", "")) for p in found)
    return None


def new(problems: Counter, baseline: Counter) -> list[tuple[str, str, str]]:
    """Problems this run is answerable for: there now, and not there before the coder started."""
    return sorted((problems - baseline).elements())
