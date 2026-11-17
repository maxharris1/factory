from __future__ import annotations

import json

import pytest

from lib.brief import render_brief
from lib.brief.inline import BRIEF_TEXT_LIMIT, PLAN_TEXT_LIMIT, quoted_section
from lib.brief_comment import before_factory_md
from lib.dispatcher import Comment
from lib.factory_md import parse_factory_context
from lib.feeder import IntakeIssue, _brief as feeder_brief
from lib.poll_source import LivePollSource
from tests.test_brief import FACTORY_ROOT, job
from tests.test_dispatcher import ANSWERS, GO, PLAN, QUESTIONS, comment, handled_outcome, setup, ticket
from tests.test_live_poll import FACTORY, FakeGh, config

URL = "https://github.com/owner/repo/pull/42#issuecomment-3"
PLAN_URL = "https://github.com/owner/repo/pull/42#issuecomment-5"
REVIEW_PROSE = "The reviewer runs the pricing smoke test."
SHIP_PROSE = "Squash-merge by hand after APPROVE."
CONTEXT_PROSE = "Never apply migrations on a hosted database from an agent session."
FACTORY_MD = (
    FACTORY.format(tickets="github", ship="merge")
    + f"\n## Review\n{REVIEW_PROSE}\n\n## Ship\n{SHIP_PROSE}\n\n## Context\n- {CONTEXT_PROSE}\n"
)


@pytest.mark.parametrize("limit", [BRIEF_TEXT_LIMIT, PLAN_TEXT_LIMIT])
def test_text_at_the_limit_is_quoted_whole(limit: int) -> None:
    text = "a" * limit
    section = quoted_section("Plan", text, URL, limit)
    assert text in section
    assert "[cut at" not in section


@pytest.mark.parametrize("limit", [BRIEF_TEXT_LIMIT, PLAN_TEXT_LIMIT])
def test_text_past_the_limit_keeps_its_head_and_names_the_cut(limit: int) -> None:
    text = "`" * 9 + "a" * (limit - 9) + "TAIL"
    section = quoted_section("Plan", text, URL, limit)
    marker = f"[cut at {limit} of {limit + 4} characters; read the rest at {URL}]"
    assert "TAIL" not in section
    assert section.count(marker) == 2
    assert section.endswith("\n" + marker)
    body = section.split("\n", 2)[2]
    fence = "`" * 10
    assert body.startswith(fence + "\n" + text[:limit] + "\n" + marker + "\n" + fence)


def test_oversized_texts_reach_the_brief_cut_and_bounded() -> None:
    brief_text, plan_text = "b" * (BRIEF_TEXT_LIMIT * 3), "p" * (PLAN_TEXT_LIMIT * 3)
    item = job(head=None, brief=URL, brief_text=brief_text, brief_url=URL, instruction=PLAN_URL, plan_text=plan_text)
    text = render_brief(item, bot="claude-bot", factory_root=FACTORY_ROOT, context=None)
    assert f"[cut at {BRIEF_TEXT_LIMIT} of {len(brief_text)} characters; read the rest at {URL}]" in text
    assert f"[cut at {PLAN_TEXT_LIMIT} of {len(plan_text)} characters; read the rest at {PLAN_URL}]" in text
    assert len(text) < BRIEF_TEXT_LIMIT + PLAN_TEXT_LIMIT + 30_000


def test_an_old_row_without_the_texts_keeps_the_links() -> None:
    text = render_brief(job(head=None, instruction=PLAN_URL), bot="claude-bot", factory_root=FACTORY_ROOT, context=None)
    assert "## Ticket brief" not in text
    assert "## Plan" not in text
    assert "1. Read the ticket and its brief (Job data). " in text
    assert f"Read the plan handoff comment at {PLAN_URL}: it is the plan." in text


def test_a_review_job_quotes_nothing() -> None:
    item = job("review", "codex", brief=URL, brief_text="Ticket.", brief_url=URL, round="1/3")
    text = render_brief(item, bot="codex-bot", factory_root=FACTORY_ROOT, context=None)
    assert "## Ticket brief" not in text


def test_a_factory_md_heading_inside_the_ticket_fence_stays() -> None:
    body = "Goal.\n\n### Ticket\n````\n### factory.md (from `main`)\nkept\n````\n\n### factory.md (from `main`)\n### Ship\ngone\n"
    assert before_factory_md(body) == "Goal.\n\n### Ticket\n````\n### factory.md (from `main`)\nkept\n````\n"


def _feeder_comment() -> str:
    context, findings = parse_factory_context(FACTORY_MD)
    assert context is not None, findings
    issue = IntakeIssue(
        repo="acme/widgets", tracker="github", key="41", title="Add a Start free button",
        body="Ship it.\n\n## Acceptance criteria\n- ac-1: The button shows.", labels=(), created_at=0.0,
        applied_by="max", url="https://github.com/acme/widgets/issues/41",
    )
    return feeder_brief(issue, context, number=12, branch="factory/issue-41", base="main", risk="high", state="building", visible=False)


