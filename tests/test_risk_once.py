"""Issue #195: a ticket's risk is read once at intake and never silently low."""

import base64
import hashlib
import json
import sqlite3
import subprocess
from difflib import unified_diff
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from lib import cli
from lib.dispatcher import Comment, Dispatcher, TicketRef
from lib.github_actions import GhResult
from lib.intake_risk import INTAKE_RISK_SCHEMA
from lib.poll_source import LINEAR_ISSUE_QUERY, HTTPResult, IntakeRisks, LivePollSource, _risk
from tests.test_live_poll import FACTORY, FakeGh, config, linear_issue
from tests.test_live_poll_no_webhook import _dispatcher

TRUSTED = {
    "github": {"grok-login": frozenset({"grok-bot"}), "claude-login": frozenset({"claude-bot"})},
    "linear": {},
}
BRIEF_LOW = "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=low -->\n"
GO_LOW = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building risk=low -->\n"
GO_HIGH = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building risk=high -->\n"


def said(author: str, body: str, number: int, *, edited: bool = False) -> Comment:
    return Comment(
        id=str(number),
        author=author,
        body=body,
        created_at=number,
        edited_at=number + 0.5 if edited else None,
    )


def cap_over_stored_low(ticket) -> int:
    connection = sqlite3.connect(":memory:")
    connection.execute(INTAKE_RISK_SCHEMA)
    connection.execute(
        "INSERT INTO intake_risk VALUES (?, ?, 'low')", (ticket.repo.lower(), ticket.ticket_id)
    )
    return Dispatcher._config_cap(None, ticket, connection)


def test_a_later_trusted_risk_high_does_not_move_a_stored_low() -> None:
    comments = (said("grok-login", BRIEF_LOW, 1), said("grok-login", GO_HIGH, 2))
    assert _risk(comments, TRUSTED) == "low"


def test_the_first_trusted_go_sets_the_risk_when_no_brief_carries_one() -> None:
    comments = (
        said("grok-login", "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator -->\n", 1),
        said("grok-login", GO_LOW, 2),
        said("grok-login", GO_HIGH, 3),
    )
    assert _risk(comments, TRUSTED) == "low"


@pytest.mark.parametrize(
    "body",
    [
        "<!-- orch:v1 kind=ready from=claude-bot role=builder head=abc1234 risk=low -->\n",
        "<!-- orch:v1 kind=go from=claude-bot role=orchestrator state=building risk=low -->\n",
        "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator risk=low -->\n",
    ],
)
def test_only_an_orchestrator_brief_or_go_sets_the_risk(body: str) -> None:
    login = "claude-login" if "claude-bot" in body else "grok-login"
    assert _risk((said(login, body, 1),), TRUSTED) == "high"


def test_an_untrusted_or_edited_risk_comment_is_not_the_stored_one() -> None:
    assert _risk((said("stranger", BRIEF_LOW, 1),), TRUSTED) == "high"
    assert _risk((said("grok-login", BRIEF_LOW, 1, edited=True),), TRUSTED) == "high"
    assert _risk(
        (said("stranger", GO_HIGH, 1), said("grok-login", GO_LOW, 2), said("grok-login", GO_HIGH, 3)),
        TRUSTED,
    ) == "low"


def test_unknown_risk_is_high() -> None:
    assert _risk((), TRUSTED) == "high"
    assert _risk((said("grok-login", "plain words risk=low", 1),), TRUSTED) == "high"


def test_an_edited_first_header_is_unknown_not_the_next_header() -> None:
    comments = (said("grok-login", BRIEF_LOW, 1, edited=True), said("grok-login", GO_LOW, 2))
    assert _risk(comments, TRUSTED) == "high"


def github_gh(*bodies: str) -> FakeGh:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.comments = [
        {
            "id": number,
            "user": {"login": "grok-login"},
            "body": body,
            "created_at": f"2026-11-11T12:00:{number:02d}Z",
            "html_url": f"https://github.test/comment/{number}",
        }
        for number, body in enumerate(bodies, start=1)
    ]
    return gh


GITHUB_REF = (TicketRef("acme/widgets", "12", "github"),)


def test_github_ticket_keeps_its_intake_risk() -> None:
    source = LivePollSource(config(), gh_runner=github_gh(BRIEF_LOW, GO_HIGH))
    (ticket,) = source.tracked_tickets(GITHUB_REF)
    assert ticket.risk == "low"


@pytest.mark.parametrize(
    ("first", "edit", "expected"),
    [
        (BRIEF_LOW, "Clarified acceptance criteria.", "low"),
        (BRIEF_LOW, "", "low"),
        (GO_HIGH, "", "high"),
    ],
)
def test_editing_the_intake_comment_never_moves_the_stored_risk(
    first: str, edit: str, expected: str
) -> None:
    later = GO_HIGH if expected == "low" else GO_LOW
    gh = github_gh(first, later)
    source = LivePollSource(config(), gh_runner=gh)
    (before,) = source.tracked_tickets(GITHUB_REF)
    gh.comments[0]["body"] = first.replace(f"risk={expected}", "") + edit
    gh.comments[0]["updated_at"] = "2026-11-11T12:01:00Z"
    source.begin_cycle()
    (after,) = source.tracked_tickets(GITHUB_REF)
    assert (before.risk, after.risk) == (expected, expected)
    assert cap_over_stored_low(after) == cap_over_stored_low(before)


