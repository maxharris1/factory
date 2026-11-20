from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from lib import containers
from lib import waiter as waiter_module
from lib.containers import Ledger, Stack, Ticket, owner, removable, stack_name
from lib.machines_yml import orch_host
from lib.remote import Called
from tests.fake_docker import Docker
from tests.test_waiter import FakeChild, enqueue, make_queue, set_state, sweeping_waiter

T = time.time() - 1000
REPO = "aut0-code/aut0"


def finish(db: Path, job_id: str, job_state: str = "done", at: float = T) -> None:
    set_state(db, job_id, job_state)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET finished_at = ? WHERE id = ?", (at, job_id))


class Setup:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.db, self.queue = make_queue(tmp_path)
        self.docker = Docker(tmp_path)
        monkeypatch.setenv("PATH", self.docker.path())
        self.log: list[str] = []
        self.waiter, self.now = sweeping_waiter(tmp_path, self.queue, self.db, self.log)
        self.host = orch_host(self.waiter.pool.config)
        self.keys = 0

    def job(self, ticket_id: str = "AUT-2129", job_state: str | None = "done", repo: str = REPO) -> str:
        self.keys += 1
        job_id = enqueue(self.queue, f"k{self.keys}", repo=repo, ticket_id=ticket_id)
        if job_state in containers.FINISHED_STATES:
            finish(self.db, job_id, job_state)
        elif job_state is not None:
            set_state(self.db, job_id, job_state)
        return job_id

    def sweep(self) -> list[str]:
        self.now[0] += waiter_module.CONTAINER_SWEEP_SECONDS
        self.waiter._reap()
        return [line for line in self.log if " stack " in line]

    def gone(self, ids: list[str]) -> bool:
        return not set(ids) & self.docker.ids() and not self.docker.networks() and not self.docker.volumes()

    def intact(self, ids: list[str]) -> bool:
        return set(ids) <= self.docker.ids() and len(self.docker.networks()) == len(self.docker.volumes()) == 1


def test_a_finished_tickets_stack_is_removed_with_its_networks_and_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Setup(tmp_path, monkeypatch)
    s.job()
    s.job()
    project = f"aut0-aut2129-{s.host}"
    ids = s.docker.stack(project, 3, T - 100)
    lines = s.sweep()
    assert s.gone(ids)
    assert lines == [
        f"removed compose stack {project} on {s.host} (3 container(s), its networks and volumes)"
        f" of finished ticket {REPO}#AUT-2129"
    ]


def test_a_supabase_stack_is_removed_by_its_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Setup(tmp_path, monkeypatch)
    s.job()
    project = f"aut0-aut2129-{s.host}"
    ids = s.docker.stack(project, 2, T - 100, kind="supabase")
    lines = s.sweep()
    assert s.gone(ids)
    assert len(lines) == 1 and lines[0].startswith(f"removed supabase stack {project} ")


def test_a_supabase_stack_also_labelled_compose_loses_its_supabase_only_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Setup(tmp_path, monkeypatch)
    s.job()
    project = f"aut0-aut2129-{s.host}"
    both = {"com.docker.compose.project": project, "com.supabase.cli.project": project}
    network = s.docker.network(**{"com.supabase.cli.project": project})
    volume = s.docker.volume(**{"com.supabase.cli.project": project})
    ids = [s.docker.start(T - 100, networks=(network,), volumes=(volume,), **both) for _ in range(2)]
    lines = s.sweep()
    assert s.gone(ids)
    assert len(lines) == 1 and lines[0].startswith(f"removed compose stack {project} ")


