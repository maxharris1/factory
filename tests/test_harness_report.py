import gc
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from lib import cli, exposure, harness_report, telemetry
from lib.harness_report import Measure, Row, VerdictMix
from lib.pricing import estimate_cost_usd
from lib.role_router import Draw, Hardness, Sample, sample_json

REPO = "Owner/Repo"
UNKNOWN = Measure(None, 0)
UNMEASURED = (UNKNOWN,) * 7

CI_EVENTS_DDL = """
CREATE TABLE ci_events (
    repo TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    head TEXT NOT NULL,
    status TEXT NOT NULL,
    gate_count_after INTEGER NOT NULL,
    PRIMARY KEY (repo, ticket_id, head)
)
"""

JOBS_DDL = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    repo TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    action TEXT NOT NULL,
    recipient TEXT NOT NULL,
    source_comment_id TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    head TEXT,
    detail TEXT,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_activity_at REAL NOT NULL,
    finished_at REAL,
    handoff_comment_id TEXT,
    failure_handled INTEGER NOT NULL DEFAULT 0,
    claim_token_hash TEXT
)
"""


def _verdict_heads_ddl(with_verdict: bool) -> str:
    verdict = ", verdict TEXT" if with_verdict else ""
    return f"""
    CREATE TABLE verdict_heads (
        repo TEXT NOT NULL,
        ticket_id TEXT NOT NULL,
        comment_id TEXT NOT NULL,
        head TEXT NOT NULL,
        review_round INTEGER,
        posted_at REAL,
        changed_lines INTEGER{verdict},
        PRIMARY KEY (repo, ticket_id, comment_id)
    )
    """


class Db:
    def __init__(
        self,
        path: Path,
        *,
        exposure_table: bool = True,
        verdict_table: bool = True,
        verdict_column: bool = True,
        ci_table: bool = True,
        telemetry_table: bool = True,
        tokens_unknown_column: bool = True,
    ):
        self.path = path
        self.verdict_column = verdict_column
        self.tokens_unknown_column = tokens_unknown_column
        with sqlite3.connect(path) as connection:
            connection.execute(JOBS_DDL)
            if exposure_table:
                connection.execute(exposure.DDL)
            if verdict_table:
                connection.execute(_verdict_heads_ddl(verdict_column))
            if ci_table:
                connection.execute(CI_EVENTS_DDL)
            if telemetry_table:
                ddl = telemetry.DDL
                if not tokens_unknown_column:
                    ddl = ddl.replace("tokens_unknown INTEGER,", "")
                connection.execute(ddl)
        connection.close()

    def job(
        self,
        job_id: str,
        ticket: str,
        action: str,
        recipient: str,
        *,
        at: float | str = 100.0,
        repo: str = REPO,
        risk: str | None = None,
        routing: dict | None = None,
        handoff: str | None = None,
        model: str | None = None,
        state: str = "done",
        kind: str | None = None,
    ) -> None:
        detail: dict = {}
        if risk is not None:
            detail["risk"] = risk
        if kind is not None:
            detail["kind"] = kind
        if routing is not None:
            detail["routing"] = routing
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, dedupe_key, repo, ticket_id, action, recipient, source_comment_id,
                    timeout_seconds, generation, attempt, detail, state, created_at,
                    last_activity_at, handoff_comment_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'c0', 7200, 1, 1, ?, ?, ?, ?, ?)
                """,
                (job_id, job_id, repo, ticket, action, recipient, json.dumps(detail), state, at,
                 at, handoff),
            )
            if model is not None:
                connection.execute(
                    f"""
                    INSERT INTO {exposure.TABLE} (
                        job_id, model, cli_version, brief_sha256, skill_paths, host,
                        factory_revision, recorded_at, effort, harness
                    ) VALUES (?, ?, 'unknown', 'x', 'unknown', 'h', 'unknown', 0, 'unknown', ?)
                    """,
                    (job_id, model, recipient),
                )
        connection.close()

    def verdict(
        self,
        ticket: str,
        comment: str,
        verdict: str | None,
        at: float | None,
        repo: str = REPO,
        changed_lines: int | None = None,
    ) -> None:
        columns = "repo, ticket_id, comment_id, head, posted_at, changed_lines"
        values: tuple = (repo.lower(), ticket, comment, "abc", at, changed_lines)
        if self.verdict_column:
            columns += ", verdict"
            values += (verdict,)
        marks = ", ".join("?" for _ in values)
        with sqlite3.connect(self.path) as connection:
            connection.execute(f"INSERT INTO verdict_heads ({columns}) VALUES ({marks})", values)
        connection.close()

    def ci(self, ticket: str, head: str, status: str, repo: str = REPO) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO ci_events (repo, ticket_id, head, status, gate_count_after) "
                "VALUES (?, ?, ?, ?, 0)",
                (repo.lower(), ticket, head, status),
            )
        connection.close()

    def telemetry(
        self,
        job_id: str,
        harness: str,
        *,
        elapsed_seconds: float = 60.0,
        tokens_in: int = 0,
        tokens_cached: int = 0,
        tokens_out: int = 0,
        cost_usd: float | None = None,
        tokens_unknown: int | None = 0,
    ) -> None:
        columns = (
            "job_id, bot, harness, repo, started_at, updated_at, elapsed_seconds, events, "
            "turns, tool_calls, files_touched, commands, test_runs, tokens_in, tokens_cached, "
            "tokens_out, cost_usd, steps"
        )
        values: tuple = (job_id, "bot", harness, REPO, 0, 0, elapsed_seconds, 0, 0, "{}", 0, 0,
                         0, tokens_in, tokens_cached, tokens_out, cost_usd, "[]")
        if self.tokens_unknown_column:
            columns += ", tokens_unknown"
            values += (tokens_unknown,)
        marks = ", ".join("?" for _ in values)
        with sqlite3.connect(self.path) as connection:
            connection.execute(f"INSERT INTO job_telemetry ({columns}) VALUES ({marks})", values)
        connection.close()