def test_the_stored_risk_survives_a_poller_restart(tmp_path) -> None:
    path = tmp_path / "orch.db"
    first = LivePollSource(config(), gh_runner=github_gh(BRIEF_LOW, GO_HIGH), risks=IntakeRisks(path))
    assert first.tracked_tickets(GITHUB_REF)[0].risk == "low"
    gh = github_gh(GO_HIGH)
    restarted = LivePollSource(config(), gh_runner=gh, risks=IntakeRisks(path))
    assert restarted.tracked_tickets(GITHUB_REF)[0].risk == "low"


def test_intake_risks_are_raised_but_never_lowered(tmp_path) -> None:
    risks = IntakeRisks(tmp_path / "orch.db")
    assert risks.get("acme/widgets", "12") is None
    assert risks.keep("Acme/Widgets", "12", "low") == "low"
    assert risks.keep("acme/widgets", "12", "low") == "low"
    assert risks.keep("acme/widgets", "12", "high") == "high"
    assert risks.keep("acme/widgets", "12", "low") == "high"
    assert IntakeRisks(tmp_path / "orch.db").get("acme/widgets", "12") == "high"


class FilesGh(FakeGh):
    def __init__(self, files: list | None) -> None:
        super().__init__(FACTORY.format(tickets="linear", ship="handoff"))
        self.files = files
        self.blobs = {
            item["sha"]: item["source"]
            for item in files or ()
            if isinstance(item, dict) and "source" in item
        }

    def __call__(self, argv, timeout):
        endpoint = next(part for part in argv if part.startswith(("repos/", "search/")))
        if "/files?" in endpoint:
            self.calls.append(list(argv))
            if self.files is None:
                return GhResult(1, "", "failed (HTTP 502)")
            entries = [
                {"filename": item, "changes": 1, "patch": "@@ -0,0 +1 @@\n+export {};"}
                if isinstance(item, str)
                else {key: value for key, value in item.items() if key != "source"}
                for item in self.files
            ]
            return GhResult(0, json.dumps(entries), "")
        if "/git/blobs/" in endpoint:
            self.calls.append(list(argv))
            source = self.blobs.get(endpoint.rsplit("/", 1)[1])
            if source is None:
                return GhResult(1, "", "failed (HTTP 404)")
            encoded = base64.b64encode(source.encode()).decode()
            lines = "\n".join(encoded[at : at + 60] for at in range(0, len(encoded), 60))
            return GhResult(0, json.dumps({"encoding": "base64", "content": lines}), "")
        return super().__call__(argv, timeout)


def linear_ticket(
    *,
    labels: list[str] | None = None,
    files: list[str] | None = (),
    linked: bool = True,
    go: str = GO_LOW,
    errors: list | None = None,
    risks: IntakeRisks | None = None,
):
    issue = linear_issue()
    issue["comments"]["nodes"][0]["body"] = go
    if labels is not None:
        issue["labels"] = {
            "nodes": [{"name": name} for name in labels],
            "pageInfo": {"hasNextPage": False},
        }
    gh = FilesGh(None if files is None else list(files))
    if not linked:
        gh.branch_pulls = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        return HTTPResult(200, json.dumps({"data": {"issue": issue}}))

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=transport,
        error_sink=None if errors is None else lambda *item: errors.append(item),
        risks=risks,
    )
    (ticket,) = source.tracked_tickets((TicketRef("acme/widgets", "AUT-42", "linear"),))
    return ticket, gh


def test_linear_issue_query_reads_the_issue_labels() -> None:
    assert "labels(first: 50) { nodes { name } pageInfo { hasNextPage } }" in LINEAR_ISSUE_QUERY


def test_a_linear_low_ticket_with_no_risk_signal_stays_low() -> None:
    ticket, _ = linear_ticket(labels=["bug"], files=["src/app.ts"])
    assert ticket.risk == "low"


def test_a_linear_migrations_ticket_is_high_without_a_risk_high_header() -> None:
    ticket, _ = linear_ticket(labels=["Migrations"], files=["src/app.ts"])
    assert ticket.risk == "high"


@pytest.mark.parametrize(
    "path",
    [
        "supabase/migrations/20261114_add_table.sql",
        "db/migration_0042.py",
        "supabase/realtime/tables.sql",
    ],
)
def test_a_linear_migration_or_realtime_change_is_high(path: str) -> None:
    ticket, _ = linear_ticket(labels=[], files=["src/app.ts", path])
    assert ticket.risk == "high"


def test_a_linear_ticket_whose_labels_are_unread_is_high() -> None:
    ticket, _ = linear_ticket(labels=None, files=[])
    assert ticket.risk == "high"


