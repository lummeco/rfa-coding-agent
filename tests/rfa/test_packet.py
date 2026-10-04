import pytest

from rfa.packet import Packet, PacketFile, add_decision, parse, problems

REPO_FILES = {"invoicing": {"src/editor/InvoiceEditor.tsx", "src/editor/LineRow.tsx", "tests/editor.test.ts"}}


def packet(**overrides) -> Packet:
    return Packet(
        **{
            "title": "Add duplicate invoice line functionality.",
            "goal": "Allow a user to duplicate an existing invoice line.",
            "current_behavior": "Lines can be added, edited and deleted, but not duplicated.",
            "acceptance_criteria": [
                "Clicking Duplicate creates exactly one new line.",
                "It copies:\n  - product\n  - VAT",
            ],
            "complexity": 2,
            "complexity_reason": "A normal feature following an existing pattern.",
            **overrides,
        }
    )


def test_render_omits_empty_sections_and_never_leaks_the_grounded_paths():
    """`files` is the host's: it validates and scopes with it, but the coder must inspect the repo itself."""
    body = packet(files=[PacketFile(path="invoicing/src/editor/LineRow.tsx", why="the row")]).render()
    assert "## Decisions" not in body and "LineRow.tsx" not in body
    assert "2. It copies:\n  - product\n  - VAT" in body
    assert body.endswith("  - VAT\n") and "\n\n\n" not in body


DECISIONS = ["Copies are made server-side -- one code path, over a client-side copy.", "Only saved invoices."]


def test_parse_reads_a_rendered_packet_back_into_its_fields():
    p = packet(decisions=DECISIONS)
    fields = parse("\n" + p.render())
    assert fields["title"] == p.title
    assert fields["goal"] == p.goal
    assert fields["current_behavior"] == p.current_behavior
    assert fields["decisions"] == p.decisions
    assert fields["acceptance_criteria"] == p.acceptance_criteria


def test_parse_reads_an_older_packets_constraints_and_non_goals_as_decisions():
    """Cards planned before decisions existed must not lose those sections the first time they are edited."""
    body = packet().render() + "\n## Constraints\n- Keep it local.\n\n## Non-goals\n- Bulk duplication.\n"
    assert parse(body)["decisions"] == ["Keep it local.", "Not in scope: Bulk duplication."]


def test_parse_keeps_nested_bullets_with_their_item():
    fields = parse(packet().render())
    assert fields["acceptance_criteria"] == [
        "Clicking Duplicate creates exactly one new line.",
        "It copies:\n  - product\n  - VAT",
    ]


def test_parse_render_is_a_round_trip():
    p = packet(decisions=DECISIONS)
    again = Packet(**parse(p.render()), complexity=p.complexity, complexity_reason=p.complexity_reason)
    assert again.render() == p.render()


@pytest.mark.parametrize("body", ["\n# Idea\n\nSome idea, not a packet.\n", ""])
def test_parse_refuses_a_body_that_is_not_a_packet(body):
    with pytest.raises(ValueError, match="not a rendered packet"):
        parse(body)


@pytest.mark.parametrize(
    ("path", "complaint"),
    [
        ("invoicing/src/editor/LineRow.tsx", None),
        ("invoicing/src/editor/NewFile.tsx", None),  # new file in a folder that exists
        ("work/repos/invoicing/src/editor/LineRow.tsx", None),  # container path is normalised away
        ("`invoicing/src/editor/LineRow.tsx`", None),  # the model likes backticks
        ("invoicing/src/nowhere/Thing.tsx", "does not exist"),
        ("billing/src/editor/LineRow.tsx", "must start with a repository name"),
        ("invoicing/.github/workflows/ci.yml", "never land it"),
    ],
)
def test_problems_grounds_every_path_against_the_repository(path, complaint):
    found = problems(packet(files=[PacketFile(path=path, why="w")]), REPO_FILES, tool_calls=5, min_tool_calls=3)
    assert (found[0] if found else None) == complaint if complaint is None else complaint in found[0]


def test_problems_caps_decisions_and_acceptance_criteria():
    """The owner reads every packet; the host sends a sprawling one back rather than let it through."""
    assert problems(packet(decisions=["d"] * 10, acceptance_criteria=["c"] * 5), REPO_FILES, 5, 3) == []
    found = problems(packet(decisions=["d"] * 11, acceptance_criteria=["c"] * 6), REPO_FILES, 5, 3)
    assert len(found) == 2 and "11 decisions" in found[0] and "6 acceptance criteria" in found[1]


def test_problems_rejects_a_packet_written_without_reading_the_code():
    assert "at least 3 tool calls" in problems(packet(), REPO_FILES, tool_calls=0, min_tool_calls=3)[0]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "# Task\nT\n\n## Decisions\n- Keep it local.\n  - nested\n\n## Acceptance criteria\n1. Works.\n",
            "## Decisions\n- Keep it local.\n  - nested\n- Ask first.\n\n## Acceptance criteria\n1. Works.\n",
        ),
        (
            "# Task\nT\n\n## Goal\nG\n\n## Acceptance criteria\n1. Works.\n",
            "## Goal\nG\n\n## Decisions\n- Ask first.\n\n## Acceptance criteria\n1. Works.\n",
        ),
        ("\n# Idea\n\nJust an idea\n", "# Idea\n\nJust an idea\n\n## Decisions\n- Ask first.\n"),
        ("# Task\nT\n\n## Decisions\n- Keep it local.\n", "## Decisions\n- Keep it local.\n- Ask first.\n"),
    ],
)
def test_an_answer_joins_the_decisions_wherever_the_body_keeps_them(body, expected):
    assert add_decision(body, " Ask first. ").endswith(expected)
