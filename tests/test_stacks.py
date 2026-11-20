from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from lib.queue import JobRequest, SQLiteJobQueue
from lib.stacks import (
    Container,
    Keep,
    Ledger,
    Project,
    Remove,
    TicketFacts,
    decide,
    listing_format,
    parse_listing,
    read_ledger,
    stack_name,
)

OWNER = ("aut0-code/aut0", "AUT-2129")
OTHER = ("aut0-code/aut0", "AUT-7")
NAME = "aut0-aut2129-pc"
ROOTS = ("/home/bot/factory-work", "/srv/remote/factory-work/")
BOX_JOB = "/home/bot/factory-work/jobs"
FINISH = 1000.0


def facts(*, finished=True, last=FINISH, held_on=(), live=False):
    return TicketFacts(finished, last, frozenset(held_on), live)


def ledger(tickets=None, jobs=None):
    return Ledger(
        tickets if tickets is not None else {OWNER: facts()},
        jobs if jobs is not None else {"job1": OWNER},
    )


def project(name=NAME, *, created=(FINISH - 60,), working_dir="", labelled=False):
    containers = tuple(
        Container(f"{index:012x}", at, working_dir, labelled) for index, at in enumerate(created)
    )
    return Project("compose", name, containers)


DECISIONS = [
    pytest.param(project(), ledger(), None, id="removed: tie by name"),
    pytest.param(
        project("unrelated", working_dir=f"{BOX_JOB}/job1/repo"),
        ledger(),
        None,
        id="removed: tie by working_dir under the box root",
    ),
    pytest.param(
        project("unrelated", working_dir="/srv/remote/factory-work/jobs/job1"),
        ledger(),
        None,
        id="removed: tie by working_dir under a remote root",
    ),
    pytest.param(project(created=(FINISH,)), ledger(), None, id="removed: created exactly at the finish"),
    pytest.param(
        project(), ledger({OWNER: facts(held_on={"mac"})}), None, id="removed: slot on another device"
    ),
    pytest.param(
        project(), ledger({OWNER: facts(finished=False)}), "unfinished", id="kept: unfinished job"
    ),
    pytest.param(
        project(), ledger({OWNER: facts(held_on={"pc"})}), "slot on pc", id="kept: slot on this device"
    ),
    pytest.param(project(), ledger({OWNER: facts(live=True)}), "live run", id="kept: live run"),
    pytest.param(project(), ledger({OWNER: facts(last=None)}), "no finish", id="kept: no finished_at"),
    pytest.param(project(created=(FINISH + 0.5,)), ledger(), "newer", id="kept: newer than the finish"),
    pytest.param(
        project(created=(FINISH - 60, FINISH + 1)), ledger(), "newer", id="kept: one container newer"
    ),
    pytest.param(project("someone-elses-stack"), ledger(), "no ticket", id="kept: no owner"),
    pytest.param(
        project(),
        ledger({("a/aut0", "AUT-2129"): facts(), ("b/aut0", "AUT-2129"): facts()}, {}),
        "2 tickets",
        id="kept: two tickets give the same name",
    ),
    pytest.param(
        project(working_dir=f"{BOX_JOB}/job7"),
        ledger({OWNER: facts(), OTHER: facts()}, {"job1": OWNER, "job7": OTHER}),
        "2 tickets",
        id="kept: working_dir and name tie to different tickets",
    ),
    pytest.param(
        project("unrelated", working_dir=f"{BOX_JOB}/job"),
        ledger(),
        "no ticket",
        id="kept: jobs/job is not job1",
    ),
    pytest.param(
        project("unrelated", working_dir=f"{BOX_JOB}/job12/repo"),
        ledger(),
        "no ticket",
        id="kept: jobs/job12 is not job1",
    ),
    pytest.param(
        project("unrelated", working_dir="/home/bot/factory-workx/jobs/job1"),
        ledger(),
        "no ticket",
        id="kept: root is a prefix, not a parent",
    ),
    pytest.param(
        project("unrelated", working_dir="/home/bot/factory-work/tmp/job1"),
        ledger(),
        "no ticket",
        id="kept: job id outside jobs/",
    ),
    pytest.param(
        project("unrelated", working_dir="/home/bot/factory-work/tmp/../jobs/job1"),
        ledger(),
        "no ticket",
        id="kept: working_dir with ..",
    ),
    pytest.param(
        project("unrelated", working_dir="home/bot/factory-work/jobs/job1"),
        ledger(),
        "no ticket",
        id="kept: relative working_dir",
    ),
    pytest.param(project("$(reboot)"), ledger(), "name", id="kept: hostile name"),
    pytest.param(project(NAME.upper()), ledger(), "name", id="kept: uppercase name"),
    pytest.param(project(labelled=True), ledger(), "factory.job", id="kept: labelled container"),
]