def test_a_linear_ticket_whose_changed_files_are_unread_is_high() -> None:
    errors: list = []
    ticket, _ = linear_ticket(labels=[], files=None, errors=errors)
    assert ticket.risk == "high"
    assert [(repo, ticket_id) for repo, ticket_id, _ in errors] == [("acme/widgets", "AUT-42")]


def test_a_linear_ticket_with_no_pr_yet_is_judged_on_its_labels() -> None:
    assert linear_ticket(labels=[], linked=False)[0].risk == "low"
    assert linear_ticket(labels=["migrations"], linked=False)[0].risk == "high"


def test_a_linear_high_ticket_reads_no_changed_files() -> None:
    ticket, gh = linear_ticket(labels=[], files=["src/app.ts"], go=GO_HIGH)
    assert ticket.risk == "high"
    assert not any("/files?" in part for call in gh.calls for part in call)


def realtime_patch(line: str) -> dict:
    return {"filename": "db/schema.sql", "changes": 1, "patch": f"@@ -0,0 +1 @@\n{line}"}


@pytest.mark.parametrize(
    "line",
    [
        "+ALTER PUBLICATION supabase_realtime ADD TABLE public.messages;",
        "-alter publication supabase_realtime drop table public.messages;",
        "+ALTER TABLE public.messages REPLICA IDENTITY FULL;",
    ],
)
def test_a_linear_realtime_table_change_outside_a_realtime_path_is_high(line: str) -> None:
    ticket, _ = linear_ticket(labels=[], files=["src/app.ts", realtime_patch(line)])
    assert ticket.risk == "high"
    assert cap_over_stored_low(ticket) == 3


def test_a_realtime_word_only_in_unchanged_sql_context_fails_high() -> None:
    item = realtime_patch(" -- realtime notes\n+SELECT 1;")
    assert linear_ticket(labels=[], files=[item])[0].risk == "high"


def with_source(item: dict, source: str) -> dict:
    return {**item, "sha": hashlib.sha1(source.encode()).hexdigest(), "source": source}


def sql_change(
    before: str, after: str, path: str = "db/schema.sql", *, source: bool = False
) -> dict:
    patch = "".join(
        unified_diff(
            before.splitlines(True), after.splitlines(True), fromfile=f"a/{path}", tofile=f"b/{path}"
        )
    )
    item = {"filename": path, "changes": 2, "patch": patch}
    return with_source(item, after) if source else item


