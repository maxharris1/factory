from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from lib import containers
from lib.queue import JobRequest, SQLiteJobQueue
from lib.stacks import (
    KINDS,
    Container,
    Keep,
    Ledger,
    Project,
    Remove,
    TicketFacts,
    decide,
    list_script,
    listing_format,
    parse_listing,
    read_ledger,
    remove_script,
    stack_name,
    sweep,
)
from tests.fake_docker import Docker

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
    pytest.param(
        Project("compose", NAME, project().containers, complete=False),
        ledger(),
        "could not be read",
        id="kept: a container could not be read",
    ),
    pytest.param(Project("compose", NAME, ()), ledger(), "could not be read", id="kept: no containers"),
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


def docker_json(value):
    return json.dumps(value, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def line(created, *, id_="0123456789ab", name=NAME, working_dir="/w", job=""):
    return "\t".join(docker_json(field) for field in (id_, name, working_dir, job, created))


def test_listing_format_has_the_fields_parse_listing_reads():
    fields = listing_format("supabase").split("\t")
    assert len(fields) == 5
    assert all(field.startswith("{{json ") for field in fields)
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


GOOD_AT = "2026-11-20 13:05:01 +0000 UTC"


@pytest.mark.parametrize(
    "bad",
    [
        line("yesterday", name="good"),
        line("2026-13-20 13:05:01 +0000 UTC", name="good"),
        line("2026-11-20 13:05:01 UTC", name="good"),
        line("2026-11-20", name="good"),
        line("", name="good"),
        line(GOOD_AT, name="good", id_="0123456789AB"),
        line(GOOD_AT, name="good", id_="$(rm -rf /)"),
    ],
)
def test_an_unreadable_container_keeps_its_whole_project(bad):
    good = line(GOOD_AT, id_="fedcba987654", name="good")
    sibling = line(GOOD_AT, id_="aaaaaaaaaaaa", name="sibling")
    projects, unreadable = parse_listing("compose", f"{bad}\n{good}\n{sibling}\n")
    assert unreadable == 1
    by_name = {p.name: p for p in projects}
    assert [c.id for c in by_name["good"].containers] == ["fedcba987654"]
    assert not by_name["good"].complete
    assert by_name["sibling"].complete
    assert decide(by_name["good"], ledger({OWNER: facts()}, {}), "pc", ROOTS) == Keep(
        "a container could not be read"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "\t".join(("0123456789ab", "good", "/w", "", GOOD_AT)),
        line(GOOD_AT, name="good")[:-1],
        line(GOOD_AT, name="good") + '\t""',
        "\t".join(line(GOOD_AT, name="good").split("\t")[:4]),
        line(GOOD_AT, name="good").replace('"good"', "7"),
        line(GOOD_AT, name=""),
    ],
)
def test_a_line_that_names_no_project_keeps_every_project(bad):
    good = line(GOOD_AT, id_="fedcba987654", name="good")
    projects, unreadable = parse_listing("compose", f"{good}\n{bad}\n")
    assert unreadable == 1
    assert [p.name for p in projects] == ["good"]
    assert not projects[0].complete


def test_a_junk_created_at_on_a_labelled_sibling_keeps_the_project():
    stdout = "\n".join(
        (
            line(GOOD_AT, id_="aaaaaaaaaaaa", name=NAME),
            line("junk", id_="bbbbbbbbbbbb", name=NAME, job="job9"),
        )
    )
    projects, unreadable = parse_listing("compose", stdout)
    assert unreadable == 1
    assert isinstance(decide(projects[0], ledger(), "pc", ROOTS), Keep)


HOSTILE = [
    "/manual\t\t" + GOOD_AT + "\ncccccccccccc\tunrelated\t/root/jobs/job1",
    "/w\r\n0123456789ab\tother",
    "/w\x85cccccccccccc",
    "/w\u2028x\u2029y\x1c\x1d\x1e",
    '/w"\\\t"',
]


@pytest.mark.parametrize("working_dir", HOSTILE)
def test_a_hostile_field_round_trips_as_one_container(working_dir):
    projects, unreadable = parse_listing(
        "compose", line(GOOD_AT, id_="aaaaaaaaaaaa", name="unrelated", working_dir=working_dir) + "\n"
    )
    assert unreadable == 0
    assert projects == [
        Project(
            "compose",
            "unrelated",
            (Container("aaaaaaaaaaaa", epoch(2026, 11, 20, 13, 5, 1), working_dir, False),),
        )
    ]