@pytest.mark.parametrize(("stack", "facts_ledger", "kept_because"), DECISIONS)
def test_decide(stack, facts_ledger, kept_because):
    verdict = decide(stack, facts_ledger, "pc", ROOTS)
    if kept_because is None:
        assert verdict == Remove(OWNER)
    else:
        assert isinstance(verdict, Keep)
        assert kept_because in verdict.reason


@pytest.mark.parametrize(
    ("repo", "ticket", "device", "name"),
    [
        ("aut0-code/aut0", "AUT-2129", "pc", "aut0-aut2129-pc"),
        ("aut0", "AUT-2129", "pc", "aut0-aut2129-pc"),
        ("org/My_App.web", "ENG-12", "Mac_Mini-2", "myappweb-eng12-macmini2"),
    ],
)
def test_stack_name(repo, ticket, device, name):
    assert stack_name(repo, ticket, device) == name


def epoch(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def line(created, *, id_="0123456789ab", name=NAME, working_dir="/w", job=""):
    return "\t".join((id_, name, working_dir, job, created))


def test_listing_format_has_the_fields_parse_listing_reads():
    fields = listing_format("supabase").split("\t")
    assert len(fields) == 5
    assert "com.supabase.cli.project" in fields[1]
    assert "factory.job" in fields[3]


@pytest.mark.parametrize(
    ("created", "at"),
    [
        ("2026-11-20 13:05:01 +0000 UTC", epoch(2026, 11, 20, 13, 5, 1)),
        ("2026-11-20 08:05:01 -0500 EST", epoch(2026, 11, 20, 13, 5, 1)),
        ("2026-11-20 13:05:01.25 +0000 UTC", epoch(2026, 11, 20, 13, 5, 1) + 0.25),
        ("2026-11-20 13:05:01.123456789 +0000 UTC", epoch(2026, 11, 20, 13, 5, 1) + 0.123456789),
    ],
)
def test_parse_listing_reads_created_at(created, at):
    projects, unreadable = parse_listing("compose", line(created) + "\n")
    assert unreadable == 0
    assert [p.name for p in projects] == [NAME]
    assert projects[0].containers[0].created == pytest.approx(at, abs=1e-6)


@pytest.mark.parametrize(
    "bad",
    [
        line("yesterday"),
        line("2026-13-20 13:05:01 +0000 UTC"),
        line("2026-11-20 13:05:01 UTC"),
        line("2026-11-20"),
        line(""),
        "0123456789ab\taut0\t/w\t2026-11-20 13:05:01 +0000 UTC",
        line("2026-11-20 13:05:01 +0000 UTC") + "\textra",
        line("2026-11-20 13:05:01 +0000 UTC", id_="0123456789AB"),
        line("2026-11-20 13:05:01 +0000 UTC", id_="$(rm -rf /)"),
        line("2026-11-20 13:05:01 +0000 UTC", name=""),
    ],
)
def test_parse_listing_counts_unreadable_lines(bad):
    good = line("2026-11-20 13:05:01 +0000 UTC", id_="fedcba987654", name="good")
    projects, unreadable = parse_listing("compose", f"{bad}\n{good}\n")
    assert unreadable == 1
    assert [(p.name, [c.id for c in p.containers]) for p in projects] == [("good", ["fedcba987654"])]


def test_parse_listing_groups_and_keeps_hostile_fields_as_data():
    created = "2026-11-20 13:05:01 +0000 UTC"
    stdout = "\n".join(
        (
            line(created, id_="bbbbbbbbbbbb", name="zeta"),
            line(created, id_="aaaaaaaaaaaa", name="$(rm -rf /)", working_dir="/w/$(reboot) x", job="j9"),
            line(created, id_="cccccccccccc", name="zeta"),
        )
    )
    projects, unreadable = parse_listing("supabase", stdout)
    assert unreadable == 0
    assert projects == [
        Project(
            "supabase",
            "$(rm -rf /)",
            (Container("aaaaaaaaaaaa", epoch(2026, 11, 20, 13, 5, 1), "/w/$(reboot) x", True),),
        ),
        Project(
            "supabase",
            "zeta",
            (
                Container("bbbbbbbbbbbb", epoch(2026, 11, 20, 13, 5, 1), "/w", False),
                Container("cccccccccccc", epoch(2026, 11, 20, 13, 5, 1), "/w", False),
            ),
        ),
    ]
    hostile = Project("supabase", "$(rm -rf /)", (Container("aaaaaaaaaaaa", 0.0, "/w", False),))
    assert isinstance(decide(hostile, ledger({("o/rm", "rf"): facts()}, {}), "pc", ROOTS), Keep)


@pytest.fixture
def orch_db(tmp_path):
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 500.0)
    ids = {}

    def add(key, ticket, state, finished_at=None, repo="aut0-code/aut0"):
        request = JobRequest(repo, ticket, "build", "claude", f"c-{key}", 60)
        job = queue.enqueue(request, dedupe_key=key)
        with queue.connect() as connection:
            connection.execute(
                "UPDATE jobs SET state = ?, finished_at = ? WHERE id = ?", (state, finished_at, job.id)
            )
        ids[key] = job.id

    add("done-1", "AUT-2129", "done", 900.0)
    add("done-2", "AUT-2129", "failed", 950.0)
    add("done-3", "AUT-2129", "cancelled", None)
    add("running", "AUT-7", "running")
    add("ran", "AUT-7", "done", 800.0)
    add("queued", "AUT-8", "queued")
    add("qa", "AUT-9", "done", 700.0)
    add("other-repo", "AUT-2129", "done", 600.0, repo="b/aut0")
    return queue, ids


