from collections import Counter

import pytest

from rfa import lint

OLD = '{"filename": "/r/a.py", "code": "F401", "message": "`os` imported but unused", "location": {"row": %d}}'


def test_a_problem_that_only_moved_is_not_new_but_a_second_copy_of_it_is():
    before = lint.parse(f"[{OLD % 3}]")
    assert lint.new(lint.parse(f"[{OLD % 9}]"), before) == []
    assert lint.new(lint.parse(f"[{OLD % 9}, {OLD % 12}]"), before) == [("/r/a.py", "F401", "`os` imported but unused")]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("warning: `ruff` config has [deprecated] keys\n[]\n", Counter()),  # stderr before the report
        (
            '[\n  {"filename": "/r/b.md", "code": null, "message": "Expected a statement"}\n]',
            Counter({("/r/b.md", "", "Expected a statement"): 1}),
        ),
        ("lib/main.dart:3:1 unused import", None),  # not a report: judged by exit code instead
        ("", None),
    ],
)
def test_parse_reads_ruffs_json_out_of_whatever_else_the_command_printed(output, expected):
    assert lint.parse(output) == expected
