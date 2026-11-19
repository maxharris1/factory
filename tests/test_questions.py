import json
from dataclasses import replace
from pathlib import Path

from lib.brief import render_brief
from lib.dispatch.questions import Decision, decisions_of
from lib.dispatch.types import CIResult, Comment
from lib.queue import Job, SQLiteJobQueue
from tests.test_brief import job as brief_job
from tests.test_dispatcher import ANSWERS, BRIEF, GO, QUESTIONS, comment, setup, ticket

T = 1795096800.0
HEAD = "a" * 40
QUESTION_TEXT = "Which export format ships first?"
QUESTIONS_BODY = QUESTIONS.replace(
    "Questions.\n",
    f"**Questions**\n{QUESTION_TEXT}\n- CSV\n- JSON\n\nAnything else is out of scope.\n",
)
CHOICE = "CSV only; JSON waits for #12."
ANSWERS_BODY = ANSWERS.replace(
    "Answers.\n", f"**Question**\n{QUESTION_TEXT}\n\n**Answer**\n{CHOICE}\n"
)
DASHBOARD_BODY = ANSWERS.replace(
    "Answers.\n",
    "**Max's words** (Max 2026-11-19 9:03 AM ET)\n> Ship CSV.\n>\n> JSON later.\n",
)
ANSWERS_URL = "https://github.test/comment/answers-1"
NOT_BLOCKING = "A decided point is not raised again as a `[blocking]` finding"


def render(job: Job, bot: str = "claude-bot") -> str:
    return render_brief(job, bot=bot, factory_root="/srv/orch/factory", context=None)


def decision_rows(queue: SQLiteJobQueue) -> list[dict]:
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM decisions ORDER BY answered_at, answer_id, position"
        ).fetchall()
    return [dict(row) for row in rows]


def answered(tmp_path: Path, answer_body: str, *, answerer: str = "grok-login"):
    dispatcher, queue = setup(tmp_path, now=[T + 100])
    base = (
        comment("brief-1", BRIEF, author="grok-login", created_at=T + 1),
        comment("go-1", GO.format(state="building"), author="grok-login", created_at=T + 2),
    )
    dispatcher.process(replace(ticket(*base), pr_number="7"))
    build = queue.claim("claude")
    assert build is not None and build.request.action == "build"
    queue.finish(build.id, owner="claude", handoff_comment_id="questions-1")
    question = comment("questions-1", QUESTIONS_BODY, author="claude-login", created_at=T + 3)
    dispatcher.process(replace(ticket(*base, question), pr_number="7"))
    wake = queue.claim("grok")
    assert wake is not None and wake.request.action == "clarify"
    queue.finish(wake.id, owner="grok", handoff_comment_id="answers-1")
    answer = comment(
        "answers-1", answer_body, author=answerer, created_at=T + 180, url=ANSWERS_URL
    )
    comments = (*base, question, answer)
    dispatcher.process(replace(ticket(*comments), pr_number="7"))
    return dispatcher, queue, comments


def test_answers_resume_the_step_and_record_the_decision(tmp_path: Path) -> None:
    dispatcher, queue, comments = answered(tmp_path, ANSWERS_BODY)

    resumed = queue.claim("claude")
    assert resumed is not None and resumed.request.action == "build"
    assert decision_rows(queue) == [
        {
            "repo": "owner/repo",
            "ticket_id": "42",
            "answer_id": "answers-1",
            "position": 0,
            "question_id": "questions-1",
            "question": QUESTION_TEXT,
            "options": json.dumps(["CSV", "JSON"]),
            "choice": CHOICE,
            "answerer": "grok-login",
            "answered_at": T + 180,
            "url": ANSWERS_URL,
        }
    ]

    dispatcher.process(replace(ticket(*comments), pr_number="7"))
    assert len(decision_rows(queue)) == 1


def test_dashboard_answer_records_max_words_unquoted(tmp_path: Path) -> None:
    _, queue, _ = answered(tmp_path, DASHBOARD_BODY)

    [row] = decision_rows(queue)
    assert row["choice"] == "Ship CSV.\n\nJSON later."
    assert row["question"] == QUESTIONS_BODY.partition("\n")[2].strip()
    assert json.loads(row["options"]) == ["CSV", "JSON"]


def test_untrusted_answers_record_nothing(tmp_path: Path) -> None:
    _, queue, _ = answered(tmp_path, ANSWERS_BODY, answerer="stranger-login")

    assert decision_rows(queue) == []