def test_a_forged_second_container_cannot_tie_a_project_to_a_job():
    owner = ("org/repo", "T1")
    tied = Ledger({owner: TicketFacts(True, epoch(2025, 1, 2), frozenset(), False)}, {"job1": owner})
    created = "2025-01-01 00:00:00 +0000 UTC"
    working_dir = "/manual\t\t" + created + "\ncccccccccccc\tunrelated\t/root/jobs/job1"
    physical = Project("compose", "unrelated", (Container("aaaaaaaaaaaa", epoch(2025, 1, 1), working_dir, False),))
    assert decide(physical, tied, "box", ["/root"]) == Keep("no ticket owns it")
    projects, unreadable = parse_listing(
        "compose", line(created, id_="aaaaaaaaaaaa", name="unrelated", working_dir=working_dir)
    )
    assert (projects, unreadable) == ([physical], 0)
    assert decide(projects[0], tied, "box", ["/root"]) == Keep("no ticket owns it")


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


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_stack_the_fake_starts_parses_back_from_its_listing(tmp_path, monkeypatch, kind):
    docker = Docker(tmp_path)
    monkeypatch.setenv("PATH", docker.path())
    started = docker.start_stack(NAME, kind, working_dir=f"{BOX_JOB}/job1/repo", created=GOOD_AT)
    docker.start(app="web")

    listed = containers.local_runner()(list_script(kind))

    assert listed.code == 0, listed.stderr
    assert parse_listing(kind, listed.stdout) == (
        [Project(kind, NAME, (Container(started, epoch(2026, 11, 20, 13, 5, 1), f"{BOX_JOB}/job1/repo", False),))],
        0,
    )


def test_a_container_started_after_the_listing_stops_the_removal_before_networks_and_volumes(
    tmp_path, monkeypatch
):
    docker = Docker(tmp_path)
    monkeypatch.setenv("PATH", docker.path())
    listed = docker.start_stack(NAME)
    run = containers.local_runner()
    started = []

    def runner(script):
        if "docker rm" in script:
            started.append(docker.start(**{KINDS["compose"]: NAME}))
        return run(script)

    finished = ledger({OWNER: facts(last=epoch(2027, 1, 1))}, {})
    lines = sweep(runner, lambda: finished, "pc", ROOTS)

    assert docker.ids() == set(started)
    assert listed not in docker.ids()
    assert docker.networks() == [f"{NAME}_default"]
    assert docker.volumes() == [f"{NAME}_db"]
    assert lines == [f"kept compose project {NAME} on pc: a container started after the listing"]


@pytest.mark.parametrize(
    "hostile",
    [
        Project("compose", "$(touch pwned)", (Container("0123456789ab", 0.0, "", False),)),
        Project("compose", NAME, (Container("$(touch pwned)", 0.0, "", False),)),
    ],
)
def test_remove_script_refuses_a_hostile_name_or_id(hostile):
    with pytest.raises(ValueError):
        remove_script(hostile)


SECOND = "aut0-aut2130-pc"
NEWER = "2027-06-01 00:00:00 +0000 UTC"


def labelled_helper(docker):
    docker.start(**{KINDS["compose"]: NAME, "factory.job": "live-job"})


def newer_compose_container(docker):
    docker.start(NEWER, **{KINDS["compose"]: NAME})


def unfinished_supabase_in_the_same_compose_project(docker):
    docker.start(**{KINDS["compose"]: NAME, KINDS["supabase"]: SECOND})


@pytest.mark.parametrize(
    "sibling, reason",
    [
        pytest.param(labelled_helper, "a container is labelled factory.job", id="labelled helper"),
        pytest.param(newer_compose_container, f"newer than {OWNER[1]}'s last finish", id="newer container"),
        pytest.param(
            unfinished_supabase_in_the_same_compose_project, "AUT-2130 has an unfinished job", id="through compose"
        ),
    ],
)
def test_a_supabase_project_is_kept_when_a_project_sharing_its_containers_is_kept(
    tmp_path, monkeypatch, sibling, reason
):
    docker = Docker(tmp_path)
    monkeypatch.setenv("PATH", docker.path())
    database = docker.start_stack(NAME, "supabase")
    sibling(docker)
    started = docker.ids()
    finished = ledger(
        {OWNER: facts(last=epoch(2027, 1, 1)), ("aut0-code/aut0", "AUT-2130"): facts(finished=False)}, {}
    )

    lines = sweep(containers.local_runner(), lambda: finished, "pc", ROOTS)

    assert database in docker.ids()
    assert docker.ids() == started
    assert docker.networks() == [f"{NAME}_default"]
    assert docker.volumes() == [f"{NAME}_db"]
    assert f"kept supabase project {NAME} on pc: a project sharing its containers is kept ({reason})" in lines
    assert not any(line.startswith("removed") for line in lines)
