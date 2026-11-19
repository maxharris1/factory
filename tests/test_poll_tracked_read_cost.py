import base64
import hashlib
import json
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from lib.dispatcher import TicketRef, TrackedTicket
from lib.github_actions import GhResult
from lib.orch_yml import OrchConfig, Project
from lib.poll_source import LivePollSource
from tests.test_live_poll import FACTORY

REPO = "acme/widgets"
HELD_FACTORY = FACTORY.format(tickets="github", ship="merge").replace(
    "skills: []\n", "skills: []\nhold: {}\n"
)
PER_PR = ("pull", "comments", "check-runs", "status", "labels", "events")
FULL_READ = dict.fromkeys(PER_PR, 1)
NO_READ = dict.fromkeys(PER_PR, 0)
ALIAS = re.compile(r"\bp(\d+): pullRequest\(number: (\d+)\)")
CHECK_RUN_FIELDS = re.compile(r"\.\.\. on CheckRun \{ ([^}]*) \}")


def comment(number: int, body: str, at: str = "2026-11-18T09:00:00Z") -> dict:
    return {
        "id": number,
        "user": {"login": "grok-bot"},
        "body": body,
        "created_at": "2026-11-18T09:00:00Z",
        "updated_at": at,
        "html_url": f"https://github.com/{REPO}/pull/12#issuecomment-{number}",
    }


def label_event(number: int, label: str) -> dict:
    return {
        "id": number,
        "event": "labeled",
        "label": {"name": label},
        "actor": {"login": "maintainer"},
        "created_at": "2026-11-18T09:00:00Z",
    }


@dataclass
class Pull:
    number: int
    head: str
    state: str = "open"
    merged: bool = False
    updated_at: str = "2026-11-18T09:00:00Z"
    base_sha: str = "c" * 40
    comments: list[dict] = field(default_factory=lambda: [comment(1, "first")])
    labels: list[str] = field(default_factory=lambda: ["orch:state/building"])
    label_events: list[dict] = field(
        default_factory=lambda: [label_event(1, "orch:state/building")]
    )
    reviews: int = 0
    check_runs: list[dict] = field(
        default_factory=lambda: [
            {"id": 1, "name": "test", "status": "completed", "conclusion": "success"}
        ]
    )
    statuses: list[dict] = field(
        default_factory=lambda: [{"context": "deploy", "state": "success"}]
    )
    contexts_truncated: bool = False

    def rest(self) -> dict:
        return {
            "number": self.number,
            "state": self.state,
            "merged": self.merged,
            "draft": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "title": "Make the widget faster",
            "body": None,
            "html_url": f"https://github.com/{REPO}/pull/{self.number}",
            "head": {"sha": self.head, "ref": f"widget-{self.number}"},
            "base": {"sha": self.base_sha, "ref": "main"},
            "additions": 3,
            "deletions": 1,
            "commits": 1,
        }

    def check_runs_page(self) -> dict:
        return {"total_count": len(self.check_runs), "check_runs": self.check_runs}

    def combined_status(self) -> dict:
        states = {status["state"] for status in self.statuses}
        combined = next(
            (state for state in ("failure", "error", "pending") if state in states),
            "success",
        )
        return {
            "total_count": len(self.statuses),
            "state": combined if self.statuses else "pending",
            "statuses": self.statuses,
        }

    def node(self, check_run_fields: list[str]) -> dict:
        contexts = [
            {
                key: value
                for key, value in {
                    "__typename": "CheckRun",
                    "databaseId": run["id"],
                    "status": run["status"].upper(),
                    "conclusion": run["conclusion"] and run["conclusion"].upper(),
                    "title": (run.get("output") or {}).get("title"),
                    "summary": (run.get("output") or {}).get("summary"),
                }.items()
                if key == "__typename" or key in check_run_fields
            }
            for run in self.check_runs
        ] + [
            {
                "__typename": "StatusContext",
                "context": status["context"],
                "state": status["state"].upper(),
            }
            for status in self.statuses
        ]
        rollup = (
            {
                "contexts": {
                    "totalCount": len(contexts) + (100 if self.contexts_truncated else 0),
                    "pageInfo": {"hasNextPage": self.contexts_truncated},
                    "nodes": contexts,
                }
            }
            if contexts
            else None
        )
        return {
            "state": "MERGED" if self.merged else self.state.upper(),
            "merged": self.merged,
            "isDraft": False,
            "updatedAt": self.updated_at,
            "title": "Make the widget faster",
            "headRefName": f"widget-{self.number}",
            "headRefOid": self.head,
            "baseRefName": "main",
            "baseRefOid": self.base_sha,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "labels": {
                "totalCount": len(self.labels),
                "pageInfo": {"hasNextPage": False},
                "nodes": [{"name": name} for name in self.labels],
            },
            "comments": {
                "totalCount": len(self.comments),
                "nodes": [
                    {"databaseId": item["id"], "updatedAt": item["updated_at"]}
                    for item in self.comments[-100:]
                ],
            },
            "reviews": {"totalCount": self.reviews},
            "commits": {
                "nodes": [{"commit": {"oid": self.head, "statusCheckRollup": rollup}}]
            },
        }


