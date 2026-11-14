"""#172 (T02e): Factory may post a capped-ticket go as maxharris1, short-term,
only quoting Max's exact words, and never an ok.

The go is posted through lib.orchestrate.approver_go_post and driven through
the real Dispatcher; a hold ok is Max's alone and still clears a hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import lib.orchestrate as orchestrate
from lib.handoff import parse_comment
from lib.orchestrate import OrchestrateError, approver_go_post
from tests.test_approver_credential import (
    APP_BOT,
    FAKE_TOKENS,
    MAX,
    AppFlow,
    approvals_for,
    hold_dispatcher,
    ok_from,
)
from tests.test_dispatcher import handled_outcome
from tests.test_ship_hold import HEAD, approve, hold_factory, ready, snapshot, workflow_state

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "grok" / "orchestrate-project" / "SKILL.md"
WORDS = "Yes, give #42 one more round.\nFix the flaky test first."


def post(kind: str, words: str, tmp_path: Path, *, enabled: bool = True):
    profile = tmp_path / "grok" / "gh-approver"
    switch = approvals_for(tmp_path, enabled=enabled, profile=str(profile))
    return approver_go_post(
        kind,
        words,
        {"PATH": "/usr/bin", "GH_CONFIG_DIR": "/home/grok/.config/gh-app", **FAKE_TOKENS},
        app_trust=switch.app_trust,
        gh_config_dir=switch.gh_config_dir,
    )


# --- AC 1: a go posts through the path ----------------------------------------------


def test_a_go_through_the_path_lifts_the_cap(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()
    go = post("go", WORDS, tmp_path)

    assert flow.post("go-2", go.body, MAX) == "queued-fix"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)


def test_the_go_quotes_max_exact_words_under_a_go_header(tmp_path: Path) -> None:
    go = post("go", WORDS, tmp_path)
    fields = dict(parse_comment(go.body).fields)

    assert fields == {"kind": "go", "from": "grok-bot", "role": "orchestrator", "state": "building"}
    assert "> Yes, give #42 one more round.\n> Fix the flaky test first.\n" in go.body


def test_the_go_posts_only_through_max_approver_profile(tmp_path: Path) -> None:
    go = post("go", WORDS, tmp_path)

    assert go.env == {"PATH": "/usr/bin", "GH_CONFIG_DIR": str(tmp_path / "grok" / "gh-approver")}


def test_the_go_as_the_app_bot_does_not_lift_the_cap(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()

    assert flow.post("go-2", post("go", WORDS, tmp_path).body, APP_BOT) == "go-not-from-approver"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)


@pytest.mark.parametrize("words", ["", "   \n  ", None])
def test_a_go_without_max_words_is_refused(tmp_path: Path, words: object) -> None:
    with pytest.raises(OrchestrateError, match="Max's exact words"):
        post("go", words, tmp_path)  # type: ignore[arg-type]


# --- AC 2: an ok through the path fails ---------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
def test_an_ok_through_the_path_fails(tmp_path: Path, enabled: bool) -> None:
    with pytest.raises(OrchestrateError, match="never post kind=ok as Max"):
        post("ok", WORDS, tmp_path, enabled=enabled)


@pytest.mark.parametrize("kind", ["verdict", "answers", "GO", "go ", ""])
def test_any_kind_but_go_fails(tmp_path: Path, kind: str) -> None:
    with pytest.raises(OrchestrateError, match="never post"):
        post(kind, WORDS, tmp_path)


def test_the_path_has_no_ok_helper() -> None:
    assert not hasattr(orchestrate, "approval_post_env")
    names = [name for name in vars(orchestrate) if "ok" in name.lower().split("_")]
    assert names == []


# --- AC 3: the Grok skill ----------------------------------------------------------


def test_the_skill_says_never_to_post_ok_as_max_and_states_the_go_limit() -> None:
    text = " ".join(SKILL.read_text(encoding="utf-8").split())

    assert "Never post `kind=ok` as Max" in text
    assert "approver_go_post" in text
    assert "approval_post_env" not in text
    assert "short-term" in text
    assert "Max's exact words" in text
    assert "`approver_gh_config_dir` is removed once T02f (#173) lands and Max confirms its `/go`" in text


# --- AC 4: Max's own hold ok still clears a hold -----------------------------------


@pytest.mark.parametrize("enabled", [True, False])
def test_a_real_max_hold_ok_still_clears_the_hold(tmp_path: Path, enabled: bool) -> None:
    dispatcher, queue, actions = hold_dispatcher(tmp_path, enabled)
    ready(dispatcher, queue)
    factory = hold_factory(approver=MAX)

    dispatcher.process(snapshot(approve(), factory=factory, labels=("factory:hold",)))
    assert actions.merges == []
    dispatcher.process(snapshot(approve(), ok_from(MAX), factory=factory, labels=("factory:hold",)))

    assert actions.merges == [(True, HEAD)]
    assert workflow_state(queue) == "merged"
    assert handled_outcome(queue, "ok-1") == "hold-ok"