def add_slot_tables(connection, slots, qa_slots):
    connection.execute(
        "CREATE TABLE device_slots (job_id TEXT PRIMARY KEY, device TEXT NOT NULL,"
        " harness TEXT NOT NULL, os_user TEXT NOT NULL, placed_at REAL NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE qa_device_slots (job_id TEXT PRIMARY KEY, device TEXT NOT NULL, placed_at REAL NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO device_slots VALUES (?, ?, 'claude', 'bot', 1.0)", slots
    )
    connection.executemany("INSERT INTO qa_device_slots VALUES (?, ?, 1.0)", qa_slots)


def test_read_ledger_over_a_real_orch_db(orch_db):
    queue, ids = orch_db
    with queue.connect() as connection:
        add_slot_tables(
            connection,
            [(ids["done-1"], "mac"), (ids["running"], "pc")],
            [(ids["qa"], "pc"), ("ghost-job", "pc")],
        )
        read = read_ledger(connection, live={ids["ran"], "ghost-job"})

    assert read.tickets == {
        OWNER: TicketFacts(True, 950.0, frozenset({"mac"}), False),
        ("aut0-code/aut0", "AUT-7"): TicketFacts(False, 800.0, frozenset({"pc"}), True),
        ("aut0-code/aut0", "AUT-8"): TicketFacts(False, None, frozenset(), False),
        ("aut0-code/aut0", "AUT-9"): TicketFacts(True, 700.0, frozenset({"pc"}), False),
        ("b/aut0", "AUT-2129"): TicketFacts(True, 600.0, frozenset(), False),
    }
    assert read.job_ticket[ids["done-3"]] == OWNER
    assert read.job_ticket[ids["queued"]] == ("aut0-code/aut0", "AUT-8")
    assert len(read.job_ticket) == len(ids)


def test_read_ledger_without_slot_tables(orch_db):
    queue, ids = orch_db
    with queue.connect() as connection:
        read = read_ledger(connection, live=())
    assert read.tickets[OWNER] == TicketFacts(True, 950.0, frozenset(), False)
    assert all(not facts.held_on for facts in read.tickets.values())


def test_read_ledger_raises_on_a_database_without_jobs(tmp_path):
    with pytest.raises(sqlite3.Error):
        read_ledger(sqlite3.connect(tmp_path / "empty.db"), live=())