TABLES = "".join(f"    public.t{n},\n" for n in range(12))
STATEMENTS = "".join(f"SELECT {n};\n" for n in range(12))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "ALTER PUBLICATION supabase_realtime ADD TABLE\n    public.messages;\n",
            "ALTER PUBLICATION supabase_realtime ADD TABLE\n    public.messages, public.events;\n",
        ),
        (
            "alter publication supabase_realtime\n  add table\n    public.messages;\n",
            "alter publication supabase_realtime\n  add table\n    public.events;\n",
        ),
        (
            "ALTER TABLE public.messages\n  REPLICA IDENTITY DEFAULT;\n",
            "ALTER TABLE public.events\n  REPLICA IDENTITY DEFAULT;\n",
        ),
        (
            "CREATE TABLE a (id int);\nALTER PUBLICATION supabase_realtime ADD TABLE a, b;\n",
            "CREATE TABLE a (id int);\nALTER PUBLICATION supabase_realtime ADD TABLE a,\n  c; SELECT 1;\n",
        ),
        (
            f"ALTER PUBLICATION supabase_realtime ADD TABLE\n{TABLES}    public.last;\n",
            f"ALTER PUBLICATION supabase_realtime ADD TABLE\n{TABLES}    public.last,\n    public.events;\n",
        ),
        (
            f"CREATE TABLE a (\n{TABLES}    id int\n);\n",
            f"CREATE TABLE a (\n{TABLES}    id bigint\n);\n",
        ),
        ("SELECT\n  1\n", "SELECT\n  2\n"),
        (
            "ALTER PUBLICATION supabase_realtime ADD TABLE /* existing members; more below */\n"
            "    public.messages;\n",
            "ALTER PUBLICATION supabase_realtime ADD TABLE /* existing members; more below */\n"
            "    public.messages, public.events;\n",
        ),
        (
            "ALTER PUBLICATION supabase_realtime ADD TABLE /*\n  members; more\n  below */\n"
            "    public.messages;\n",
            "ALTER PUBLICATION supabase_realtime ADD TABLE /*\n  members; more\n  below */\n"
            "    public.messages, public.events;\n",
        ),
        (
            "ALTER PUBLICATION supabase_realtime ADD TABLE /* outer /* inner; */ still; */\n"
            "    public.messages;\n",
            "ALTER PUBLICATION supabase_realtime ADD TABLE /* outer /* inner; */ still; */\n"
            "    public.events;\n",
        ),
        (
            "ALTER PUBLICATION supabase_realtime ADD TABLE -- members; more below\n"
            "    public.messages;\n",
            "ALTER PUBLICATION supabase_realtime ADD TABLE -- members; more below\n"
            "    public.messages, public.events;\n",
        ),
        (
            "ALTER PUBLICATION supabase_realtime SET (publish = 'insert;\n  update');\n",
            "ALTER PUBLICATION supabase_realtime SET (publish = 'insert;\n  update, delete');\n",
        ),
        (
            "ALTER PUBLICATION supabase_realtime ADD TABLE \"odd;name\",\n    public.messages;\n",
            "ALTER PUBLICATION supabase_realtime ADD TABLE \"odd;name\",\n    public.events;\n",
        ),
        (
            "DO $$ BEGIN EXECUTE 'ALTER PUBLICATION supabase_realtime ADD TABLE ' ||\n"
            "  'public.messages'; END $$;\n",
            "DO $$ BEGIN EXECUTE 'ALTER PUBLICATION supabase_realtime ADD TABLE ' ||\n"
            "  'public.events'; END $$;\n",
        ),
        (
            "DO $body$ BEGIN\n  EXECUTE format($q$ALTER PUBLICATION supabase_realtime ADD TABLE %s;$q$,\n"
            "    'public.messages');\nEND $body$;\n",
            "DO $body$ BEGIN\n  EXECUTE format($q$ALTER PUBLICATION supabase_realtime ADD TABLE %s;$q$,\n"
            "    'public.events');\nEND $body$;\n",
        ),
        (
            f"{STATEMENTS}ALTER PUBLICATION supabase_realtime ADD TABLE /*\n  a\n  b; c\n  d\n  e\n*/ public.messages;\n",
            f"{STATEMENTS}ALTER PUBLICATION supabase_realtime ADD TABLE /*\n  a\n  b; c\n  d\n  e\n*/ public.events;\n",
        ),
        (
            f"{STATEMENTS}CREATE FUNCTION f() RETURNS void AS $$\nBEGIN\n"
            "  ALTER PUBLICATION supabase_realtime ADD TABLE public.a;\n"
            "  SELECT 1;\n  SELECT 2;\n  SELECT 3;\n  SELECT 4;\nEND $$ LANGUAGE plpgsql;\n",
            f"{STATEMENTS}CREATE FUNCTION f() RETURNS void AS $$\nBEGIN\n"
            "  ALTER PUBLICATION supabase_realtime ADD TABLE public.a;\n"
            "  SELECT 1;\n  SELECT 2;\n  SELECT 3;\n  SELECT 5;\nEND $$ LANGUAGE plpgsql;\n",
        ),
        (
            f"{STATEMENTS}ALTER PUBLICATION supabase_realtime ADD TABLE public.a, '\n  x\n  y; z\n  w\n  v\n', public.messages;\n",
            f"{STATEMENTS}ALTER PUBLICATION supabase_realtime ADD TABLE public.a, '\n  x\n  y; z\n  w\n  v\n', public.events;\n",
        ),
    ],
    ids=[
        "keyword-line-is-context",
        "keyword-two-lines-up",
        "replica-identity-trailing-context",
        "statement-split-mid-line",
        "statement-head-outside-the-hunk",
        "unreadable-statement-head",
        "unterminated-statement",
        "semicolon-in-block-comment",
        "semicolon-in-multiline-block-comment",
        "semicolon-in-nested-block-comment",
        "semicolon-in-line-comment",
        "semicolon-in-string-literal",
        "semicolon-in-quoted-identifier",
        "publication-in-dynamic-sql-string",
        "semicolon-in-dollar-quoted-body",
        "hunk-starts-inside-a-block-comment",
        "hunk-starts-inside-a-dollar-quoted-body",
        "hunk-starts-inside-a-string-literal",
    ],
)
def test_a_sql_change_inside_a_realtime_or_unclassifiable_statement_is_high(
    before: str, after: str
) -> None:
    ticket, _ = linear_ticket(labels=[], files=[sql_change(before, after)])
    assert ticket.risk == "high"
    assert cap_over_stored_low(ticket) == 3


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "ALTER PUBLICATION supabase_realtime ADD TABLE public.messages;\n"
            "CREATE TABLE a (\n  id int\n);\n",
            "ALTER PUBLICATION supabase_realtime ADD TABLE public.messages;\n"
            "CREATE TABLE a (\n  id bigint\n);\n",
        ),
        (
            "-- realtime tables live elsewhere; see docs\nCREATE TABLE a (\n  id int\n);\n",
            "-- realtime tables live elsewhere; see docs\nCREATE TABLE a (\n  id bigint\n);\n",
        ),
        (
            "CREATE TABLE a ( -- not realtime; plain\n  id int\n);\n",
            "CREATE TABLE a ( -- not realtime; plain\n  id bigint\n);\n",
        ),
        (
            "/*\nALTER PUBLICATION supabase_realtime ADD TABLE public.events;\n*/\n",
            "ALTER PUBLICATION supabase_realtime ADD TABLE public.events;\n",
        ),
        (
            "ALTER PUBLICATION supabase_realtime ADD TABLE /*\n"
            "  existing\n  members\n  begin\n  comment;\n"
            " -- */ public.messages,\n    public.events;\n",
            "ALTER PUBLICATION supabase_realtime ADD TABLE /*\n"
            "  existing\n  members\n  begin\n  comment;\n"
            " -- */ public.messages,\n    public.new_events;\n",
        ),
        (
            "/* realtime; publication notes */ CREATE TABLE a (\n  id int\n);\n",
            "/* realtime; publication notes */ CREATE TABLE a (\n  id bigint\n);\n",
        ),
        (
            "ALTER PUBLICATION supabase_realtime ADD TABLE public.messages;\n"
            "CREATE TABLE a (\n  id int\n);\n/* a long note\n  that goes on\n",
            "ALTER PUBLICATION supabase_realtime ADD TABLE public.messages;\n"
            "CREATE TABLE a (\n  id bigint\n);\n/* a long note\n  that goes on\n",
        ),
        (
            f"{STATEMENTS}/*\nSELECT 1;\n*/\n{STATEMENTS}",
            f"{STATEMENTS}SELECT 1;\n{STATEMENTS}",
        ),
        (
            f"{STATEMENTS}SELECT 1;\n{STATEMENTS}",
            f"{STATEMENTS}/*\nSELECT 1;\n{STATEMENTS}",
        ),
        (
            f"/*\n{STATEMENTS}  note one\n  note two;\n*/ SELECT 1;\n",
            f"/*\n{STATEMENTS}  note one\n  note three;\n*/ SELECT 1;\n",
        ),
        (
            f"/*\n{STATEMENTS}  a\n  b; -- */ c\n  d\n*/ SELECT 1;\n",
            f"/*\n{STATEMENTS}  a\n  b; -- */ c\n  e\n*/ SELECT 1;\n",
        ),
    ],
    ids=[
        "next-to-a-realtime-statement",
        "after-a-realtime-comment",
        "realtime-in-a-line-comment-inside-the-statement",
        "uncommented-publication",
        "hunk-starts-inside-a-comment-near-a-publication",
        "realtime-in-a-block-comment-before-the-statement",
        "hunk-ending-inside-a-comment-near-a-publication",
        "removed-comment-delimiters",
        "added-comment-opener",
        "hunk-starts-inside-a-block-comment",
        "line-comment-inside-a-block-comment",
    ],
)
def test_a_sql_change_near_a_publication_or_in_an_unparsed_comment_fails_high(
    before: str, after: str
) -> None:
    ticket, _ = linear_ticket(labels=[], files=[sql_change(before, after)])
    assert ticket.risk == "high"
    assert cap_over_stored_low(ticket) == 3


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            f"ALTER PUBLICATION supabase_realtime ADD TABLE a;\n{STATEMENTS}CREATE TABLE b (\n  id int\n);\n",
            f"ALTER PUBLICATION supabase_realtime ADD TABLE a;\n{STATEMENTS}CREATE TABLE b (\n  id bigint\n);\n",
        ),
        (
            "INSERT INTO a VALUES ('x; y',\n  1);\n",
            "INSERT INTO a VALUES ('x; y',\n  2);\n",
        ),
    ],
    ids=[
        "statement-head-in-context",
        "semicolon-in-an-ordinary-string",
    ],
)
def test_a_sql_change_in_an_ordinary_statement_stays_low(before: str, after: str) -> None:
    ticket, _ = linear_ticket(labels=[], files=[sql_change(before, after, source=True)])
    assert ticket.risk == "low"