@pytest.fixture
def db(tmp_path: Path) -> Db:
    return Db(tmp_path / "orch.db")


def _draw(role: str, harness: str, picked: bool) -> Draw:
    hardness = Hardness("low", False, 1, None, 1.0) if role == "build" else None
    return Draw(role, harness, "seed", 0.1, 0.2, 0.2, picked, None, hardness)


def _routing(
    *, build: Draw | None = None, review: Draw | None = None, **models: str
) -> dict:
    routing: dict = dict(models)
    if build is not None or review is not None:
        routing["sample"] = sample_json(Sample(build=build, review=review))
    return routing


def _only(rows: list[Row], role: str) -> Row:
    (row,) = [row for row in rows if row.role == role]
    return row


def _seed_claude_codex_and_gemini(db: Db) -> None:
    gemini_build = _draw("build", "gemini", True)
    db.job("t1-plan", "1", "plan", "claude", at=100, risk="low",
           routing=_routing(planner="claude", plan_model="claude-opus"))
    db.job("t1-build", "1", "build", "codex", at=110, risk="low", model="gpt-5")
    db.job("t1-review1", "1", "review", "claude", at=120, risk="low", handoff="c1",
           routing=_routing(reviewer_model="claude-opus"))
    db.job("t1-fix", "1", "fix", "codex", at=130, risk="low", model="gpt-5")
    db.job("t1-review2", "1", "review", "claude", at=140, risk="low", handoff="c2",
           routing=_routing(reviewer_model="claude-opus"))
    db.job("t1-merge", "1", "merge", "claude", at=150)
    db.verdict("1", "c1", "REVISE", 121)
    db.verdict("1", "c2", "APPROVE", 141)

    db.job("t2-build", "2", "build", "gemini", at=200, risk="high", model="gemini-3",
           routing=_routing(build=gemini_build))
    db.job("t2-review", "2", "review", "codex", at=210, risk="high", handoff="c3",
           routing=_routing(reviewer_model="gpt-5"))
    db.verdict("2", "c3", "APPROVE", 211)

    db.job("t3-plan", "3", "plan", "claude", at=300, risk="low", model="claude-opus")
    db.job("t3-build", "3", "build", "codex", at=310, risk="low",
           routing=_routing(builder_model="gpt-5"))
    db.job("t3-review", "3", "review", "claude", at=320, risk="low", handoff="c4",
           model="claude-opus")
    db.verdict("3", "c4", "BLOCKED", 321)