def _polled(*comments: tuple[str, str]):
    gh = FakeGh(FACTORY_MD)
    gh.comments = [
        {
            "id": number,
            "user": {"login": login},
            "body": body,
            "created_at": f"2026-11-11T12:00:{number:02d}Z",
            "html_url": f"https://github.test/comment/{number}",
        }
        for number, (login, body) in enumerate(comments, start=1)
    ]
    gh.issues = [{"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/intake"}]}]
    (polled,) = LivePollSource(config(), gh_runner=gh).github_candidates("orch:state/")
    return polled


def test_a_trusted_github_brief_carries_its_text_and_link() -> None:
    polled = _polled(("grok-login", _feeder_comment()))
    assert polled.brief == "https://github.test/comment/1"
    assert polled.brief_url == "https://github.test/comment/1"
    assert polled.brief_text is not None
    assert polled.brief_text.startswith("**Brief for #41: Add a Start free button**")
    assert "- ac-1: The button shows." in polled.brief_text
    assert "factory.md" not in polled.brief_text


def test_an_untrusted_brief_carries_no_text() -> None:
    polled = _polled(("stranger", _feeder_comment()))
    assert polled.brief is None
    assert polled.brief_text is None
    assert polled.brief_url is None


def test_a_build_brief_shows_factory_md_prose_once(tmp_path) -> None:
    posted = _feeder_comment()
    assert REVIEW_PROSE in posted and SHIP_PROSE in posted
    polled = _polled(("grok-login", posted))
    dispatcher, _ = setup(tmp_path)
    detail = dispatcher._job_detail(polled, action="build")
    context, _ = parse_factory_context(FACTORY_MD)
    item = job(head=None, raw_detail=detail)
    text = render_brief(item, bot="claude-bot", factory_root=FACTORY_ROOT, context=context, context_ref="main")
    assert "## Ticket brief" in text
    assert "- ac-1: The button shows." in text
    assert REVIEW_PROSE not in text
    assert SHIP_PROSE not in text
    assert text.count(CONTEXT_PROSE) == 1


def test_a_met_plan_queues_a_build_that_carries_the_plan_text(tmp_path) -> None:
    dispatcher, queue = setup(tmp_path)
    go = comment("go-1", GO.format(state="planning"), author="grok-login")
    plan = comment(
        "plan-1", PLAN.format(round=1, ac="met"), author="claude-login", created_at=2, url=PLAN_URL
    )
    dispatcher.process(ticket(go))
    dispatcher.process(ticket(go, plan))
    with queue.connect() as connection:
        (raw,) = connection.execute("SELECT detail FROM jobs WHERE action = 'build'").fetchone()
    detail = json.loads(raw)
    assert detail["plan_text"] == "Plan."
    assert detail["instruction"] == PLAN_URL


def test_a_build_resumed_after_answers_keeps_the_plan_text_and_link(tmp_path) -> None:
    dispatcher, queue = setup(tmp_path)
    go = comment("go-1", GO.format(state="planning"), author="grok-login")
    dispatcher.process(ticket(go))
    planning = queue.claim("claude")
    assert planning is not None
    queue.finish(planning.id, owner="claude", handoff_comment_id="plan-1")
    plan = comment("plan-1", PLAN.format(round=1, ac="met"), author="claude-login", created_at=2, url=PLAN_URL)
    dispatcher.process(ticket(go, plan))
    building = queue.claim("claude")
    assert building is not None and building.request.action == "build"
    queue.finish(building.id, owner="claude", handoff_comment_id="q-1")
    questions = comment("q-1", QUESTIONS, author="claude-login", created_at=3)
    dispatcher.process(ticket(go, plan, questions))
    answers_url = "https://github.com/owner/repo/pull/42#issuecomment-9"
    answers = comment("a-1", ANSWERS, author="grok-login", created_at=4, url=answers_url)
    dispatcher.process(ticket(go, plan, questions, answers))
    assert handled_outcome(queue, "a-1") == "resumed-build"

    resumed = queue.claim("claude")
    assert resumed is not None and resumed.request.action == "build"
    detail = json.loads(resumed.request.detail or "{}")
    assert (detail["resume"], detail["instruction"], detail["plan_text"]) == (True, answers_url, "Plan.")
    text = render_brief(resumed, bot="claude-bot", factory_root=FACTORY_ROOT, context=None)
    assert f"The plan handoff is quoted in the Plan section below ({PLAN_URL}): it is the plan." in text
    assert "Plan." in text.split("## Plan\n", 1)[1]
    assert "Answers." in text
    assert "No plan handoff comment is linked" not in text