def git_change(before: str, after: str) -> dict:
    with TemporaryDirectory() as directory:
        old, new = Path(directory) / "old.sql", Path(directory) / "new.sql"
        old.write_bytes(before.encode())
        new.write_bytes(after.encode())
        result = subprocess.run(
            ["git", "diff", "--no-index", "--", str(old), str(new)], capture_output=True
        )
    assert result.returncode == 1, result.stderr
    return {"filename": "db/schema.sql", "changes": 2, "patch": result.stdout.decode()}


NOTES = "".join(f"  note {n};\n" for n in range(30))


@pytest.mark.parametrize(
    ("before", "header"),
    [
        (
            f"ALTER PUBLICATION supabase_realtime ADD TABLE /*\n{NOTES}*/ public.events;\n",
            "@@ ALTER PUBLICATION supabase_realtime ADD TABLE /*",
        ),
        (
            f'ALTER PUBLICATION supabase_realtime ADD TABLE "events\n{NOTES}";\n',
            '@@ ALTER PUBLICATION supabase_realtime ADD TABLE "events',
        ),
        (
            f"CREATE TABLE a ( /*\n{NOTES}*/ id int\n);\n",
            "@@ CREATE TABLE a ( /*",
        ),
    ],
    ids=[
        "publication-and-comment-opener-only-in-the-hunk-header",
        "publication-only-in-the-hunk-header",
        "comment-opener-only-in-the-hunk-header",
    ],
)
def test_sql_context_in_a_git_hunk_header_fails_high(before: str, header: str) -> None:
    item = git_change(before, before.replace("  note 15;\n", "  changed note 15;\n"))
    assert header in item["patch"]
    ticket, _ = linear_ticket(labels=[], files=[item])
    assert (ticket.risk, cap_over_stored_low(ticket)) == ("high", 3)