def test_rows_group_claude_codex_and_gemini_jobs(db: Db) -> None:
    _seed_claude_codex_and_gemini(db)

    assert harness_report.rows(db.path) == [
        Row("plan", "claude", "claude-opus", "low", True, "default", 2, 2,
            Measure(1.0, 2), Measure(2.0, 1), VerdictMix(1, 1, 1, 3), Measure(0.5, 2),
            *UNMEASURED),
        Row("build", "codex", "gpt-5", "low", True, "default", 2, 3,
            Measure(2 / 3, 3), Measure(2.0, 1), VerdictMix(1, 1, 1, 3), Measure(0.5, 2),
            *UNMEASURED),
        Row("build", "gemini", "gemini-3", "high", False, "sampled", 1, 1,
            Measure(1 / 3, 3), Measure(1.0, 1), VerdictMix(1, 0, 0, 1), Measure(0.0, 1),
            *UNMEASURED),
        Row("review", "claude", "claude-opus", "low", True, "default", 2, 3,
            Measure(2 / 3, 3), Measure(2.0, 1), VerdictMix(1, 1, 1, 3), Measure(0.5, 2),
            *UNMEASURED),
        Row("review", "codex", "gpt-5", "high", False, "default", 1, 1,
            Measure(1 / 3, 3), Measure(1.0, 1), VerdictMix(1, 0, 0, 1), Measure(0.0, 1),
            *UNMEASURED),
    ]


def test_a_review_row_counts_only_its_own_verdicts(db: Db) -> None:
    db.job("build", "1", "build", "claude")
    db.job("r-claude", "1", "review", "claude", handoff="c1")
    db.job("r-codex", "1", "review", "codex", handoff="c2")
    db.verdict("1", "c1", "REVISE", 1)
    db.verdict("1", "c2", "APPROVE", 2)

    rows = harness_report.rows(db.path)

    assert _only(rows, "build").verdicts == VerdictMix(1, 1, 0, 2)
    reviews = {row.harness: row.verdicts for row in rows if row.role == "review"}
    assert reviews == {"claude": VerdictMix(0, 1, 0, 1), "codex": VerdictMix(1, 0, 0, 1)}


def test_rounds_to_approve_counts_up_to_the_first_approve_by_post_time(db: Db) -> None:
    db.job("build", "1", "build", "claude")
    db.verdict("1", "c-late", "APPROVE", 30)
    db.verdict("1", "c-unposted", "APPROVE", None)
    db.verdict("1", "c-first", "REVISE", 10)
    db.verdict("1", "c-second", "BLOCKED", 20)

    assert _only(harness_report.rows(db.path), "build").rounds_to_approve == Measure(3.0, 1)


def test_unknown_values_are_none_not_zero(db: Db) -> None:
    db.job("build", "1", "build", "claude")

    assert harness_report.rows(db.path) == [
        Row("build", "claude", None, None, False, "default", 1, 1,
            Measure(1.0, 1), Measure(None, 0), None, Measure(0.0, 1), *UNMEASURED),
    ]


def test_a_db_without_the_verdict_column_reads_verdicts_as_unknown(tmp_path: Path) -> None:
    db = Db(tmp_path / "orch.db", verdict_column=False)
    db.job("review", "1", "review", "claude", handoff="c1")
    db.verdict("1", "c1", None, 1)

    row = _only(harness_report.rows(db.path), "review")

    assert row.verdicts is None
    assert row.rounds_to_approve == Measure(None, 0)


def test_null_verdicts_are_not_counted(db: Db) -> None:
    db.job("review", "1", "review", "claude", handoff="c1")
    db.verdict("1", "c1", None, 1)

    assert _only(harness_report.rows(db.path), "review").verdicts is None


def test_since_counts_later_jobs_but_keeps_ticket_facts(db: Db) -> None:
    db.job("plan", "1", "plan", "claude", at=100)
    db.job("fix", "1", "fix", "codex", at=110)
    db.job("build", "1", "build", "claude", at=200)

    assert harness_report.rows(db.path, since=150) == [
        Row("build", "claude", None, None, True, "default", 1, 1,
            Measure(1.0, 1), Measure(None, 0), None, Measure(1.0, 1), *UNMEASURED),
    ]


