import re
from pathlib import Path

import pytest

from lib import cli
from lib.approver_commands import parse_slash_go
from lib.hold import _SLASH_OK
from lib.orchestrate import RECOMMENDED_NEXT
from lib.pstack_pin import load_pin

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "grok" / "orchestrate-project" / "SKILL.md"
PLAYBOOK = "skills/poteto-mode/playbooks/orchestrate.md"
MAX_LINES = 181
MAX_BYTES = 19305

STALE = (
    r"second[^.|]*(unmet|acceptance-criterion)",
    r"once T02f[^.]*lands",
    r"Max decides whether to retry",
    r"[Aa]sk Max[^.|]*round",
)

PSTACK_QUOTES = (
    "Retry by cause: a smaller scope after a context or memory limit, the same kickoff after "
    "a dropped connection, and a different model after a tool error. After two retries, drop "
    "the unit and replan around it.",
    "Ask the user only for a real decision or a blocker",
    "Keep only the fixes that block the program, and park the rest as follow-ups.",
    "Each kickoff names the evidence that proves the work is done",
)

STALLED = (
    "narrower instruction", "challenge to the premise", "split into smaller tickets",
    "two retries", "replan", "on the PR", "`for=factory`",
)

MAX_DECIDES = ("cannot be undone", "product call", "survived a replan", "hold `ok`", "`for=max`")

TRIAGE = (
    "`followup_issue`", "`factory:ready`", "Promote", "Split", "Close",
    "one-line reason", "a bug a user would hit", "blocks planned work",
)


def collapse(text: str) -> str:
    return " ".join(text.split())


def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def section(name: str) -> str:
    _, found, rest = skill_text().partition(f"\n## {name}\n")
    return rest.split("\n## ", 1)[0] if found else ""


def cli_commands() -> set[str]:
    source = (ROOT / "lib" / "cli.py").read_text(encoding="utf-8")
    return set(re.findall(r'argv\[0\] == "([a-z][a-z-]*)"', source))


@pytest.mark.parametrize("pattern", STALE)
def test_no_stale_rule(pattern: str) -> None:
    assert re.search(pattern, collapse(skill_text())) is None, pattern


def test_every_named_orch_command_exists() -> None:
    named = set(re.findall(r"`orch ([a-z][a-z-]*)", skill_text()))
    assert "retry" in named
    assert named <= cli_commands(), sorted(named - cli_commands())
    assert cli.main(["retry", "--help"]) == 0


def test_commands_section_names_max_and_grok_commands() -> None:
    commands = section("Commands")
    for phrase in ("`/go`", "`/ok <sha>`", "`orch retry", "Auto extra round", "`orch-engine` notice"):
        assert phrase in commands, phrase
    go = parse_slash_go("/go\nFactory: narrower instruction")
    assert go is not None and go.instruction.startswith("Factory:")
    assert _SLASH_OK.fullmatch("/ok " + "a" * 40)


@pytest.mark.parametrize("quote", PSTACK_QUOTES)
def test_pstack_quote_matches_the_pinned_playbook(quote: str) -> None:
    assert PLAYBOOK in load_pin(ROOT).hashes()
    playbook = (ROOT / "skills" / "pstack" / PLAYBOOK).read_text(encoding="utf-8")
    assert quote in collapse(playbook.replace("**", ""))
    assert quote in collapse(skill_text())


def test_stalled_ticket_retries_by_cause_then_replans() -> None:
    stalled = collapse(section("Stalled ticket")).casefold()
    for phrase in STALLED:
        assert phrase.casefold() in stalled, phrase


def sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", collapse(text))


def test_stalled_ticket_never_replans_with_a_go() -> None:
    offers = [
        s for s in sentences(section("Stalled ticket"))
        if re.search(r"plan go|job=plan|state=planning", s) and "Never" not in s
    ]
    assert offers == []
    assert "file smaller intake tickets" in section("Stalled ticket")


def test_max_decides_only_five_things() -> None:
    body = section("When Max decides")
    bullets = [line for line in body.splitlines() if line.startswith("- ")]
    assert len(bullets) == len(MAX_DECIDES), bullets
    for bullet, phrase in zip(bullets, MAX_DECIDES):
        assert phrase in bullet, (phrase, bullet)
    assert "only when it quotes his own words for that PR and head" in collapse(body)


def test_follow_up_triage() -> None:
    triage = collapse(section("Follow-up triage"))
    for phrase in TRIAGE:
        assert phrase in triage, phrase


def test_promotion_never_labels_the_parked_issue() -> None:
    triage = section("Follow-up triage")
    labels_parked = [
        s for s in sentences(triage)
        if "intake label" in s and "parked issue" in s and "Never label" not in s
    ]
    assert labels_parked == []
    assert "Never label the parked issue" in triage
    assert "one intake-template issue per item, from that item alone" in collapse(triage)


def test_untrusted_data_rule_covers_every_source() -> None:
    assert any(
        "untrusted data" in s and all(word in s for word in ("ticket", "question", "verdict"))
        for s in sentences(skill_text())
    )


def test_brief_sizes_the_work() -> None:
    brief = collapse(section("Brief"))
    assert "one line per criterion" in brief
    assert "150 to 400 changed lines" in brief


@pytest.mark.parametrize("state", ("stuck", "capped"))
def test_recommended_next_does_not_wait_on_max(state: str) -> None:
    for phrase in ("Max decides", "Max's own words"):
        assert phrase not in RECOMMENDED_NEXT[state], (state, phrase)


def test_skill_stays_small() -> None:
    assert skill_text().count("\n") <= MAX_LINES
    assert len(SKILL.read_bytes()) <= MAX_BYTES