def test_an_ordinary_git_hunk_header_stays_low() -> None:
    before = STATEMENTS * 3
    after = before.replace("SELECT 5;\n", "SELECT 50;\n", 2)
    item = with_source(git_change(before, after), after)
    assert "@@ SELECT" in item["patch"]
    assert linear_ticket(labels=[], files=[item])[0].risk == "low"


PROBE = {
    "uncomment": (
        "/*\nALTER PUBLICATION supabase_realtime ADD TABLE public.events;\n*/\n",
        "ALTER PUBLICATION supabase_realtime ADD TABLE public.events;\n",
    ),
    "partial-comment-context": (
        "ALTER PUBLICATION supabase_realtime ADD TABLE /*\n"
        "  existing\n  members\n  begin\n  comment;\n"
        " -- */ public.messages,\n    public.events;\n",
        "ALTER PUBLICATION supabase_realtime ADD TABLE /*\n"
        "  existing\n  members\n  begin\n  comment;\n"
        " -- */ public.messages,\n    public.new_events;\n",
    ),
}
NOTE_LINES = "".join(f"-- note {n}\n" for n in range(6))
REALTIME = f"ALTER PUBLICATION supabase_realtime ADD TABLE\n{TABLES}    public.last;\n"
FILTERED = (
    "ALTER PUBLICATION supabase_realtime ADD TABLE public.a WHERE (note <> 'x\n"
    " a\n b\n c\n d\n foo;\n qux')\n"
)


@pytest.mark.parametrize("name", PROBE)
def test_codex_round_4_probe_is_high_from_the_whole_file(name: str) -> None:
    ticket, _ = linear_ticket(labels=[], files=[sql_change(*PROBE[name], source=True)])
    assert (ticket.risk, cap_over_stored_low(ticket)) == ("high", 3)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            f"{STATEMENTS}/*\n{NOTE_LINES}{REALTIME}*/\n{STATEMENTS}",
            f"{STATEMENTS}{NOTE_LINES}{REALTIME}{STATEMENTS}",
        ),
        (
            f"{STATEMENTS}{NOTE_LINES}{REALTIME}{STATEMENTS}",
            f"{STATEMENTS}/*\n{NOTE_LINES}{REALTIME}*/\n{STATEMENTS}",
        ),
        (
            f"{STATEMENTS}ALTER PUBLICATION supabase_realtime ADD TABLE /*\n{NOTE_LINES}"
            "  members begin; here\n -- */ public.messages,\n    public.events;\n",
            f"{STATEMENTS}ALTER PUBLICATION supabase_realtime ADD TABLE /*\n{NOTE_LINES}"
            "  members begin; here\n -- */ public.messages,\n    public.new_events;\n",
        ),
        (
            f"{STATEMENTS[:60]}{FILTERED} , public.events  -- don't drop\n ;\n",
            f"{STATEMENTS[:60]}{FILTERED} , public.new_events  -- don't drop\n ;\n",
        ),
        (
            f"{STATEMENTS}ALTER PUBLICATION supabase_realtime ADD TABLE\n{TABLES}"
            "    public.last /* x */,\n    public.events;\n",
            f"{STATEMENTS}ALTER PUBLICATION supabase_realtime ADD TABLE\n{TABLES}"
            "    public.last /* x\n,    public.events; */ ;\n",
        ),
        (
            f"/*\nSELECT 1;\n{NOTE_LINES}{REALTIME}{NOTE_LINES}*/\n",
            f"SELECT 1;\n{NOTE_LINES}{REALTIME}{NOTE_LINES}",
        ),
        (
            f"SELECT 1;\n{NOTE_LINES}{REALTIME}{NOTE_LINES}",
            f"/*\nSELECT 1;\n{NOTE_LINES}{REALTIME}{NOTE_LINES}*/\n",
        ),
        (
            f"SELECT 1;\n{NOTE_LINES}SELECT 'a;\n{NOTE_LINES}{REALTIME}{NOTE_LINES}';\n",
            f"SELECT 1;\n{NOTE_LINES}SELECT a;\n{NOTE_LINES}{REALTIME}{NOTE_LINES}';\n",
        ),
    ],
    ids=[
        "uncommented-publication-far-from-its-delimiters",
        "commented-out-publication-far-from-its-delimiters",
        "hunk-starts-inside-a-block-comment-with-a-line-comment-closer",
        "hunk-starts-inside-a-row-filter-string",
        "moved-comment-closer-drops-a-member",
        "uncommented-publication-after-an-ordinary-statement",
        "commented-out-publication-after-an-ordinary-statement",
        "removed-quote-exposes-a-publication-after-an-ordinary-statement",
    ],
)
def test_a_publication_change_outside_the_hunk_is_high_from_the_whole_file(
    before: str, after: str
) -> None:
    item = sql_change(before, after, source=True)
    assert "supabase_realtime" not in item["patch"]
    ticket, _ = linear_ticket(labels=[], files=[item])
    assert (ticket.risk, cap_over_stored_low(ticket)) == ("high", 3)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            f"{STATEMENTS}/*\nSELECT 1;\n*/\n{STATEMENTS}",
            f"{STATEMENTS}SELECT 1;\n{STATEMENTS}",
        ),
        (
            f"/*\n{STATEMENTS}  note one\n  note two;\n*/ SELECT 1;\n",
            f"/*\n{STATEMENTS}  note one\n  note three;\n*/ SELECT 1;\n",
        ),
        (
            f"/*\n{STATEMENTS}  a\n  b; -- */ c\n  d\n*/ SELECT 1;\n",
            f"/*\n{STATEMENTS}  a\n  b; -- */ c\n  e\n*/ SELECT 1;\n",
        ),
        (
            f"CREATE TABLE a (\n{TABLES}    id int\n);\n",
            f"CREATE TABLE a (\n{TABLES}    id bigint\n);\n",
        ),
        (
            f"INSERT INTO a VALUES ('x\n{STATEMENTS}', 1);\nSELECT 1;\n",
            f"INSERT INTO a VALUES ('x\n{STATEMENTS}', 2);\nSELECT 1;\n",
        ),
        (
            f"{REALTIME}{STATEMENTS}SELECT 1;\n",
            f"{REALTIME}{STATEMENTS}SELECT 2;\n",
        ),
    ],
    ids=[
        "removed-comment-delimiters",
        "change-inside-a-block-comment",
        "line-comment-inside-a-block-comment",
        "statement-head-outside-the-hunk",
        "hunk-starts-inside-an-ordinary-string",
        "publication-earlier-in-the-file",
    ],
)
def test_an_ordinary_change_is_low_from_the_whole_file(before: str, after: str) -> None:
    ticket, _ = linear_ticket(labels=[], files=[sql_change(before, after, source=True)])
    assert ticket.risk == "low"


