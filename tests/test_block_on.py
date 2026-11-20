from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import review as review_brief
from lib.factory_md import parse_factory_md
from lib.followups import followup_start
from tests import regenerate_brief_snapshots as snapshots
from tests.test_dispatcher import handled_outcome
from tests.test_followup_findings import (
    FOLLOWUP_A,
    MINOR,
    VERDICT_ID,
    FollowupActions,
    at_round,
    factory_text,
    filings,
    verdict_ticket,
)

ROOT = Path(__file__).resolve().parents[1]
AUTOPILOT_FULL = ROOT / "skills/pstack/skills/poteto-mode/playbooks/autopilot-full.md"
BAD_VALUE = "review.block_on must be any-failure or user-impact"


def block_on_text(value: object, **top: object) -> str:
    text = factory_text(**top)
    return text.replace("review:\n", f"review:\n  block_on: {value}\n", 1)


def test_block_on_defaults_to_any_failure() -> None:
    config, findings = parse_factory_md(factory_text())
    assert findings == []
    assert config is not None and config.block_on == "any-failure"


@pytest.mark.parametrize("value", ["any-failure", "user-impact"])
def test_block_on_parses_both_values(value: str) -> None:
    config, findings = parse_factory_md(block_on_text(value))
    assert findings == []
    assert config is not None and config.block_on == value


@pytest.mark.parametrize("value", ["user", "''", "true", "1", "null", "[user-impact]"])
def test_block_on_refuses_anything_else(value: str) -> None:
    config, findings = parse_factory_md(block_on_text(value))
    assert config is None
    assert [(f.severity, f.message) for f in findings] == [("error", BAD_VALUE)]

    lenient, lenient_findings = parse_factory_md(block_on_text(value), strict=False)
    assert lenient is not None and lenient.block_on == "any-failure"
    assert [(f.severity, f.message) for f in lenient_findings] == [("warning", BAD_VALUE)]


def test_user_impact_is_refused_with_linear_tickets() -> None:
    config, findings = parse_factory_md(block_on_text("user-impact", tickets="linear", ship="handoff"))
    assert config is None
    assert [(f.severity, f.message) for f in findings] == [
        ("error", "tickets is linear and review.block_on is user-impact")
    ]
    plain, plain_findings = parse_factory_md(block_on_text("any-failure", tickets="linear", ship="handoff"))
    assert plain is not None and plain_findings == []


def test_check_config_refuses_an_unknown_value(tmp_path: Path) -> None:
    path = tmp_path / "factory.md"
    path.write_text(block_on_text("strict"), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin/orch"), "check-config", "--factory", str(path)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode != 0
    assert BAD_VALUE in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("followup_from", "block_on", "start"),
    [
        (None, "any-failure", None),
        (4, "any-failure", 4),
        (None, "user-impact", 1),
        (4, "user-impact", 1),
    ],
)
def test_followup_start(followup_from: int | None, block_on: str, start: int | None) -> None:
    assert followup_start(followup_from, block_on) == start


def round_one_approve(tmp_path: Path, block_on: str, followup: int | None):
    actions = FollowupActions()
    dispatcher, queue = at_round(tmp_path, actions, 1)
    approve = verdict_ticket("APPROVE", 1, (FOLLOWUP_A, MINOR), followup=followup)
    dispatcher.process(replace(approve, factory=replace(approve.factory, block_on=block_on)))
    return actions, queue


def test_user_impact_files_a_round_one_follow_up(tmp_path: Path) -> None:
    actions, queue = round_one_approve(tmp_path, "user-impact", None)
    assert handled_outcome(queue, VERDICT_ID) == "merged"
    assert filings(queue) == [(VERDICT_ID, 1, "commented", 300)]
    [(title, body, labels)] = actions.issues.values()
    assert title == "Follow-ups from #42 r1"
    assert labels == ()
    assert "> 1. [follow-up] lib/a.py:10 breaks the path-format rule from round 2." in body


def test_any_failure_files_nothing_before_followup_from_round(tmp_path: Path) -> None:
    actions, queue = round_one_approve(tmp_path, "any-failure", 4)
    assert handled_outcome(queue, VERDICT_ID) == "merged"
    assert filings(queue) == []
    assert actions.issues == {}


@pytest.mark.parametrize("quote", [review_brief.PSTACK_BAR, review_brief.PSTACK_FIX_ROUND])
def test_the_brief_quotes_pstack_verbatim(quote: str) -> None:
    assert quote in AUTOPILOT_FULL.read_text(encoding="utf-8")


def box(kind: str) -> str:
    return snapshots.render_snapshot(kind).split("===== remote (")[0]


def test_round_one_states_the_bar_and_drops_minor() -> None:
    brief = box("review-user-impact")
    assert "So REVISE only for an unmet acceptance criterion, a failed gate" in brief
    assert "A problem the base already has does not block unless the PR copies it or newly reaches it." in brief
    assert "The tag is one of `[blocking]` or `[follow-up]` and" in brief
    assert "[minor]" not in brief
    assert "Run interrogate and blast-radius, and say which checks did not run." in brief
    assert "Mode: `full`." in brief


def test_round_two_checks_the_fix_only() -> None:
    brief = box("review-user-impact-round-2")
    assert "Mode: `fix-check`." in brief
    assert "(b) Rerun the gates on this head" in brief
    assert "(c) Read the fix delta for a bug the fix introduced." in brief
    assert "Do not run interrogate or blast-radius: the dispatcher picked `fix-check`." in brief
    assert "Close the earlier rounds first" not in brief
    assert "Run interrogate and blast-radius, and say which checks did not run." not in brief
    assert "[minor]" not in brief


UNCONDITIONAL_PASS = ("Run interrogate and blast-radius, and say",)
MERGE = {"last_reviewed_head": "a" * 40, "merge": "b" * 40, "commits": []}


@pytest.mark.parametrize("verify", ["none", "verify-cleffy"])
@pytest.mark.parametrize(
    "update",
    [None, MERGE, {**MERGE, "commits": ["c" * 40]}],
    ids=["fix-round", "merge-update", "merge-update-with-commits"],
)
def test_a_later_user_impact_round_never_orders_the_full_pass(update: dict | None, verify: str) -> None:
    facts = {
        "base": "main",
        "head": "d" * 40,
        "pr": 42,
        "verify": verify,
        "verify_unavailable": False,
        "round_number": 2,
        "followup_from": None,
        "block_on": "user-impact",
        "update": update,
        "rebase": None,
        "size_flag": None,
        "start_before_ci": False,
        "no_ci_base": None,
        "local_checks": (),
        "qa_gate": None,
        "qa_criteria": None,
    }
    lead = review_brief.lead(facts)
    assert lead.goal.startswith(f"Review PR #42 at {'d' * 40} ")
    text = "\n".join(lead.method)
    assert not [phrase for phrase in UNCONDITIONAL_PASS if phrase in text]
    assert "Do not run interrogate or blast-radius: the dispatcher picked `fix-check`." in text
    assert "blast-radius only when" not in text
