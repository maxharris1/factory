import base64
import json

import pytest

from lib.dispatch.cycle_clock import CycleClock
from lib.dispatcher import TicketRef
from lib.github_actions import GhResult
from lib.poll_source import (
    BACKOFF_SECONDS,
    MAX_POLL_INTERVAL_SECONDS,
    RATE_PAUSE_REMAINING,
    LivePollSource,
    Pacing,
    PollSourceError,
    QuotaDeferred,
    RATE_RESET_GRACE_SECONDS,
    RateWindow,
    pace,
)
from tests.test_live_poll import FACTORY, FakeGh, config

WALL = 1_800_000_000.0
FACTORY_BODY = json.dumps(
    {
        "encoding": "base64",
        "content": base64.b64encode(
            FACTORY.format(tickets="github", ship="merge").encode()
        ).decode(),
    }
)


def response(status: int, body: str = "{}", **headers: object) -> GhResult:
    reason = {200: "OK", 403: "Forbidden", 429: "Too Many Requests"}[status]
    lines = [f"HTTP/2.0 {status} {reason}"]
    lines += [f"{name.replace('_', '-')}: {value}" for name, value in headers.items()]
    stdout = "\r\n".join(lines) + "\r\n\r\n" + body
    if status == 200:
        return GhResult(0, stdout, "")
    return GhResult(1, stdout, f"gh: API rate limit exceeded (HTTP {status})")


def limited(status: int = 403, **headers: object) -> GhResult:
    return response(
        status,
        '{"message": "API rate limit exceeded"}',
        **{"X-RateLimit-Limit": 5000, "X-RateLimit-Remaining": 0, **headers},
    )


class Quota:
    def __init__(self) -> None:
        self.mono = 1_000.0
        self.wall = WALL
        self.factory: list[GhResult] = []
        self.probe: GhResult = GhResult(1, "", "gh: HTTP 500")
        self.calls: list[list[str]] = []
        self.lines: list[str] = []
        self.source = LivePollSource(
            config(),
            gh_runner=self.run,
            clock=lambda: self.mono,
            wall=lambda: self.wall,
            sleep=lambda seconds: None,
        )
        self.source.warning_sink = self.lines.append

    def run(self, argv, timeout):
        del timeout
        call = list(argv)
        self.calls.append(call)
        if "rate_limit" in call:
            return self.probe
        if any("/contents/factory.md" in part for part in call):
            return self.factory.pop(0)
        return GhResult(0, "[]", "")

    def read_factory(self) -> None:
        self.source.factory_context(config().projects[0])

    def comments(self) -> None:
        self.source.github.list_comments("acme/widgets", "7")

    def reads(self) -> int:
        return sum("rate_limit" not in call for call in self.calls)

    def hit_limit(self, result: GhResult) -> None:
        self.factory.append(result)
        with pytest.raises(PollSourceError):
            self.read_factory()


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limit_pauses_every_read_until_the_reset_not_300s(status) -> None:
    quota = Quota()
    quota.hit_limit(limited(status, X_RateLimit_Reset=int(WALL + 900)))
    reads = quota.reads()

    quota.mono += BACKOFF_SECONDS + 1
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono = 1_000.0 + 900 + RATE_RESET_GRACE_SECONDS - 0.1
    with pytest.raises(QuotaDeferred):
        quota.comments()
    assert quota.reads() == reads

    quota.mono = 1_000.0 + 900 + RATE_RESET_GRACE_SECONDS
    quota.wall = WALL + 900 + RATE_RESET_GRACE_SECONDS
    quota.comments()
    assert quota.reads() == reads + 1


def test_wall_clock_jump_does_not_lift_the_pause() -> None:
    quota = Quota()
    quota.hit_limit(limited(X_RateLimit_Reset=int(WALL + 900)))

    quota.wall += 3600
    quota.mono += 10
    with pytest.raises(QuotaDeferred):
        quota.comments()


def test_retry_after_wins_over_the_reset_header() -> None:
    quota = Quota()
    quota.hit_limit(
        limited(X_RateLimit_Remaining=1000, X_RateLimit_Reset=int(WALL + 900), Retry_After=120)
    )

    quota.mono += 119
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono += 1
    quota.comments()


def test_no_reset_header_and_no_probe_falls_back_to_300s() -> None:
    quota = Quota()
    quota.hit_limit(response(429, '{"message": "rate limited"}'))

    assert any("rate_limit" in call for call in quota.calls)
    quota.mono += BACKOFF_SECONDS - 1
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono += 1
    quota.comments()


def test_probe_supplies_the_reset_when_the_response_has_none() -> None:
    quota = Quota()
    quota.probe = GhResult(
        0,
        json.dumps(
            {"resources": {"core": {"limit": 5000, "remaining": 0, "reset": int(WALL + 600)}}}
        ),
        "",
    )
    quota.hit_limit(response(429, '{"message": "rate limited"}'))

    quota.mono += 600 + RATE_RESET_GRACE_SECONDS - 1
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono += 1
    quota.wall += 600 + RATE_RESET_GRACE_SECONDS
    quota.comments()
    assert "core 0/5000 left" in quota.lines[0]