def test_since_measures_job_costs_on_the_window_and_ci_on_the_ticket(db: Db) -> None:
    db.job("old", "1", "build", "claude", at=100)
    db.job("new", "1", "build", "claude", at=200)
    db.telemetry("old", "claude", elapsed_seconds=6000, cost_usd=9.0)
    db.telemetry("new", "claude", elapsed_seconds=600, cost_usd=1.0)
    db.ci("1", "h1", "success")

    row = _only(harness_report.rows(db.path, since=150), "build")

    assert (row.jobs, row.elapsed_minutes, row.cost_usd, row.first_push_ci) == (
        1, Measure(10.0, 1), Measure(1.0, 1), Measure(1.0, 1),
    )


def test_repo_filter_ignores_case(db: Db) -> None:
    db.job("mine", "1", "build", "claude")
    db.job("other", "1", "build", "codex", repo="Owner/Other")

    rows = harness_report.rows(db.path, repo="OWNER/REPO")

    assert [(row.harness, row.share) for row in rows] == [("claude", Measure(1.0, 1))]


def test_the_same_ticket_number_in_two_repos_is_two_tickets(db: Db) -> None:
    db.job("plan-other", "1", "plan", "claude", repo="Owner/Other")
    db.job("mine", "1", "build", "claude")
    db.job("other", "1", "build", "claude", repo="Owner/Other")

    rows = [row for row in harness_report.rows(db.path) if row.role == "build"]

    assert [(row.plan_first, row.tickets) for row in rows] == [(False, 1), (True, 1)]


def test_mark_is_sampled_when_the_draw_picked_gemini_and_the_job_ran_on_gemini(db: Db) -> None:
    db.job("build", "1", "build", "gemini", routing=_routing(build=_draw("build", "gemini", True)))

    assert harness_report.rows(db.path)[0].mark == "sampled"


def test_mark_is_default_when_the_draw_picked_gemini_but_the_job_ran_on_claude(db: Db) -> None:
    db.job("build", "1", "build", "claude", routing=_routing(build=_draw("build", "gemini", True)))

    assert harness_report.rows(db.path)[0].mark == "default"


def test_mark_is_default_when_the_draw_did_not_pick(db: Db) -> None:
    db.job("build", "1", "build", "gemini", routing=_routing(build=_draw("build", "gemini", False)))

    assert harness_report.rows(db.path)[0].mark == "default"


def test_mark_is_default_without_a_sample(db: Db) -> None:
    db.job("build", "1", "build", "gemini")

    assert harness_report.rows(db.path)[0].mark == "default"


def test_mark_reads_the_ticket_sample_from_its_newest_job_with_one(db: Db) -> None:
    db.job("old", "1", "plan", "gemini", at=100,
           routing=_routing(build=_draw("build", "gemini", False)))
    db.job("new", "1", "build", "claude", at=200,
           routing=_routing(build=_draw("build", "gemini", True)))
    db.job("newest", "1", "fix", "gemini", at=300, routing={"sample": {"bogus": 1}})

    marks = {row.mark for row in harness_report.rows(db.path) if row.harness == "gemini"}

    assert marks == {"sampled"}


def test_the_review_role_uses_the_review_draw(db: Db) -> None:
    routing = _routing(build=_draw("build", "gemini", False), review=_draw("review", "gemini", True))
    db.job("build", "1", "build", "gemini", routing=routing)
    db.job("review", "1", "review", "gemini", routing=routing)

    rows = harness_report.rows(db.path)

    assert [(row.role, row.mark) for row in rows] == [("build", "default"), ("review", "sampled")]


def test_plan_first_is_true_when_the_ticket_has_a_plan_job(db: Db) -> None:
    db.job("plan", "1", "plan", "claude")
    db.job("build", "1", "build", "claude")

    assert _only(harness_report.rows(db.path), "build").plan_first is True


def test_plan_first_is_false_without_a_plan_job(db: Db) -> None:
    db.job("build", "1", "build", "claude")

    assert _only(harness_report.rows(db.path), "build").plan_first is False


def test_the_reported_model_beats_the_routed_one(db: Db) -> None:
    db.job("build", "1", "build", "codex", model="gpt-5.1", routing=_routing(builder_model="gpt-5"))

    assert harness_report.rows(db.path)[0].model == "gpt-5.1"


