import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, Dispatcher
from lib.factory_md import FactoryConfig, FactoryRouting, parse_factory_md
from lib.natural_intake import NaturalIntakeSession
from lib.orch_yml import load_orch_yml
from lib.orchestrate import RouteRequest, choose_route, go_comment
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    ticket,
)

ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40


def _root_factory() -> FactoryConfig:
    config, findings = parse_factory_md((ROOT / "factory.md").read_text())
    assert config is not None, findings
    return config


def _checked_in_refused() -> tuple[str, ...]:
    config, findings = load_orch_yml(ROOT / "orch.yml", allow_placeholders=True, overlay=False)
    assert config is not None, findings
    return config.refused_harnesses


def _gemini_builds(config: FactoryConfig) -> FactoryConfig:
    assert config.routing is not None
    return replace(
        config,
        routing=replace(
            config.routing,
            slots={"build": (("gemini", "gemini-4-neon", "high"),)},
        ),
    )


def _gemini_reviews(config: FactoryConfig) -> FactoryConfig:
    assert config.routing is not None
    return replace(
        config,
        routing=replace(
            config.routing,
            slots={"review": (("gemini", "gemini-4-neon", "high"),)},
        ),
    )


def _dispatcher(tmp_path: Path, refused: tuple[str, ...]) -> tuple[Dispatcher, SQLiteJobQueue]:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    logins = {
        **TRUSTED_LOGINS,
        "github": {**TRUSTED_LOGINS["github"], "gemini-login": {"gemini-bot"}},
    }
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=logins,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        refused_harnesses=refused,
    )
    return dispatcher, queue


@pytest.mark.parametrize(
    ("configure", "refused", "reviewer"),
    [
        (lambda config: config, lambda: (), ("codex", "gpt-6.2-sol")),
        (_gemini_reviews, lambda: (), ("gemini", "gemini-4-neon")),
        (lambda config: config, _checked_in_refused, ("codex", "gpt-6.2-sol")),
        (_gemini_builds, lambda: (), ("codex", "gpt-6.2-sol")),
    ],
    ids=[
        "codex-heads-the-list",
        "gemini-review-slot",
        "gemini-refused-in-orch-yml",
        "after-a-gemini-build",
    ],
)
def test_review_is_queued_for_the_reviewer_choose_route_picked(
    tmp_path: Path, configure, refused, reviewer: tuple[str, str]
) -> None:
    config = configure(_root_factory())
    refused_harnesses = refused()
    decision = choose_route(
        RouteRequest(config, "high", False, "feature", ("x.py",)),
        refused_harnesses=refused_harnesses,
    )
    assert (decision.reviewer_harness, decision.reviewer_model) == reviewer

    dispatcher, queue = _dispatcher(tmp_path, refused_harnesses)
    go = comment("go-1", go_comment(decision, "none"), author="grok-login")
    dispatcher.process(ticket(go, factory_config=config))

    build = queue.claim(decision.builder_harness)
    assert build is not None and build.request.action == "build"
    routing = json.loads(build.request.detail)["routing"]
    assert (routing["reviewer"], routing["reviewer_model"]) == reviewer
    queue.finish(build.id, owner=decision.builder_harness, handoff_comment_id="ready-1")

    ready = comment(
        "ready-1",
        f"<!-- orch:v1 kind=ready from={decision.builder_harness}-bot role=builder "
        f"head={HEAD} -->\nReady.\n",
        author=f"{decision.builder_harness}-login",
        created_at=2,
    )
    dispatcher.process(
        ticket(go, ready, head=HEAD, ci={HEAD: CIResult("success")}, factory_config=config)
    )

    review = queue.claim(reviewer[0])
    assert review is not None and review.request.action == "review"
    detail = json.loads(review.request.detail)
    assert detail["reviewer_harness"] == reviewer[0]
    assert (detail["routing"]["reviewer"], detail["routing"]["reviewer_model"]) == reviewer
    assert detail["routing"]["reviewer_effort"] == "high"


def test_default_intake_routes_around_the_checked_in_refusals_and_starts_the_build(
    tmp_path: Path,
) -> None:
    config = _root_factory()
    session = NaturalIntakeSession(config)
    questions = session.classify_and_post("Tighten the retry helper.", [])
    artifacts = session.promote(
        goal="Tighten the retry helper",
        acceptance_criteria=["Retries cap at 5s"],
        likely_files=["lib/retry.py"],
        risk="high",
        done_evidence="pytest tests/test_retry.py",
    )
    decision = artifacts.decision
    assert (decision.reviewer_harness, decision.reviewer_model) == ("codex", "gpt-6.2-sol")

    dispatcher, queue = _dispatcher(tmp_path, _checked_in_refused())
    dispatcher.process(
        ticket(
            comment("q-1", questions.comment, author="grok-login", created_at=1),
            comment("brief-1", artifacts.brief_comment, author="grok-login", created_at=2),
            comment("go-1", artifacts.go_comment, author="grok-login", created_at=3),
            factory_config=config,
        )
    )

    build = queue.claim(decision.builder_harness)
    assert build is not None and build.request.action == "build"
    routing = json.loads(build.request.detail)["routing"]
    assert (routing["reviewer"], routing["reviewer_model"]) == ("codex", "gpt-6.2-sol")


def test_choose_route_reads_the_deployed_refusals_by_default() -> None:
    decision = choose_route(RouteRequest(_root_factory(), "high", False, "feature", ("x.py",)))
    assert "gemini" in _checked_in_refused()
    assert (decision.reviewer_harness, decision.reviewer_model) == ("codex", "gpt-6.2-sol")