def test_paused_adapter_read_never_reaches_the_runner() -> None:
    quota = Quota()
    quota.hit_limit(limited(X_RateLimit_Reset=int(WALL + 900)))
    calls = len(quota.calls)

    for _ in range(3):
        with pytest.raises(QuotaDeferred):
            quota.comments()

    assert len(quota.calls) == calls


def test_one_throttle_line_per_episode() -> None:
    quota = Quota()
    quota.hit_limit(limited(X_RateLimit_Reset=int(WALL + 900)))
    for _ in range(10):
        with pytest.raises(QuotaDeferred):
            quota.comments()
    quota.mono += 905
    quota.wall += 905
    quota.hit_limit(limited(X_RateLimit_Reset=int(quota.wall + 60)))

    assert len(quota.lines) == 1
    line = quota.lines[0]
    assert line.startswith("github throttle: HTTP 403 rate limit on GET contents/factory.md")
    assert "core 0/5000 left" in line
    assert "paused 15m05s" in line

    quota.mono += 65
    quota.wall += 65
    quota.comments()
    quota.hit_limit(limited(X_RateLimit_Reset=int(quota.wall + 900)))
    assert len(quota.lines) == 2


def test_replayed_headers_do_not_overwrite_a_newer_window() -> None:
    quota = Quota()
    reset = int(WALL + 600)
    quota.factory.append(
        response(
            200,
            FACTORY_BODY,
            X_RateLimit_Limit=5000,
            X_RateLimit_Remaining=70,
            X_RateLimit_Reset=reset,
        )
    )
    quota.read_factory()
    quota.source.begin_cycle()
    quota.factory.append(
        response(
            200,
            FACTORY_BODY,
            X_RateLimit_Limit=5000,
            X_RateLimit_Remaining=4900,
            X_RateLimit_Reset=reset,
        )
    )
    quota.read_factory()
    quota.source.begin_cycle()
    quota.factory.append(
        response(
            200,
            FACTORY_BODY,
            X_RateLimit_Limit=5000,
            X_RateLimit_Remaining=4999,
            X_RateLimit_Reset=reset - 3600,
        )
    )
    quota.read_factory()
    quota.source.begin_cycle()

    quota.hit_limit(response(403, '{"message": "API rate limit exceeded"}'))

    assert "core 70/5000 left" in quota.lines[0]
    quota.mono += 600 + RATE_RESET_GRACE_SECONDS - 1
    with pytest.raises(QuotaDeferred):
        quota.comments()


def test_replayed_rate_limit_response_cannot_shorten_the_pause() -> None:
    quota = Quota()
    reset = int(WALL + 900)
    quota.factory.append(
        response(
            200,
            FACTORY_BODY,
            X_RateLimit_Limit=5000,
            X_RateLimit_Remaining=0,
            X_RateLimit_Reset=reset,
        )
    )
    quota.read_factory()
    quota.source.begin_cycle()

    quota.hit_limit(limited(429, X_RateLimit_Remaining=1, X_RateLimit_Reset=reset))

    quota.mono += BACKOFF_SECONDS
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono = 1_000.0 + 900 + RATE_RESET_GRACE_SECONDS - 0.1
    with pytest.raises(QuotaDeferred):
        quota.comments()
    quota.mono = 1_000.0 + 900 + RATE_RESET_GRACE_SECONDS
    quota.wall = WALL + 900 + RATE_RESET_GRACE_SECONDS
    quota.comments()


def test_quota_pause_is_a_deferral_not_a_cycle_error() -> None:
    quota = Quota()
    deferrals: list[str] = []
    errors: list[tuple[str, str, str]] = []
    quota.source.deferral_sink = deferrals.append
    quota.source.error_sink = lambda *error: errors.append(error)
    quota.hit_limit(limited(X_RateLimit_Reset=int(WALL + 900)))
    quota.source.begin_cycle()
    errors.clear()

    assert quota.source.github_candidates("orch:state/") == ()
    quota.source.end_cycle()

    assert errors == []
    assert any("github quota" in line for line in deferrals)


def window(remaining: int, reset_in: float) -> RateWindow:
    return RateWindow(5000, remaining, WALL + reset_in)


@pytest.mark.parametrize(
    ("window", "billed", "expected"),
    [
        pytest.param(window(4000, 1800), 30, Pacing(30.0, False, "normal"), id="normal"),
        pytest.param(
            window(1500, 1800), 60, Pacing(1800 * 60 / 1450 - 5, False, "stretch"), id="stretch"
        ),
        pytest.param(
            window(900, 1800), 30, Pacing(1800 * 30 / 850 - 5, True, "shed"), id="shed"
        ),
        pytest.param(
            window(RATE_PAUSE_REMAINING, 1800), 30, Pacing(120.0, True, "pause"), id="pause"
        ),
        pytest.param(window(10, -1), 30, Pacing(30.0, False, "normal"), id="reset-past"),
        pytest.param(window(200, 3000), 200, Pacing(120.0, True, "shed"), id="clamp"),
        pytest.param(None, 30, Pacing(30.0, False, "normal"), id="no-window"),
    ],
)
def test_pace(window: RateWindow | None, billed: int, expected: Pacing) -> None:
    got = pace(window, WALL, 30.0, billed, 5.0)

    assert got.interval_seconds == pytest.approx(expected.interval_seconds)
    assert (got.shed_low_priority, got.reason) == (expected.shed_low_priority, expected.reason)
    assert 30.0 <= got.interval_seconds <= MAX_POLL_INTERVAL_SECONDS