class GitHub:
    def __init__(self, *pulls: Pull):
        self.pulls = {str(pull.number): pull for pull in pulls}
        self.calls: Counter[tuple[str, str]] = Counter()
        self.probe_down = False
        self.unprobed: set[str] = set()
        self.errors: list[dict] = []
        encoded = base64.b64encode(HELD_FACTORY.encode()).decode()
        self.factory = {"encoding": "base64", "content": encoded}

    def reads(self, number: str) -> dict[str, int]:
        return {route: self.calls[route, number] for route in PER_PR}

    def __call__(self, argv, timeout) -> GhResult:
        call = list(argv)
        if "graphql" in call:
            return self._probe(call)
        endpoint = next(part for part in call if part.startswith(f"repos/{REPO}/"))
        path = endpoint.removeprefix(f"repos/{REPO}/").partition("?")[0].split("/")
        match path:
            case ["contents", "factory.md"]:
                route, number, value = "factory", "-", self.factory
            case ["pulls", number]:
                route, value = "pull", self.pulls[number].rest()
            case ["pulls", number, "files"]:
                route, value = "files", [{"filename": "lib/widget.py"}]
            case ["issues", number, "comments"]:
                route, value = "comments", self.pulls[number].comments
            case ["issues", number, "labels"]:
                route, value = "labels", [{"name": name} for name in self.pulls[number].labels]
            case ["issues", number, "events"]:
                route, value = "events", self.pulls[number].label_events
            case ["commits", sha, "check-runs"]:
                number = self._at_head(sha)
                route, value = "check-runs", self.pulls[number].check_runs_page()
            case ["commits", sha, "status"]:
                number = self._at_head(sha)
                route, value = "status", self.pulls[number].combined_status()
            case _:
                raise AssertionError(f"unexpected gh call {call}")
        self.calls[route, number] += 1
        body = json.dumps(value)
        if "--include" in call:
            etag = hashlib.sha1(body.encode()).hexdigest()
            body = f'HTTP/2.0 200 OK\r\netag: "{etag}"\r\n\r\n{body}'
        return GhResult(0, body, "")

    def _at_head(self, sha: str) -> str:
        return next(number for number, pull in self.pulls.items() if pull.head == sha)

    def _probe(self, call: list[str]) -> GhResult:
        self.calls["probe", "-"] += 1
        if self.probe_down:
            return GhResult(1, "", "boom")
        fields = dict(
            call[index + 1].partition("=")[::2]
            for index, arg in enumerate(call)
            if arg in ("-f", "-F")
        )
        assert f"{fields['owner']}/{fields['name']}" == REPO
        aliases = ALIAS.findall(fields["query"])
        check_run_fields = CHECK_RUN_FIELDS.search(fields["query"]).group(1).split()
        assert aliases and all(alias == number for alias, number in aliases)
        repository = {
            f"p{number}": None if number in self.unprobed else self.pulls[number].node(check_run_fields)
            for _, number in aliases
        }
        document = {"data": {"repository": repository}}
        if not self.errors:
            return GhResult(0, json.dumps(document), "")
        for error in self.errors:
            *parents, leaf = error.get("path") or [None]
            if leaf is not None:
                target = document["data"]
                for key in parents:
                    target = target[key]
                target[leaf] = None
        document["errors"] = self.errors
        return GhResult(1, json.dumps(document), "gh: partial GraphQL response")


