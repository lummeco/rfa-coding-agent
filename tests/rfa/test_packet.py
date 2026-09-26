import pytest

from rfa.packet import Packet, PacketFile, Verification, parse, problems

REPO_FILES = {"invoicing": {"src/editor/InvoiceEditor.tsx", "src/editor/LineRow.tsx", "tests/editor.test.ts"}}


def packet(**overrides) -> Packet:
    return Packet(
        **{
            "title": "Add duplicate invoice line functionality.",
            "goal": "Allow a user to duplicate an existing invoice line.",
            "current_behavior": "Lines can be added, edited and deleted, but not duplicated.",
            "required_behavior": ["Add a duplicate action.", "Copy:\n  - product\n  - VAT"],
            "acceptance_criteria": ["Clicking Duplicate creates exactly one new line."],
            "complexity": 2,
            "complexity_reason": "A normal feature following an existing pattern.",
            **overrides,
        }
    )


def test_render_omits_empty_sections_and_keeps_nested_bullets():
    body = packet().render()
    assert "## Constraints" not in body and "## Relevant areas" not in body and "## Verification" not in body
    assert "- Copy:\n  - product\n  - VAT" in body
    assert body.endswith("new line.\n") and "\n\n\n" not in body


def test_render_marks_areas_as_hints_and_never_leaks_the_grounded_paths():
    """`files` is the host's: it validates and scopes with it, but the coder must inspect the repo itself."""
    body = packet(
        areas=["invoice editor"], files=[PacketFile(path="invoicing/src/editor/LineRow.tsx", why="the row")]
    ).render()
    assert "## Relevant areas\nLikely relevant:\n- invoice editor" in body
    assert "These are hints. Inspect the repository before deciding what needs changing." in body
    assert "LineRow.tsx" not in body


@pytest.mark.parametrize(
    ("verification", "expected"),
    [
        (Verification(manual=["Run the tests."]), "## Verification\nRun the tests.\n"),
        (Verification(manual=["Open it.", "Click it."]), "## Verification\n1. Open it.\n2. Click it.\n"),
        (Verification(commands=["pnpm test"]), "## Verification\n```bash\npnpm test\n```\n"),
        (
            Verification(commands=["pnpm test"], manual=["Open it."]),
            "## Verification\n```bash\npnpm test\n```\nManual:\n1. Open it.\n",
        ),
    ],
)
def test_render_numbers_manual_steps_only_when_they_are_a_sequence(verification, expected):
    assert expected in packet(verification=verification).render()


def test_parse_reads_a_rendered_packet_back_into_its_fields():
    p = packet(
        constraints=["Keep it local."],
        areas=["invoice editor"],
        verification=Verification(commands=["pnpm test"], manual=["Open it.", "Click it."]),
        non_goals=["Bulk duplication."],
    )
    fields = parse("\n" + p.render())
    assert fields["title"] == p.title
    assert fields["goal"] == p.goal
    assert fields["current_behavior"] == p.current_behavior
    assert fields["required_behavior"] == p.required_behavior
    assert fields["constraints"] == p.constraints
    assert fields["areas"] == p.areas
    assert fields["acceptance_criteria"] == p.acceptance_criteria
    assert fields["verification"] == {"commands": ["pnpm test"], "manual": ["Open it.", "Click it."]}
    assert fields["non_goals"] == p.non_goals


def test_parse_keeps_nested_bullets_with_their_item():
    fields = parse(packet().render())
    assert fields["required_behavior"] == ["Add a duplicate action.", "Copy:\n  - product\n  - VAT"]


def test_parse_render_is_a_round_trip():
    p = packet(
        constraints=["Keep it local."],
        areas=["invoice editor"],
        verification=Verification(commands=["pnpm test"], manual=["Open it.", "Click it."]),
        non_goals=["Bulk duplication."],
    )
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


def test_problems_rejects_a_packet_written_without_reading_the_code():
    assert "at least 3 tool calls" in problems(packet(), REPO_FILES, tool_calls=0, min_tool_calls=3)[0]


@pytest.mark.parametrize(
    ("command", "rejected"),
    [
        ("docker compose -f docker-compose.dev.yml exec app pytest tests/x", True),
        ("docker-compose exec app ruff check src", True),
        ("cd app && pytest tests/x", False),
        ("pytest tests/test_dockerfile.py", False),
    ],
)
def test_problems_rejects_checks_that_need_docker(command, rejected):
    found = problems(packet(verification=Verification(commands=[command])), REPO_FILES, tool_calls=5, min_tool_calls=3)
    assert bool(found) == rejected and all("cd app && pytest tests/x" in f for f in found)