@pytest.mark.parametrize("reported", [None, exposure.UNKNOWN])
def test_the_routed_model_stands_in_when_none_was_reported(db: Db, reported: str | None) -> None:
    routing = _routing(planner="claude", plan_model="p", builder_model="b", reviewer_model="r")
    db.job("plan", "1", "plan", "claude", model=reported, routing=routing)
    db.job("fix", "1", "fix", "claude", model=reported, routing=routing)
    db.job("review", "1", "review", "claude", model=reported, routing=routing)

    assert [(row.role, row.model) for row in harness_report.rows(db.path)] == [
        ("plan", "p"), ("build", "b"), ("review", "r"),
    ]


@pytest.mark.parametrize("state", ["queued", "running", "cancelled"])
def test_only_jobs_that_ran_count(db: Db, state: str) -> None:
    db.job("plan", "1", "plan", "claude", state=state)
    db.job("gemini", "1", "build", "gemini", state=state,
           routing=_routing(build=_draw("build", "gemini", True)))
    db.job("fix", "1", "fix", "claude", state=state)
    db.job("build", "1", "build", "claude")

    assert harness_report.rows(db.path) == [
        Row("build", "claude", None, None, False, "default", 1, 1,
            Measure(1.0, 1), Measure(None, 0), None, Measure(0.0, 1), *UNMEASURED),
    ]


def test_a_failed_job_ran(db: Db) -> None:
    db.job("build", "1", "build", "claude", state="failed")

    assert _only(harness_report.rows(db.path), "build").jobs == 1


def test_a_merge_only_fix_is_not_a_build_job_or_a_fix_round(db: Db) -> None:
    db.job("build", "1", "build", "claude", risk="low")
    db.job("update", "1", "fix", "claude", risk="low", kind="merge-only")

    assert [(row.role, row.jobs, row.fix_rounds) for row in harness_report.rows(db.path)] == [
        ("build", 1, Measure(0.0, 1)),
    ]


def test_iso_created_at_is_read_as_epoch_seconds(db: Db) -> None:
    db.job("old", "1", "build", "codex", at="2025-01-01T00:00:00Z")
    db.job("new", "2", "build", "claude", at="2026-01-01T00:00:00Z")
    db.job("newer", "3", "build", "gemini", at=1780000000.0)

    rows = harness_report.rows(db.path, since=1760000000.0)

    assert [row.harness for row in rows] == ["claude", "gemini"]


def test_an_unreadable_created_at_is_outside_any_since_window(db: Db) -> None:
    db.job("bad", "1", "build", "codex", at="yesterday")
    db.job("good", "2", "build", "claude", at=200)

    assert [row.harness for row in harness_report.rows(db.path, since=100)] == ["claude"]
    assert [row.harness for row in harness_report.rows(db.path)] == ["claude", "codex"]


def test_the_ticket_sample_follows_created_at_order_across_formats(db: Db) -> None:
    db.job("old", "1", "plan", "gemini", at="2025-01-01T00:00:00Z",
           routing=_routing(build=_draw("build", "gemini", False)))
    db.job("new", "1", "build", "gemini", at=1780000000.0,
           routing=_routing(build=_draw("build", "gemini", True)))

    assert {row.mark for row in harness_report.rows(db.path)} == {"sampled"}


def test_a_plan_without_a_planner_falls_back_to_the_builder_model(db: Db) -> None:
    routing = {"builder": "gemini", "builder_model": "gemini-4-neon", "reviewer": "codex"}
    db.job("plan", "1", "plan", "gemini", routing=routing)

    assert harness_report.rows(db.path)[0].model == "gemini-4-neon"


def test_no_routed_model_when_the_routed_side_is_another_harness(db: Db) -> None:
    routing = {"builder": "claude", "builder_model": "claude-opus-5-5", "reviewer": "codex"}
    db.job("build", "1", "build", "codex", routing=routing)

    assert harness_report.rows(db.path)[0].model is None