class Poller:
    def __init__(self, github: GitHub, *numbers: str, allow_merged: bool = False):
        self.github = github
        self.refs = tuple(
            TicketRef(REPO, number, "github", allow_merged=allow_merged) for number in numbers
        )
        self.now = 0.0
        self.deferrals: list[str] = []
        self.terminal: list[tuple[str, str, str]] = []
        self.source = LivePollSource(
            OrchConfig(
                poll_seconds=30,
                projects=(Project(REPO, factory_ref="main"),),
                trusted_logins={"grok-login": frozenset({"grok-bot"})},
            ),
            gh_runner=github,
            clock=lambda: self.now,
            sleep=lambda seconds: None,
            deferral_sink=self.deferrals.append,
            terminal_sink=lambda repo, number, reason, ticket: self.terminal.append(
                (repo, number, reason)
            ),
        )

    def cycle(self, at: float | None = None) -> dict[str, TrackedTicket]:
        self.now = self.now + 30 if at is None else at
        self.github.calls.clear()
        self.source.begin_cycle()
        tickets = self.source.tracked_tickets(self.refs)
        self.source.end_cycle()
        return {ticket.ticket_id: ticket for ticket in tickets}


def test_an_unchanged_pr_is_served_from_its_last_read_after_one_probe_per_repo() -> None:
    github = GitHub(Pull(12, head="a" * 40), Pull(13, head="b" * 40))
    poller = Poller(github, "12", "13")
    poller.cycle()
    second = poller.cycle()

    third = poller.cycle()

    assert github.calls["probe", "-"] == 1
    assert github.reads("12") == NO_READ
    assert github.reads("13") == NO_READ
    assert set(third) == {"12", "13"}
    assert third == second


def add_comment(pull: Pull) -> None:
    pull.comments.append(comment(2, "second"))


def edit_comment(pull: Pull) -> None:
    pull.comments[0] = comment(1, "first, edited", at="2026-11-18T10:00:00Z")


def push(pull: Pull) -> None:
    pull.head = "d" * 40


def review(pull: Pull) -> None:
    pull.reviews += 1
    pull.updated_at = "2026-11-18T10:00:00Z"


def relabel(pull: Pull) -> None:
    pull.labels.append("hold")
    pull.label_events.append(label_event(2, "hold"))


def fail_check(pull: Pull) -> None:
    pull.check_runs = [{**pull.check_runs[0], "conclusion": "failure"}]


def rerun_check(pull: Pull) -> None:
    pull.check_runs = [{**pull.check_runs[0], "id": 2}]


def fail_status(pull: Pull) -> None:
    pull.statuses = [{"context": "deploy", "state": "failure"}]


def ci_failed(before: TrackedTicket, after: TrackedTicket) -> bool:
    return before.ci[before.current_head].status == "success" and (
        after.ci[after.current_head].status == "failure"
    )


CHANGES = {
    "new comment": (
        add_comment,
        lambda before, after: [c.body for c in after.comments] == ["first", "second"],
    ),
    "edited comment": (
        edit_comment,
        lambda before, after: [c.body for c in after.comments] == ["first, edited"],
    ),
    "push": (
        push,
        lambda before, after: after.current_head == "d" * 40 and set(after.ci) == {"d" * 40},
    ),
    "review": (review, lambda before, after: after == before),
    "label": (
        relabel,
        lambda before, after: after.labels == ("hold", "orch:state/building")
        and len(after.label_events) == 2,
    ),
    "check run result": (fail_check, ci_failed),
    "check rerun at the same head": (rerun_check, lambda before, after: after == before),
    "commit status result": (fail_status, ci_failed),
}


