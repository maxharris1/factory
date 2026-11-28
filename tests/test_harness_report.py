import gc
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lib import exposure, harness_report
from lib.harness_report import Measure, Row, VerdictMix
from lib.role_router import Draw, Hardness, Sample, sample_json

REPO = "Owner/Repo"

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
    ):
        self.path = path
        self.verdict_column = verdict_column
        with sqlite3.connect(path) as connection:
            connection.execute(JOBS_DDL)
            if exposure_table:
                connection.execute(exposure.DDL)
            if verdict_table:
                connection.execute(_verdict_heads_ddl(verdict_column))
        connection.close()

    def job(
        self,
        job_id: str,
        ticket: str,
        action: str,
        recipient: str,
        *,
        at: float = 100.0,
        repo: str = REPO,
        risk: str | None = None,
        routing: dict | None = None,
        handoff: str | None = None,
        model: str | None = None,
    ) -> None:
        detail: dict = {}
        if risk is not None:
            detail["risk"] = risk
        if routing is not None:
            detail["routing"] = routing
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, dedupe_key, repo, ticket_id, action, recipient, source_comment_id,
                    timeout_seconds, generation, attempt, detail, state, created_at,
                    last_activity_at, handoff_comment_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'c0', 7200, 1, 1, ?, 'done', ?, ?, ?)
                """,
                (job_id, job_id, repo, ticket, action, recipient, json.dumps(detail), at, at, handoff),
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
        self, ticket: str, comment: str, verdict: str | None, at: float | None, repo: str = REPO
    ) -> None:
        columns = "repo, ticket_id, comment_id, head, posted_at"
        values: tuple = (repo.lower(), ticket, comment, "abc", at)
        if self.verdict_column:
            columns += ", verdict"
            values += (verdict,)
        marks = ", ".join("?" for _ in values)
        with sqlite3.connect(self.path) as connection:
            connection.execute(f"INSERT INTO verdict_heads ({columns}) VALUES ({marks})", values)
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


def test_rows_group_claude_codex_and_gemini_jobs(db: Db) -> None:
    gemini_build = _draw("build", "gemini", True)
    db.job("t1-plan", "1", "plan", "claude", at=100, risk="low",
           routing=_routing(plan_model="claude-opus"))
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

    assert harness_report.rows(db.path) == [
        Row("plan", "claude", "claude-opus", "low", True, "default", 2, 2,
            Measure(1.0, 2), Measure(2.0, 1), VerdictMix(1, 1, 1, 3), Measure(0.5, 2)),
        Row("build", "codex", "gpt-5", "low", True, "default", 2, 3,
            Measure(2 / 3, 3), Measure(2.0, 1), VerdictMix(1, 1, 1, 3), Measure(0.5, 2)),
        Row("build", "gemini", "gemini-3", "high", False, "sampled", 1, 1,
            Measure(1 / 3, 3), Measure(1.0, 1), VerdictMix(1, 0, 0, 1), Measure(0.0, 1)),
        Row("review", "claude", "claude-opus", "low", True, "default", 2, 3,
            Measure(2 / 3, 3), Measure(2.0, 1), VerdictMix(1, 1, 1, 3), Measure(0.5, 2)),
        Row("review", "codex", "gpt-5", "high", False, "default", 1, 1,
            Measure(1 / 3, 3), Measure(1.0, 1), VerdictMix(1, 0, 0, 1), Measure(0.0, 1)),
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
            Measure(1.0, 1), Measure(None, 0), None, Measure(0.0, 1)),
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
            Measure(1.0, 1), Measure(None, 0), None, Measure(1.0, 1)),
    ]


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
    routing = _routing(plan_model="p", builder_model="b", reviewer_model="r")
    db.job("plan", "1", "plan", "claude", model=reported, routing=routing)
    db.job("fix", "1", "fix", "claude", model=reported, routing=routing)
    db.job("review", "1", "review", "claude", model=reported, routing=routing)

    assert [(row.role, row.model) for row in harness_report.rows(db.path)] == [
        ("plan", "p"), ("build", "b"), ("review", "r"),
    ]


def test_the_database_is_opened_read_only(db: Db, monkeypatch: pytest.MonkeyPatch) -> None:
    db.job("build", "1", "build", "claude", model="opus")
    db.job("review", "1", "review", "codex", handoff="c1")
    db.verdict("1", "c1", "APPROVE", 1)
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
    db = Db(tmp_path / "orch.db", exposure_table=False, verdict_table=False)
    db.job("build", "1", "build", "claude", routing=_routing(builder_model="opus"))

    assert harness_report.rows(db.path) == [
        Row("build", "claude", "opus", None, False, "default", 1, 1,
            Measure(1.0, 1), Measure(None, 0), None, Measure(0.0, 1)),
    ]


def test_a_db_with_no_tables_has_no_rows(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    sqlite3.connect(path).close()

    assert harness_report.rows(path) == []


def test_the_module_does_not_use_the_queue() -> None:
    source = Path(harness_report.__file__).read_text(encoding="utf-8")

    assert "open_queue" not in source
    assert "SQLiteJobQueue" not in source
