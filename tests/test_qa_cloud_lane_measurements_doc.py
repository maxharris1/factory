from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "plans" / "qa-cloud-lane-measurements.md"

FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.M | re.S)
HEADING = re.compile(r"^(#{1,6}) (.+)$", re.M)


def sections() -> dict[str, str]:
    text = DOC.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    path: list[str] = []
    marks = list(HEADING.finditer(text))
    for mark, following in zip(marks, [*marks[1:], None]):
        depth = len(mark.group(1))
        path = [*path[: depth - 1], mark.group(2).strip()]
        end = following.start() if following else len(text)
        found[" / ".join(path)] = text[mark.end():end]
    return found


def fences(body: str) -> list[tuple[str, str, int]]:
    return [(m.group(1), m.group(2), m.start()) for m in FENCE.finditer(body)]


def under(heading: str) -> dict[str, str]:
    return {name: body for name, body in sections().items() if heading in name.split(" / ")}


def test_every_reported_exit_code_follows_the_command_that_produced_it() -> None:
    broken = []
    for name, body in sections().items():
        exit_line = re.search(r"^- Exit code: `(\d+)`.*$", body, re.M)
        if not exit_line:
            continue
        commands = [code for lang, code, start in fences(body) if lang == "bash" and start < exit_line.start()]
        if not commands:
            broken.append((name, "no bash block before the exit code"))
        elif exit_line.group(1) == "124" and not any("timeout" in code for code in commands):
            broken.append((name, "exit 124 reported without the timeout that produced it"))
    assert broken == []


def test_the_inventory_invocation_is_given_with_its_prompt() -> None:
    commands = [code for body in under("Connectors").values() for lang, code, _ in fences(body) if lang == "bash"]
    inventory = [code for code in commands if "--cloud session_" in code and "-p \"" in code]
    assert inventory, "the Connectors section shows the claude -p ... --cloud <session-id> inventory command"
    assert all(re.search(r'-p "[^"]{20,}"', code) for code in inventory), "the prompt is in the command"


REST_CALLS = {
    "named-environment launch": lambda code: "-X POST" in code and '"env"' in code,
    "explicit-repo launch": lambda code: "-X POST" in code and '"repos"' in code,
    "status read": lambda code: "-X POST" not in code and re.search(r"/v1/agents/[^/\s'\"]+['\"]?\s*$", code.strip()),
    "cancel": lambda code: "-X POST" in code and "/cancel" in code,
}


@pytest.mark.parametrize("call", REST_CALLS)
def test_each_cursor_rest_call_has_its_command_and_what_came_back(call: str) -> None:
    bodies = under("Scripted launch: Cursor")
    assert not any("still owed" in body.lower() for body in bodies.values())
    matching = [
        body for body in bodies.values()
        if any(lang == "bash" and "api.cursor.com/v1/agents" in code and REST_CALLS[call](code) for lang, code, _ in fences(body))
    ]
    assert matching, f"no bash block with the {call} curl command"
    assert any(re.search(r"HTTP `?\d{3}", body) for body in matching), f"no HTTP status recorded for the {call}"


def test_connector_free_launch_is_observed_or_marked_unverified() -> None:
    [body] = [body for name, body in sections().items() if name.endswith("/ Account connectors")]
    observed = any("ListConnectors" in code for _, code, _ in fences(body))
    assert observed or "unverified" in body, "a ListConnectors result from a fresh session, or an unverified label with the failing step"