@pytest.mark.parametrize("change, seen", CHANGES.values(), ids=CHANGES.keys())
def test_a_change_on_a_pr_brings_a_full_read_of_that_pr_alone(
    change: Callable[[Pull], None],
    seen: Callable[[TrackedTicket, TrackedTicket], bool],
) -> None:
    github = GitHub(Pull(12, head="a" * 40), Pull(13, head="b" * 40))
    poller = Poller(github, "12", "13")
    poller.cycle()
    before = poller.cycle()["12"]

    change(github.pulls["12"])
    after = poller.cycle()["12"]

    assert github.reads("12") == FULL_READ
    assert github.reads("13") == NO_READ
    assert seen(before, after)

    assert poller.cycle()["12"] == after
    assert github.reads("12") == NO_READ


def test_a_new_check_run_summary_at_the_same_result_is_read_and_reclassified() -> None:
    pull = Pull(12, head="a" * 40)
    pull.check_runs[0].update(
        conclusion="failure",
        output={"title": "Failed", "summary": "The hosted runner encountered an error while running your job"},
    )
    github = GitHub(pull)
    poller = Poller(github, "12")
    poller.cycle()
    before = poller.cycle()["12"]

    pull.check_runs[0]["output"] = {"title": "Failed", "summary": "AssertionError: wrong widget"}
    after = poller.cycle()["12"]

    assert github.reads("12") == FULL_READ
    assert before.ci[before.current_head].failed[0].cause == "infra"
    assert after.ci[after.current_head].failed[0].cause == "code"


class SlowGitHub(GitHub):
    def __init__(self, *pulls: Pull):
        super().__init__(*pulls)
        self.poller: Poller | None = None

    def __call__(self, argv, timeout) -> GhResult:
        if self.poller is not None:
            self.poller.now += min(19 if "graphql" in argv else 3, timeout)
        return super().__call__(argv, timeout)


def test_the_probe_is_charged_to_its_repos_tracked_read_budget() -> None:
    numbers = [str(number) for number in range(12, 19)]
    github = SlowGitHub(*(Pull(int(number), head=f"{int(number):040x}") for number in numbers))
    poller = Poller(github, *numbers)
    poller.cycle(at=0)
    poller.cycle(at=30)
    for pull in github.pulls.values():
        add_comment(pull)
    github.poller = poller

    tickets = poller.cycle(at=60)

    assert poller.now - 60 <= poller.source.tracked_read_budget_seconds
    assert len(tickets) < len(numbers)
    assert poller.deferrals


class SlowProbes(GitHub):
    def __init__(self, *pulls: Pull, failing_probe: int | None):
        super().__init__(*pulls)
        self.poller: Poller | None = None
        self.failing_probe = failing_probe

    def __call__(self, argv, timeout) -> GhResult:
        if self.poller is not None and "graphql" in argv:
            self.poller.now += 19
            if self.calls["probe", "-"] + 1 == self.failing_probe:
                self.calls["probe", "-"] += 1
                return GhResult(1, "", "probe failed")
        return super().__call__(argv, timeout)


@pytest.mark.parametrize("failing_probe", [None, 7])
def test_probes_that_spend_the_budget_still_let_tracked_tickets_progress(
    failing_probe: int | None,
) -> None:
    numbers = [str(number) for number in range(12, 163)]
    github = SlowProbes(
        *(Pull(int(number), head=f"{int(number):040x}") for number in numbers),
        failing_probe=failing_probe,
    )
    poller = Poller(github, *numbers)
    poller.cycle(at=0)
    poller.cycle(at=30)
    fail_check(github.pulls["12"])
    github.poller = poller

    cycles = [poller.cycle(at=at) for at in (200, 400, 600)]

    assert all(len(tickets) >= len(numbers) - 25 for tickets in cycles)
    assert set().union(*cycles[:2]) == set(numbers)
    first = next(tickets["12"] for tickets in cycles if "12" in tickets)
    assert first.ci[first.current_head].failed