@pytest.mark.parametrize(
    "case, reason",
    [
        ("running", "not every job of the ticket is finished"),
        ("queued", "not every job of the ticket is finished"),
        ("another ticket", "not every job of the ticket is finished"),
        ("no jobs", "no ticket's job directory or stack name"),
        ("another device", "no ticket's job directory or stack name"),
        ("newer", "newer than the ticket's last finish"),
        ("held slot", "holds a slot on"),
        ("live run", "a run of the ticket is live"),
    ],
)
def test_a_stack_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, reason: str) -> None:
    s = Setup(tmp_path, monkeypatch)
    done = s.job()
    project, created = f"aut0-aut2129-{s.host}", T - 100
    if case in ("running", "queued"):
        s.job(job_state=case)
    elif case == "another ticket":
        s.job("AUT-2130", "running")
        project = f"aut0-aut2130-{s.host}"
    elif case == "no jobs":
        project = f"aut0-aut9999-{s.host}"
    elif case == "another device":
        project = "aut0-aut2129-mac"
    elif case == "newer":
        created = T + 100
    elif case == "held slot":
        with sqlite3.connect(s.db) as connection:
            connection.execute(
                "INSERT INTO device_slots (job_id, device, harness, os_user, placed_at) VALUES (?, ?, ?, ?, ?)",
                (done, s.host, "claude", "claude-bot", T),
            )
    elif case == "live run":
        s.waiter._children[done] = (FakeChild(4242), None)
    ids = s.docker.stack(project, 2, created)
    lines = s.sweep()
    assert s.intact(ids)
    assert len(lines) == 1, lines
    assert lines[0].startswith(f"kept compose stack {project} on {s.host}: ") and reason in lines[0], lines


def test_a_labelled_container_takes_the_labelled_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Setup(tmp_path, monkeypatch)
    done, running = s.job(), s.job(job_state="running")
    project = f"aut0-aut2129-{s.host}"
    labelled = {job: s.docker.start(T - 100, **{"com.docker.compose.project": project, "factory.job": job})
                for job in (done, running)}
    lines = s.sweep()
    assert s.docker.ids() == {labelled[running]}
    assert sum(f"removed 1 container(s) on {s.host} of finished job {done}" == line for line in s.log) == 1
    assert lines == []


def test_working_dir_ownership_removes_a_stack_whatever_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Setup(tmp_path, monkeypatch)
    done = s.job()
    jobs = s.waiter.state_dir / "jobs"
    ids = s.docker.stack("myapp", 2, T - 100, working_dir=str(jobs / done / "app"))
    assert len(s.sweep()) == 1
    assert s.gone(ids)

    kept = s.docker.stack("myapp", 2, T - 100, working_dir=str(jobs / f"{done}x" / "app"))
    lines = s.sweep()
    assert s.intact(kept)
    assert lines[-1] == f"kept compose stack myapp on {s.host}: working_dir {jobs / f'{done}x' / 'app'} is under job {done}x, not in orch.db"


def test_a_supabase_workdir_label_ties_a_stack_to_its_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Setup(tmp_path, monkeypatch)
    done = s.job()
    workdir = str(s.waiter.state_dir / "jobs" / done)
    ids = [s.docker.start(T - 100, **{"com.supabase.cli.project": "app", "com.supabase.cli.workdir": workdir})]
    assert s.sweep()[0].startswith("removed supabase stack app ")
    assert not set(ids) & s.docker.ids()


A = ("aut0-code/aut0", "AUT-2129")
B = ("aut0-code/aut0", "AUT-2130")
C = ("other/aut0", "AUT-2129")
JA, JB, JC = "jobaaa", "jobbbb", "jobccc"


def ticket(key: tuple[str, str], **fields) -> Ticket:
    values = {"all_finished": True, "last_finish": T, "held_on": frozenset(), "live": False} | fields
    return Ticket(key[0], key[1], **values)


def ledger(*tickets: Ticket) -> Ledger:
    jobs = {A: JA, B: JB, C: JC}
    return Ledger(
        {(t.repo, t.ticket_id): t for t in tickets},
        {jobs[(t.repo, t.ticket_id)]: (t.repo, t.ticket_id) for t in tickets},
    )


def stack(project: str, *working_dirs: str, newest: float = T - 100) -> Stack:
    return Stack("compose", project, frozenset(working_dirs), ("0123456789ab",), newest)


ROOT = "/srv/state/"