def seen(quota: Quota, remaining: int, reset_in: float = 900) -> None:
    quota.factory.append(
        response(
            200,
            FACTORY_BODY,
            X_RateLimit_Limit=5000,
            X_RateLimit_Remaining=remaining,
            X_RateLimit_Reset=int(WALL + reset_in),
        )
    )
    quota.read_factory()


def test_reads_pause_at_the_floor_until_the_reset() -> None:
    quota = Quota()
    seen(quota, RATE_PAUSE_REMAINING)
    reads = quota.reads()

    for _ in range(3):
        with pytest.raises(QuotaDeferred):
            quota.comments()

    assert quota.reads() == reads
    assert len(quota.lines) == 1
    assert quota.lines[0].startswith(f"github throttle: core {RATE_PAUSE_REMAINING}/5000 left;")
    assert "paused 15m00s" in quota.lines[0]
    quota.wall += 900
    quota.comments()
    assert quota.reads() == reads + 1


def test_shedding_skips_low_priority_reads_and_keeps_tracked_reads() -> None:
    quota = Quota()
    seen(quota, 900)
    clock = CycleClock()
    quota.source.cycle_clock = clock
    clock.start()
    reads = quota.reads()

    with clock.phase("github-candidates"), pytest.raises(QuotaDeferred):
        quota.comments()
    assert quota.reads() == reads

    with clock.phase("tracked-read"):
        quota.comments()
    assert quota.reads() == reads + 1


def test_pacing_reads_the_live_window() -> None:
    quota = Quota()
    seen(quota, 1500, reset_in=1800)

    assert quota.source.pacing(30.0, 60, 5.0) == pace(window(1500, 1800), WALL, 30.0, 60, 5.0)


def test_reads_without_rate_headers_spend_the_recorded_quota() -> None:
    quota = Quota()
    seen(quota, RATE_PAUSE_REMAINING + 2)
    quota.comments()
    quota.comments()
    reads = quota.reads()

    with pytest.raises(QuotaDeferred, match=f"{RATE_PAUSE_REMAINING}/5000 left"):
        quota.comments()
    assert quota.reads() == reads
    assert quota.source.pacing(30.0, 2, 5.0).reason == "pause"


def test_writes_spend_the_recorded_quota() -> None:
    quota = Quota()
    seen(quota, RATE_PAUSE_REMAINING + 1)
    quota.source.github.post_issue_comment("acme/widgets", 7, "hello")

    with pytest.raises(QuotaDeferred):
        quota.comments()


def test_paginated_reads_spend_one_unit_per_page() -> None:
    quota = Quota()
    seen(quota, 1001)
    quota.source.gh_runner = lambda argv, timeout: GhResult(0, "[]\n[]", "")
    quota.comments()

    assert quota.source.pacing(30.0, 1, 0.0) == pace(window(999, 900), WALL, 30.0, 1, 0.0)


def test_spent_quota_returns_with_the_next_reset() -> None:
    quota = Quota()
    seen(quota, RATE_PAUSE_REMAINING + 1)
    quota.comments()
    quota.wall += 900

    quota.comments()
    assert quota.source.pacing(30.0, 1, 0.0) == Pacing(30.0, False, "normal")


def test_fresh_ticket_reads_stop_at_the_floor_when_only_some_responses_carry_headers() -> None:
    left = [RATE_PAUSE_REMAINING + 15]
    fake = FakeGh(FACTORY.format(tickets="github", ship="merge"))

    def run(argv, timeout):
        left[0] -= 1
        result = fake(argv, timeout)
        if "--include" not in argv:
            return result
        headers = (
            "HTTP/2.0 200 OK\r\nX-RateLimit-Limit: 5000\r\n"
            f"X-RateLimit-Remaining: {left[0]}\r\nX-RateLimit-Reset: {int(WALL + 900)}\r\n\r\n"
        )
        return GhResult(result.returncode, headers + result.stdout, result.stderr)

    source = LivePollSource(
        config(), environ={}, gh_runner=run, wall=lambda: WALL, clock=lambda: 1000.0
    )
    for number in ("7", "8", "9", "10"):
        try:
            source.fresh_ticket(TicketRef("acme/widgets", number, "github"))
        except QuotaDeferred:
            pass

    assert left[0] >= RATE_PAUSE_REMAINING
    assert source.pacing(30.0, 1, 0.0) == pace(window(left[0], 900), WALL, 30.0, 1, 0.0)