OTHER = "acme/zzgadgets"


class TimedOutWidgetsProbes(GitHub):
    def __init__(self, *pulls: Pull):
        super().__init__(*pulls)
        self.now = 0.0
        self.slow = False
        self.widgets_probed: set[str] = set()

    def __call__(self, argv, timeout) -> GhResult:
        call = list(argv)
        if "graphql" in call and "name=widgets" in call:
            query = next(part for part in call if part.startswith("query="))
            self.widgets_probed.update(number for _, number in ALIAS.findall(query))
            if self.slow:
                self.now += timeout
                raise subprocess.TimeoutExpired(call, timeout)
        return super().__call__(
            [
                part.replace(f"repos/{OTHER}/", f"repos/{REPO}/").replace(
                    "name=zzgadgets", "name=widgets"
                )
                for part in call
            ],
            timeout,
        )


def test_a_repos_timed_out_probe_costs_no_other_repo_its_reads() -> None:
    numbers = range(12, 38)
    github = TimedOutWidgetsProbes(
        *(Pull(number, head=f"{number:040x}") for number in numbers),
        Pull(100, head=f"{100:040x}"),
    )
    source = LivePollSource(
        OrchConfig(
            poll_seconds=30,
            projects=(Project(REPO, factory_ref="main"), Project(OTHER, factory_ref="main")),
        ),
        gh_runner=github,
        clock=lambda: github.now,
        sleep=lambda seconds: setattr(github, "now", github.now + seconds),
    )
    refs = tuple(TicketRef(REPO, str(number), "github") for number in numbers) + (
        TicketRef(OTHER, "100", "github"),
    )

    def cycle(at: float) -> dict[tuple[str, str], TrackedTicket]:
        github.now = at
        github.widgets_probed.clear()
        source.begin_cycle()
        tickets = source.tracked_tickets(refs)
        source.end_cycle()
        return {(ticket.repo, ticket.ticket_id): ticket for ticket in tickets}

    cycle(0)
    cycle(30)
    github.slow = True
    fail_check(github.pulls["100"])

    for at in (60, 240, 420):
        tickets = cycle(at)

        assert len(tickets) == 27
        assert len(github.widgets_probed) == 25
        gadget = tickets[OTHER, "100"]
        assert gadget.ci[gadget.current_head].status == "failure"


def test_a_reopened_pr_is_read_in_full_and_open() -> None:
    github = GitHub(Pull(12, head="a" * 40, state="closed"))
    poller = Poller(github, "12")
    poller.cycle()
    assert poller.cycle()["12"].closed is True

    github.pulls["12"].state = "open"
    reopened = poller.cycle()["12"]

    assert github.reads("12") == FULL_READ
    assert reopened.closed is False


def test_a_merged_pr_read_on_purpose_sees_a_new_comment() -> None:
    github = GitHub(Pull(12, head="a" * 40, state="closed", merged=True))
    poller = Poller(github, "12", allow_merged=True)
    poller.cycle()
    poller.cycle()

    add_comment(github.pulls["12"])
    ticket = poller.cycle()["12"]

    assert github.reads("12") == FULL_READ
    assert [c.body for c in ticket.comments] == ["first", "second"]


def test_a_merged_pr_is_ended_every_cycle_from_its_pull_read_alone() -> None:
    github = GitHub(Pull(12, head="a" * 40, state="closed", merged=True))
    poller = Poller(github, "12")

    tickets = [poller.cycle(), poller.cycle(), poller.cycle()]

    assert tickets == [{}, {}, {}]
    assert github.reads("12") == {**NO_READ, "pull": 1}
    assert poller.terminal == [(REPO, "12", "merged")] * 3