def test_resumed_build_brief_quotes_the_decision_once(tmp_path: Path) -> None:
    _, queue, _ = answered(tmp_path, ANSWERS_BODY)
    resumed = queue.claim("claude")
    assert resumed is not None

    text = render(resumed)
    decided = text.split("## Decided\n")[1].split("\n## ")[0]
    assert f"````\n{QUESTION_TEXT}\n````" in decided
    assert "Options offered: `CSV`, `JSON`" in decided
    assert f"````\n{CHOICE}\n````" in decided
    assert f"Answered by `grok-login` at 2026-11-19 14:03 UTC ({ANSWERS_URL})" in decided
    assert "do not ask" in decided
    assert NOT_BLOCKING not in decided
    assert "The `answers` comments from Max or the orchestrator" not in text
    assert text.count(f"````\n{CHOICE}\n````") == 1
    assert "(quoted in the Decided section)" in text


def next_round(tmp_path: Path) -> tuple[str, str]:
    dispatcher, queue, comments = answered(tmp_path, ANSWERS_BODY)
    resumed = queue.claim("claude")
    assert resumed is not None
    queue.finish(resumed.id, owner="claude", handoff_comment_id="ready-1")
    ready = comment(
        "ready-1",
        f"<!-- orch:v1 kind=ready from=claude-bot role=builder head={HEAD} -->\nReady.\n",
        author="claude-login",
        created_at=T + 200,
    )
    comments = (*comments, ready)
    ci = {HEAD: CIResult("success")}
    dispatcher.process(replace(ticket(*comments, head=HEAD, ci=ci), pr_number="7"))
    review = queue.claim("codex")
    assert review is not None and review.request.action == "review"
    review_text = render(review, "codex-bot")
    queue.finish(review.id, owner="codex", handoff_comment_id="verdict-1")
    verdict = comment(
        "verdict-1",
        "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
        f"round=1 head={HEAD} verdict=REVISE risk=high -->\n"
        "1. [blocking] f-1. Rule: ac-1. lib/x.py:1. Breaks.\n",
        author="codex-login",
        created_at=T + 300,
    )
    dispatcher.process(replace(ticket(*comments, verdict, head=HEAD, ci=ci), pr_number="7"))
    fix = queue.claim("claude")
    assert fix is not None and fix.request.action == "fix"
    return review_text, render(fix)


def test_next_round_review_and_fix_briefs_carry_the_decision(tmp_path: Path) -> None:
    review_text, fix_text = next_round(tmp_path)

    for text in (review_text, fix_text):
        decided = text.split("## Decided\n")[1].split("\n## ")[0]
        assert f"````\n{CHOICE}\n````" in decided
    assert NOT_BLOCKING in review_text
    assert "a `[follow-up]` at most" in review_text
    assert NOT_BLOCKING not in fix_text


def test_malformed_decisions_are_skipped() -> None:
    good = {
        "question": "Q?",
        "options": [],
        "choice": "A.",
        "answerer": "grok-login",
        "answered_at": T,
        "url": None,
        "answer_id": "answers-1",
        "position": 0,
    }
    text = render(brief_job("build", decisions=[{"question": 1}, "x", good]))

    decided = text.split("## Decided\n")[1].split("\n## ")[0]
    assert "Options offered: none recorded" in decided
    assert "Answered by `grok-login` at 2026-11-19 14:00 UTC." in decided
    assert decided.count("Choice:") == 1


def test_brief_without_decisions_has_no_decided_section() -> None:
    assert "## Decided" not in render(brief_job("build", decisions=[]))
    assert "## Decided" not in render(brief_job("build"))


def answer(body: str) -> Comment:
    return comment("a-1", body, author="grok-login", created_at=T, url=ANSWERS_URL)


def test_decisions_of_question_answer_blocks() -> None:
    body = ANSWERS.replace(
        "Answers.\n",
        "**Question**\nFormat?\n\n**Answer**\nCSV.\n\n"
        "**Question**\nDeadline?\n\n**Answer**\nFriday.\nNo slip.\n",
    )
    question = comment(
        "q-1",
        QUESTIONS.replace(
            "Questions.\n",
            "Format?\n- CSV\n- JSON\n\nDeadline?\n1. Friday\n2) Monday\n",
        ),
        author="claude-login",
    )

    assert decisions_of(question, answer(body)) == [
        Decision("Format?", ("CSV", "JSON"), "CSV.", "grok-login", T, ANSWERS_URL, "a-1", 0),
        Decision(
            "Deadline?", ("Friday", "Monday"), "Friday.\nNo slip.", "grok-login", T, ANSWERS_URL, "a-1", 1
        ),
    ]


def test_decisions_of_unmatched_question_offers_every_list_item() -> None:
    body = ANSWERS.replace("Answers.\n", "**Question**\nScope?\n\n**Answer**\nSmall.\n")
    question = comment(
        "q-1", QUESTIONS.replace("Questions.\n", "* Small\n* Large\n"), author="claude-login"
    )

    [decision] = decisions_of(question, answer(body))
    assert decision.options == ("Small", "Large")


def test_decisions_of_plain_answer_without_question_comment() -> None:
    [decision] = decisions_of(None, answer(ANSWERS))

    assert (decision.question, decision.options, decision.choice) == ("", (), "Answers.")
