from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.helpers import _route_go
from lib.factory_md import FactoryRouting
from lib.role_router import Draw, HarnessShares, central_routing, resolve_slot
from tests.test_dispatcher import factory, ticket

ROOT = Path(__file__).resolve().parents[1]
DECISION = (ROOT / "docs/decisions/1830-gemini-shares.md").read_text()
SPEC = (ROOT / "docs/SPEC.md").read_text()
ROUTED = replace(factory(), routing=FactoryRouting(budget="medium"))


def test_a_zero_share_stops_the_draw_but_gemini_stays_a_fallback() -> None:
    assert "A share of 0 stops the draw, not Gemini." in DECISION
    assert "add `gemini` to `orch.yml` `refused_harnesses`" in DECISION
    assert "Gemini stays a fallback in the lists" in SPEC

    off = replace(central_routing(), shares={"gemini": HarnessShares(build=0.0, review=0.0)})
    build = resolve_slot("build", routing=off, refused_harnesses=("claude", "codex"))
    review = resolve_slot(
        "review",
        routing=off,
        built_by=("claude", "claude-opus-5-5"),
        refused_harnesses=("codex",),
    )
    assert (build.harness, review.harness) == ("gemini", "gemini")


@pytest.mark.parametrize("named", [{"builder": "claude"}, {"reviewer": "codex"}])
def test_a_go_naming_one_side_takes_no_draw_for_either_side(named: dict[str, str]) -> None:
    assert "so neither side draws, even the side it leaves out" in DECISION
    assert "A go that names `builder=` or `reviewer=` takes no draw for either side." in SPEC

    routed = _route_go(ticket(factory_config=ROUTED), named)
    assert routed is not None and routed.router_picks is not None
    assert routed.router_picks.sample is None


@pytest.mark.parametrize("picked", [True, False])
def test_every_unskipped_review_draw_on_a_gemini_build_is_same_family(picked: bool) -> None:
    assert "every review draw that no guard skipped, won or lost, is recorded as lost" in DECISION

    drawn = Draw(
        role="review",
        harness="gemini",
        seed="o/r#1:review",
        draw=0.1 if picked else 0.9,
        share=0.25,
        chance=0.3,
        picked=picked,
        skipped_reason=None,
    )
    slot = resolve_slot(
        "review", routing=central_routing(), built_by=("gemini", "gemini-4-neon"), drawn=drawn
    )
    assert slot.draw == replace(drawn, picked=False, skipped_reason="same-family")


def test_the_dispatcher_draw_reads_gemini_headroom_though_choose_route_takes_no_usage() -> None:
    assert "The dispatcher's Gemini share draw does (`routing`, section 6)" in SPEC
    assert "so the choice of builder and reviewer harness does not depend on usage" not in SPEC