def test_an_unchanged_pr_is_read_in_full_once_its_last_full_read_is_600_seconds_old() -> None:
    github = GitHub(Pull(12, head="a" * 40))
    poller = Poller(github, "12")
    poller.cycle(at=0)
    second = poller.cycle(at=10)["12"]

    assert poller.cycle(at=10 + 599)["12"] == second
    assert github.reads("12") == NO_READ

    assert poller.cycle(at=10 + 600)["12"] == second
    assert github.reads("12") == FULL_READ


def test_a_failed_probe_is_deferred_and_falls_back_to_full_reads() -> None:
    github = GitHub(Pull(12, head="a" * 40))
    poller = Poller(github, "12")
    poller.cycle()
    second = poller.cycle()
    assert poller.deferrals == []

    github.probe_down = True
    third = poller.cycle()

    assert github.reads("12") == FULL_READ
    assert third["12"] == second["12"]
    assert len(poller.deferrals) == 1


def test_a_pr_the_probe_does_not_return_is_read_in_full() -> None:
    github = GitHub(Pull(12, head="a" * 40), Pull(13, head="b" * 40))
    poller = Poller(github, "12", "13")
    poller.cycle()
    poller.cycle()

    github.unprobed.add("12")
    poller.cycle()

    assert github.reads("12") == FULL_READ
    assert github.reads("13") == NO_READ


def test_a_pr_whose_ci_contexts_run_past_one_page_is_read_in_full_every_cycle() -> None:
    github = GitHub(Pull(12, head="a" * 40, contexts_truncated=True))
    poller = Poller(github, "12")
    poller.cycle()
    poller.cycle()

    for _ in range(2):
        poller.cycle()
        assert github.reads("12") == FULL_READ


ROLLUP = ["commits", "nodes", 0, "commit", "statusCheckRollup"]
PARTIAL = {
    "at the pr's ci rollup": (["repository", "p12", *ROLLUP], NO_READ),
    "at the pr's comments": (["repository", "p12", "comments"], NO_READ),
    "with no path": (None, FULL_READ),
    "at the repository": (["repository"], FULL_READ),
}


@pytest.mark.parametrize("path, other_reads", PARTIAL.values(), ids=PARTIAL.keys())
def test_a_probe_error_touching_a_pr_reads_that_pr_in_full_every_cycle(
    path: list | None, other_reads: dict[str, int]
) -> None:
    github = GitHub(Pull(12, head="a" * 40), Pull(13, head="b" * 40))
    github.errors = [{"type": "INTERNAL", **({"path": path} if path else {})}]
    poller = Poller(github, "12", "13")
    poller.cycle()
    poller.cycle()
    poller.cycle()
    assert github.reads("13") == other_reads

    fail_check(github.pulls["12"])
    after = poller.cycle()["12"]

    assert github.reads("12") == FULL_READ
    assert after.ci[after.current_head].status == "failure"


class CachingGitHub(GitHub):
    def __init__(self, *pulls: Pull):
        super().__init__(*pulls)
        self.now = lambda: 0.0
        self.stored: dict[str, tuple[float, GhResult]] = {}

    def __call__(self, argv, timeout) -> GhResult:
        call = list(argv)
        if "--cache" not in call:
            return super().__call__(call, timeout)
        endpoint = next(part for part in call if part.startswith("repos/"))
        stored = self.stored.get(endpoint)
        if stored is not None and self.now() - stored[0] < 60:
            return stored[1]
        result = super().__call__(call, timeout)
        self.stored[endpoint] = (self.now(), result)
        return result


def test_a_push_seen_by_the_probe_is_never_served_from_ghs_cache() -> None:
    pull = Pull(12, head="a" * 40)
    github = CachingGitHub(pull)
    poller = Poller(github, "12")
    github.now = lambda: poller.now
    poller.cycle(at=0)
    poller.cycle(at=30)

    push(pull)

    assert poller.cycle(at=50)["12"].current_head == "d" * 40
    assert poller.cycle(at=80)["12"].current_head == "d" * 40
