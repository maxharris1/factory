import json
import math

import pytest

from lib.dispatcher import LINEAR_HANDOFF_QUERY, TicketRef
from lib.github_actions import GhResult
from lib.orch_yml import LINEAR_POLL_SECONDS
from lib.poll_source import (
    HTTPResult,
    LINEAR_PROCESS_HEADROOM,
    LINEAR_REQUESTS_PER_HOUR,
    LINEAR_SHARE,
    LINEAR_TRACKED_RESERVE,
    LivePollSource,
)
from tests.test_live_poll import FACTORY, FakeGh, config, linear_issue, linear_search_page

CYCLE_REQUEST_LIMIT = (
    int(LINEAR_REQUESTS_PER_HOUR * LINEAR_SHARE) * LINEAR_POLL_SECONDS // 3600
)


def issue(identifier: str) -> dict:
    value = linear_issue(identifier=identifier)
    value["id"] = f"id-{identifier}"
    value["comments"]["nodes"][0]["id"] = f"comment-{identifier}"
    return value


class FakeLinear:
    def __init__(self, searchable: list[dict]):
        self.searchable = searchable
        self.by_key = {
            key: item for item in searchable for key in (item["id"], item["identifier"])
        }
        self.searches = 0

    def __call__(self, url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        if request["query"].startswith("query FactoryPoll"):
            self.searches += 1
            start = int(request["variables"]["after"] or 0)
            end = min(start + 50, len(self.searchable))
            return linear_search_page(
                [
                    {
                        "id": item["id"],
                        "identifier": item["identifier"],
                        "project": item["project"],
                    }
                    for item in self.searchable[start:end]
                ],
                has_next=end < len(self.searchable),
                cursor=str(end) if end < len(self.searchable) else None,
            )
        found = self.by_key[request["variables"]["id"]]
        return HTTPResult(200, json.dumps({"data": {"issue": found}}))


def linear_source(
    linear: FakeLinear, deferrals: list[str], errors: list[tuple[str, str, str]]
) -> LivePollSource:
    return LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=FakeGh(FACTORY.format(tickets="linear", ship="handoff")),
        linear_transport=linear,
        error_sink=lambda repo, ticket, error: errors.append((repo, ticket, error)),
        deferral_sink=deferrals.append,
    )


def discover(
    source: LivePollSource,
    linear: FakeLinear,
    *,
    exclude: frozenset[str] = frozenset(),
    reserve: int = 0,
) -> tuple[list[str], int]:
    linear.searches = 0
    source.begin_cycle()
    found = source.linear_candidates(
        LINEAR_HANDOFF_QUERY, exclude=exclude, reserve=reserve
    )
    return [ticket.ticket_id for ticket in found], linear.searches


def test_linear_discovery_rotation_reaches_every_candidate() -> None:
    pending = [issue(f"AUT-{index:03d}") for index in range(40)]
    linear = FakeLinear(pending)
    deferrals: list[str] = []
    errors: list[tuple[str, str, str]] = []
    source = linear_source(linear, deferrals, errors)

    first, searches = discover(source, linear)
    source.end_cycle()
    capacity = CYCLE_REQUEST_LIMIT - searches - LINEAR_PROCESS_HEADROOM
    assert 0 < capacity < len(pending)
    cycles = [first]
    for _ in range(math.ceil(len(pending) / capacity) - 1):
        cycles.append(discover(source, linear)[0])
        source.end_cycle()

    assert [len(found) for found in cycles] == [capacity] * len(cycles)
    assert set().union(*cycles) == {item["identifier"] for item in pending}
    assert deferrals == ["Linear poll budget deferred discovery x1"] * len(cycles)
    assert errors == []


@pytest.mark.parametrize(
    ("reserve", "kept", "deferred"),
    [
        (
            LINEAR_TRACKED_RESERVE,
            LINEAR_TRACKED_RESERVE,
            "Linear poll budget deferred discovery x1",
        ),
        (
            0,
            0,
            "Linear poll budget deferred discovery x1; "
            f"Linear poll budget deferred tracked ticket x{LINEAR_TRACKED_RESERVE}",
        ),
    ],
    ids=["reserve", "no-reserve"],
)
def test_tracked_reserve_keeps_its_reads_when_discovery_is_saturated(
    reserve: int, kept: int, deferred: str
) -> None:
    tracked = [issue(f"AUT-T{index}") for index in range(LINEAR_TRACKED_RESERVE)]
    pending = [issue(f"AUT-{index:03d}") for index in range(40)]
    linear = FakeLinear([*tracked, *pending])
    deferrals: list[str] = []
    errors: list[tuple[str, str, str]] = []
    source = linear_source(linear, deferrals, errors)
    tracked_ids = frozenset(item["identifier"] for item in tracked)

    discovered, _ = discover(source, linear, exclude=tracked_ids, reserve=reserve)
    found = source.tracked_tickets(
        tuple(
            TicketRef("acme/widgets", identifier, "linear")
            for identifier in sorted(tracked_ids)
        )
    )
    source.end_cycle()

    assert 0 < len(discovered) < len(pending)
    assert tracked_ids.isdisjoint(discovered)
    assert len({ticket.ticket_id for ticket in found} & tracked_ids) == kept
    assert deferrals == [deferred]
    assert errors == []


class PagedGh(FakeGh):
    def __call__(self, argv, timeout):
        call = list(argv)
        if any(part.endswith("/issues?state=open&per_page=100") for part in call):
            self.calls.append(call)
            pages = "".join(
                json.dumps(self.issues[start : start + 100])
                for start in range(0, len(self.issues), 100)
            )
            return GhResult(0, pages, "")
        return super().__call__(argv, timeout)


def test_github_discovery_reads_every_labelled_pr_past_the_first_page() -> None:
    gh = PagedGh(FACTORY.format(tickets="github", ship="merge"))
    gh.issues = [
        {
            "number": number,
            "pull_request": {},
            "labels": [{"name": "orch:state/intake"}],
        }
        for number in range(1, 131)
    ]
    errors: list[tuple[str, str, str]] = []
    source = LivePollSource(
        config(),
        gh_runner=gh,
        error_sink=lambda repo, ticket, error: errors.append((repo, ticket, error)),
    )

    source.begin_cycle()
    tickets = source.github_candidates("orch:state/")
    source.end_cycle()

    assert sorted(int(ticket.ticket_id) for ticket in tickets) == list(range(1, 131))
    assert errors == []