def test_the_database_is_opened_read_only(db: Db, monkeypatch: pytest.MonkeyPatch) -> None:
    db.job("build", "1", "build", "claude", model="opus")
    db.job("review", "1", "review", "codex", handoff="c1")
    db.verdict("1", "c1", "APPROVE", 1, changed_lines=12)
    db.ci("1", "h1", "success")
    db.telemetry("build", "claude", tokens_in=100, cost_usd=0.5)
    opened: list[str] = []
    query_only: list[int] = []
    real = sqlite3.connect

    class Recording(sqlite3.Connection):
        def close(self) -> None:
            query_only.append(self.execute("PRAGMA query_only").fetchone()[0])
            super().close()

    def spy(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        opened.append(str(database))
        gc.collect()
        return real(database, *args, factory=Recording, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("report ran a subprocess"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("report ran a subprocess"))
    gc.collect()
    before = db.path.read_bytes()

    assert harness_report.rows(db.path)

    assert opened and all(name.endswith("?mode=ro") for name in opened)
    assert query_only and all(value == 1 for value in query_only)
    assert db.path.read_bytes() == before


def test_a_db_with_only_jobs_still_reports(tmp_path: Path) -> None:
    db = Db(tmp_path / "orch.db", exposure_table=False, verdict_table=False, ci_table=False,
            telemetry_table=False)
    db.job("build", "1", "build", "claude", routing=_routing(builder_model="opus"))

    assert harness_report.rows(db.path) == [
        Row("build", "claude", "opus", None, False, "default", 1, 1,
            Measure(1.0, 1), Measure(None, 0), None, Measure(0.0, 1), *UNMEASURED),
    ]


def test_a_db_with_no_tables_has_no_rows(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    sqlite3.connect(path).close()

    assert harness_report.rows(path) == []


def test_the_module_does_not_use_the_queue() -> None:
    source = Path(harness_report.__file__).read_text(encoding="utf-8")

    assert "open_queue" not in source
    assert "SQLiteJobQueue" not in source


def test_every_row_carries_ci_size_time_tokens_and_cost_with_their_counts(db: Db) -> None:
    db.job("b1", "1", "build", "claude")
    db.job("b2", "2", "build", "claude")
    db.job("b3", "3", "build", "codex")
    db.ci("1", "h1", "success")
    db.ci("2", "h2", "failure")
    db.verdict("1", "c1", "APPROVE", 10, changed_lines=40)
    db.verdict("2", "c2", "REVISE", 20, changed_lines=100)
    db.telemetry("b1", "claude", elapsed_seconds=600, tokens_in=1000, tokens_cached=400,
                 tokens_out=200, cost_usd=1.5)
    db.telemetry("b2", "claude", elapsed_seconds=1200, tokens_in=3000, tokens_cached=600,
                 tokens_out=400, cost_usd=0.5)

    assert harness_report.rows(db.path) == [
        Row("build", "claude", None, None, False, "default", 2, 2,
            Measure(2 / 3, 3), Measure(1.0, 1), VerdictMix(1, 1, 0, 2), Measure(0.0, 2),
            first_push_ci=Measure(0.5, 2), changed_lines=Measure(70.0, 2),
            elapsed_minutes=Measure(15.0, 2), tokens_in=Measure(2000.0, 2),
            tokens_cached=Measure(500.0, 2), tokens_out=Measure(300.0, 2),
            cost_usd=Measure(1.0, 2)),
        Row("build", "codex", None, None, False, "default", 1, 1,
            Measure(1 / 3, 3), UNKNOWN, None, Measure(0.0, 1), *UNMEASURED),
    ]


def test_first_push_ci_reads_the_first_recorded_status_and_skips_tickets_without_one(
    db: Db,
) -> None:
    db.job("b1", "1", "build", "claude")
    db.job("b2", "2", "build", "claude")
    db.ci("1", "h-first", "failure")
    db.ci("1", "h-later", "success")

    assert _only(harness_report.rows(db.path), "build").first_push_ci == Measure(0.0, 1)


def test_changed_lines_takes_the_first_head_by_post_time_that_has_a_count(db: Db) -> None:
    db.job("build", "1", "build", "claude")
    db.verdict("1", "c-unposted", "APPROVE", None, changed_lines=999)
    db.verdict("1", "c-second", "REVISE", 20, changed_lines=30)
    db.verdict("1", "c-first", "REVISE", 10)

    assert _only(harness_report.rows(db.path), "build").changed_lines == Measure(30.0, 1)


def test_a_reported_cost_beats_the_token_estimate(db: Db) -> None:
    db.job("build", "1", "build", "claude")
    db.telemetry("build", "claude", tokens_in=1_000_000, cost_usd=0.25)

    assert _only(harness_report.rows(db.path), "build").cost_usd == Measure(0.25, 1)


def test_the_token_estimate_stands_in_when_no_cost_was_reported(db: Db) -> None:
    db.job("build", "1", "build", "claude")
    db.telemetry("build", "claude", tokens_in=1_000_000)
    estimate = estimate_cost_usd("claude", tokens_in=1_000_000, tokens_cached=0, tokens_out=0)

    assert estimate == 3.0
    assert _only(harness_report.rows(db.path), "build").cost_usd == Measure(estimate, 1)


def test_the_estimate_prices_by_the_job_recipient_when_telemetry_names_no_harness(
    db: Db,
) -> None:
    db.job("build", "1", "build", "codex")
    db.telemetry("build", "", tokens_in=1_000_000)
    estimate = estimate_cost_usd("codex", tokens_in=1_000_000, tokens_cached=0, tokens_out=0)

    assert _only(harness_report.rows(db.path), "build").cost_usd == Measure(estimate, 1)


def test_unknown_tokens_are_not_counted_and_cost_only_when_reported(db: Db) -> None:
    db.job("silent", "1", "build", "claude")
    db.job("billed", "2", "build", "claude")
    db.telemetry("silent", "claude", tokens_in=1_000_000, tokens_unknown=1)
    db.telemetry("billed", "claude", tokens_in=1_000_000, cost_usd=2.0, tokens_unknown=1)

    row = _only(harness_report.rows(db.path), "build")

    assert (row.tokens_in, row.tokens_cached, row.tokens_out) == (UNKNOWN, UNKNOWN, UNKNOWN)
    assert row.cost_usd == Measure(2.0, 1)


def test_a_null_unknown_flag_means_unknown_only_when_every_token_count_is_zero(db: Db) -> None:
    db.job("empty", "1", "build", "claude")
    db.job("counted", "2", "build", "claude")
    db.telemetry("empty", "claude", tokens_unknown=None)
    db.telemetry("counted", "claude", tokens_in=10, tokens_out=4, tokens_unknown=None)

    row = _only(harness_report.rows(db.path), "build")

    assert (row.tokens_in, row.tokens_out) == (Measure(10.0, 1), Measure(4.0, 1))


def test_a_telemetry_table_without_the_unknown_column_judges_tokens_by_their_counts(
    tmp_path: Path,
) -> None:
    db = Db(tmp_path / "orch.db", tokens_unknown_column=False)
    db.job("empty", "1", "build", "claude")
    db.job("counted", "2", "build", "claude")
    db.telemetry("empty", "claude")
    db.telemetry("counted", "claude", tokens_in=10)

    assert _only(harness_report.rows(db.path), "build").tokens_in == Measure(10.0, 1)


def _report(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = cli.main(["harness-report", *argv])
    return code, capsys.readouterr().out


def _body(table: str) -> list[list[str]]:
    lines = table.split("\n\n")[0].splitlines()
    return [re.split(r" {2,}", line.strip()) for line in lines[1:]]


def test_harness_report_prints_the_json_rows_as_a_table(
    db: Db, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_claude_codex_and_gemini(db)
    db.telemetry("t2-build", "gemini", elapsed_seconds=120, tokens_in=1000, cost_usd=0.25)

    code, out = _report(capsys, "--db", str(db.path), "--json")
    assert code == 0
    rows = json.loads(out)
    assert rows == [asdict(row) for row in harness_report.rows(db.path)]

    code, table = _report(capsys, "--db", str(db.path))
    assert code == 0
    header = table.splitlines()[0].split()
    body = _body(table)
    assert len(body) == len(rows)
    for cells, row in zip(body, rows):
        assert len(cells) == len(header)
        cell = dict(zip(header, cells))
        expected = (
            row["role"], row["harness"], row["model"] or "-", row["risk"] or "-",
            "yes" if row["plan_first"] else "no", row["mark"], str(row["tickets"]),
            str(row["jobs"]),
        )
        shown = tuple(cell[name] for name in (
            "role", "harness", "model", "risk", "plan-first", "mark", "tickets", "jobs"
        ))
        assert shown == expected
    by_harness = {(c[0], c[1]): dict(zip(header, c)) for c in body}
    assert by_harness[("build", "gemini")]["cost-usd"] == "0.25 (1)"
    assert by_harness[("build", "gemini")]["minutes"] == "2.00 (1)"
    assert by_harness[("plan", "claude")]["cost-usd"] == "-"
    assert by_harness[("plan", "claude")]["verdicts"] == "A1 R1 B1"
    assert "first-push CI is approximate" in table


def test_harness_report_with_no_jobs_still_prints_the_header_and_footer(
    db: Db, capsys: pytest.CaptureFixture[str]
) -> None:
    code, table = _report(capsys, "--db", str(db.path))

    assert code == 0
    assert table.splitlines()[0].split()[0] == "role"
    assert _body(table) == [["no jobs ran in this window"]]
    assert "first-push CI is approximate" in table


def test_harness_report_filters_by_since_and_repo(
    db: Db, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_claude_codex_and_gemini(db)
    db.job("elsewhere", "9", "build", "claude", at=400, repo="Owner/Other")

    _, out = _report(capsys, "--db", str(db.path), "--json", "--since", "1970-01-01T00:03:20Z")
    assert json.loads(out) == [
        asdict(row) for row in harness_report.rows(db.path, since=200)
    ]
    assert {(r["role"], r["harness"]) for r in json.loads(out)} == {
        ("plan", "claude"), ("build", "codex"), ("build", "gemini"),
        ("review", "claude"), ("review", "codex"), ("build", "claude"),
    }
    assert all(r["jobs"] == 1 for r in json.loads(out))

    _, out = _report(capsys, "--db", str(db.path), "--json", "--repo", "owner/other")
    assert [(r["role"], r["harness"]) for r in json.loads(out)] == [("build", "claude")]


@pytest.mark.parametrize(
    "since",
    ["2026-11-27", "2026-11-27T00:00:00", "2026-1-5T0:0:0Z", "2026-11-27T00:00:00+00:00",
     "2026-13-01T00:00:00Z", "nonsense"],
)
def test_harness_report_rejects_a_bad_since(
    db: Db, capsys: pytest.CaptureFixture[str], since: str
) -> None:
    assert cli.main(["harness-report", "--db", str(db.path), "--since", since]) == 2
    assert "orch harness-report: --since" in capsys.readouterr().err


def test_harness_report_without_a_database_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.db"
    assert cli.main(["harness-report", "--db", str(missing)]) == 2
    assert "orch harness-report: " in capsys.readouterr().err
    assert not missing.exists()

    monkeypatch.delenv("ORCH_DB", raising=False)
    assert cli.main(["harness-report"]) == 2
    assert "no dispatcher database" in capsys.readouterr().err

    monkeypatch.setenv("ORCH_DB", "")
    assert cli.main(["harness-report"]) == 2
    assert "no dispatcher database" in capsys.readouterr().err


def test_harness_report_reads_without_writing_or_starting_a_job(
    db: Db, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_claude_codex_and_gemini(db)
    db.telemetry("t2-build", "gemini", cost_usd=0.25)
    with sqlite3.connect(db.path) as connection:
        jobs = connection.execute("SELECT count(*) FROM jobs").fetchone()
    connection.close()
    opened: list[str] = []
    real = sqlite3.connect

    def spy(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        opened.append(str(database))
        return real(database, *args, **kwargs)

    def started(*args: object, **kwargs: object) -> None:
        pytest.fail("harness-report started a process")

    monkeypatch.setattr(sqlite3, "connect", spy)
    monkeypatch.setattr(subprocess, "run", started)
    monkeypatch.setattr(subprocess, "Popen", started)
    monkeypatch.setattr(os, "fork", started)
    gc.collect()
    before = db.path.read_bytes()

    assert cli.main(["harness-report", "--db", str(db.path)]) == 0
    assert cli.main(["harness-report", "--db", str(db.path), "--json"]) == 0

    monkeypatch.setattr(sqlite3, "connect", real)
    assert opened and all(name.endswith("?mode=ro") for name in opened)
    assert db.path.read_bytes() == before
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == jobs
    connection.close()
    capsys.readouterr()


def test_usage_documents_harness_report() -> None:
    assert "\n  harness-report   " in cli.USAGE
    section = cli.USAGE.split("\nharness-report [", 1)[1]
    for text in ("--since", "YYYY-MM-DDTHH:MM:SSZ", "--json"):
        assert text in section