def test_a_whole_file_ending_inside_a_comment_fails_high() -> None:
    before, after = f"{STATEMENTS}SELECT 1;\n{STATEMENTS}", f"{STATEMENTS}/*\nSELECT 1;\n{STATEMENTS}"
    ticket, _ = linear_ticket(labels=[], files=[sql_change(before, after, source=True)])
    assert ticket.risk == "high"


def test_a_source_that_does_not_match_the_patch_fails_high() -> None:
    before, after = f"{STATEMENTS}SELECT 1;\n", f"{STATEMENTS}SELECT 2;\n"
    item = with_source(sql_change(before, after), after.replace("SELECT 2;", "SELECT 3;"))
    assert linear_ticket(labels=[], files=[item])[0].risk == "high"


def test_an_unread_source_fails_high_and_is_recorded() -> None:
    before, after = f"{STATEMENTS}SELECT 1;\n", f"{STATEMENTS}SELECT 2;\n"
    item = {**sql_change(before, after), "sha": "f" * 40}
    errors: list = []
    ticket, gh = linear_ticket(labels=[], files=[item], errors=errors)
    assert ticket.risk == "high"
    assert any("/git/blobs/" + "f" * 40 in part for call in gh.calls for part in call)
    assert [(repo, ticket_id) for repo, ticket_id, _ in errors] == [("acme/widgets", "AUT-42")]


def test_a_patch_from_line_one_reads_no_source() -> None:
    item = sql_change("SELECT 1;\nSELECT 2;\n", "SELECT 1;\nSELECT 3;\n", source=True)
    ticket, gh = linear_ticket(labels=[], files=[item])
    assert ticket.risk == "low"
    assert not any("/git/blobs/" in part for call in gh.calls for part in call)


def test_a_non_sql_change_needs_no_statement_context() -> None:
    item = sql_change("// realtime\nconst a = 1\nconst b = 2\n", "// realtime\nconst a = 1\nconst b = 3\n", "src/app.ts")
    assert linear_ticket(labels=[], files=[item])[0].risk == "low"


def test_a_linear_change_whose_contents_are_unread_is_high() -> None:
    item = {"filename": "db/schema.sql", "changes": 4}
    assert linear_ticket(labels=[], files=["src/app.ts", item])[0].risk == "high"


def test_a_pure_rename_needs_no_patch() -> None:
    item = {"filename": "src/new.ts", "previous_filename": "src/old.ts", "changes": 0}
    assert linear_ticket(labels=[], files=[item])[0].risk == "low"


def test_an_edited_linear_intake_header_cannot_lower_a_stored_high(tmp_path) -> None:
    risks = IntakeRisks(tmp_path / "orch.db")
    assert linear_ticket(labels=[], files=["src/app.ts"], go=GO_HIGH, risks=risks)[0].risk == "high"
    reworded, _ = linear_ticket(labels=[], files=["src/app.ts"], go=GO_LOW, risks=risks)
    assert reworded.risk == "high"


def test_a_definite_linear_raise_is_stored_but_an_unread_one_is_not(tmp_path) -> None:
    risks = IntakeRisks(tmp_path / "orch.db")
    assert linear_ticket(labels=[], files=None, risks=risks)[0].risk == "high"
    assert linear_ticket(labels=[], files=["src/app.ts"], risks=risks)[0].risk == "low"
    assert linear_ticket(labels=["migrations"], files=["src/app.ts"], risks=risks)[0].risk == "high"
    assert linear_ticket(labels=[], files=["src/app.ts"], risks=risks)[0].risk == "high"