@pytest.mark.parametrize(
    "the_stack, tickets, expected",
    [
        (stack("aut0-aut2129-box"), [ticket(A), ticket(B)], A),
        (stack("myapp", f"{ROOT}jobs/{JA}/app"), [ticket(A), ticket(B)], A),
        (stack("aut0-aut2129-box", f"/srv/state/jobs/{JA}/x/../app"), [ticket(A)], A),
        (stack("aut0-aut2130-box", f"{ROOT}jobs/{JA}"), [ticket(A), ticket(B)], "conflicting"),
        (stack("x", f"{ROOT}jobs/{JA}", f"{ROOT}jobs/{JB}"), [ticket(A), ticket(B)], "conflicting"),
        (stack("aut0-aut2129-box"), [ticket(A), ticket(C)], "ambiguous"),
        (stack("aut0-aut2129-box", f"{ROOT}jobs/nojob/app"), [ticket(A)], "job nojob, not in orch.db"),
        (stack("myapp", f"{ROOT}jobs/{JA}x/app"), [ticket(A)], f"job {JA}x, not in orch.db"),
        (stack("myapp", f"/elsewhere/jobs/{JA}"), [ticket(A)], "no ticket's job directory or stack name on box"),
        (stack("aut0-aut2129-mac"), [ticket(A)], "no ticket's job directory or stack name on box"),
        (stack("aut0-aut2129-box"), [ticket(A, all_finished=False)], "not every job of the ticket is finished"),
        (stack("aut0-aut2129-box"), [ticket(A, held_on=frozenset({"box"}))], "holds a slot on box"),
        (stack("aut0-aut2129-box"), [ticket(A, held_on=frozenset({"pc"}))], A),
        (stack("aut0-aut2129-box"), [ticket(A, live=True)], "a run of the ticket is live"),
        (stack("aut0-aut2129-box"), [ticket(A, last_finish=None)], "no finish time"),
        (stack("aut0-aut2129-box", newest=T + 1), [ticket(A)], "newer than the ticket's last finish"),
        (stack("aut0-aut2129-box", newest=T), [ticket(A)], A),
    ],
)
def test_owner_table(the_stack: Stack, tickets: list[Ticket], expected: object) -> None:
    found = owner(the_stack, "box", ledger(*tickets), [ROOT])
    decision = found if isinstance(found, str) else removable(the_stack, found, "box")
    if isinstance(expected, tuple):
        assert decision is None and (found.repo, found.ticket_id) == expected
    else:
        assert decision is not None and expected in decision, decision


def test_stack_name() -> None:
    assert stack_name("aut0-code/aut0", "AUT-2129", "pc") == "aut0-aut2129-pc"
    assert stack_name("maxharris1/factory", "1235", "box") == "factory-1235-box"
    assert stack_name("Org/MyRepo", "Ab_C-9", "pc") == "myrepo-abc9-pc"


def test_unparseable_rows_never_reach_a_script() -> None:
    def row(container: str, project: str, created: str) -> str:
        return "\t".join([container, "", project, "", "", created, ""])

    good_time = time.strftime("%Y-%m-%d %H:%M:%S +0000 UTC", time.gmtime(T - 100))
    listing = "\n".join([
        row("$(touch x)", "aut0-aut2129-box", good_time),
        row("0123456789ab", "Bad Project;rm", good_time),
        row("0123456789ac", "aut0-aut2129-box", "yesterday"),
        row("0123456789ad", "aut0-aut2129-box", good_time),
        "0123456789ae\tshort",
        "",
    ])
    scripts: list[str] = []

    def runner(script: str) -> Called:
        scripts.append(script)
        return Called(0, listing if len(scripts) == 1 else "", "")

    lines = containers.sweep_stacks(runner, ledger(ticket(A)), "box", [ROOT])
    assert lines[0] == "kept 4 compose/supabase container(s) on box: unreadable row"
    assert lines[1].startswith("removed compose stack aut0-aut2129-box on box (1 container(s)")
    assert len(scripts) == 2
    assert "0123456789ad" in scripts[1]
    assert not any(bad in scripts[1] for bad in ("touch", "Bad", "0123456789ab", "0123456789ac", "0123456789ae"))