def test_the_poller_runtime_keeps_risks_in_its_database(tmp_path) -> None:
    database = tmp_path / "orch.db"
    _dispatcher(database)
    orch = tmp_path / "orch.yml"
    orch.write_text(
        "poll_seconds: 60\n"
        "grok:\n  webhook_url_env: U\n  webhook_key_env: K\n"
        "trusted_logins:\n  grok-login: [grok-bot]\n"
        "projects:\n  - repo: acme/widgets\n    factory_ref: main\n",
        encoding="utf-8",
    )
    _, first = cli._runtime(database, orch, create=False)
    first.risks.keep("acme/widgets", "12", "low")
    _, restarted = cli._runtime(database, orch, create=False)
    assert restarted.risks.get("acme/widgets", "12") == "low"


def dollar_quote_cases(tag: str) -> dict[str, tuple[str, str]]:
    quoted = f"SELECT ${tag}$/*${tag}$;\n", f"SELECT ${tag}$*/${tag}$;\n"
    active = f"{quoted[0]}{NOTE_LINES}{REALTIME}{NOTE_LINES}{quoted[1]}"
    member = (
        f"{quoted[0]}ALTER PUBLICATION supabase_realtime ADD TABLE /*\n{NOTE_LINES}"
        f"  comment;\n -- */ public.messages,\n    public.events;\n{quoted[1]}"
    )
    return {
        "activate": (f"/*\n{active}*/\n", active),
        "deactivate": (active, f"/*\n{active}*/\n"),
        "partial-comment": (member, member.replace("public.events;", "public.new_events;")),
    }


@pytest.mark.parametrize("case", ["activate", "deactivate", "partial-comment"])
@pytest.mark.parametrize("tag", ["é", "aé", "名前", "_😀1", "x́", "ß_2"])
def test_a_non_ascii_dollar_quote_tag_hides_no_comment_delimiter(tag: str, case: str) -> None:
    item = sql_change(*dollar_quote_cases(tag)[case], source=True)
    assert "supabase_realtime" not in item["patch"]
    ticket, _ = linear_ticket(labels=[], files=[item])
    assert (ticket.risk, cap_over_stored_low(ticket)) == ("high", 3)


ESCAPE_CONTINUED = f"SELECT E'a'\n'\\'/*';\n{NOTE_LINES}{REALTIME}{NOTE_LINES}-- */\n"
CR_MEMBER = (
    " SELECT 1; -- prefix\rALTER PUBLICATION supabase_realtime ADD TABLE /*\n"
    f"{NOTE_LINES} comment;\n -- */ public.messages,\n    public.events;\n"
)
CR_IN_COMMENT = f" SELECT 1; /* a\r*/ {REALTIME}{NOTE_LINES} SELECT 1;\n"


def git_source_change(before: str, after: str) -> dict:
    return with_source(git_change(before, after), after)


@pytest.mark.parametrize(
    "item",
    [
        sql_change(f"/*\n{ESCAPE_CONTINUED}*/\n", ESCAPE_CONTINUED, source=True),
        sql_change(ESCAPE_CONTINUED, f"/*\n{ESCAPE_CONTINUED}*/\n", source=True),
        sql_change(
            f"/*\n{ESCAPE_CONTINUED.replace('E', 'e', 1)}*/\n",
            ESCAPE_CONTINUED.replace("E", "e", 1),
            source=True,
        ),
        git_source_change(CR_MEMBER, CR_MEMBER.replace("public.events;", "public.new_events;")),
        git_source_change(CR_IN_COMMENT, CR_IN_COMMENT.replace("SELECT 1;\n", "SELECT 2;\n")),
    ],
    ids=[
        "activate-escape-continuation",
        "deactivate-escape-continuation",
        "lowercase-escape-continuation",
        "partial-comment-after-a-bare-carriage-return",
        "bare-carriage-return-anywhere-near-a-publication",
    ],
)
def test_an_escape_string_or_bare_carriage_return_near_a_publication_fails_high(
    item: dict,
) -> None:
    assert "supabase_realtime" not in item["patch"]
    ticket, _ = linear_ticket(labels=[], files=[item])
    assert (ticket.risk, cap_over_stored_low(ticket)) == ("high", 3)


CRLF_STATEMENTS = STATEMENTS.replace("\n", "\r\n")


@pytest.mark.parametrize(
    "item",
    [
        sql_change(
            f"SELECT E'a\\'b';\n{STATEMENTS}SELECT 1;\n",
            f"SELECT E'a\\'b';\n{STATEMENTS}SELECT 2;\n",
            source=True,
        ),
        git_source_change(f"{CRLF_STATEMENTS}SELECT 1;\r\n", f"{CRLF_STATEMENTS}SELECT 2;\r\n"),
    ],
    ids=["escape-string-with-no-publication", "crlf-line-endings"],
)
def test_an_escape_string_or_crlf_file_with_no_publication_stays_low(item: dict) -> None:
    ticket, _ = linear_ticket(labels=[], files=[item])
    assert ticket.risk == "low"
